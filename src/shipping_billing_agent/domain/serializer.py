"""Project billing records onto the standard six-key export contract."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal

from .charge_normalizer import normalize_document_date
from .currency import is_valid_iso4217
from .models import OperationType, ProcessedBillingRecord, ReviewStatus, dumps_decimal_json

ExportMode = Literal["official", "full_standard", "audit"]

_READY_STATUSES = {ReviewStatus.AUTO_READY, ReviewStatus.APPROVED}
_READY_OPERATIONS = {OperationType.CREATE, OperationType.UPDATE, OperationType.NO_CHANGE}
_DEFAULT_REASONING = {
    OperationType.CREATE: "标记为新增账单",
    OperationType.UPDATE: "标记为更新历史账单",
    OperationType.NO_CHANGE: "历史账单的核心业务字段一致",
    OperationType.PENDING_REVIEW: "账单待人工复核",
}
_AUDIT_FIELDS = {
    "source_doc_id",
    "review_status",
    "matched_existing_bill_id",
    "shipment_refs",
    "source_evidence",
    "field_diffs",
    "validation_alerts",
    "excluded_clauses",
    "human_overrides",
    "human_review_history",
    "reject_reason",
    "retry_count",
}


def _json_values(value: Any) -> Any:
    # Python-mode model dumps keep exact Decimals for dumps_decimal_json.
    if isinstance(value, Enum):
        return _json_values(value.value)
    if isinstance(value, dict):
        return {_json_values(key): _json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_values(item) for item in value), key=dumps_decimal_json)
    return value


def _serialize_record(record: ProcessedBillingRecord, *, with_audit: bool) -> dict[str, Any]:
    currency = record.currency
    if "currency" in record.raw_invalid_fields or not is_valid_iso4217(currency):
        currency = None
    document_date = None
    if "document_date" not in record.raw_invalid_fields:
        document_date, _ = normalize_document_date(record.document_date)

    matched_ref = record.matched_bill_ref or record.matched_existing_bill_id
    if matched_ref and not matched_ref.strip():
        matched_ref = record.matched_existing_bill_id
    if not record.accounting_entity.strip():
        raise ValueError(f"账单 {record.id} 缺少记账主体")
    if record.operation in _READY_OPERATIONS and (
        not record.charge_item
        or not record.charge_item.strip()
        or record.amount is None
        or currency is None
        or record.posting_type is None
    ):
        raise ValueError(f"账单 {record.id} 缺少可入账所需的费用、金额、币种或 AR/AP")
    if record.operation is OperationType.UPDATE and (not matched_ref or not matched_ref.strip()):
        raise ValueError(f"更新账单 {record.id} 缺少历史账单引用")

    reasoning = [reason for reason in record.ai_reasoning if reason.strip()]
    source = record.source_evidence
    audit_trail = {
        "ai_reasoning": reasoning or [_DEFAULT_REASONING[record.operation]],
        "match_level": record.match_level.value,
        "matched_bill_ref": matched_ref,
        "source_file": source.source_file,
        "extraction_channel": source.extraction_channel.value
        if source.extraction_channel
        else None,
    }
    if with_audit:
        audit_trail["record_id"] = record.id
        audit_trail.update(_json_values(record.model_dump(mode="python", include=_AUDIT_FIELDS)))

    return {
        "operation": record.operation.value,
        "document_info": {
            "document_type": record.document_type,
            "reference_no": record.reference_no,
            "document_date": document_date,
        },
        "transaction_party": {"issuer": record.issuer, "recipient": record.recipient},
        "charge_details": {
            "charge_item": record.charge_item,
            "raw_charge_name": record.raw_charge_name,
            "quantity": record.quantity,
            "unit_price": record.unit_price,
            "amount": record.amount,
            "currency": currency,
            "payment_term": record.payment_term.value if record.payment_term else None,
        },
        "financial_attributes": {
            "accounting_entity": record.accounting_entity,
            "posting_type": record.posting_type.value if record.posting_type else None,
        },
        "audit_trail": audit_trail,
    }


def serialize_export_bundle(
    records: Sequence[ProcessedBillingRecord], mode: ExportMode = "official"
) -> dict[str, Any]:
    if mode not in ("official", "full_standard", "audit"):
        raise ValueError(f"未知的账单导出口径 {mode!r}")

    items = []
    excluded_pending_count = 0
    excluded_rejected_count = 0
    for record in records:
        if record.review_status is ReviewStatus.REJECTED:
            excluded_rejected_count += 1
        elif mode == "official" and (
            record.review_status not in _READY_STATUSES or record.operation not in _READY_OPERATIONS
        ):
            excluded_pending_count += 1
        else:
            items.append(_serialize_record(record, with_audit=mode == "audit"))
    return {
        "mode": mode,
        "total_records": len(records),
        "included_count": len(items),
        "excluded_pending_count": excluded_pending_count,
        "excluded_rejected_count": excluded_rejected_count,
        "items": items,
    }


def serialize_records(
    records: Sequence[ProcessedBillingRecord], mode: ExportMode = "official"
) -> list[dict[str, Any]]:
    return serialize_export_bundle(records, mode)["items"]


def serialize_records_json(
    records: Sequence[ProcessedBillingRecord], mode: ExportMode = "official"
) -> str:
    return dumps_decimal_json(serialize_records(records, mode))
