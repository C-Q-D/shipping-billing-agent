"""Produce standard JSON downloads and portable review snapshots."""

from urllib.parse import quote

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ...domain import ExportMode, serialize_export_bundle
from ...services import ResourceNotFoundError, ServiceValidationError, export_review_bundle
from .http import (
    BatchDependency,
    DecimalJsonResponse,
    extract_expected_version,
    read_decimal_json_body,
    validate_body,
)

router = APIRouter(prefix="/api/v1/batches/{batch_id}", tags=["Export"])


class _SyncBaseline(BaseModel):
    idempotency_key: str | None = None


def _export(batch_id: str, service: BatchDependency, mode: ExportMode) -> dict:
    batch = service.repository.get_batch(batch_id)
    if batch is None:
        raise ResourceNotFoundError("未找到该批次")
    return serialize_export_bundle(batch["records"], mode)


def _attachment(batch_id: str, suffix: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{quote(batch_id, safe="")}-{suffix}.json"'
    }


@router.get("/export", operation_id="exportBatchJson")
def export_preview(
    batch_id: str, service: BatchDependency, mode: ExportMode = "official"
) -> DecimalJsonResponse:
    output = _export(batch_id, service, mode)
    return DecimalJsonResponse(
        {
            "mode": output["mode"],
            "included_count": output["included_count"],
            "excluded_pending_count": output["excluded_pending_count"],
            "excluded_rejected_count": output["excluded_rejected_count"],
            "records": output["items"],
        }
    )


@router.get("/export/download", operation_id="downloadBatchJsonArray")
def download_json_array(
    batch_id: str, service: BatchDependency, mode: ExportMode = "official"
) -> DecimalJsonResponse:
    output = _export(batch_id, service, mode)
    return DecimalJsonResponse(output["items"], headers=_attachment(batch_id, mode))


@router.get("/export/review-bundle", operation_id="downloadReviewBundle")
def download_review_bundle(batch_id: str, service: BatchDependency) -> DecimalJsonResponse:
    bundle = export_review_bundle(service.repository, batch_id)
    return DecimalJsonResponse(bundle, headers=_attachment(batch_id, "review-bundle"))


@router.post("/sync-baseline", operation_id="syncBatchToBaseline")
async def sync_baseline(
    batch_id: str, request: Request, service: BatchDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request, optional=True)
    validate_body(_SyncBaseline, body)
    version = extract_expected_version(request, body)

    def sync() -> dict[str, int]:
        with service.repository.transaction() as transaction:
            batch = transaction.get_batch(batch_id)
            if batch is None:
                raise ResourceNotFoundError("未找到该批次")
            if batch["status"] == "PROCESSING":
                raise ServiceValidationError(
                    "BATCH_PROCESSING", "请等待批次处理结束", status_code=409
                )
            return transaction.sync_batch_to_baseline(batch_id, expected_version=version)

    counts = await run_in_threadpool(sync)
    return DecimalJsonResponse({f"{key}_count": value for key, value in counts.items()})
