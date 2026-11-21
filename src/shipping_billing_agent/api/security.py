"""Reject untrusted browser write origins and enforce optional access limits."""

from __future__ import annotations

import hashlib
import hmac
import html
import math
import re
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import AliasChoices, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config.settings import AppSettings
from .routers.http import error_response

Origin = tuple[str, str, int]
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOCAL_ORIGINS: frozenset[Origin] = frozenset(
    ("http", host, port) for host in ("127.0.0.1", "localhost") for port in (8000, 5173, 5174)
)
_AUTHORITY = re.compile(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]+)?")
_HEAVY_POST_PATHS = (
    re.compile(r"^/api/v1/batches/?$"),
    re.compile(r"^/api/v1/batches/demo/?$"),
    re.compile(r"^/api/v1/batches/[^/]+/documents/[^/]+/(?:retry|force-parse)/?$"),
    re.compile(r"^/api/v1/eval-runs/?$"),
)
_API_EXPIRED_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})
_PUBLIC_BYPASS_PATHS = frozenset({"/api/v1/health", "/favicon.ico"})
_LOGIN_PATH = "/_access/login"
_LOGOUT_PATH = "/_access/logout"
_COOKIE_NAME = "sb_access_session"
_PLACEHOLDER_PASSCODE = "ChangeMe_StrongPass_2026"
_MIN_PASSCODE_LENGTH = 8
_WINDOW_SECONDS = 60.0
_LOGIN_WINDOW_SECONDS = 900.0
_LOGIN_MAX_FAILURES = 10
_MAX_LOGIN_FORM_BYTES = 4096
_MAX_TRACKED_IPS = 1024
_EXPIRED_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>临时演示链接已过期</title>
</head>
<body style="font-family:sans-serif;max-width:560px;margin:10vh auto;padding:0 20px;color:#1e293b;">
  <h1 style="font-size:20px;margin-bottom:12px;">临时演示访问已到期</h1>
  <p style="line-height:1.6;color:#475569;">当前临时演示链接已过期，请联系作者重新开启。</p>
</body>
</html>"""

_monotonic = time.monotonic


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApiSettings(AppSettings):
    allowed_origins: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("APP_ALLOWED_ORIGINS", "allowed_origins")
    )
    rate_limit_per_minute: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("APP_RATE_LIMIT_PER_MINUTE", "rate_limit_per_minute"),
    )
    heavy_rate_limit_per_minute: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "APP_HEAVY_RATE_LIMIT_PER_MINUTE", "heavy_rate_limit_per_minute"
        ),
    )
    daily_heavy_quota: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("APP_DAILY_HEAVY_QUOTA", "daily_heavy_quota"),
    )
    max_concurrent_heavy: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("APP_MAX_CONCURRENT_HEAVY", "max_concurrent_heavy"),
    )
    disable_live_eval: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_DISABLE_LIVE_EVAL", "disable_live_eval"),
    )
    access_expires_at: str = Field(
        default="",
        validation_alias=AliasChoices("APP_ACCESS_EXPIRES_AT", "access_expires_at"),
    )
    trust_proxy_headers: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_TRUST_PROXY_HEADERS", "trust_proxy_headers"),
    )
    access_passcode: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("APP_ACCESS_PASSCODE", "access_passcode"),
    )
    access_session_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        validation_alias=AliasChoices("APP_ACCESS_SESSION_HOURS", "access_session_hours"),
    )


def validate_access_passcode(value: str) -> str:
    """Validate optional web passcode, returning empty string when disabled."""
    text = value.strip()
    if not text:
        return ""
    if len(text) < _MIN_PASSCODE_LENGTH or text == _PLACEHOLDER_PASSCODE:
        raise ValueError(
            "APP_ACCESS_PASSCODE must be at least 8 characters and not use the example placeholder"
        )
    return text


def parse_access_expires_at(value: str) -> datetime | None:
    """Parse an optional ISO-8601 expiration timestamp normalized to UTC."""
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("APP_ACCESS_EXPIRES_AT must be a valid ISO-8601 timestamp") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_origin(value: str, *, referer: bool = False) -> Origin | None:
    if not value or any(char.isspace() or ord(char) < 32 or char in "\\\x7f" for char in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in _DEFAULT_PORTS
        or not parts.hostname
        or port == 0
        or _AUTHORITY.fullmatch(parts.netloc) is None
        or not referer
        and (parts.path or "?" in value or "#" in value)
    ):
        return None
    return parts.scheme, parts.hostname, port if port is not None else _DEFAULT_PORTS[parts.scheme]


def allowed_origin_tuples(origins: Iterable[str]) -> frozenset[Origin]:
    allowed = set(_LOCAL_ORIGINS)
    for value in origins:
        origin = _parse_origin(value)
        if origin is None:
            raise ValueError("APP_ALLOWED_ORIGINS entries must be HTTP or HTTPS origins")
        allowed.add(origin)
    return frozenset(allowed)


class OriginMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: frozenset[Origin]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    def _allows(self, scope: Scope) -> bool:
        headers = Headers(scope=scope)
        origins = headers.getlist("origin")
        values = origins or headers.getlist("referer")
        if not values:
            return True
        if len(values) != 1:
            return False
        origin = _parse_origin(values[0], referer=not origins)
        if origin is None:
            return False
        hosts = headers.getlist("host")
        target = _parse_origin(f"{scope['scheme']}://{hosts[0]}") if len(hosts) == 1 else None
        return origin == target or origin in self.allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] in _WRITE_METHODS
            and not self._allows(scope)
        ):
            response = error_response(403, "FORBIDDEN_ORIGIN", "请求来源不在允许的写入源范围内")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _is_heavy_request(method: str, path: str) -> bool:
    return method == "POST" and any(pattern.fullmatch(path) for pattern in _HEAVY_POST_PATHS)


def _safe_next_path(raw: str | None) -> str:
    if not raw:
        return "/"
    text = raw.strip()
    if (
        not text
        or not text.startswith("/")
        or text.startswith(("//", "/\\"))
        or text.startswith("/_access/")
        or any(ord(ch) < 32 or ch == "\x7f" for ch in text)
    ):
        return "/"
    return text


async def _read_limited_body(receive: Receive, limit: int = _MAX_LOGIN_FORM_BYTES) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        msg_type = message.get("type")
        if msg_type == "http.disconnect":
            return None
        if msg_type != "http.request":
            continue
        chunk = message.get("body", b"")
        if chunk:
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _render_login_html(
    *,
    next_path: str,
    error_message: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    escaped_next = html.escape(next_path, quote=True)
    error_block = (
        f'<div class="err" role="alert">{html.escape(error_message)}</div>'
        if error_message
        else ""
    )
    expiry_block = (
        f'<p class="meta">临时访问有效期至 '
        f"{html.escape(expires_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC'))}</p>"
        if expires_at is not None
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="robots" content="noindex, nofollow, noarchive" />
  <title>临时访客口令验证 · 航运智能账单复核工作台</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #f1f5f9; color: #0f172a;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
      padding: 20px;
    }}
    .card {{
      width: 100%; max-width: 420px; background: #ffffff; border: 1px solid #e2e8f0;
      border-radius: 14px; box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08); padding: 28px 26px;
    }}
    .badge {{
      display: inline-block; font-size: 12px; font-weight: 600; color: #1e3a8a;
      background: #dbeafe; padding: 3px 10px; border-radius: 999px; margin-bottom: 12px;
    }}
    h1 {{ font-size: 20px; margin: 0 0 8px; color: #0f172a; }}
    .desc {{ font-size: 14px; line-height: 1.6; color: #475569; margin: 0 0 18px; }}
    .meta {{ font-size: 12px; color: #64748b; margin: 0 0 18px; }}
    .err {{
      background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c;
      font-size: 13px; border-radius: 8px; padding: 10px 12px; margin-bottom: 16px;
    }}
    label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
    .input-wrap {{ position: relative; display: flex; align-items: center; margin-bottom: 18px; }}
    input[type="password"], input[type="text"] {{
      width: 100%; height: 42px; padding: 0 64px 0 12px; border: 1px solid #cbd5e1;
      border-radius: 8px; font-size: 15px; color: #0f172a; outline: none;
    }}
    input:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14); }}
    .toggle {{
      position: absolute; right: 8px; border: none; background: transparent; color: #475569;
      font-size: 12px; cursor: pointer; padding: 6px 8px; border-radius: 6px;
    }}
    .toggle:hover {{ background: #f1f5f9; color: #0f172a; }}
    .submit {{
      width: 100%; height: 42px; border: none; border-radius: 8px; background: #1e293b;
      color: #ffffff; font-size: 15px; font-weight: 600; cursor: pointer;
    }}
    .submit:hover {{ background: #0f172a; }}
  </style>
</head>
<body>
  <main class="card">
    <span class="badge">临时评估访问</span>
    <h1>航运智能账单复核工作台</h1>
    <p class="desc">请输入临时访问口令后进入工作台。</p>
    {expiry_block}
    {error_block}
    <form method="post" action="/_access/login">
      <input type="hidden" name="next" value="{escaped_next}" />
      <label for="passcode">访问口令</label>
      <div class="input-wrap">
        <input id="passcode" name="passcode" type="password" required autofocus
          autocomplete="current-password" placeholder="请输入临时访问口令" />
        <button id="toggleBtn" class="toggle" type="button"
          aria-label="显示或隐藏口令">显示</button>
      </div>
      <button class="submit" type="submit">进入工作台</button>
    </form>
  </main>
  <script>
    (function () {{
      var input = document.getElementById("passcode");
      var btn = document.getElementById("toggleBtn");
      if (!input || !btn) return;
      btn.addEventListener("click", function () {{
        var hidden = input.type === "password";
        input.type = hidden ? "text" : "password";
        btn.textContent = hidden ? "隐藏" : "显示";
      }});
    }})();
  </script>
</body>
</html>"""


class AccessGuardMiddleware:
    """Enforce optional passcode login, expiration, rate limits, heavy quota, and concurrency."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_limit_per_minute: int = 0,
        heavy_rate_limit_per_minute: int = 0,
        daily_heavy_quota: int = 0,
        max_concurrent_heavy: int = 0,
        expires_at: datetime | None = None,
        trust_proxy_headers: bool = False,
        access_passcode: str = "",
        access_session_hours: int = 24,
    ) -> None:
        self.app = app
        self.rate_limit_per_minute = rate_limit_per_minute
        self.heavy_rate_limit_per_minute = heavy_rate_limit_per_minute
        self.daily_heavy_quota = daily_heavy_quota
        self.max_concurrent_heavy = max_concurrent_heavy
        self.expires_at = expires_at
        self.trust_proxy_headers = trust_proxy_headers
        self.access_passcode = validate_access_passcode(access_passcode)
        self.access_session_hours = access_session_hours
        self._signing_key = (
            hashlib.sha256(f"shipping-billing-access-v1:{self.access_passcode}".encode()).digest()
            if self.access_passcode
            else b""
        )
        self._lock = Lock()
        self._general_hits: dict[str, deque[float]] = {}
        self._heavy_hits: dict[str, deque[float]] = {}
        self._login_failures: dict[str, deque[float]] = {}
        self._quota_day: date | None = None
        self._daily_heavy_count = 0
        self._active_heavy = 0

    def _client_ip(self, scope: Scope) -> str:
        if self.trust_proxy_headers:
            headers = Headers(scope=scope)
            real_ip = (headers.get("x-real-ip") or "").strip()
            if real_ip:
                return real_ip
        client = scope.get("client")
        if isinstance(client, tuple | list) and client and isinstance(client[0], str) and client[0]:
            return client[0]
        return "unknown"

    def _is_https(self, scope: Scope) -> bool:
        if scope.get("scheme") == "https":
            return True
        if not self.trust_proxy_headers:
            return False
        proto = (Headers(scope=scope).get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _verify_session_token(self, token: str | None, now: datetime) -> bool:
        if not token or not self._signing_key:
            return False
        exp_str, sep, sig_hex = token.partition(".")
        if not sep or not exp_str.isdigit() or not sig_hex:
            return False
        if int(exp_str) <= int(now.timestamp()):
            return False
        expected = hmac.new(self._signing_key, exp_str.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)

    def _issue_session_token(self, now: datetime) -> tuple[str, int]:
        exp_dt = now + timedelta(hours=self.access_session_hours)
        if self.expires_at is not None and self.expires_at < exp_dt:
            exp_dt = self.expires_at
        max_age = max(1, int((exp_dt - now).total_seconds()))
        exp_str = str(int(exp_dt.timestamp()))
        sig = hmac.new(self._signing_key, exp_str.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{exp_str}.{sig}", max_age

    @staticmethod
    def _security_page_headers(*extra: tuple[str, str]) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        }
        headers.update(extra)
        return headers

    @staticmethod
    def _prune_bucket(
        buckets: dict[str, deque[float]], key: str, cutoff: float
    ) -> deque[float] | None:
        bucket = buckets.get(key)
        if bucket is None:
            return None
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del buckets[key]
            return None
        return bucket

    @classmethod
    def _sweep_stale_keys(cls, buckets: dict[str, deque[float]], cutoff: float) -> None:
        for stale_key in list(buckets):
            cls._prune_bucket(buckets, stale_key, cutoff)

    @staticmethod
    def _expired_response(path: str) -> Response:
        if path.startswith("/api/") or path in _API_EXPIRED_PATHS:
            return error_response(
                403,
                "ACCESS_EXPIRED",
                "当前临时演示链接已过期，请联系作者重新开启",
                headers={"Cache-Control": "no-store"},
            )
        return HTMLResponse(
            _EXPIRED_HTML,
            status_code=403,
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
            },
        )

    def _login_page_response(
        self,
        *,
        next_path: str,
        status_code: int = 200,
        error_message: str | None = None,
        retry_after: int | None = None,
    ) -> HTMLResponse:
        extra = (("Retry-After", str(retry_after)),) if retry_after is not None else ()
        return HTMLResponse(
            _render_login_html(
                next_path=next_path,
                error_message=error_message,
                expires_at=self.expires_at,
            ),
            status_code=status_code,
            headers=self._security_page_headers(*extra),
        )

    def _handle_login_get(self, scope: Scope, now: datetime) -> Response:
        raw_qs = scope.get("query_string", b"").decode("latin-1")
        next_param = parse_qs(raw_qs).get("next", [""])[0]
        next_path = _safe_next_path(next_param)
        token = Request(scope).cookies.get(_COOKIE_NAME)
        if self._verify_session_token(token, now):
            return RedirectResponse(
                url=next_path, status_code=303, headers=self._security_page_headers()
            )
        return self._login_page_response(next_path=next_path)

    async def _handle_login_post(self, scope: Scope, receive: Receive, now: datetime) -> Response:
        client_ip = self._client_ip(scope)
        now_mono = _monotonic()
        cutoff = now_mono - _LOGIN_WINDOW_SECONDS

        with self._lock:
            failures = self._prune_bucket(self._login_failures, client_ip, cutoff)
            if len(self._login_failures) > _MAX_TRACKED_IPS:
                self._sweep_stale_keys(self._login_failures, cutoff)
            if failures is not None and len(failures) >= _LOGIN_MAX_FAILURES:
                retry_after = max(1, math.ceil(failures[0] + _LOGIN_WINDOW_SECONDS - now_mono))
                return self._login_page_response(
                    next_path="/",
                    status_code=429,
                    error_message="口令错误次数过多，请 15 分钟后再试",
                    retry_after=retry_after,
                )

        body = await _read_limited_body(receive)
        if body is None:
            return self._login_page_response(
                next_path="/", status_code=400, error_message="请求体格式无效或超出大小限制"
            )
        form = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
        passcode = form.get("passcode", [""])[0].strip()
        next_path = _safe_next_path(form.get("next", [""])[0])

        matched = bool(passcode) and hmac.compare_digest(
            passcode.encode("utf-8"), self.access_passcode.encode("utf-8")
        )
        if not matched:
            fail_now = _monotonic()
            with self._lock:
                self._prune_bucket(
                    self._login_failures, client_ip, fail_now - _LOGIN_WINDOW_SECONDS
                )
                self._login_failures.setdefault(client_ip, deque()).append(fail_now)
            return self._login_page_response(
                next_path=next_path,
                status_code=401,
                error_message="访问口令不正确，请确认后重新输入",
            )

        with self._lock:
            self._login_failures.pop(client_ip, None)

        token, max_age = self._issue_session_token(now)
        response = RedirectResponse(
            url=next_path, status_code=303, headers=self._security_page_headers()
        )
        response.set_cookie(
            key=_COOKIE_NAME,
            value=token,
            max_age=max_age,
            path="/",
            secure=self._is_https(scope),
            httponly=True,
            samesite="lax",
        )
        return response

    def _handle_logout_post(self, scope: Scope) -> Response:
        response = RedirectResponse(
            url=_LOGIN_PATH, status_code=303, headers=self._security_page_headers()
        )
        response.delete_cookie(
            key=_COOKIE_NAME,
            path="/",
            secure=self._is_https(scope),
            httponly=True,
            samesite="lax",
        )
        return response

    @staticmethod
    def _unauthenticated_response(scope: Scope, path: str) -> Response:
        if (
            path.startswith(("/api/", "/assets/"))
            or path in _API_EXPIRED_PATHS
        ):
            return error_response(
                401,
                "AUTH_REQUIRED",
                "请先输入临时访问口令后再访问工作台",
                headers={"Cache-Control": "no-store"},
            )
        raw_qs = scope.get("query_string", b"").decode("latin-1")
        target = f"{path}?{raw_qs}" if raw_qs else path
        next_path = _safe_next_path(target)
        login_url = (
            _LOGIN_PATH
            if next_path == "/"
            else f"{_LOGIN_PATH}?{urlencode({'next': next_path})}"
        )
        return RedirectResponse(
            url=login_url,
            status_code=303,
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
            },
        )

    def _check_and_acquire(self, scope: Scope, path: str) -> tuple[Response | None, bool]:
        now_mono = _monotonic()
        cutoff = now_mono - _WINDOW_SECONDS
        is_heavy = _is_heavy_request(scope["method"], path)
        today = _utc_now().date() if (is_heavy and self.daily_heavy_quota > 0) else None
        needs_ip = self.rate_limit_per_minute > 0 or (
            is_heavy and self.heavy_rate_limit_per_minute > 0
        )
        client_ip = self._client_ip(scope) if needs_ip else ""

        with self._lock:
            if today is not None and self._quota_day != today:
                self._quota_day = today
                self._daily_heavy_count = 0

            if self.rate_limit_per_minute > 0:
                bucket = self._prune_bucket(self._general_hits, client_ip, cutoff)
                if len(self._general_hits) > _MAX_TRACKED_IPS:
                    self._sweep_stale_keys(self._general_hits, cutoff)
                if bucket is not None and len(bucket) >= self.rate_limit_per_minute:
                    retry_after = max(1, math.ceil(bucket[0] + _WINDOW_SECONDS - now_mono))
                    return (
                        error_response(
                            429,
                            "RATE_LIMITED",
                            "请求过于频繁，请稍后再试",
                            headers={"Retry-After": str(retry_after)},
                        ),
                        False,
                    )

            if is_heavy:
                if self.heavy_rate_limit_per_minute > 0:
                    heavy_bucket = self._prune_bucket(self._heavy_hits, client_ip, cutoff)
                    if len(self._heavy_hits) > _MAX_TRACKED_IPS:
                        self._sweep_stale_keys(self._heavy_hits, cutoff)
                    if (
                        heavy_bucket is not None
                        and len(heavy_bucket) >= self.heavy_rate_limit_per_minute
                    ):
                        retry_after = max(
                            1, math.ceil(heavy_bucket[0] + _WINDOW_SECONDS - now_mono)
                        )
                        return (
                            error_response(
                                429,
                                "RATE_LIMITED",
                                "单据解析与模型调用过于频繁，请稍后再试",
                                headers={"Retry-After": str(retry_after)},
                            ),
                            False,
                        )

                if self.daily_heavy_quota > 0 and self._daily_heavy_count >= self.daily_heavy_quota:
                    return (
                        error_response(
                            429,
                            "DAILY_QUOTA_EXCEEDED",
                            "今日演示环境单据解析额度已达上限，请明日再试或联系作者调整额度",
                        ),
                        False,
                    )

                if (
                    self.max_concurrent_heavy > 0
                    and self._active_heavy >= self.max_concurrent_heavy
                ):
                    return (
                        error_response(
                            429,
                            "TOO_MANY_CONCURRENT_JOBS",
                            "当前已有解析任务正在执行，请等待数秒后重试",
                            headers={"Retry-After": "5"},
                        ),
                        False,
                    )

            if self.rate_limit_per_minute > 0:
                self._general_hits.setdefault(client_ip, deque()).append(now_mono)
            acquired_heavy_slot = False
            if is_heavy:
                if self.heavy_rate_limit_per_minute > 0:
                    self._heavy_hits.setdefault(client_ip, deque()).append(now_mono)
                if self.daily_heavy_quota > 0:
                    self._daily_heavy_count += 1
                if self.max_concurrent_heavy > 0:
                    self._active_heavy += 1
                    acquired_heavy_slot = True
            return None, acquired_heavy_slot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _PUBLIC_BYPASS_PATHS:
            await self.app(scope, receive, send)
            return

        now = _utc_now()
        if self.expires_at is not None and now >= self.expires_at:
            response = self._expired_response(path)
            await response(scope, receive, send)
            return

        if self.access_passcode:
            method = scope["method"]
            if path == _LOGIN_PATH:
                if method in {"GET", "HEAD"}:
                    response = self._handle_login_get(scope, now)
                elif method == "POST":
                    response = await self._handle_login_post(scope, receive, now)
                else:
                    response = error_response(405, "METHOD_NOT_ALLOWED", "不支持该请求方法")
                await response(scope, receive, send)
                return

            if path == _LOGOUT_PATH:
                if method == "POST":
                    response = self._handle_logout_post(scope)
                else:
                    response = error_response(405, "METHOD_NOT_ALLOWED", "不支持该请求方法")
                await response(scope, receive, send)
                return

            token = Request(scope).cookies.get(_COOKIE_NAME)
            if not self._verify_session_token(token, now):
                response = self._unauthenticated_response(scope, path)
                await response(scope, receive, send)
                return

        has_limits = bool(
            self.rate_limit_per_minute
            or self.heavy_rate_limit_per_minute
            or self.daily_heavy_quota
            or self.max_concurrent_heavy
        )
        if not path.startswith("/api/") or not has_limits:
            await self.app(scope, receive, send)
            return

        rejected, acquired_slot = self._check_and_acquire(scope, path)
        if rejected is not None:
            await rejected(scope, receive, send)
            return

        if not acquired_slot:
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._active_heavy = max(0, self._active_heavy - 1)
