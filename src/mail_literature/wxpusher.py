"""WxPusher 标准 API：单次请求、不重放 POST、不输出凭据或响应原文。"""
from dataclasses import dataclass, field
from hashlib import sha256
import html
from http.client import HTTPException
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
QUERY_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/query/status"
MAX_RESPONSE = 128 * 1024


class PushError(Exception):
    """仅包含固定安全错误码。"""


@dataclass(frozen=True)
class PushConfig:
    token: str = field(repr=False)
    uids: tuple[str, ...] = field(repr=False)
    timeout: int = 20


@dataclass(frozen=True)
class PushResult:
    status: str
    error_code: str | None = None
    send_record_id: int | None = None
    provider_code: int | None = None


def recipient_key(uid: str) -> str:
    # Token 轮换不改变去重范围；同一接收者跨应用也不重发同一文献。
    return sha256(("wxpusher\0" + uid).encode("utf-8")).hexdigest()


def load_config(path: Path) -> PushConfig:
    try:
        values = {}
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise PushError("invalid_wxpusher_config")
            key, value = line.split("=", 1)
            if key.strip().startswith("WXPUSHER_"):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
        token = values.get("WXPUSHER_APP_TOKEN", "")
        raw_uids = values.get("WXPUSHER_UIDS") or values.get("WXPUSHER_UID", "")
        uids = tuple(dict.fromkeys(part.strip() for part in raw_uids.split(",") if part.strip()))
        if (not re.fullmatch(r"AT_[A-Za-z0-9_-]+", token) or not uids or len(uids) > 20 or
                any(not re.fullmatch(r"UID_[A-Za-z0-9_-]+", uid) for uid in uids)):
            raise PushError("missing_or_invalid_wxpusher_credentials")
        if values.get("WXPUSHER_TOPIC_ID"):
            raise PushError("wxpusher_topics_not_supported")
        return PushConfig(token, uids)
    except (OSError, UnicodeError):
        raise PushError("wxpusher_config_read_failed") from None


def validate_content(content: str, summary: str) -> None:
    # 为服务端转 HTML 留余量；不裁掉期刊/分区/链接等必需字段。
    converted = html.escape(content).replace("\n", "<br/>")
    if (not content.strip() or len(content) > 40000 or len(summary) > 100 or
            len(content.encode("utf-8")) > 16000 or len(converted.encode("utf-8")) > 60000):
        raise PushError("wxpusher_content_too_large_or_empty")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class WxPusherClient:
    def __init__(self, config: PushConfig, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.opener = opener or build_opener(NoRedirect())
        self.clock, self.sleep = clock, sleep
        self.last_request = None
        self.requests = 0

    def _request(self, request):
        if self.last_request is not None:
            delay = 1.0 - (self.clock() - self.last_request)
            if delay > 0:
                self.sleep(delay)
        self.last_request = self.clock()
        self.requests += 1
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                if response.geturl().split("?", 1)[0] not in {ENDPOINT, QUERY_ENDPOINT}:
                    raise PushError("wxpusher_redirect_rejected")
                if response.status != 200:
                    raise PushError("wxpusher_http_failed")
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise PushError("wxpusher_response_too_large")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict) or type(parsed.get("code")) is not int:
                raise PushError("invalid_wxpusher_response")
            return parsed
        except HTTPError as exc:
            exc.close()
            raise PushError("wxpusher_network_or_http_failed") from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise PushError("wxpusher_network_or_http_failed") from None
        except (ValueError, UnicodeError, RecursionError):
            raise PushError("invalid_wxpusher_response") from None

    def send(self, uid: str, content: str, summary: str) -> PushResult:
        if uid not in self.config.uids:
            raise PushError("invalid_wxpusher_recipient")
        validate_content(content, summary)
        payload = {"appToken": self.config.token, "uids": [uid], "content": content,
                   "summary": summary, "contentType": 1}
        request = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            result = self._request(request)
        except PushError as exc:
            return PushResult("unknown", str(exc))
        if result["code"] != 1000:
            return PushResult("failed", "wxpusher_business_rejected", provider_code=result["code"])
        data = result.get("data")
        if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict) or
                data[0].get("uid") != uid or type(data[0].get("code")) is not int):
            return PushResult("unknown", "invalid_wxpusher_target_response")
        target = data[0]
        if target["code"] != 1000:
            return PushResult("failed", "wxpusher_target_rejected", provider_code=target["code"])
        record = target.get("sendRecordId")
        if type(record) is not int or record <= 0:
            return PushResult("accepted", "wxpusher_missing_record_id", provider_code=1000)
        return PushResult("accepted", send_record_id=record, provider_code=1000)

    def query(self, record_id: int) -> tuple[int, int | None]:
        if type(record_id) is not int or record_id <= 0:
            raise PushError("invalid_wxpusher_record_id")
        result = self._request(Request(QUERY_ENDPOINT + f"?sendRecordId={record_id}"))
        data = result.get("data")
        status = data.get("status") if isinstance(data, dict) else data
        # 官方没有列出完整状态值映射；只保存数字，不据此推断用户已收到。
        return result["code"], status if type(status) is int else None
