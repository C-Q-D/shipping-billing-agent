"""Accept billing inputs, retain originals, and run batches with durable progress."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO, Literal, Self
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from ..agent.graph import BillingGraphInput, _BillingNodes, _BillingState, _package_id
from ..config.limits import ResourceLimits
from ..config.settings import AppSettings, get_settings
from ..domain.currency import is_valid_iso4217
from ..domain.entity_resolver import entity_matches
from ..domain.models import (
    AccountingEntityProfile,
    AlertSeverity,
    BatchPipelineResult,
    BatchStage,
    BatchStatus,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    DocumentStatus,
    ExistingBillRecord,
    ExtractionChannel,
    FailedDocumentAlert,
    OcrMode,
    ParsedDocument,
    ProcessedBillingRecord,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ReviewStatus,
    SourceKind,
    loads_decimal_json,
    sanitize_source_path,
)
from ..domain.serializer import _json_values
from ..gateways.llm_client import OpenAICompatibleLlmClient, StructuredLlmClient
from ..gateways.local_ocr import RapidOcrFallback
from ..gateways.ocr_client import PltPltOcrClient
from ..gateways.url_guard import redact_url_queries_in_text, validate_public_url
from ..parsers.dispatcher import (
    DocumentParserDispatcher,
    LocalOcrProtocol,
    RelayPublisherProtocol,
    RemoteOcrProtocol,
    UrlValidatorProtocol,
    compute_retry_context,
    validate_batch_inputs,
)
from ..parsers.filename_sanitizer import detect_source_kind, sanitize_filename
from ..storage import BillingRepository, DatabaseManager, ResourceNotFoundError
from ..storage.db import BatchWorkerLease, document_root_ids

Upload = tuple[str, bytes | BinaryIO]
SourceChannel = Literal["WEB", "CLI"]

_LOGGER = logging.getLogger(__name__)
_DEFAULT_LIMITS = ResourceLimits()
_READ_CHUNK_BYTES = 1024 * 1024
_DEFAULT_ENTITY = "NEPTUNE SHIPPING LIMITED"
_PROCESSING_FAILURE = "批次处理异常，可从保留的原件重试"
_STAGE_MESSAGES = {
    BatchStage.QUEUED: "等待处理",
    BatchStage.UNPACKING: "准备原件与邮件包",
    BatchStage.EXTRACTING: "抽取单据内容与费用明细",
    BatchStage.MATCHING: "匹配并继承历史账单",
    BatchStage.VALIDATING: "校验汇总结果与单据关联",
}
_DOCUMENT_SUMMARY_FIELDS = (
    "id",
    "parent_doc_id",
    "original_filename",
    "sanitized_filename",
    "source_kind",
    "extraction_channel",
    "status",
    "error_message",
    "affected_record_ids",
    "excluded_clauses",
    "shipment_refs",
    "subtotal_amount",
    "tax_amount",
    "total_amount",
    "total_currency",
)


class ServiceValidationError(ValueError):
    """Business error that HTTP and CLI adapters can render without parsing its text."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


class _StaleBatchWorker(RuntimeError):
    """The worker no longer owns the processing version it is about to write."""


@dataclass(frozen=True, slots=True)
class _BatchScope:
    profiles: list[AccountingEntityProfile]
    baseline: list[ExistingBillRecord]
    existing_bills: list[ExistingBillRecord]
    charge_dictionary: list[ChargeDictionaryItem]


def _require_batch(repository: BillingRepository, batch_id: str) -> dict[str, Any]:
    batch = repository.get_batch(batch_id)
    if batch is None:
        raise ResourceNotFoundError("未找到该批次")
    return batch


def _entity_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise ServiceValidationError("INVALID_ACCOUNTING_ENTITY", "记账主体长度须为 1–120 字符")
    return value.strip()


def _ocr_mode(value: OcrMode | str) -> OcrMode:
    try:
        return OcrMode(value)
    except ValueError as exc:
        raise ServiceValidationError(
            "INVALID_OCR_MODE", "OCR 模式须为 REMOTE_ONLY 或 REMOTE_WITH_LOCAL_FALLBACK"
        ) from exc


def _existing_bills_snapshot(payload: str | None) -> list[ExistingBillRecord] | None:
    if payload is None:
        return None
    try:
        values = loads_decimal_json(payload)
        if not isinstance(values, list):
            raise ValueError("Expected an array")
        bills = [ExistingBillRecord.model_validate(value) for value in values]
        ids = {bill.id for bill in bills}
        if len(ids) != len(bills) or any(
            not bill.id.strip()
            or not bill.charge_item.strip()
            or not is_valid_iso4217(bill.currency)
            or not 1 <= len(bill.accounting_entity.strip()) <= 120
            for bill in bills
        ):
            raise ValueError("Invalid or repeated bill")
    except (ValueError, TypeError, ArithmeticError, RecursionError) as exc:
        raise ServiceValidationError(
            "INVALID_EXISTING_BILLS", "随批历史账单须是有效的 JSON 数组，账单 ID 须唯一"
        ) from exc
    return bills


def _load_batch_scope(
    repository: BillingRepository,
    batch: Mapping[str, Any],
    entity: str,
    *,
    shared_baseline: list[ExistingBillRecord] | None = None,
) -> _BatchScope:
    profiles = [
        AccountingEntityProfile.model_validate(profile)
        for profile in repository.list_accounting_entities()
    ]
    bills = batch["batch_existing_bills"]
    if bills is None:
        bills = repository.list_existing_bills() if shared_baseline is None else shared_baseline
    return _BatchScope(
        profiles=profiles,
        baseline=bills,
        existing_bills=[
            bill for bill in bills if entity_matches(bill.accounting_entity, entity, profiles)
        ],
        charge_dictionary=[
            ChargeDictionaryItem.model_validate(item)
            for item in repository.list_charge_dictionary()
        ],
    )


def _document_bundle(row: Mapping[str, Any]) -> DocumentExtractionBundle:
    document = ParsedDocument.model_validate({**row, "doc_id": row["id"]})
    extraction = row["extraction_snapshot"]
    # Older rows and imported review bundles can have summary columns without an envelope.
    if extraction is None and any(
        row[field] is not None
        for field in ("subtotal_amount", "tax_amount", "total_amount", "total_currency")
    ):
        extraction = RawExtractionEnvelope(
            extraction=RawDocumentExtraction.model_validate(
                {field: row[field] for field in RawDocumentExtraction.model_fields if field in row}
            )
        )
    return DocumentExtractionBundle(
        document=document, extraction=extraction, retry_count=row["retry_count"]
    )


def _record_detail(record: ProcessedBillingRecord) -> dict[str, Any]:
    include = {
        "id",
        "source_doc_id",
        "source_line_index",
        "operation",
        "review_status",
        "match_level",
        "matched_existing_bill_id",
        "matched_bill_ref",
        "candidate_bills",
        "shipment_refs",
        "source_evidence",
        "field_diffs",
        "validation_alerts",
        "ai_reasoning",
        "human_overrides",
        "reject_reason",
        "created_at",
        "updated_at",
    }
    detail = record.model_dump(mode="python", include=include)
    detail.update(
        inherited_fields=list(record.inherited_fields),
        document_info={
            "document_type": record.document_type,
            "reference_no": record.reference_no,
            "document_date": record.document_date,
        },
        transaction_party={"issuer": record.issuer, "recipient": record.recipient},
        charge_details={
            "charge_item": record.charge_item,
            "raw_charge_name": record.raw_charge_name,
            "quantity": record.quantity,
            "unit_price": record.unit_price,
            "amount": record.amount,
            "currency": record.currency,
            "payment_term": record.payment_term,
        },
        financial_attributes={
            "accounting_entity": record.accounting_entity,
            "posting_type": record.posting_type,
        },
        source_evidence=record.source_evidence.with_sanitized_paths().model_dump(),
    )
    return _json_values(detail)


def _batch_detail(batch: Mapping[str, Any]) -> dict[str, Any]:
    documents = []
    failures = {alert.doc_id: alert for alert in batch["failed_document_alerts"]}
    for row in batch["documents"]:
        if (
            batch["status"] == BatchStatus.PROCESSING
            and row["status"] == DocumentStatus.FAILED
            and row["error_message"] is None
        ):
            # The document schema has no pending status for a retained, unread source.
            continue
        document = {field: row[field] for field in _DOCUMENT_SUMMARY_FIELDS}
        if alert := failures.get(row["id"]):
            document["error_message"] = alert.error_message
        document["original_filename"] = sanitize_source_path(document["original_filename"])
        document["sanitized_filename"] = sanitize_source_path(document["sanitized_filename"])
        documents.append(document)
    return {
        **{
            key: value
            for key, value in batch.items()
            if key not in {
                "documents",
                "records",
                "failed_document_alerts",
                "batch_existing_bills",
                "shared_baseline_snapshot",
            }
        },
        "documents": documents,
        "failed_documents": [alert.model_dump() for alert in failures.values()],
        "records": [_record_detail(record) for record in batch["records"]],
    }


def _root_documents(documents: Sequence[ParsedDocument]) -> dict[str, str]:
    return document_root_ids({document.doc_id: document.parent_doc_id for document in documents})


def _batch_outcome(
    documents: Sequence[ParsedDocument],
    records: Sequence[ProcessedBillingRecord],
    alerts: Sequence[FailedDocumentAlert],
) -> dict[str, Any]:
    by_id = {document.doc_id: document for document in documents}
    roots = _root_documents(documents)
    packages = {doc_id: _package_id(doc, by_id) for doc_id, doc in by_id.items()}
    if any(record.source_doc_id not in by_id for record in records):
        raise ValueError("Billing record has no source document in its batch")
    record_ids = {record.id for record in records}
    failures = {}
    for alert in alerts:
        if alert.doc_id not in by_id:
            continue
        failures[alert.doc_id] = alert
    for document in documents:
        if document.status is DocumentStatus.FAILED and document.doc_id not in failures:
            failures[document.doc_id] = FailedDocumentAlert(
                doc_id=document.doc_id,
                filename=document.original_filename,
                error_message=document.error_message or "单据解析失败，可重试",
            )

    for doc_id, alert in failures.items():
        doc = by_id[doc_id]
        affected = [record_id for record_id in alert.affected_record_ids if record_id in record_ids]
        for record in records:
            source = by_id[record.source_doc_id]
            if (
                source.doc_id == doc_id
                or doc.content_sha256 is not None
                and doc.content_sha256 == source.content_sha256
                or doc.parent_doc_id is not None
                and doc.source_kind is not SourceKind.EML_BODY
                and packages[doc.parent_doc_id] == packages[source.doc_id]
            ):
                affected.append(record.id)
        failures[doc_id] = alert.model_copy(
            update={
                "filename": sanitize_source_path(alert.filename),
                "error_message": redact_url_queries_in_text(alert.error_message),
                "affected_record_ids": list(dict.fromkeys(affected)),
            }
        )
    total = len(set(roots.values()))
    failed = len({roots[doc_id] for doc_id in failures})
    return {
        "status": BatchStatus.PARTIAL_ERROR if failures else BatchStatus.COMPLETED,
        "stage": BatchStage.FAILED
        if failed == total and failed and not records
        else (BatchStage.COMPLETED),
        "stage_message": f"处理结束（{total - failed} 份成功，{failed} 份待处理）",
        "total_files": total,
        "succeeded_files": total - failed,
        "failed_files": failed,
        "failed_document_alerts": list(failures.values()),
    }


class _ServiceBillingNodes(_BillingNodes):
    """Observe B02 node boundaries and limit repair extraction to the requested subtree."""

    def __init__(
        self,
        client: StructuredLlmClient | None,
        extractor_mode: str,
        limits: ResourceLimits,
        stage_changed: Callable[[BatchStage], None] | None,
        targets: set[str] | None,
    ) -> None:
        super().__init__(client, extractor_mode, limits, use_sync_client=True)
        self.stage_changed = stage_changed
        self.targets = targets

    async def build_context_packages(self, state: _BillingState) -> dict:
        output = await super().build_context_packages(state)
        if self.targets is not None:
            output["bundles"] = [
                bundle for bundle in output["bundles"] if bundle.document.doc_id in self.targets
            ]
            output["failures"] = {
                doc_id: error
                for doc_id, error in output["failures"].items()
                if doc_id in self.targets
            }
        return output

    def normalize_match_and_validate(self, state: _BillingState) -> dict:
        if self.stage_changed is not None:
            self.stage_changed(BatchStage.MATCHING)
        return super().normalize_match_and_validate(state)

    def finalize_output(self, state: _BillingState) -> dict:
        if self.stage_changed is not None:
            self.stage_changed(BatchStage.VALIDATING)
        result = super().finalize_output(state)
        # The graph performs the domain gates, then this stage checks the result before storage.
        for record in result["result"].records:
            if record.review_status in {ReviewStatus.APPROVED, ReviewStatus.AUTO_READY} and any(
                alert.severity is not AlertSeverity.RESOLVED for alert in record.validation_alerts
            ):
                raise ValueError("An unsettled billing record was marked ready")
        return result


class _UnpackingDispatcher(DocumentParserDispatcher):
    """Reuse B01's MIME tree and limits while deferring the work of reading each leaf."""

    def __init__(self, limits: ResourceLimits, url_validator: UrlValidatorProtocol) -> None:
        super().__init__(limits=limits, url_validator=url_validator)
        self.pending: set[str] = set()

    def _parse_office(self, doc: ParsedDocument, data: bytes) -> None:
        self.pending.add(doc.doc_id)

    def _parse_pdf_or_image(self, doc: ParsedDocument, data: bytes) -> None:
        self.pending.add(doc.doc_id)

    def _recognize_url(self, doc: ParsedDocument, url: str) -> None:
        self.pending.add(doc.doc_id)


def _read_bounded(stream: BinaryIO, max_bytes: int) -> bytes:
    buffer = BytesIO()
    while chunk := stream.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - buffer.tell())):
        buffer.write(chunk)
        if buffer.tell() > max_bytes:
            raise ServiceValidationError("FILE_TOO_LARGE", f"文件超过 {max_bytes} 字节")
    return buffer.getvalue()


class BatchService:
    """Own the batch executor and the retained sources under the configured data directory."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        repository: BillingRepository | None = None,
        limits: ResourceLimits = _DEFAULT_LIMITS,
        remote_ocr: RemoteOcrProtocol | None = None,
        local_ocr: LocalOcrProtocol | None = None,
        url_validator: UrlValidatorProtocol = validate_public_url,
        relay_publisher: RelayPublisherProtocol | None = None,
        llm_client: StructuredLlmClient | None = None,
        extractor_mode: str = "auto",
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self.repository = (
            repository
            if repository is not None
            else BillingRepository(DatabaseManager(self.settings.sqlite_db_path))
        )
        self.limits = limits
        self._app_data_dir = self.settings.app_data_dir.resolve()
        self._remote_ocr = remote_ocr if remote_ocr is not None else PltPltOcrClient(self.settings)
        self._local_ocr = local_ocr if local_ocr is not None else RapidOcrFallback()
        self._url_validator = url_validator
        self._relay_publisher = relay_publisher
        self._llm_client = llm_client
        self._extractor_mode = extractor_mode
        self._executor = ThreadPoolExecutor(
            max_workers=limits.max_concurrency, thread_name_prefix="billing-batch"
        )
        self._lock = Lock()
        self._tasks: dict[str, Future[dict[str, Any]]] = {}
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Drain queued work and join the executor before its caller closes borrowed clients."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return _batch_detail(_require_batch(self.repository, batch_id))

    def wait_for_batch(self, batch_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(batch_id)
        if task is not None:
            task.result(timeout=timeout)
        batch = self.get_batch(batch_id)
        if batch["status"] == BatchStatus.PROCESSING:
            raise RuntimeError("当前服务没有该批次的后台任务")
        return batch

    def _forget_task(self, batch_id: str, task: Future[dict[str, Any]]) -> None:
        if error := task.exception():
            _LOGGER.error("Could not finalize billing batch %s", batch_id, exc_info=error)
        with self._lock:
            self._tasks.pop(batch_id, None)

    def _validate_inputs(self, files: Sequence[Upload], urls: Sequence[str]) -> None:
        if any(not isinstance(url, str) for url in urls):
            raise ServiceValidationError("INVALID_URL", "公网文件 URL 须为字符串")
        # B01 can check names and count before a stream is read. Its byte limit is
        # checked again with the real bytes after the bounded read.
        candidates = [(name, data if isinstance(data, bytes) else b"x") for name, data in files]
        issues = validate_batch_inputs(candidates, urls, self.limits, self._url_validator)
        if issues:
            issue = issues[0]
            details: dict[str, Any] = {"issues": [item.model_dump() for item in issues]}
            if issue.code == "SSRF_BLOCKED":
                details["url"] = issue.filename_or_url
            raise ServiceValidationError(issue.code, issue.message, details=details)
        if len(files) + len(urls) > self.limits.max_expanded_documents:
            raise ServiceValidationError("TOO_MANY_FILES", "顶层文件数量超过可展开单据上限")

    def _upload_bytes(self, upload: Upload) -> bytes:
        name, content = upload
        if isinstance(content, bytes):
            return content
        try:
            data = _read_bounded(content, self.limits.max_file_bytes)
        except OSError as exc:
            raise ServiceValidationError("UPLOAD_READ_FAILED", "无法读取上传文件") from exc
        self._validate_inputs([(name, data)], ())
        return data

    def _upload_dir(self, batch_id: str) -> Path:
        if not batch_id or batch_id in {".", ".."} or any(char in batch_id for char in "/\\:"):
            raise ValueError("Invalid batch storage directory")
        directory = self._app_data_dir / "uploads" / batch_id
        if directory.resolve() != directory:
            raise ValueError("Batch storage directory must remain under the data directory")
        return directory

    @contextmanager
    def _staged_originals(self, batch_id: str) -> Iterator[list[Path]]:
        paths: list[Path] = []
        try:
            yield paths
        except BaseException:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("Could not remove an uncommitted upload for batch %s", batch_id)
            if paths:
                directory = self._upload_dir(batch_id)
                try:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    _LOGGER.warning("Could not remove unused upload directory for %s", batch_id)
            raise

    def _write_original(
        self, batch_id: str, filename: str, content: bytes, paths: list[Path]
    ) -> Path:
        directory = self._upload_dir(batch_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / sanitize_filename(f"{uuid4().hex}-{sanitize_filename(filename)}")
        with path.open("xb") as stream:
            paths.append(path)
            stream.write(content)
        return path

    def _read_original(self, batch_id: str, row: Mapping[str, Any]) -> bytes:
        try:
            path = Path(row["stored_path"] or "").resolve(strict=True)
            if not path.is_relative_to(self._upload_dir(batch_id)) or not path.is_file():
                raise ValueError("Source file is not in the batch upload directory")
            with path.open("rb") as stream:
                content = _read_bounded(stream, self.limits.max_file_bytes)
            if sha256(content).hexdigest() != row["content_sha256"]:
                raise ValueError("Source bytes do not match the stored content hash")
        except (OSError, ValueError, RuntimeError) as exc:
            raise ServiceValidationError(
                "ORIGINAL_FILE_UNAVAILABLE",
                "当前库没有可用的原件。请在重试时重新上传文件或提供公网文件 URL。",
            ) from exc
        return content

    def create_batch(
        self,
        *,
        accounting_entity: str,
        files: Sequence[Upload] = (),
        public_urls: Sequence[str] = (),
        existing_bills_json: str | None = None,
        ocr_mode: OcrMode | str = OcrMode.REMOTE_ONLY,
        source_channel: SourceChannel = "WEB",
        wait: bool = True,
    ) -> dict[str, Any]:
        entity = _entity_name(accounting_entity)
        mode = _ocr_mode(ocr_mode)
        snapshot = _existing_bills_snapshot(existing_bills_json)
        if source_channel not in ("WEB", "CLI"):
            raise ServiceValidationError("INVALID_SOURCE_CHANNEL", "来源渠道须为 WEB 或 CLI")
        self._validate_inputs(files, public_urls)
        batch_id = uuid4().hex
        submission_error = None
        with ExitStack() as pending_lease:
            with self._staged_originals(batch_id) as paths:
                documents, stored_paths = self._retain_inputs(batch_id, files, public_urls, paths)
                with self._lock:
                    if self._closed:
                        raise RuntimeError("Batch service is closed")
                    lease = self.repository.db.try_acquire_worker_lease(batch_id)
                    if lease is None:
                        raise RuntimeError(f"Billing batch {batch_id} already has a worker")
                    pending_lease.enter_context(lease)
                    with self.repository.transaction() as transaction:
                        summary = transaction.create_batch(
                            entity,
                            batch_id=batch_id,
                            stage_message=_STAGE_MESSAGES[BatchStage.QUEUED],
                            source_channel=source_channel,
                            ocr_mode=mode,
                            total_files=len(documents),
                            batch_existing_bills=snapshot,
                        )
                        transaction.upsert_source_documents(
                            batch_id, documents, stored_paths=stored_paths
                        )
                    try:
                        task = self._executor.submit(self._process_batch, lease, summary["version"])
                    except RuntimeError as exc:
                        submission_error = exc
                    else:
                        self._tasks[batch_id] = task
                        pending_lease.pop_all()  # The queued worker now owns the lease.
            if submission_error is not None:
                _LOGGER.error(
                    "Could not queue billing batch %s", batch_id, exc_info=submission_error
                )
                self._mark_processing_failure(lease, summary["version"])
                return self.get_batch(batch_id) if wait else summary
        task.add_done_callback(lambda completed: self._forget_task(batch_id, completed))
        return task.result() if wait else summary

    def _retain_inputs(
        self,
        batch_id: str,
        files: Sequence[Upload],
        public_urls: Sequence[str],
        paths: list[Path],
    ) -> tuple[list[ParsedDocument], dict[str, Path]]:
        documents = []
        stored_paths = {}
        for upload in files:
            filename = upload[0]
            data = self._upload_bytes(upload)
            name = sanitize_filename(filename)
            doc_id = uuid4().hex
            stored_paths[doc_id] = self._write_original(batch_id, name, data, paths)
            documents.append(
                ParsedDocument(
                    doc_id=doc_id,
                    original_filename=filename,
                    sanitized_filename=name,
                    source_kind=detect_source_kind(name),
                    content_sha256=sha256(data).hexdigest(),
                    extraction_channel=ExtractionChannel.FAILED,
                    status=DocumentStatus.FAILED,
                )
            )
        for url in public_urls:
            filename = unquote(urlsplit(url.strip()).path.rsplit("/", 1)[-1]) or "download"
            documents.append(
                ParsedDocument(
                    doc_id=uuid4().hex,
                    original_filename=filename,
                    sanitized_filename=sanitize_filename(filename),
                    source_kind=SourceKind.URL,
                    public_url=url.strip(),
                    extraction_channel=ExtractionChannel.FAILED,
                    status=DocumentStatus.FAILED,
                )
            )
        return documents, stored_paths

    def create_demo_batch(
        self,
        *,
        accounting_entity: str = _DEFAULT_ENTITY,
        ocr_mode: OcrMode | str = OcrMode.REMOTE_ONLY,
        wait: bool = True,
    ) -> dict[str, Any]:
        override = os.environ.get("SHIPPING_BILLING_REAL_SAMPLES_DIR")
        if override:
            directory = Path(override)
        else:
            repo_root = Path(__file__).resolve().parents[3]
            directory = repo_root / "笔试题AIAGENT"
            if not directory.is_dir() and repo_root.parent.name == ".worktree":
                directory = repo_root.parent.parent / "笔试题AIAGENT"
        samples = [
            directory / name for name in ("图片示例.png", "PDF示例账单1.pdf", "PDF 示例账单2.pdf")
        ]
        emails = sorted(directory.glob("深圳容宇 PRE-ALERT*.eml"))
        if len(emails) != 1 or not all(path.is_file() for path in [*samples, *emails]):
            raise ServiceValidationError(
                "DEMO_SAMPLES_MISSING",
                "演示需四份原题样本。请将原件放入笔试题AIAGENT，"
                "或将 SHIPPING_BILLING_REAL_SAMPLES_DIR 指向样本目录。",
            )
        with ExitStack() as stack:
            try:
                files = [
                    (path.name, stack.enter_context(path.open("rb")))
                    for path in [*samples, *emails]
                ]
            except OSError as exc:
                raise ServiceValidationError(
                    "DEMO_SAMPLES_MISSING", "无法读取演示样本原件"
                ) from exc
            return self.create_batch(
                files=files, accounting_entity=accounting_entity, ocr_mode=ocr_mode, wait=wait
            )

    def _dispatcher(self, mode: OcrMode) -> DocumentParserDispatcher:
        return DocumentParserDispatcher(
            ocr_mode=mode,
            limits=self.limits,
            remote_ocr=self._remote_ocr,
            local_ocr=self._local_ocr,
            url_validator=self._url_validator,
            relay_publisher=self._relay_publisher,
            public_relay_base_url=self.settings.public_relay_base_url,
            relay_presign_ttl_seconds=self.settings.relay_presign_ttl_seconds,
        )

    def _run_pipeline(
        self,
        documents: Sequence[ParsedDocument],
        entity: str,
        scope: _BatchScope,
        batch_id: str,
        *,
        stage_changed: Callable[[BatchStage], None] | None = None,
        targets: set[str] | None = None,
    ) -> BatchPipelineResult:
        with ExitStack() as stack:
            client = self._llm_client
            if (
                client is None
                and self._extractor_mode != "rule_only"
                and self.settings.is_llm_configured()
            ):
                owned_client = OpenAICompatibleLlmClient(self.settings)
                stack.callback(owned_client.close)
                client = owned_client
            graph = _ServiceBillingNodes(
                client, self._extractor_mode, self.limits, stage_changed, targets
            ).compile()
            output = asyncio.run(
                graph.ainvoke(
                    BillingGraphInput(
                        documents=documents,
                        accounting_entity=entity,
                        existing_bills=scope.existing_bills,
                        charge_dictionary=scope.charge_dictionary,
                        profiles=scope.profiles,
                        batch_id=batch_id,
                    )
                )
            )
        # B02 hands only sanitized URLs to the model. Keep the original locator for local retry.
        public_urls = {document.doc_id: document.public_url for document in documents}
        for bundle in output["result"].documents:
            bundle.document.public_url = public_urls[bundle.document.doc_id]
        return output["result"]

    def _store_child_originals(
        self,
        batch_id: str,
        documents: Sequence[ParsedDocument],
        paths: list[Path],
        reused: Mapping[str, str | Path | None],
    ) -> dict[str, str | Path | None]:
        stored = {}
        for document in documents:
            if document.doc_id in reused:
                stored[document.doc_id] = reused[document.doc_id]
            elif document.raw_bytes is not None:
                stored[document.doc_id] = self._write_original(
                    batch_id, document.sanitized_filename, document.raw_bytes, paths
                )
            else:
                stored[document.doc_id] = None
        return stored

    @contextmanager
    def _worker_transaction(
        self, lease: BatchWorkerLease, version: int
    ) -> Iterator[BillingRepository]:
        with self.repository.transaction() as transaction:
            if not lease.active or not transaction.is_processing_version(lease.batch_id, version):
                raise _StaleBatchWorker(lease.batch_id)
            yield transaction

    def _advance(self, lease: BatchWorkerLease, stage: BatchStage, version: int) -> int:
        with self._worker_transaction(lease, version) as transaction:
            batch = transaction.update_batch_stage(
                lease.batch_id, stage, stage_message=_STAGE_MESSAGES[stage]
            )
        return batch["version"]

    def _parse_retained(
        self,
        batch_id: str,
        row: Mapping[str, Any],
        dispatcher: DocumentParserDispatcher,
        depth: int,
        remaining_slots: int,
    ) -> list[ParsedDocument]:
        url = row["public_url"]
        try:
            data = self._read_original(batch_id, row) if url is None else None
        except ServiceValidationError as exc:
            document = _document_bundle(row).document
            document.error_message = exc.message
            return [document]
        return dispatcher.retry_single_document(
            row["original_filename"],
            data,
            existing_doc_id=row["id"],
            parent_doc_id=row["parent_doc_id"],
            media_type=row["media_type"],
            public_url=url,
            depth=depth,
            remaining_expanded_slots=remaining_slots,
        )

    def _unpack_batch(
        self, batch: Mapping[str, Any], lease: BatchWorkerLease, version: int
    ) -> set[str]:
        batch_id = lease.batch_id
        unpacker = _UnpackingDispatcher(self.limits, self._url_validator)
        remaining_slots = self.limits.max_expanded_documents - batch["total_files"]
        for row in batch["documents"]:
            parsed = self._parse_retained(batch_id, row, unpacker, 1, remaining_slots)
            remaining_slots -= len(parsed) - 1
            with self._staged_originals(batch_id) as paths:
                paths_by_id = self._store_child_originals(
                    batch_id, parsed, paths, {row["id"]: row["stored_path"]}
                )
                with self._worker_transaction(lease, version) as transaction:
                    transaction.upsert_source_documents(batch_id, parsed, stored_paths=paths_by_id)
        return unpacker.pending

    def _extract_documents(
        self,
        lease: BatchWorkerLease,
        version: int,
        dispatcher: DocumentParserDispatcher,
        pending: set[str],
    ) -> list[ParsedDocument]:
        batch_id = lease.batch_id
        rows = _require_batch(self.repository, batch_id)["documents"]
        unpacked = [_document_bundle(row).document for row in rows]
        documents = []
        for row, document in zip(rows, unpacked, strict=True):
            if document.doc_id in pending:
                depth, slots = compute_retry_context(unpacked, document.doc_id, self.limits)
                (document,) = self._parse_retained(batch_id, row, dispatcher, depth, slots)
                with self._worker_transaction(lease, version) as transaction:
                    transaction.upsert_source_documents(batch_id, [document])
                document.raw_bytes = None
            documents.append(document)
        return documents

    def _process_batch(self, lease: BatchWorkerLease, version: int) -> dict[str, Any]:
        batch_id = lease.batch_id

        def advance(stage: BatchStage) -> None:
            nonlocal version
            version = self._advance(lease, stage, version)

        with lease:
            try:
                advance(BatchStage.UNPACKING)
                batch = _require_batch(self.repository, batch_id)
                entity = batch["accounting_entity"]
                scope = _load_batch_scope(self.repository, batch, entity)
                pending = self._unpack_batch(batch, lease, version)
                dispatcher = self._dispatcher(OcrMode(batch["ocr_mode"]))
                advance(BatchStage.EXTRACTING)
                documents = self._extract_documents(lease, version, dispatcher, pending)

                result = self._run_pipeline(
                    documents, entity, scope, batch_id, stage_changed=advance
                )
                outcome = _batch_outcome(
                    [bundle.document for bundle in result.documents],
                    result.records,
                    result.failed_document_alerts,
                )
                if batch["batch_existing_bills"] is None:
                    outcome["shared_baseline_snapshot"] = scope.baseline
                with self._worker_transaction(lease, version) as transaction:
                    transaction.upsert_source_documents(batch_id, result.documents)
                    transaction.upsert_billing_records(batch_id, result.records)
                    transaction.update_batch_with_version_check(batch_id, version, **outcome)
            except _StaleBatchWorker:
                _LOGGER.debug("Stopped superseded billing worker for batch %s", batch_id)
            except Exception:
                _LOGGER.exception("Billing batch %s failed during processing", batch_id)
                self._mark_processing_failure(lease, version)
            return self.get_batch(batch_id)

    def _mark_processing_failure(self, lease: BatchWorkerLease, version: int) -> None:
        batch_id = lease.batch_id
        try:
            with self._worker_transaction(lease, version) as transaction:
                batch = _require_batch(transaction, batch_id)
                documents = [_document_bundle(row).document for row in batch["documents"]]
                for document in documents:
                    if document.status is DocumentStatus.SKIPPED_NOISE:
                        continue
                    document.status = DocumentStatus.FAILED
                    document.extraction_channel = ExtractionChannel.FAILED
                    document.error_message = document.error_message or _PROCESSING_FAILURE
                    transaction.update_document_status_and_snapshot(
                        batch_id,
                        document.doc_id,
                        status=document.status,
                        extraction_channel=document.extraction_channel,
                        error_message=document.error_message,
                    )
                outcome = _batch_outcome(
                    documents, batch["records"], batch["failed_document_alerts"]
                )
                outcome.update(
                    status=BatchStatus.PARTIAL_ERROR,
                    stage=BatchStage.FAILED,
                    stage_message=_PROCESSING_FAILURE,
                )
                transaction.update_batch_with_version_check(batch_id, version, **outcome)
        except _StaleBatchWorker:
            _LOGGER.debug("Kept current state after failure of superseded worker for %s", batch_id)
