"""Reject state-changing browser requests from untrusted origins."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from starlette.datastructures import Headers
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


class ApiSettings(AppSettings):
    allowed_origins: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("APP_ALLOWED_ORIGINS", "allowed_origins")
    )


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
