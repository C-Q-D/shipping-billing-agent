"""Shared JSON and request handling for the HTTP adapters."""

from __future__ import annotations

from typing import Annotated, Any, TypeVar
from uuid import uuid4

from fastapi import Depends, Request
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.responses import Response

from ...domain import dumps_decimal_json, loads_decimal_json
from ...services import BatchService, ReviewService, ServiceValidationError

Model = TypeVar("Model", bound=BaseModel)
_VERSION = TypeAdapter(Annotated[int, Field(ge=1)])


class DecimalJsonResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps_decimal_json(content).encode("utf-8")


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> DecimalJsonResponse:
    return DecimalJsonResponse(
        {"code": code, "message": message, "details": details or {}, "request_id": uuid4().hex},
        status_code=status_code,
        headers=headers,
    )


def decode_json_value(raw: str | bytes) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return loads_decimal_json(text)
    except (ValueError, ArithmeticError, RecursionError) as exc:
        raise ServiceValidationError("INVALID_JSON", "请求须为有效的 UTF-8 JSON") from exc


async def read_decimal_json_body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if optional and not raw:
        return {}
    payload = decode_json_value(raw)
    if not isinstance(payload, dict):
        raise ServiceValidationError("INVALID_REQUEST", "JSON 请求体须为对象")
    return payload


def validate_body(  # noqa: UP047 - Python 3.11 is supported.
    model: type[Model],
    payload: Any,
    *,
    status_code: int = 400,
    code: str = "INVALID_REQUEST",
) -> Model:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ServiceValidationError(
            code,
            "请求字段缺失或格式不合法",
            status_code=status_code,
            details={"fields": [".".join(map(str, error["loc"])) for error in exc.errors()]},
        ) from exc


def extract_expected_version(request: Request, payload: dict[str, Any] | None = None) -> int | None:
    """Accept the batch version from the request body or query string."""
    query = request.query_params.get("expected_version")
    body = payload.pop("expected_version", None) if payload is not None else None
    try:
        if isinstance(body, bool):
            raise ValueError
        supplied = [_VERSION.validate_python(value) for value in (query, body) if value is not None]
        if len(set(supplied)) > 1:
            raise ValueError
    except ValueError as exc:
        raise ServiceValidationError(
            "INVALID_REVIEW_VERSION", "请提供一致的正整数批次版本"
        ) from exc
    return supplied[0] if supplied else None


def get_batch_service(request: Request) -> BatchService:
    return request.app.state.resolve_batch_service()


BatchDependency = Annotated[BatchService, Depends(get_batch_service)]


def _get_review_service(service: BatchDependency) -> ReviewService:
    return ReviewService(service)


ReviewDependency = Annotated[ReviewService, Depends(_get_review_service)]
