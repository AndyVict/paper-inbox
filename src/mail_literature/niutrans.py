"""小牛文本翻译：HTTPS POST、固定错误码、拒绝重定向，不保存原始响应。"""
from dataclasses import dataclass, field
from http.client import HTTPException
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://api.niutrans.com/NiuTransServer/translation"
NEU_ENDPOINT = "https://trans.neu.edu.cn/niutrans/textTranslation"
MAX_SEGMENT_BYTES = 4500
# The neu service rejected a 2151-character request with business code 2088.
# Use a conservative byte budget; the legacy cloud budget remains unchanged.
NEU_MAX_SEGMENT_BYTES = 1500
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
NEU_ERROR_CODES = {2088: "translation_input_too_long"}
ERROR_CODES = {
    "10000": "translation_empty_input", "10001": "translation_rate_limited",
    "10003": "translation_input_too_long", "10005": "translation_encoding_failed",
    "13001": "translation_quota_or_access_denied", "13002": "translation_missing_api_key",
    "13003": "translation_content_filtered", "13005": "translation_same_language",
    "13007": "translation_language_unsupported", "13008": "translation_service_timeout",
    "14001": "translation_sentence_failed", "14002": "translation_tokenization_failed",
    "14003": "translation_postprocessing_failed", "14004": "translation_alignment_failed",
    "000000": "translation_invalid_parameters", "000001": "translation_invalid_content_type",
}
SAFE_ERRORS = set(ERROR_CODES.values()) | {
    "translation_service_failed", "translation_http_failed", "translation_network_failed",
    "translation_redirect_rejected", "translation_response_too_large", "invalid_translation_response",
}


class TranslationError(Exception):
    """只包含固定错误码。"""


@dataclass(frozen=True)
class TranslationOptions:
    dict_no: str = ""
    memory_no: str = ""
    cache_version: str = "1"
    api_variant: str = "cloud"


@dataclass(frozen=True)
class NiuTransConfig:
    api_key: str = field(repr=False)
    options: TranslationOptions = field(default_factory=TranslationOptions)


def validate_options(options):
    for value in (options.dict_no, options.memory_no, options.cache_version):
        if not isinstance(value, str) or len(value) > 128 or (value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
            raise TranslationError("invalid_translation_options")
    if not options.cache_version:
        raise TranslationError("invalid_translation_options")
    if options.api_variant not in {"cloud", "neu"}:
        raise TranslationError("invalid_translation_api_variant")
    if options.api_variant == "neu" and (options.dict_no or options.memory_no):
        raise TranslationError("neu_translation_dictionary_options_unsupported")


def load_config(path: Path) -> NiuTransConfig:
    try:
        values = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name in {"NIUTRANS_API_KEY", "TRANSLATION_API_KEY", "NIUTRANS_DICT_NO", "NIUTRANS_MEMORY_NO",
                        "NIUTRANS_CACHE_VERSION", "NIUTRANS_API_VARIANT"}:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if name in values:
                    raise TranslationError("duplicate_translation_config")
                values[name] = value
        # This project has explicitly selected NiuTrans. Keep the existing local
        # field usable; a present provider-specific field always takes priority.
        key = values.get("NIUTRANS_API_KEY", values.get("TRANSLATION_API_KEY", ""))
        if (not 8 <= len(key) <= 512 or not key.isascii() or any(c.isspace() or ord(c) < 33 for c in key) or
                re.search(r"replace|placeholder|your[_ -]|example", key, re.I)):
            raise TranslationError("missing_or_invalid_niutrans_api_key")
        options = TranslationOptions(values.get("NIUTRANS_DICT_NO", ""), values.get("NIUTRANS_MEMORY_NO", ""),
                                     values.get("NIUTRANS_CACHE_VERSION", "1"), values.get("NIUTRANS_API_VARIANT", "cloud"))
        validate_options(options)
        return NiuTransConfig(key, options)
    except (OSError, UnicodeError):
        raise TranslationError("translation_config_read_failed") from None


@dataclass(frozen=True)
class TranslationResult:
    status: str
    text: str | None = field(default=None, repr=False)
    error_code: str | None = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def segment_byte_limit(options: TranslationOptions) -> int:
    return NEU_MAX_SEGMENT_BYTES if options.api_variant == "neu" else MAX_SEGMENT_BYTES


def split_text(text: str, *, max_bytes=MAX_SEGMENT_BYTES) -> list[tuple[str, str]]:
    """按所选 UTF-8 字节上限分段；保留原文和段间空白，不截断正文。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000 or "\x00" in text:
        raise TranslationError("invalid_translation_input")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SEGMENT_BYTES:
        raise TranslationError("invalid_translation_input")
    remaining, parts = text.strip(), []
    while remaining:
        prefix = remaining.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        cut = len(prefix)
        if cut < len(remaining):
            for pattern in (r"\n+", r"(?<=[.!?])\s+", r"\s+"):
                boundaries = list(re.finditer(pattern, prefix))
                if boundaries:
                    cut = boundaries[-1].end()
                    break
        body = remaining[:cut].rstrip()
        if not body:
            raise TranslationError("invalid_translation_input")
        separator = remaining[len(body):cut]
        parts.append((body, separator))
        remaining = remaining[cut:]
    return parts


def _neu_translation(payload, text):
    """按 paragraphOrder/order 排序并核对原文，拒绝缺失/重复/错位片段。"""
    blocks = payload.get("data")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError
    fragments, block_orders = {}, set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError
        block_order, sentences = block.get("order"), block.get("sentences")
        if (type(block_order) is not int or block_order < 0 or block_order in block_orders or
                not isinstance(sentences, list) or not sentences):
            raise ValueError
        block_orders.add(block_order)
        for sentence in sentences:
            if not isinstance(sentence, dict):
                raise ValueError
            paragraph, order = sentence.get("paragraphOrder"), sentence.get("order")
            source, target = sentence.get("contrastSourceText"), sentence.get("data")
            if (type(paragraph) is not int or paragraph != block_order or type(order) is not int or order < 0 or
                    (paragraph, order) in fragments or not isinstance(source, str) or
                    not isinstance(target, str) or bool(source.strip()) != bool(target.strip()) or
                    sentence.get("sourceLanguageAbbreviation") != "en" or
                    sentence.get("targetLanguageAbbreviation") != "zh"):
                raise ValueError
            fragments[paragraph, order] = (source, target.strip())
    ordered = sorted(fragments)
    source = "".join(fragments[key][0] for key in ordered)
    if "".join(source.split()) != "".join(text.split()):
        raise ValueError
    # The service emits empty blocks for empty input lines. Reconstruct gaps
    # from the exact request text instead of treating every block as a paragraph.
    gaps, position = {}, 0
    for character in text:
        if character.isspace():
            gaps[position] = gaps.get(position, "") + character
        else:
            position += 1
    visible = [fragments[key] for key in ordered if fragments[key][0].strip()]
    translated, position = [], 0
    for index, (source, target) in enumerate(visible):
        translated.append(target)
        position += len("".join(source.split()))
        if index < len(visible) - 1:
            translated.append(gaps.get(position, ""))
    return "".join(translated)


class NiuTransClient:
    def __init__(self, config: NiuTransConfig, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        validate_options(config.options)
        self.config = config
        self.opener = opener or build_opener(NoRedirect()).open
        self.clock, self.sleep = clock, sleep
        self.requests = self.characters = 0
        self.last_request = None
        self.stopped = None
        self.last_service_code = None
        self.last_http_status = None

    @property
    def endpoint(self):
        # Safe to display: the credential-bearing request URL stays private.
        return NEU_ENDPOINT if self.config.options.api_variant == "neu" else ENDPOINT

    def translate(self, text: str) -> TranslationResult:
        if self.stopped:
            return TranslationResult("failed", error_code=self.stopped)
        if not text.strip() or len(text.encode("utf-8")) > segment_byte_limit(self.config.options):
            raise TranslationError("invalid_translation_input")
        if self.last_request is not None:
            self.sleep(max(0, 1.0 - (self.clock() - self.last_request)))
        if self.config.options.api_variant == "neu":
            params = {"from": "en", "to": "zh", "src_text": text}
            request_url = NEU_ENDPOINT + "?" + urlencode({"apikey": self.config.api_key})
            body = json.dumps(params, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            params = {"from": "en", "to": "zh", "apikey": self.config.api_key, "src_text": text}
            if self.config.options.dict_no:
                params["dictNo"] = self.config.options.dict_no
            if self.config.options.memory_no:
                params["memoryNo"] = self.config.options.memory_no
            request_url = ENDPOINT
            body = urlencode(params, encoding="utf-8").encode("ascii")
            content_type = "application/x-www-form-urlencoded; charset=utf-8"
        request = Request(request_url, data=body, method="POST",
                          headers={"Content-Type": content_type,
                                   "Accept": "application/json", "User-Agent": "mail-literature/0.1"})
        self.last_request = self.clock()
        self.requests += 1
        self.characters += len(text)
        # Diagnostics belong to this POST, never to a previous successful request.
        self.last_http_status = None
        self.last_service_code = None
        error = None
        try:
            with self.opener(request, timeout=30) as response:
                status = getattr(response, "status", None)
                self.last_http_status = status if type(status) is int and 100 <= status <= 599 else None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                error = "translation_response_too_large"
            else:
                payload = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(payload, dict):
                    raise ValueError
                neu = self.config.options.api_variant == "neu"
                code = payload.get("code") if neu else payload.get("error_code")
                if neu and type(code) is int and (code in NEU_ERROR_CODES or code in {0, 1, -1, 200, 400, 401, 403, 404, 413, 429, 500}):
                    self.last_service_code = str(code)
                if neu and code != 200:
                    error = NEU_ERROR_CODES.get(code, "translation_service_failed") if type(code) is int else "translation_service_failed"
                elif not neu and code is not None and code != "":
                    self.last_service_code = str(code) if str(code) in ERROR_CODES else None
                    error = ERROR_CODES.get(str(code), "translation_service_failed")
                else:
                    translated = _neu_translation(payload, text) if neu else payload.get("tgt_text")
                    if (not isinstance(translated, str) or not translated.strip() or len(translated) > 500_000 or
                            "\x00" in translated or self.config.api_key in translated or
                            ("src_text" in payload and payload["src_text"] != text) or
                            ("from" in payload and payload["from"] != "en") or
                            ("to" in payload and payload["to"] != "zh")):
                        raise ValueError
                    return TranslationResult("complete", translated.strip())
        except HTTPError as exc:
            status = exc.code
            self.last_http_status = status if type(status) is int and 100 <= status <= 599 else None
            exc.close()
            error = ("translation_redirect_rejected" if 300 <= status < 400 else
                     "translation_rate_limited" if status == 429 else
                     "translation_quota_or_access_denied" if status in {401, 403} else "translation_http_failed")
        except (URLError, OSError, TimeoutError, HTTPException):
            error = "translation_network_failed"
        except (ValueError, UnicodeError):
            error = "invalid_translation_response"
        # POST can consume credits before a timeout. Never automatically repeat it.
        self.stopped = error
        return TranslationResult("failed", error_code=error)
