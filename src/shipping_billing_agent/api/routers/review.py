"""Adapt record and document review requests to the existing review service."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ...domain.models import OcrMode
from ...services import ServiceValidationError
from .http import (
    DecimalJsonResponse,
    ReviewDependency,
    extract_expected_version,
    read_decimal_json_body,
    validate_body,
)

router = APIRouter(prefix="/api/v1/batches/{batch_id}")


class _SwitchEntity(BaseModel):
    accounting_entity: str = Field(min_length=1, max_length=120)


class _Decision(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    expected_updated_at: str | None = None


class _DocumentRetry(BaseModel):
    public_url: str | None = None
    ocr_mode: OcrMode | None = None


@router.post("/switch-entity", tags=["Batches"], operation_id="switchBatchAccountingEntity")
async def switch_entity(
    batch_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    version = extract_expected_version(request, body)
    fields = validate_body(_SwitchEntity, body)
    result = await run_in_threadpool(
        review.switch_entity, batch_id, fields.accounting_entity, expected_version=version
    )
    return DecimalJsonResponse(result)


@router.post("/reapply-dictionary", tags=["Batches"], operation_id="reapplyBatchChargeDictionary")
def reapply_dictionary(
    batch_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    return DecimalJsonResponse(
        review.reapply_dictionary(batch_id, expected_version=extract_expected_version(request))
    )


@router.patch("/documents/{doc_id}", tags=["Batches"], operation_id="patchDocumentTotals")
async def patch_document_totals(
    batch_id: str, doc_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    result = await run_in_threadpool(
        review.patch_document_totals,
        batch_id,
        doc_id,
        body,
        expected_version=extract_expected_version(request, body),
    )
    return DecimalJsonResponse(result)


@router.post("/documents/{doc_id}/retry", tags=["Batches"], operation_id="retryOrReplaceDocument")
async def retry_document(
    batch_id: str, doc_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    if request.headers.get("content-type", "").partition(";")[0].lower() == "multipart/form-data":
        async with request.form(max_files=1) as form:
            body = dict(form)
            upload = body.pop("file", None)
            if upload is not None and (not isinstance(upload, UploadFile) or not upload.filename):
                raise ServiceValidationError("INVALID_UPLOAD", "file 须为替换文件")
            version = extract_expected_version(request, body)
            fields = validate_body(_DocumentRetry, body)
            result = await run_in_threadpool(
                review.retry_or_replace_document,
                batch_id,
                doc_id,
                file=(upload.filename, upload.file) if upload is not None else None,
                public_url=fields.public_url,
                ocr_mode=fields.ocr_mode,
                expected_version=version,
            )
    else:
        body = await read_decimal_json_body(request, optional=True)
        version = extract_expected_version(request, body)
        fields = validate_body(_DocumentRetry, body)
        result = await run_in_threadpool(
            review.retry_or_replace_document,
            batch_id,
            doc_id,
            public_url=fields.public_url,
            ocr_mode=fields.ocr_mode,
            expected_version=version,
        )
    return DecimalJsonResponse(result)


@router.post(
    "/documents/{doc_id}/force-parse", tags=["Batches"], operation_id="forceParseSkippedDocument"
)
def force_parse_document(
    batch_id: str, doc_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    return DecimalJsonResponse(
        review.force_parse_skipped_document(
            batch_id,
            doc_id,
            expected_version=extract_expected_version(request),
        )
    )


@router.post("/records", tags=["Records"], operation_id="addManualBillingRecord", status_code=201)
async def add_record(
    batch_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    result = await run_in_threadpool(
        review.add_manual_record,
        batch_id,
        body,
        expected_version=extract_expected_version(request, body),
    )
    return DecimalJsonResponse(result, status_code=201)


@router.patch("/records/{record_id}", tags=["Records"], operation_id="patchBillingRecord")
async def patch_record(
    batch_id: str, record_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    result = await run_in_threadpool(
        review.patch_record,
        batch_id,
        record_id,
        body,
        expected_version=extract_expected_version(request, body),
    )
    return DecimalJsonResponse(result)


@router.post("/records/{record_id}/approve", tags=["Records"], operation_id="approveBillingRecord")
async def approve_record(
    batch_id: str, record_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    decision = validate_body(_Decision, body, status_code=422, code="INVALID_REVIEW_REASON")
    result = await run_in_threadpool(
        review.approve_record,
        batch_id,
        record_id,
        decision.reason,
        expected_version=extract_expected_version(request, body),
        expected_updated_at=decision.expected_updated_at,
    )
    return DecimalJsonResponse(result)


@router.post("/records/{record_id}/reject", tags=["Records"], operation_id="rejectBillingRecord")
async def reject_record(
    batch_id: str, record_id: str, request: Request, review: ReviewDependency
) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    decision = validate_body(_Decision, body, status_code=422, code="INVALID_REVIEW_REASON")
    result = await run_in_threadpool(
        review.reject_record,
        batch_id,
        record_id,
        decision.reason,
        expected_version=extract_expected_version(request, body),
        expected_updated_at=decision.expected_updated_at,
    )
    return DecimalJsonResponse(result)
