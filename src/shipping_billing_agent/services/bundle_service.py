"""Move a batch's review state between databases without copying its source files."""

from __future__ import annotations

import ntpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from graphlib import TopologicalSorter
from hashlib import sha256
from sqlite3 import IntegrityError
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints

from ..domain.currency import is_valid_iso4217
from ..domain.entity_resolver import entity_matches
from ..domain.models import (
    BatchStatus,
    DocumentExtractionBundle,
    DocumentStatus,
    ExistingBillRecord,
    ExtractionChannel,
    FailedDocumentAlert,
    FieldDiff,
    OcrListItem,
    OcrMode,
    OcrStructuredPayload,
    ParsedDocument,
    ProcessedBillingRecord,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ReviewStatus,
    ShipmentRefs,
    SourceKind,
    sanitize_source_path,
)
from ..domain.serializer import _json_values
from ..gateways.url_guard import HTTP_URL_PATTERN
from ..storage import BillingRepository, ResourceNotFoundError
from ..storage.db import utc_timestamp
from .batch_service import (
    ServiceValidationError,
    SourceChannel,
    _batch_detail,
    _batch_outcome,
    _document_bundle,
    _load_batch_scope,
    _require_batch,
)
from .review_service import _recheck

_BUNDLE_VERSION = "1.0"
_TOTAL_FIELDS = {"subtotal_amount", "tax_amount", "total_amount", "total_currency"}
_RECORD_SECTIONS = {
    "document_info": {"document_type", "reference_no", "document_date"},
    "transaction_party": {"issuer", "recipient"},
    "charge_details": {"charge_item", "amount", "currency", "payment_term"},
    "financial_attributes": {"accounting_entity", "posting_type"},
}
_RECORD_REQUIRED = {
    "id",
    "source_doc_id",
    "source_line_index",
    "operation",
    "review_status",
    "match_level",
    "ai_reasoning",
}
_RECORD_SOURCE_FIELDS = {
    *set().union(*_RECORD_SECTIONS.values()),
    "raw_charge_name",
    "quantity",
    "unit_price",
    "shipment_refs",
    "payer_hint",
    "payee_hint",
    "debit_credit",
    "matched_from_dictionary",
} - {"accounting_entity"}
_DOC_TOTALS_REVIEW = "document_totals_review"

_NonBlank = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class _PortableDocument(BaseModel):
    id: _NonBlank
    parent_doc_id: str | None = None
    original_filename: str
    sanitized_filename: str
    source_kind: SourceKind
    extraction_channel: ExtractionChannel
    status: DocumentStatus
    error_message: str | None = None
    content_sha256: str | None = None
    retry_count: int = Field(default=0, ge=0, le=1)
    affected_record_ids: list[str] = Field(default_factory=list)
    extraction_snapshot: RawExtractionEnvelope | None = None
    ocr_suspicious: bool = False
    document_totals_review: dict[str, Any] | None = None


class _PortableBundle(BaseModel):
    bundle_version: Literal["1.0"]
    accounting_entity: _NonBlank = Field(max_length=120)
    exported_at: str | None = None
    short_code: str | None = None
    ocr_mode: OcrMode = OcrMode.REMOTE_ONLY
    documents: list[_PortableDocument]
    records: list[dict[str, Any]]
    existing_bills_snapshot: list[ExistingBillRecord] = Field(default_factory=list)
    failed_documents: list[FailedDocumentAlert] = Field(default_factory=list)


class _AuditEntry(BaseModel):
    action: str
    operator: str
    reason: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    diff: list[FieldDiff] = Field(default_factory=list)
    timestamp: str


def _source_label(name: str) -> str:
    if name.lower().startswith(("http://", "https://")):
        return _safe_url(name)
    return ntpath.basename(name)


def _safe_url(url: str) -> str:
    cleaned = sanitize_source_path(url) or url
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return "[REDACTED]"
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]).geturl()


def _redact_sources(value: Any, field: str = "") -> Any:
    if isinstance(value, str):
        if field in {
            "source_file",
            "source_paths",
            "filename",
            "original_filename",
            "sanitized_filename",
        }:
            return _source_label(value)
        return HTTP_URL_PATTERN.sub(lambda match: _safe_url(match[0]), value)
    if isinstance(value, dict):
        return {key: _redact_sources(child, key) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_sources(child, field) for child in value]
    return value


def _has_ocr_flag(document: ParsedDocument) -> bool:
    return document.ocr_payload is not None and any(
        (item.model_extra or {}).get("suspicious") is True for item in document.ocr_payload.items
    )


def export_review_bundle(repository: BillingRepository, batch_id: str) -> dict[str, Any]:
    """Capture a consistent snapshot of a completed batch and its audit history."""
    with repository.transaction() as transaction:
        batch = _require_batch(transaction, batch_id)
        if batch["status"] == BatchStatus.PROCESSING:
            raise ServiceValidationError(
                "BATCH_PROCESSING", "批次仍在处理，请完成后再导出复核包", status_code=409
            )
        bills = batch["batch_existing_bills"]
        if bills is None:
            bills = _load_batch_scope(transaction, batch, batch["accounting_entity"]).existing_bills
        audit_by_record = defaultdict(list)
        for entry in transaction.list_review_audit_logs(batch_id):
            audit_by_record[entry["record_id"]].append(entry)

    detail = _batch_detail(batch)
    documents = []
    for row, summary in zip(batch["documents"], detail["documents"], strict=True):
        bundle = _document_bundle(row)
        documents.append(
            {
                **summary,
                "content_sha256": row["content_sha256"],
                "extraction_snapshot": (
                    bundle.extraction.model_dump() if bundle.extraction is not None else None
                ),
                "retry_count": bundle.retry_count,
                "ocr_suspicious": _has_ocr_flag(bundle.document),
                "document_totals_review": bundle.document.metadata.get(_DOC_TOTALS_REVIEW),
            }
        )
    return _redact_sources(
        _json_values(
            {
                "bundle_version": _BUNDLE_VERSION,
                "exported_at": utc_timestamp(),
                "short_code": batch["short_code"],
                "accounting_entity": batch["accounting_entity"],
                "ocr_mode": batch["ocr_mode"],
                "existing_bills_snapshot": [bill.model_dump() for bill in bills],
                "documents": documents,
                "records": [
                    {
                        **summary,
                        "record_state": record.model_dump(exclude={"batch_id"}),
                        "review_audit_logs": audit_by_record[record.id],
                    }
                    for record, summary in zip(batch["records"], detail["records"], strict=True)
                ],
                "failed_documents": detail["failed_documents"],
            }
        )
    )


def _document_snapshot(
    doc: _PortableDocument, raw: Mapping[str, Any], doc_ids: Mapping[str, str]
) -> DocumentExtractionBundle:
    digest = doc.content_sha256
    if doc.source_kind is not SourceKind.URL and digest is None:
        # The public summary has no content hash. Keep each such source distinct.
        digest = sha256(doc.id.encode("utf-8")).hexdigest()
    metadata = {}
    if doc.document_totals_review is not None:
        metadata[_DOC_TOTALS_REVIEW] = doc.document_totals_review
    ocr_payload = None
    if doc.ocr_suspicious:
        ocr_payload = OcrStructuredPayload(
            items=[OcrListItem(suspicious=True)], has_charges=False, has_metadata=False
        )
    document = ParsedDocument(
        doc_id=doc_ids[doc.id],
        parent_doc_id=doc_ids[doc.parent_doc_id] if doc.parent_doc_id is not None else None,
        original_filename=doc.original_filename,
        sanitized_filename=doc.sanitized_filename,
        source_kind=doc.source_kind,
        content_sha256=digest,
        status=doc.status,
        extraction_channel=doc.extraction_channel,
        error_message=doc.error_message,
        metadata=metadata,
        ocr_payload=ocr_payload,
    )
    summary = RawDocumentExtraction.model_validate(
        {
            field: raw[field]
            for field in (*_TOTAL_FIELDS, "shipment_refs", "excluded_clauses")
            if field in raw
        }
    )
    snapshot = doc.extraction_snapshot
    if snapshot is not None:
        if any(
            getattr(summary, field) != getattr(snapshot.extraction, field)
            for field in summary.model_fields_set
        ):
            raise ValueError("来源单据摘要与提取快照不一致")
    elif (
        summary.shipment_refs != ShipmentRefs()
        or summary.excluded_clauses
        or any(getattr(summary, name) is not None for name in _TOTAL_FIELDS)
    ):
        snapshot = RawExtractionEnvelope(extraction=summary)
    return DocumentExtractionBundle(
        document=document, extraction=snapshot, retry_count=doc.retry_count
    )


def _record_snapshot(raw: Mapping[str, Any]) -> ProcessedBillingRecord:
    if not _RECORD_REQUIRED <= raw.keys():
        raise ValueError("复核记录缺少必填字段")
    state = raw.get("record_state", {})
    if not isinstance(state, dict):
        raise ValueError("复核记录快照须为对象")
    displayed = {
        key: value for key, value in raw.items() if key in ProcessedBillingRecord.model_fields
    }
    displayed.pop("inherited_fields", None)
    for section, required in _RECORD_SECTIONS.items():
        fields = raw.get(section)
        if not isinstance(fields, dict) or not required <= fields.keys():
            raise ValueError(f"复核记录缺少 {section} 必填字段")
        displayed.update({key: fields[key] for key in required})
        if section == "charge_details":
            displayed.update(
                {
                    key: fields[key]
                    for key in ("raw_charge_name", "quantity", "unit_price")
                    if key in fields
                }
            )
    record = ProcessedBillingRecord.model_validate({**state, **displayed})
    if not record.id.strip() or not record.source_doc_id.strip():
        raise ValueError("复核记录和来源单据 ID 不能为空")
    if (record.inherited_fields.keys() | record.source_values_before_overrides.keys()) - (
        _RECORD_SOURCE_FIELDS
    ):
        raise ValueError("复核记录内的继承或修订快照包含不可恢复字段")
    inherited_fields = raw.get("inherited_fields", [])
    if not isinstance(inherited_fields, list) or any(
        not isinstance(field, str) for field in inherited_fields
    ):
        raise ValueError("inherited_fields 须为字符串数组")
    if set(inherited_fields) != record.inherited_fields.keys():
        if state:
            raise ValueError("复核记录与继承快照不一致")
        # A display-only bundle cannot prove the original values of inherited fields.
        record.review_status = ReviewStatus.NEEDS_REVIEW
    return record


def _remap_record(
    record: ProcessedBillingRecord,
    batch_id: str,
    entity: str,
    doc_ids: Mapping[str, str],
    record_ids: Mapping[str, str],
) -> ProcessedBillingRecord:
    evidence = record.source_evidence
    totals = record.source_doc_totals
    return record.model_copy(
        update={
            "id": record_ids[record.id],
            "batch_id": batch_id,
            "accounting_entity": entity,
            "source_doc_id": doc_ids[record.source_doc_id],
            "source_evidence": evidence.model_copy(
                update={
                    "source_doc_id": doc_ids[evidence.source_doc_id]
                    if evidence.source_doc_id
                    else None
                }
            ),
            "source_doc_totals": totals.model_copy(
                update={"source_doc_id": doc_ids[totals.source_doc_id]}
            )
            if totals is not None
            else None,
            "package_sibling_refs": [
                refs.model_copy(update={"source_doc_id": doc_ids[refs.source_doc_id]})
                for refs in record.package_sibling_refs
            ],
        }
    )


def _fresh_ids(ids: Sequence[str]) -> dict[str, str]:
    result = {old_id: uuid4().hex for old_id in ids}
    if len(result) != len(ids):
        raise ValueError("复核包中存在重复 ID")
    return result


def _remap_audit_snapshot(
    snapshot: dict[str, Any] | None,
    batch_id: str,
    doc_ids: Mapping[str, str],
    record_ids: Mapping[str, str],
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    # Historical audit entries can refer to a source that was later replaced.
    snapshot = snapshot.copy()
    snapshot["batch_id"] = batch_id
    for field, ids in (("id", record_ids), ("source_doc_id", doc_ids)):
        if isinstance(value := snapshot.get(field), str):
            snapshot[field] = ids.get(value, value)
    return snapshot


def _import_bundle(
    repository: BillingRepository, payload: dict[str, Any], source_channel: SourceChannel
) -> dict[str, Any]:
    bundle = _PortableBundle.model_validate(payload)
    records = [_record_snapshot(item) for item in bundle.records]
    doc_ids = _fresh_ids([doc.id for doc in bundle.documents])
    record_ids = _fresh_ids([record.id for record in records])
    parents = {
        doc.id: [] if doc.parent_doc_id is None else [doc.parent_doc_id] for doc in bundle.documents
    }
    if set(TopologicalSorter(parents).static_order()) != doc_ids.keys():
        raise ValueError("复核包中的父单据不存在")
    if any(
        record_id not in record_ids
        for doc in bundle.documents
        for record_id in doc.affected_record_ids
    ):
        raise ValueError("复核包中的单据引用了不存在的记录")
    bills = bundle.existing_bills_snapshot
    if len({bill.id for bill in bills}) != len(bills) or any(
        not bill.id.strip()
        or not bill.charge_item.strip()
        or not 1 <= len(bill.accounting_entity.strip()) <= 120
        or not is_valid_iso4217(bill.currency)
        for bill in bills
    ):
        raise ValueError("复核包含有无效或重复的历史账单")

    documents = [
        _document_snapshot(doc, raw, doc_ids)
        for doc, raw in zip(bundle.documents, payload["documents"], strict=True)
    ]
    failures = [
        alert.model_copy(
            update={
                "doc_id": doc_ids[alert.doc_id],
                "affected_record_ids": [
                    record_ids[record_id] for record_id in alert.affected_record_ids
                ],
            }
        )
        for alert in bundle.failed_documents
    ]
    with repository.transaction() as transaction:
        batch = transaction.create_batch(
            bundle.accounting_entity,
            source_channel=source_channel,
            ocr_mode=bundle.ocr_mode,
            batch_existing_bills=bills,
        )
        batch_id = batch["id"]
        scope = _load_batch_scope(
            transaction, {"batch_existing_bills": bills}, bundle.accounting_entity
        )
        if any(
            not entity_matches(record.accounting_entity, bundle.accounting_entity, scope.profiles)
            for record in records
        ):
            raise ValueError("复核记录与批次的记账主体不一致")
        prior = [
            _remap_record(record, batch_id, bundle.accounting_entity, doc_ids, record_ids)
            for record in records
        ]
        transaction.upsert_source_documents(batch_id, documents)
        checked = _recheck(prior, documents, scope, prior)
        if {record.id for record in checked} != set(record_ids.values()):
            raise ValueError("复核包中的重复来源会导致部分复核记录被合并")
        transaction.upsert_billing_records(batch_id, checked)
        for record, raw in zip(prior, bundle.records, strict=True):
            audit_entries = raw.get("review_audit_logs", [])
            if not isinstance(audit_entries, list):
                raise ValueError("复核日志须为数组")
            for item in audit_entries:
                entry = _AuditEntry.model_validate(item)
                transaction.insert_review_audit_log(
                    batch_id,
                    record.id,
                    action=entry.action,
                    operator=entry.operator,
                    reason=entry.reason,
                    diff=entry.diff,
                    timestamp=entry.timestamp,
                    before=_remap_audit_snapshot(entry.before, batch_id, doc_ids, record_ids),
                    after=_remap_audit_snapshot(entry.after, batch_id, doc_ids, record_ids),
                )
        outcome = _batch_outcome([item.document for item in documents], checked, failures)
        transaction.update_batch_with_version_check(batch_id, batch["version"], **outcome)
        return _batch_detail(_require_batch(transaction, batch_id))


def import_review_bundle(
    repository: BillingRepository,
    payload: Mapping[str, Any],
    *,
    source_channel: SourceChannel = "WEB",
) -> dict[str, Any]:
    """Validate and recheck an isolated bundle before publishing its new batch."""
    if payload.get("bundle_version") != _BUNDLE_VERSION:
        raise ServiceValidationError("INVALID_BUNDLE_VERSION", '复核包版本须为 "1.0"')
    try:
        return _import_bundle(repository, _redact_sources(dict(payload)), source_channel)
    except KeyError as exc:
        raise ServiceValidationError(
            "INVALID_REVIEW_BUNDLE", "复核包引用了不存在的单据或费用记录"
        ) from exc
    except (
        ValueError,
        TypeError,
        ArithmeticError,
        RecursionError,
        IntegrityError,
        ResourceNotFoundError,
    ) as exc:
        raise ServiceValidationError(
            "INVALID_REVIEW_BUNDLE", "复核包的主体、来源引用、记录或历史快照不合法"
        ) from exc
