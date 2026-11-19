"""Check file URLs before handing them to the remote OCR service."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit

HTTP_URL_PATTERN = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)
_AUTHORITY = re.compile(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")


class SsrfBlockedError(ValueError):
    code = "SSRF_BLOCKED"


def _require_public_address(address: IPv4Address | IPv6Address) -> None:
    if isinstance(address, IPv6Address) and (
        address.ipv4_mapped is not None or address.is_site_local
    ):
        raise SsrfBlockedError("文件 URL 使用了禁用的 IPv6 地址")
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise SsrfBlockedError("文件 URL 的目标地址不属于可用公网地址")


def validate_public_url(
    url: str, *, resolver: Callable[[str, int], list[tuple]] = socket.getaddrinfo
) -> str:
    """Validate the literal address or all DNS answers visible to this process."""
    clean_url = url.strip()
    if "\\" in clean_url or any(
        char.isspace() or ord(char) < 32 or char == "\x7f" for char in clean_url
    ):
        raise SsrfBlockedError("文件 URL 格式无效")

    try:
        parts = urlsplit(clean_url)
        port = parts.port
        host = (parts.hostname or "").encode("idna").decode("ascii").rstrip(".").lower()
    except ValueError:
        raise SsrfBlockedError("文件 URL 格式无效") from None

    if parts.scheme not in {"http", "https"}:
        raise SsrfBlockedError("仅允许 HTTP 或 HTTPS 文件 URL")
    if parts.username is not None or parts.password is not None:
        raise SsrfBlockedError("文件 URL 不得包含用户名或密码")
    if not host or "%" in host or port == 0 or not _AUTHORITY.fullmatch(parts.netloc):
        raise SsrfBlockedError("文件 URL 的主机或端口无效")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise SsrfBlockedError("文件 URL 不得使用本地主机名")

    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _require_public_address(literal)
        return clean_url
    if parts.netloc.startswith("["):
        raise SsrfBlockedError("文件 URL 的 IP 地址无效")

    try:
        answers = resolver(host, port or (443 if parts.scheme == "https" else 80))
    except OSError:
        raise SsrfBlockedError("文件 URL 的 DNS 解析失败") from None
    if not answers:
        raise SsrfBlockedError("文件 URL 的 DNS 解析没有返回地址")

    for answer in answers:
        try:
            address = ip_address(answer[4][0])
        except (ValueError, IndexError, TypeError):
            raise SsrfBlockedError("文件 URL 的 DNS 解析返回了无效地址") from None
        _require_public_address(address)
    return clean_url


def redact_url_query(url: str) -> str:
    before_fragment, fragment_separator, fragment = url.partition("#")
    base_url, query_separator, _ = before_fragment.partition("?")
    if not query_separator:
        return url
    return f"{base_url}?[REDACTED]{fragment_separator}{fragment}"


def redact_url_queries_in_text(text: str) -> str:
    return HTTP_URL_PATTERN.sub(lambda match: redact_url_query(match[0]), text)
