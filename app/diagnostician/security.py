"""Local browser trust, CSRF and bounded redaction; never tool authorization."""
import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException


def redact(value, secrets_to_hide=(), *, phones=False):
    if isinstance(value, dict):
        return {str(key): "[已隐藏]" if re.search(r"api.?key|password|secret|authorization|access.?token|private.?key", str(key), re.I)
                else redact(item, secrets_to_hide, phones=phones) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item, secrets_to_hide, phones=phones) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets_to_hide:
        if isinstance(secret, str) and len(secret) >= 4:
            value = value.replace(secret, "[已隐藏]")
    value = re.sub(r"(?i)(Bearer\s+)[^\s\"',;]+", r"\1[已隐藏]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=]\s*)[^\s\"',;]+", r"\1[已隐藏]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已隐藏]", value)
    if phones:
        value = re.sub(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)", r"\1****\2", value)
    return value


def config_secrets(cfg):
    values = []
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"key|secret|password|token", str(key), re.I) and isinstance(item, str):
                    values.append(item)
                else:
                    walk(item)
    walk(cfg)
    return values


def check_local_request(request, cfg, *, write=False, submitted_session_id=None):
    try:
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"} or request.url.scheme not in {"http", "https"}:
            raise ValueError()
        peer = request.client.host if request.client else ""
        if not ipaddress.ip_address(peer).is_loopback:
            raise ValueError()
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) != (
                    request.url.scheme, host, request.url.port or (443 if request.url.scheme == "https" else 80)):
                raise ValueError()
            if parsed.path not in {"", "/"} or parsed.username or parsed.password:
                raise ValueError()
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ValueError()
    except ValueError:
        raise HTTPException(403, "仅允许本机同源访问诊断师") from None
    if cfg.get("admin_password_hash") and request.session.get("admin") is not True:
        raise HTTPException(401, "请先登录管理后台")
    if write:
        expected = request.session.get("doctor_csrf", "")
        supplied = request.headers.get("x-csrf-token", "")
        identity = request.session.get("doctor_session", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied) or not identity:
            raise HTTPException(403, "本机会话或CSRF校验未通过")
        if submitted_session_id is not None and not secrets.compare_digest(identity, submitted_session_id):
            raise HTTPException(403, "请求不属于当前会话")


def browser_identity(request):
    if not request.session.get("doctor_session"):
        request.session["doctor_session"] = secrets.token_urlsafe(32)
        request.session["doctor_csrf"] = secrets.token_urlsafe(32)
    return request.session["doctor_session"], request.session["doctor_csrf"]
