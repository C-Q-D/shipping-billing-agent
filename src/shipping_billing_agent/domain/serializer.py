"""Project billing records onto the standard six-key export contract."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import Any, Literal

from .charge_normalizer import normalize_document_date
from .currency import is_valid_iso4217
from .entity_resolver import apply_human_overrides
from .models import (
    OperationType,
    PaymentTerm,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    dumps_decimal_json,
    parse_decimal_strict,
    sanitize_source_path,
)

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
    "source_doc_totals",
    "field_diffs",
    "validation_alerts",
    "excluded_clauses",
    "human_overrides",
    "human_review_history",
    "raw_invalid_fields",
    "raw_parse_issues",
    "reject_reason",
    "retry_count",
}


def _json_values(value: Any) -> Any:
    # Python-mode model dumps keep exact Decimals for dumps_decimal_json.
    if isinstance(value, Enum):
        return _json_values(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if (isinstance(value, Decimal) and not value.is_finite()) or (
        isinstance(value, float) and not isfinite(value)
    ):
        return str(value)
    if isinstance(value, dict):
        return {_json_values(key): _json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_values(item) for item in value), key=dumps_decimal_json)
    return value


def _export_number(value: object, *, pending: bool) -> Decimal | None:
    if value is None:
        return None
    try:
        return parse_decimal_strict(value)
    except ValueError:
        if not pending:
            raise
        return None


def _serialize_record(record: ProcessedBillingRecord, *, with_audit: bool) -> dict[str, Any]:
    operation = record.operation
    pending = operation is OperationType.PENDING_REVIEW or record.review_status in (
        ReviewStatus.NEEDS_REVIEW,
        ReviewStatus.REJECTED,
    )
    edited, corrected, override_issues = apply_human_overrides(record)
    invalid = (record.raw_invalid_fields.keys() - corrected) | {
        alert.field for alert in override_issues
    }
    currency = record.currency
    if (
        not isinstance(currency, str)
        or not is_valid_iso4217(currency)
        or "currency" in invalid
        or "currency" in record.raw_invalid_fields
        and edited.currency != currency
    ):
        currency = None
    document_date = None
    if (
        isinstance(record.document_date, str)
        and "document_date" not in invalid
        and (
            "document_date" not in record.raw_invalid_fields
            or edited.document_date == record.document_date
        )
    ):
        document_date, _ = normalize_document_date(record.document_date)

    quantity = _export_number(record.quantity, pending=pending)
    unit_price = _export_number(record.unit_price, pending=pending)
    amount = _export_number(record.amount, pending=pending)
    quantity = None if "quantity" in invalid else quantity
    unit_price = None if "unit_price" in invalid else unit_price
    amount = None if "amount" in invalid else amount
    posting_type = (
        str(record.posting_type)
        if "posting_type" not in invalid and record.posting_type in (PostingType.AR, PostingType.AP)
        else None
    )
    payment_term = (
        str(record.payment_term)
        if "payment_term" not in invalid and record.payment_term in (PaymentTerm.PP, PaymentTerm.CC)
        else None
    )
    charge_item = (
        record.charge_item
        if "charge_item" not in invalid and isinstance(record.charge_item, str)
        else None
    )
    matched_ref = record.matched_bill_ref or record.matched_existing_bill_id
    if matched_ref and not matched_ref.strip():
        matched_ref = record.matched_existing_bill_id
    if not record.accounting_entity.strip():
        raise ValueError(f"账单 {record.id} 缺少记账主体")
    if operation in _READY_OPERATIONS and (
        not charge_item
        or not charge_item.strip()
        or amount is None
        or currency is None
        or posting_type is None
    ):
        if not pending:
            raise ValueError(f"账单 {record.id} 缺少可入账所需的费用、金额、币种或 AR/AP")
        operation = OperationType.PENDING_REVIEW
    if operation is OperationType.UPDATE and (not matched_ref or not matched_ref.strip()):
        if not pending:
            raise ValueError(f"更新账单 {record.id} 缺少历史账单引用")
        operation = OperationType.PENDING_REVIEW

    reasoning = [reason for reason in record.ai_reasoning if reason.strip()]
    source = record.source_evidence.with_sanitized_paths()
    audit_trail = {
        "ai_reasoning": reasoning or [_DEFAULT_REASONING[operation]],
        "match_level": record.match_level.value,
        "matched_bill_ref": matched_ref,
        "source_file": source.source_file,
        "extraction_channel": source.extraction_channel.value
        if source.extraction_channel
        else None,
    }
    if with_audit:
        audit_trail["record_id"] = record.id
        audit = record.model_copy(update={"source_evidence": source}).model_dump(
            mode="python", include=_AUDIT_FIELDS, warnings=False
        )
        for ref in audit["shipment_refs"]["mbl_nos"]:
            ref["source"] = sanitize_source_path(ref["source"])
        audit_trail.update(_json_values(audit))

    return {
        "operation": operation.value,
        "document_info": {
            "document_type": record.document_type,
            "reference_no": record.reference_no,
            "document_date": document_date,
        },
        "transaction_party": {"issuer": record.issuer, "recipient": record.recipient},
        "charge_details": {
            "charge_item": charge_item,
            "raw_charge_name": record.raw_charge_name,
            "quantity": quantity,
            "unit_price": unit_price,
            "amount": amount,
            "currency": currency,
            "payment_term": payment_term,
        },
        "financial_attributes": {
            "accounting_entity": record.accounting_entity,
            "posting_type": posting_type,
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
        if mode != "audit" and record.review_status is ReviewStatus.REJECTED:
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
