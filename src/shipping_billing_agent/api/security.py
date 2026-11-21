"""Reject untrusted browser write origins and enforce optional access limits."""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, date, datetime
from threading import Lock
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, Response
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
_WINDOW_SECONDS = 60.0
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


class AccessGuardMiddleware:
    """Enforce optional expiration, IP rate limits, daily heavy quota, and concurrency caps."""

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
    ) -> None:
        self.app = app
        self.rate_limit_per_minute = rate_limit_per_minute
        self.heavy_rate_limit_per_minute = heavy_rate_limit_per_minute
        self.daily_heavy_quota = daily_heavy_quota
        self.max_concurrent_heavy = max_concurrent_heavy
        self.expires_at = expires_at
        self.trust_proxy_headers = trust_proxy_headers
        self._lock = Lock()
        self._general_hits: dict[str, deque[float]] = {}
        self._heavy_hits: dict[str, deque[float]] = {}
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
        if path == "/api/v1/health":
            await self.app(scope, receive, send)
            return

        if self.expires_at is not None and _utc_now() >= self.expires_at:
            response = self._expired_response(path)
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
