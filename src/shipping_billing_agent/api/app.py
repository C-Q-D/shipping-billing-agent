"""Compose the billing API, browser write guard, and optional workbench assets."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from functools import cache, partial
from importlib import import_module
from pathlib import Path
from sqlite3 import IntegrityError
from threading import Lock
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ..config.settings import AppSettings, load_settings
from ..gateways.url_guard import redact_url_query
from ..services import (
    BatchService,
    OptimisticLockError,
    ResourceNotFoundError,
    ServiceValidationError,
)
from .routers import batches, evaluation, exports, master_data, review
from .routers.evaluation import EvalRunner
from .routers.http import DecimalJsonResponse, error_response
from .security import (
    AccessGuardMiddleware,
    ApiSettings,
    OriginMiddleware,
    allowed_origin_tuples,
    parse_access_expires_at,
    validate_access_passcode,
)

_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = _ROOT / "docs/workflow/contracts/openapi.yaml"
_FRONTEND_DIST = _ROOT / "frontend/dist"
_RESERVED_PATH_SEGMENTS = {"api", "docs", "redoc", "openapi.json", "favicon.ico"}
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='8' fill='#273042'/>"
    "<path d='M8 18h16l-2.2 5.5a2 2 0 0 1-1.85 1.25h-7.9a2 2 0 0 1-1.85-1.25L8 18z"
    "m3-8h10v6H11v-6zm3-3h4v3h-4V7z' fill='#f8fafc'/></svg>"
)
_DEFAULT_EVAL_RUNNER = object()
_HARNESS_PACKAGE = "shipping_billing_agent.harness"
_HARNESS_RUNNER_MODULE = f"{_HARNESS_PACKAGE}.runner"


class _FrontendMount(Mount):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        first_segment = scope.get("path", "").lstrip("/").split("/", 1)[0]
        if first_segment in _RESERVED_PATH_SEGMENTS:
            # Let the router handle method mismatches on API and documentation routes.
            return Match.NONE, {}
        return super().matches(scope)


class _FrontendFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        segments = Path(path).parts
        first_segment = segments[0] if segments else ""
        if first_segment in _RESERVED_PATH_SEGMENTS:
            raise HTTPException(404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or first_segment == "assets" or Path(path).suffix:
                raise
        return await super().get_response("index.html", scope)


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceValidationError)
    async def service_error(_request: Request, exc: ServiceValidationError) -> DecimalJsonResponse:
        return error_response(exc.status_code, exc.code, exc.message, details=exc.details)

    @app.exception_handler(ResourceNotFoundError)
    async def missing_resource(
        _request: Request, _exc: ResourceNotFoundError
    ) -> DecimalJsonResponse:
        return error_response(404, "NOT_FOUND", "当前范围内未找到该资源")

    @app.exception_handler(OptimisticLockError)
    async def stale_resource(_request: Request, exc: OptimisticLockError) -> DecimalJsonResponse:
        message = (
            "该记录此前已同步到其他历史账单，请先核对共享历史基准后再处理改绑"
            if exc.code == "BASELINE_SYNC_TARGET_CONFLICT"
            else "资源已被更新，请刷新后重试"
        )
        return error_response(
            409,
            exc.code,
            message,
            details={
                "resource_id": exc.resource_id,
                "expected": exc.expected,
                "actual": exc.actual,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(
        _request: Request, exc: RequestValidationError
    ) -> DecimalJsonResponse:
        return error_response(
            400,
            "INVALID_REQUEST",
            "请求参数格式不合法",
            details={"fields": [".".join(map(str, error["loc"])) for error in exc.errors()]},
        )

    @app.exception_handler(IntegrityError)
    async def invalid_data(_request: Request, exc: IntegrityError) -> DecimalJsonResponse:
        if getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE":
            return error_response(409, "DATA_CONFLICT", "相同名称或标识的资源已存在")
        return error_response(400, "INVALID_DATA", "提交的数据不满足存储约束")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> DecimalJsonResponse:
        messages = {
            400: ("INVALID_REQUEST", "请求格式不合法"),
            403: ("FORBIDDEN", "请求被拒绝"),
            404: ("NOT_FOUND", "未找到该资源"),
            405: ("METHOD_NOT_ALLOWED", "该资源不支持此请求方法"),
            409: ("CONFLICT", "请求与当前状态冲突"),
            422: ("VALIDATION_FAILED", "校验未通过"),
        }
        code, message = messages.get(exc.status_code, ("HTTP_ERROR", "请求未能完成"))
        return error_response(exc.status_code, code, message, headers=exc.headers)


def _discover_eval_runner() -> EvalRunner | None:
    try:
        module = import_module(_HARNESS_RUNNER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in {_HARNESS_PACKAGE, _HARNESS_RUNNER_MODULE}:
            raise
        return None
    runner = getattr(module, "run_eval_suite", None)
    return runner if callable(runner) else None


def create_app(
    relay_publisher: object | None = None,
    *,
    settings: AppSettings | None = None,
    batch_service: BatchService | None = None,
    eval_runner: EvalRunner | None | object = _DEFAULT_EVAL_RUNNER,
    allowed_origins: Iterable[str] = (),
    frontend_dir: Path | None = None,
) -> FastAPI:
    """Create the API and own the batch service unless the caller supplies one."""
    configured_settings = (
        settings
        if settings is not None
        else (batch_service.settings if batch_service is not None else None)
    )
    origin_settings = (
        configured_settings
        if isinstance(configured_settings, ApiSettings)
        else ApiSettings(
            **(configured_settings.model_dump(exclude_none=True) if configured_settings else {})
        )
    )
    origins = allowed_origin_tuples((*origin_settings.allowed_origins, *allowed_origins))
    expires_at = parse_access_expires_at(origin_settings.access_expires_at)
    access_passcode = validate_access_passcode(origin_settings.access_passcode)
    service = batch_service
    service_lock = Lock()

    def resolve_batch_service() -> BatchService:
        nonlocal service
        with service_lock:
            if service is None:
                service = BatchService(
                    configured_settings or load_settings(), relay_publisher=relay_publisher
                )
            return service

    def resolve_eval_runner() -> EvalRunner | None:
        if eval_runner is _DEFAULT_EVAL_RUNNER:
            runner = _discover_eval_runner()
            if runner is not None and relay_publisher is not None:
                return partial(runner, relay_publisher=relay_publisher)
            return runner
        if eval_runner is None or callable(eval_runner):
            return eval_runner
        raise TypeError("eval_runner 须可调用或为 None")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal service
        active_service = await run_in_threadpool(resolve_batch_service)
        try:
            yield
        finally:
            if batch_service is None:
                await run_in_threadpool(active_service.close)
                with service_lock:
                    service = None

    fastapi_app = FastAPI(
        title="Shipping Intelligent Billing Assistant API",
        version="1.0.0",
        lifespan=lifespan,
    )
    fastapi_app.state.resolve_batch_service = resolve_batch_service
    fastapi_app.state.resolve_eval_runner = resolve_eval_runner
    fastapi_app.state.disable_live_eval = origin_settings.disable_live_eval
    if access_passcode or expires_at is not None or any(
        (
            origin_settings.rate_limit_per_minute,
            origin_settings.heavy_rate_limit_per_minute,
            origin_settings.daily_heavy_quota,
            origin_settings.max_concurrent_heavy,
        )
    ):
        fastapi_app.add_middleware(
            AccessGuardMiddleware,
            rate_limit_per_minute=origin_settings.rate_limit_per_minute,
            heavy_rate_limit_per_minute=origin_settings.heavy_rate_limit_per_minute,
            daily_heavy_quota=origin_settings.daily_heavy_quota,
            max_concurrent_heavy=origin_settings.max_concurrent_heavy,
            expires_at=expires_at,
            trust_proxy_headers=origin_settings.trust_proxy_headers,
            access_passcode=access_passcode,
            access_session_hours=origin_settings.access_session_hours,
        )
    fastapi_app.add_middleware(OriginMiddleware, allowed_origins=origins)
    _register_error_handlers(fastapi_app)

    @fastapi_app.get("/favicon.ico", include_in_schema=False)
    def get_favicon() -> Response:
        """Serve the workbench icon directly for browsers probing /favicon.ico."""
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    @fastapi_app.get("/api/v1/health", operation_id="getHealth", tags=["System"])
    def get_health() -> DecimalJsonResponse:
        """Return runtime health and capability configuration flags."""
        current = configured_settings or load_settings()
        return DecimalJsonResponse(
            {
                "status": "ok",
                "llm_configured": current.is_llm_configured(),
                "llm_model": current.llm_model,
                "ocr_gateway_url": redact_url_query(current.ocr_gateway_url),
                "ocr_direct_mode": current.is_ocr_direct_mode(),
                "public_relay_configured": current.is_public_relay_ready(relay_publisher),
            }
        )

    for router in (
        batches.router,
        review.router,
        exports.router,
        master_data.router,
        evaluation.router,
    ):
        fastapi_app.include_router(router)

    if _CONTRACT.is_file():
        # Request bodies and responses bypass FastAPI's JSON conversion to preserve Decimal.
        # Publish the canonical request/response schemas and examples from the checkout.
        @cache
        def contract_openapi() -> dict[str, Any]:
            return yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))

        fastapi_app.openapi = contract_openapi

    dist = frontend_dir if frontend_dir is not None else _FRONTEND_DIST
    if (dist / "index.html").is_file():
        fastapi_app.router.routes.append(
            _FrontendMount("/", app=_FrontendFiles(directory=dist, html=True), name="workbench")
        )

    return fastapi_app


app = create_app()
