"""Run the optional evaluation hook and expose durable evaluation history."""

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from inspect import Parameter, isawaitable, signature
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...config.settings import AppSettings
from ...domain import decimal_to_db_text
from ...services import ServiceValidationError
from .http import BatchDependency, DecimalJsonResponse, read_decimal_json_body, validate_body

router = APIRouter(prefix="/api/v1/eval-runs", tags=["Evaluation"])


class _EvalSummaryProvider(Protocol):
    def to_eval_run_summary(self) -> Mapping[str, Any]: ...


_EvalOutput = Mapping[str, Any] | _EvalSummaryProvider | None
EvalRunner = Callable[..., _EvalOutput | Awaitable[_EvalOutput]]
_RunMode = Literal["OFFLINE_DETERMINISTIC", "LIVE_LLM", "LIVE_E2E"]
_Rate = Annotated[Decimal, Field(ge=0, le=1)]
_METRICS = (
    "schema_valid_rate",
    "field_exact_match",
    "ar_ap_accuracy",
    "operation_accuracy",
    "conflict_recall",
    "pass_at_k",
    "pass_pow_k",
)
_EXACT_METRICS_KEY = "_shipping_billing_exact_eval_metrics"


class _EvalRequest(BaseModel):
    run_mode: _RunMode
    k_trials: int = Field(default=3, ge=1, le=10)


class _EvalResult(_EvalRequest):
    id: str | None = None
    status: Literal["COMPLETED", "FAILED", "UNVERIFIED"]
    error_message: str | None = None
    model_name: str
    actual_trials: int = Field(default=0, ge=0)
    total_cases: int = Field(default=0, ge=0)
    schema_valid_rate: _Rate | None = None
    field_exact_match: _Rate | None = None
    ar_ap_accuracy: _Rate | None = None
    operation_accuracy: _Rate | None = None
    conflict_recall: _Rate | None = None
    pass_at_k: _Rate | None = None
    pass_pow_k: _Rate | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


def _eval_summary(row: dict[str, Any]) -> dict[str, Any]:
    report = row["report"]
    exact_metrics = report.get(_EXACT_METRICS_KEY)
    if (
        report.keys() == {_EXACT_METRICS_KEY, "report"}
        and isinstance(exact_metrics, dict)
        and exact_metrics.keys() == set(_METRICS)
    ):
        return {**row, **exact_metrics, "report": report["report"]}
    return row


def _save_eval_run(service: BatchDependency, result: _EvalResult) -> dict[str, Any]:
    values = result.model_dump(exclude={"id", "report", *_METRICS})
    rates = {field: getattr(result, field) for field in _METRICS}
    # The SQLite metric columns are REAL. The report keeps the exact values for the API.
    report = {_EXACT_METRICS_KEY: rates, "report": result.report}
    row = service.repository.insert_eval_run(
        **values,
        run_id=result.id,
        report=report,
        **{field: decimal_to_db_text(rate) for field, rate in rates.items()},
    )
    return _eval_summary(row)


def _call_eval_runner(
    resolve_runner: Callable[[], EvalRunner | None], options: dict[str, Any], settings: AppSettings
) -> _EvalOutput | Awaitable[_EvalOutput]:
    runner = resolve_runner()
    if runner is None:
        return None
    try:
        parameters = signature(runner).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    accepts_settings = any(
        param.kind is Parameter.VAR_KEYWORD
        or (
            param.name == "settings"
            and param.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
        )
        for param in parameters
    )
    if accepts_settings:
        return runner(**options, settings=settings)
    return runner(**options)


@router.get("", operation_id="listEvalRuns")
def list_eval_runs(service: BatchDependency) -> DecimalJsonResponse:
    return DecimalJsonResponse([_eval_summary(row) for row in service.repository.list_eval_runs()])


@router.post("", operation_id="triggerEvalRun", status_code=201)
async def trigger_eval_run(request: Request, service: BatchDependency) -> DecimalJsonResponse:
    payload = await read_decimal_json_body(request)
    if isinstance(payload.get("k_trials"), bool):
        raise ServiceValidationError(
            "INVALID_REQUEST", "k_trials 须为 1–10 的整数", status_code=422
        )
    fields = validate_body(_EvalRequest, payload, status_code=422)
    options = fields.model_dump()
    output = await run_in_threadpool(
        _call_eval_runner, request.app.state.resolve_eval_runner, options, service.settings
    )
    if isawaitable(output):
        output = await output
    if output is None:
        output = {
            "status": "UNVERIFIED",
            "model_name": service.settings.llm_model,
            "error_message": "当前未接入或尚未就绪的评测执行器，未运行评测。",
        }
    to_summary = getattr(output, "to_eval_run_summary", None)
    if callable(to_summary):
        output = to_summary()
    if not isinstance(output, Mapping) or any(
        key in output and output[key] != value for key, value in options.items()
    ):
        raise ServiceValidationError(
            "INVALID_EVAL_RESULT", "评测结果的运行模式或试验次数与请求不一致", status_code=422
        )
    result = validate_body(
        _EvalResult, dict(output) | options, status_code=422, code="INVALID_EVAL_RESULT"
    )
    return DecimalJsonResponse(
        await run_in_threadpool(_save_eval_run, service, result), status_code=201
    )
