"""Send validated file URLs to the configured remote OCR gateway and interpret its response."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain.models import (
    OcrListItem,
    OcrStructuredPayload,
    dumps_decimal_json,
    loads_decimal_json,
)
from shipping_billing_agent.gateways.url_guard import (
    install_http_url_log_filter,
    redact_url_queries_in_text,
    validate_public_url,
)

_EMPTY_RESULT = "OCR 网关返回空列表且无有效单据内容"
_PAGE_FIELDS = ("page", "pageNo", "pageNumber", "pageNum")
_EXTRA_TEXT_FIELDS = ("text", "content", "ocrText", "rawText", "markdown", "md")


class PresignedRelayPublisher(Protocol):
    def publish(self, content: bytes, filename: str, *, ttl_seconds: int) -> str:
        """Publish a file and return its short-lived, signed public URL."""
        ...


class OcrGatewayError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(redact_url_queries_in_text(message))


@dataclass(frozen=True, slots=True)
class OcrRecognitionResult:
    payload: OcrStructuredPayload
    formatted_text: str
    pages_split: Any
    raw_payload: dict[str, Any]


class RemoteOcrClient:
    def __init__(
        self,
        settings: AppSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        install_http_url_log_filter()
        self._gateway_url = settings.ocr_gateway_url
        self._task = settings.ocr_task
        self._timeout = httpx.Timeout(
            connect=settings.ocr_connect_timeout_seconds,
            read=settings.ocr_read_timeout_seconds,
            write=30.0,
            pool=10.0,
        )
        self._trust_env = settings.ocr_trust_env
        self._proxy = settings.ocr_proxy_url or None
        self._transport = transport
        self._async_transport = async_transport

    def _request_body(self, clean_url: str) -> dict[str, str]:
        return {"file": clean_url, "task": self._task, "callBack": "", "taskType": ""}

    def recognize_url(self, url: str) -> OcrRecognitionResult:
        clean_url = validate_public_url(url)
        # Without a gateway idempotency key a failed POST isn't retried here.
        try:
            with httpx.Client(
                timeout=self._timeout,
                trust_env=self._trust_env,
                proxy=self._proxy,
                transport=self._transport,
            ) as client:
                response = client.post(self._gateway_url, json=self._request_body(clean_url))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            error = OcrGatewayError(f"OCR 请求失败（{type(exc).__name__}）：{exc}")
        else:
            return _parse_response(response)
        raise error from None

    async def arecognize_url(self, url: str) -> OcrRecognitionResult:
        clean_url = await asyncio.to_thread(validate_public_url, url)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=self._trust_env,
                proxy=self._proxy,
                transport=self._async_transport,
            ) as client:
                response = await client.post(self._gateway_url, json=self._request_body(clean_url))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            error = OcrGatewayError(f"OCR 请求失败（{type(exc).__name__}）：{exc}")
        else:
            return _parse_response(response)
        raise error from None


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    return value is not None and not isinstance(value, bool)


def _parse_item(value: Any) -> OcrListItem:
    if isinstance(value, dict):
        value = dict(value)
        for field in ("chargeList", "ctrList"):
            if field in value and value[field] is None:
                value[field] = []
    return OcrListItem.model_validate(value)


def _has_extra_content(item: OcrListItem) -> bool:
    extra = item.model_extra or {}
    return any(key not in _PAGE_FIELDS and _has_content(value) for key, value in extra.items())


def _extra_text_blocks(item: OcrListItem) -> list[str]:
    extra = item.model_extra or {}
    texts: list[str] = []
    seen: set[str] = set()
    for key in _EXTRA_TEXT_FIELDS:
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
            seen.add(key)
    for key, value in extra.items():
        if key in _PAGE_FIELDS or key in seen:
            continue
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return texts


def _format_item(item: OcrListItem) -> str:
    fields = item.model_dump(mode="python", by_alias=True, exclude_unset=True)
    page = next((fields[key] for key in _PAGE_FIELDS if _has_content(fields.get(key))), None)
    parts = [dumps_decimal_json(fields)]
    if not _has_content(item.charge_list):
        parts.extend(_extra_text_blocks(item))
    body = "\n".join(parts)
    return body if page is None else f"[Page {page}]\n{body}"


def _parse_response(response: httpx.Response) -> OcrRecognitionResult:
    if not response.is_success:
        raise OcrGatewayError(f"OCR 网关返回 HTTP {response.status_code}")
    try:
        raw_payload = loads_decimal_json(response.text)
    except (ValueError, InvalidOperation, RecursionError):
        raise OcrGatewayError("OCR 网关响应不是有效 JSON") from None
    if not isinstance(raw_payload, dict):
        raise OcrGatewayError("OCR 网关响应 JSON 应为对象")
    if raw_payload.get("success") is False:
        raise OcrGatewayError("OCR 网关报告识别失败")

    data = raw_payload.get("data")
    if not isinstance(data, dict):
        raise OcrGatewayError(_EMPTY_RESULT)
    values = data.get("list")
    if not isinstance(values, list) or not values:
        raise OcrGatewayError(_EMPTY_RESULT)
    try:
        items = [_parse_item(value) for value in values]
    except ValidationError:
        raise OcrGatewayError("OCR 网关的单据项格式错误") from None

    has_charges = any(_has_content(item.charge_list) for item in items)
    has_metadata = any(
        any(
            _has_content(value)
            for value in (
                item.file_type,
                item.invoice_number,
                item.mbl_number,
                item.shipper,
                item.consignee,
                item.ctr_list,
                item.other,
            )
        )
        or _has_extra_content(item)
        for item in items
    )
    if not has_charges and not has_metadata:
        raise OcrGatewayError(_EMPTY_RESULT)

    return OcrRecognitionResult(
        payload=OcrStructuredPayload(
            items=items, has_charges=has_charges, has_metadata=has_metadata
        ),
        formatted_text="\n\n".join(_format_item(item) for item in items),
        pages_split=data.get("pagesSplit", raw_payload.get("pagesSplit")),
        raw_payload=raw_payload,
    )
