"""仅官方已公开 GET API；固定主机、节流、校验缓存、有限预算。"""
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .storage import JcrError, digest, now, read_json, sanitize, write_json

API_BASE = 'https://api.clarivate.com/apis/wos-journals/v1'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JournalsClient:
    def __init__(self, snapshot, key, max_requests=10, interval=0.5, transport=None, sleep=time.sleep,
                 namespace='primary'):
        if not key or not 1 <= max_requests <= 100000 or interval < 0.21:
            raise JcrError('invalid_api_budget_or_key')
        self.snapshot, self.key = Path(snapshot), key
        self.max_requests, self.interval, self.requests = max_requests, interval, 0
        self.transport = transport or build_opener(NoRedirect()).open
        self.sleep, self.last = sleep, None
        self.cache_hits, self.stopped = 0, False
        self.namespace, self.stop_reason = namespace, 'api_stopped'

    def request(self, endpoint, params=None):
        if self.stopped:
            raise JcrError(self.stop_reason)
        if not endpoint.startswith('/journals') or '?' in endpoint or '..' in endpoint:
            raise JcrError('invalid_api_endpoint')
        params = params or {}
        if set(params) - {'jcrYear', 'page', 'limit'}:
            raise JcrError('unexpected_api_parameter')
        suffix = endpoint + ('?' + urlencode(params) if params else '')
        request_id = digest((self.namespace + ':' + suffix).encode('utf-8'))
        cache = self.snapshot / 'cache' / (request_id + '.json')
        if cache.exists():
            envelope = read_json(cache)
            payload = envelope.get('body')
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
            if envelope.get('request') != suffix or envelope.get('body_sha256') != digest(serialized):
                raise JcrError('cache_integrity_failed')
            self.cache_hits += 1
            return envelope
        if self.requests >= self.max_requests:
            raise JcrError('request_budget_reached')
        if self.last is not None:
            self.sleep(max(0, self.interval - (time.monotonic()-self.last)))
        self.requests += 1
        self.last = time.monotonic()
        code = None
        try:
            # APIKEY 只在专用请求头；禁止自动重定向。
            req = Request(API_BASE + suffix, headers={'X-ApiKey': self.key, 'Accept': 'application/json',
                                                      'User-Agent': 'LocalJCRSnapshot/1.0'})
            with self.transport(req, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise JcrError('api_response_too_large')
                body = json.loads(raw, parse_float=str)
                if not isinstance(body, dict) or 'error' in body or 'error_description' in body:
                    raise JcrError('api_unexpected_response')
                quotas = {k: v for k, v in response.headers.items()
                          if k.lower() in {'x-ratelimit-remaining', 'x-ratelimit-limit', 'x-ratelimit-reset',
                                           'x-rec-amtperyear-remaining', 'x-req-reqperday-remaining'}}
        except HTTPError as e:
            code = 'http_' + str(e.code)
            if e.code in (401, 403, 429):
                self.stopped = True
                self.stop_reason = code
            e.close()
        except (URLError, TimeoutError, OSError):
            code = 'api_network_error'
        except (ValueError, UnicodeError):
            code = 'api_invalid_json'
        except JcrError as e:
            code = str(e)
        if code:
            self._failure(request_id, endpoint, params, code)
            raise JcrError(code) from None
        body = sanitize(body, (self.key,))
        envelope = {'request': suffix, 'source_url': API_BASE + endpoint, 'parameters': params,
                    'retrieved_at': now(), 'body': body,
                    'response_sha256': digest(raw),
                    'body_sha256': digest(json.dumps(body, ensure_ascii=False, sort_keys=True).encode('utf-8')),
                    'quota_headers': sanitize(quotas, (self.key,))}
        write_json(cache, envelope)
        if any(str(v) == '0' and 'remaining' in k.lower() for k, v in quotas.items()):
            self.stopped, self.stop_reason = True, 'api_quota_exhausted'
        failures_path = self.snapshot / 'failures.json'
        failures = read_json(failures_path) if failures_path.exists() else {}
        if request_id in failures:
            failures[request_id]['resolved_at'] = now()
            write_json(failures_path, failures)
        self._event({'request_id': request_id, 'endpoint': endpoint, 'parameters': params,
                     'retrieved_at': envelope['retrieved_at'], 'body_sha256': envelope['body_sha256'],
                     'records': len(body.get('hits', [])), 'quota_headers': envelope['quota_headers'], 'status': 'cached'})
        return envelope

    def _event(self, value):
        with (self.snapshot / 'batches.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(value, ensure_ascii=False) + '\n')

    def _failure(self, request_id, endpoint, params, code):
        p = self.snapshot / 'failures.json'
        failures = read_json(p) if p.exists() else {}
        failures[request_id] = {'request_id': request_id, 'endpoint': endpoint, 'parameters': params,
                                'code': code, 'failed_at': now()}
        write_json(p, failures)
        self._event(failures[request_id])
