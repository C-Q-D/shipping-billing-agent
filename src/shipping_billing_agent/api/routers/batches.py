"""Submit, list, and reload persisted billing batches."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ...domain.models import OcrMode
from ...services import ServiceValidationError, import_review_bundle
from .http import (
    BatchDependency,
    DecimalJsonResponse,
    decode_json_value,
    read_decimal_json_body,
    validate_body,
)

router = APIRouter(prefix="/api/v1/batches", tags=["Batches"])


class _BatchFields(BaseModel):
    accounting_entity: str
    ocr_mode: OcrMode = OcrMode.REMOTE_ONLY
    public_urls_json: str | None = None
    existing_bills_json: str | None = None


class _DemoFields(BaseModel):
    accounting_entity: str = "NEPTUNE SHIPPING LIMITED"
    ocr_mode: OcrMode = OcrMode.REMOTE_ONLY


@router.get("", operation_id="listBatches")
def list_batches(
    service: BatchDependency, limit: Annotated[int, Query(ge=1, le=50)] = 20
) -> DecimalJsonResponse:
    return DecimalJsonResponse(service.repository.list_batches(limit))


@router.post("", operation_id="createBatch", status_code=201)
async def create_batch(
    request: Request, service: BatchDependency, wait: bool = True
) -> DecimalJsonResponse:
    if request.headers.get("content-type", "").partition(";")[0].lower() != "multipart/form-data":
        raise ServiceValidationError("INVALID_REQUEST", "请使用 multipart/form-data 投递单据")
    async with request.form(
        max_files=service.limits.max_batch_files, max_part_size=service.limits.max_file_bytes
    ) as form:
        fields = validate_body(_BatchFields, dict(form))
        urls = (
            decode_json_value(fields.public_urls_json)
            if fields.public_urls_json is not None
            else []
        )
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise ServiceValidationError("INVALID_URL", "public_urls_json 须为 URL 字符串数组")
        files = []
        for upload in form.getlist("files"):
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise ServiceValidationError("INVALID_UPLOAD", "files 须包含上传文件")
            files.append((upload.filename, upload.file))
        batch = await run_in_threadpool(
            service.create_batch,
            files=files,
            public_urls=urls,
            accounting_entity=fields.accounting_entity,
            ocr_mode=fields.ocr_mode,
            existing_bills_json=fields.existing_bills_json,
            wait=wait,
        )
    return DecimalJsonResponse(batch, status_code=201 if wait else 202)


@router.post("/demo", operation_id="createDemoBatch", status_code=201)
async def create_demo_batch(
    request: Request, service: BatchDependency, wait: bool = True
) -> DecimalJsonResponse:
    fields = validate_body(_DemoFields, await read_decimal_json_body(request, optional=True))
    batch = await run_in_threadpool(
        service.create_demo_batch,
        accounting_entity=fields.accounting_entity,
        ocr_mode=fields.ocr_mode,
        wait=wait,
    )
    return DecimalJsonResponse(batch, status_code=201 if wait else 202)


@router.post("/import-review-bundle", operation_id="importReviewBundle", status_code=201)
async def import_bundle(request: Request, service: BatchDependency) -> DecimalJsonResponse:
    body = await read_decimal_json_body(request)
    batch = await run_in_threadpool(import_review_bundle, service.repository, body)
    return DecimalJsonResponse(batch, status_code=201)


@router.get("/{batch_id}", operation_id="getBatchDetail")
def get_batch(batch_id: str, service: BatchDependency) -> DecimalJsonResponse:
    return DecimalJsonResponse(service.get_batch(batch_id))
