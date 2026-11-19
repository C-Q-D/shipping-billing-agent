"""Match source billing lines to historical bills and inherit omitted values."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any
from unicodedata import normalize

from .charge_normalizer import normalize_charge_item, normalize_document_date
from .currency import is_valid_iso4217
from .entity_resolver import entity_matches, normalize_party_name
from .models import (
    DECIMAL_TOLERANCE,
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    CandidateBillSummary,
    ExistingBillRecord,
    FieldDiff,
    MatchLevel,
    OperationType,
    ProcessedBillingRecord,
    ReviewStatus,
    ShipmentRefs,
    SourceRefValue,
    ValidationAlert,
    parse_decimal_strict,
)

_BILL_FIELDS = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "charge_item",
    "raw_charge_name",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "payment_term",
    "posting_type",
)
_DIFF_FIELDS = (
    "amount",
    "currency",
    "payment_term",
    "document_date",
    "issuer",
    "recipient",
    "posting_type",
)
_TRANSPORT_FIELDS = {
    "mbl_no": "mbl_nos",
    "hbl_no": "hbl_no",
    "booking_no": "booking_no",
    "container_nos": "container_nos",
}
_INVALID_FIELD_CODES = {
    "currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
    "document_date": AlertCode.INVALID_DOCUMENT_DATE,
    "amount": AlertCode.OCR_NUMERIC_CONFUSION,
    "quantity": AlertCode.OCR_NUMERIC_CONFUSION,
    "unit_price": AlertCode.OCR_NUMERIC_CONFUSION,
}
_AMOUNT_TOLERANCE = Fraction(DECIMAL_TOLERANCE)
_BillPool = tuple[ExistingBillRecord, ...]


def _reference_key(value: str | None) -> str:
    key = "".join(normalize("NFKC", value or "").upper().split())
    return key if any(character.isalnum() for character in key) else ""


def _container_keys(values: Sequence[str]) -> set[str]:
    return {key for value in values if (key := _reference_key(value))}


def _source_alerts(record: ProcessedBillingRecord) -> list[ValidationAlert]:
    # An earlier ambiguous match is recomputed against the current baseline and overrides.
    alerts = [
        alert
        for alert in record.validation_alerts
        if not (
            record.match_level is MatchLevel.AMBIGUOUS_MULTI
            and alert.code is AlertCode.AMBIGUOUS_EXISTING_BILL
            and alert.severity is not AlertSeverity.RESOLVED
        )
    ]
    for issue in record.raw_parse_issues:
        if not any(
            (alert.code, alert.field, alert.actual) == (issue.code, issue.field, issue.actual)
            for alert in alerts
        ):
            alerts.append(issue)
    blocked_fields = {alert.field for alert in alerts if alert.severity is AlertSeverity.BLOCKER}
    for field, original in record.raw_invalid_fields.items():
        if field not in blocked_fields:
            alerts.append(
                ValidationAlert.from_code(
                    _INVALID_FIELD_CODES.get(field, AlertCode.LLM_EXTRACTION_FAILED),
                    "源单字段值无效，待复核",
                    field=field,
                    actual=original,
                )
            )
    return alerts


def _prepare_record(record: ProcessedBillingRecord) -> ProcessedBillingRecord:
    values = record.model_dump()
    inherited = values["inherited_fields"]
    for field in (*_BILL_FIELDS, "shipment_refs"):
        if field in inherited:
            values[field] = inherited[field]
    invalid = record.raw_invalid_fields
    overrides = record.human_overrides
    amount_before_overrides = values["amount"]

    for field in (*_BILL_FIELDS, "payer_hint", "payee_hint"):
        if field in invalid:
            values[field] = None
        elif field in overrides:
            values[field] = overrides[field]

    refs = dict(values["shipment_refs"] or {})
    if "shipment_refs" in invalid:
        refs = {}
    else:
        if "shipment_refs" in overrides:
            refs = ShipmentRefs.model_validate(overrides["shipment_refs"] or {}).model_dump()
        for field, attribute in _TRANSPORT_FIELDS.items():
            if field in invalid:
                refs.pop(attribute, None)
            elif field in overrides:
                value = overrides[field]
                if field == "mbl_no":
                    refs[attribute] = [{"value": value, "source": "人工修订"}] if value else []
                elif field == "container_nos":
                    refs[attribute] = [] if value is None else value
                else:
                    refs[attribute] = value

    values.update(
        shipment_refs=refs,
        match_level=MatchLevel.NONE,
        matched_existing_bill_id=None,
        matched_bill_ref=None,
        candidate_bills=[],
        field_diffs=[],
        inherited_fields={},
        validation_alerts=_source_alerts(record),
    )
    prepared = ProcessedBillingRecord.model_validate(values)
    if (
        prepared.raw_explicit_amount is not None
        or amount_before_overrides is None
        or "amount" in invalid
    ):
        return prepared

    # Older callers may supply a typed source amount without explicit field metadata.
    has_source_amount = "amount" in record.explicit_fields or (
        not record.explicit_fields
        and (
            record.raw_amount_text
            or "amount" not in overrides
            or parse_decimal_strict(amount_before_overrides) != prepared.amount
        )
    )
    if has_source_amount:
        raw_amount = parse_decimal_strict(record.raw_amount_text or amount_before_overrides)
        prepared = prepared.model_copy(update={"raw_explicit_amount": raw_amount})
    return prepared


def _find_bill_pool(
    record: ProcessedBillingRecord, bills: Sequence[ExistingBillRecord], charge_item: str | None
) -> tuple[MatchLevel, _BillPool]:
    refs = record.shipment_refs
    if charge_item:
        same_charge = [
            bill for bill in bills if normalize_charge_item(bill.charge_item)[0] == charge_item
        ]
        if reference := _reference_key(record.reference_no):
            pool = tuple(
                bill for bill in same_charge if _reference_key(bill.reference_no) == reference
            )
            if pool:
                return MatchLevel.L1_DOC_CHARGE, pool

        currency = record.currency
        if "currency" not in record.raw_invalid_fields and (
            currency is None or is_valid_iso4217(currency)
        ):
            # With omitted currency, retain every BL/booking candidate for the ambiguity check.
            eligible = [
                bill for bill in same_charge if currency is None or bill.currency == currency
            ]
            mbl_keys = {key for ref in refs.mbl_nos if (key := _reference_key(ref.value))}
            hbl_key = _reference_key(refs.hbl_no)
            pool = tuple(
                bill
                for bill in eligible
                if _reference_key(bill.mbl_no) in mbl_keys
                or (hbl_key and _reference_key(bill.hbl_no) == hbl_key)
            )
            if pool:
                return MatchLevel.L2_BL_CHARGE_CURR, pool

            if booking := _reference_key(refs.booking_no):
                pool = tuple(
                    bill for bill in eligible if _reference_key(bill.booking_no) == booking
                )
                if pool:
                    return MatchLevel.L3_BOOKING_CHARGE_CURR, pool

    containers = _container_keys(refs.container_nos)
    issuer = normalize_party_name(record.issuer)
    recipient = normalize_party_name(record.recipient)
    date, _ = normalize_document_date(record.document_date)
    if containers and issuer and recipient and date and record.amount is not None:
        amount = Fraction(record.amount)
        pool = tuple(
            bill
            for bill in bills
            if containers.intersection(_container_keys(bill.container_nos))
            and normalize_party_name(bill.issuer) == issuer
            and normalize_party_name(bill.recipient) == recipient
            and normalize_document_date(bill.document_date)[0] == date
            and abs(amount - Fraction(bill.amount)) <= _AMOUNT_TOLERANCE
        )
        if pool:
            return MatchLevel.L4_CNTR_PARTY_DATE_AMT, pool
    return MatchLevel.NONE, ()


def _pairing_currency(record: ProcessedBillingRecord, currencies: set[str]) -> str | None:
    if "currency" in record.raw_invalid_fields:
        return None
    if record.currency is not None:
        return record.currency if is_valid_iso4217(record.currency) else None
    if len(currencies) == 1:
        return next(iter(currencies))
    return None


def _pair_group(
    records: Sequence[ProcessedBillingRecord], indexes: list[int], pool: _BillPool
) -> dict[int, ExistingBillRecord]:
    if len(indexes) == len(pool) == 1:
        return {indexes[0]: pool[0]}

    currencies = {bill.currency for bill in pool}
    new_by_amount: dict[tuple[Decimal, str], list[int]] = defaultdict(list)
    old_by_amount: dict[tuple[Decimal, str], list[ExistingBillRecord]] = defaultdict(list)
    for index in indexes:
        record = records[index]
        currency = _pairing_currency(record, currencies)
        if record.amount is not None and currency is not None:
            new_by_amount[record.amount, currency].append(index)
    for bill in pool:
        old_by_amount[bill.amount, bill.currency].append(bill)

    pairs = {}
    for key, new_lines in new_by_amount.items():
        old_lines = old_by_amount.get(key, [])
        if len(new_lines) == len(old_lines) == 1:
            pairs[new_lines[0]] = old_lines[0]

    used_ids = {bill.id for bill in pairs.values()}
    remaining_by_currency: dict[str, list[ExistingBillRecord]] = defaultdict(list)
    for bill in pool:
        if bill.id not in used_ids:
            remaining_by_currency[bill.currency].append(bill)
    remaining_currencies = set(remaining_by_currency)
    unpaired_by_currency: dict[str, list[int]] = defaultdict(list)
    for index in indexes:
        if index not in pairs:
            currency = _pairing_currency(records[index], remaining_currencies)
            if currency is not None:
                unpaired_by_currency[currency].append(index)

    for currency, new_lines in unpaired_by_currency.items():
        remaining = remaining_by_currency.get(currency, [])
        if len(new_lines) == len(remaining) == 1:
            pairs[new_lines[0]] = remaining[0]
    return pairs


def _with_decision(
    record: ProcessedBillingRecord, operation: OperationType, **updates: Any
) -> ProcessedBillingRecord:
    alerts = updates.get("validation_alerts", record.validation_alerts)
    needs_review = any(alert.severity is not AlertSeverity.RESOLVED for alert in alerts)
    return record.model_copy(
        update={
            **updates,
            "operation": OperationType.PENDING_REVIEW if needs_review else operation,
            "review_status": ReviewStatus.NEEDS_REVIEW if needs_review else ReviewStatus.AUTO_READY,
        }
    )


def _ambiguous(
    record: ProcessedBillingRecord, level: MatchLevel, pool: _BillPool, message: str
) -> ProcessedBillingRecord:
    candidates = []
    for bill in pool:
        shipment_ref = bill.mbl_no or bill.hbl_no or bill.booking_no or "-"
        candidates.append(
            CandidateBillSummary(
                id=bill.id,
                label=(
                    f"{bill.external_bill_key or bill.id} | {shipment_ref} | {bill.charge_item} | "
                    f"{bill.amount:f} {bill.currency} | {bill.document_date or '-'}"
                ),
                reference_no=bill.reference_no,
                charge_item=bill.charge_item,
                amount=bill.amount,
                currency=bill.currency,
                document_date=bill.document_date,
                match_level=level,
            )
        )
    return _with_decision(
        record,
        OperationType.PENDING_REVIEW,
        match_level=MatchLevel.AMBIGUOUS_MULTI,
        candidate_bills=candidates,
        validation_alerts=[
            *record.validation_alerts,
            ValidationAlert.from_code(AlertCode.AMBIGUOUS_EXISTING_BILL, message),
        ],
    )


def _source_supplied(record: ProcessedBillingRecord, field: str, value: Any) -> bool:
    if value is None or isinstance(value, str) and not value.strip():
        return False
    return (
        not record.explicit_fields
        or field in record.explicit_fields
        or field == "posting_type"
        or field == "charge_item"
        and "raw_charge_name" in record.explicit_fields
    )


def _inherit_shipment_refs(
    record: ProcessedBillingRecord, bill: ExistingBillRecord
) -> ShipmentRefs:
    refs = record.shipment_refs
    locked = record.human_overrides.keys() | record.raw_invalid_fields.keys()
    if "shipment_refs" in locked:
        return refs

    updates: dict[str, Any] = {}
    if (
        "mbl_no" not in locked
        and bill.mbl_no
        and _reference_key(bill.mbl_no)
        and not any(_reference_key(ref.value) for ref in refs.mbl_nos)
    ):
        updates["mbl_nos"] = [SourceRefValue(value=bill.mbl_no, source="历史账单")]
    for field in ("hbl_no", "booking_no"):
        historical = getattr(bill, field)
        if (
            field not in locked
            and _reference_key(historical)
            and not _reference_key(getattr(refs, field))
        ):
            updates[field] = historical
    if "container_nos" not in locked and not _container_keys(refs.container_nos):
        containers = [value for value in bill.container_nos if _reference_key(value)]
        if containers:
            updates["container_nos"] = containers
    return refs.model_copy(update=updates) if updates else refs


def _comparison_value(field: str, value: Any) -> Any:
    if field in ("issuer", "recipient"):
        return normalize_party_name(value)
    if field == "document_date":
        return normalize_document_date(value)[0]
    return value


def _inherit_and_compare(
    record: ProcessedBillingRecord, bill: ExistingBillRecord, level: MatchLevel
) -> ProcessedBillingRecord:
    locked = record.human_overrides.keys() | record.raw_invalid_fields.keys()
    updates: dict[str, Any] = {}
    originals: dict[str, Any] = {}
    for field in _BILL_FIELDS:
        if field in locked:
            continue
        current, historical = getattr(record, field), getattr(bill, field)
        if (
            historical is not None
            and historical != current
            and not _source_supplied(record, field, current)
        ):
            updates[field] = historical
            originals[field] = current
    refs = _inherit_shipment_refs(record, bill)
    if refs != record.shipment_refs:
        updates["shipment_refs"] = refs
        originals["shipment_refs"] = record.shipment_refs.model_dump()

    diffs = []
    for field in _DIFF_FIELDS:
        old = getattr(bill, field)
        new = updates.get(field, getattr(record, field))
        if _comparison_value(field, old) != _comparison_value(field, new):
            diffs.append(FieldDiff(field=field, old_value=old, new_value=new, changed=True))
    return _with_decision(
        record,
        OperationType.UPDATE if diffs else OperationType.NO_CHANGE,
        **updates,
        inherited_fields=originals,
        field_diffs=diffs,
        match_level=level,
        matched_existing_bill_id=bill.id,
        matched_bill_ref=bill.external_bill_key or bill.id,
    )


def inherit_existing_bill(
    record: ProcessedBillingRecord,
    bill: ExistingBillRecord,
    *,
    match_level: MatchLevel = MatchLevel.NONE,
    profiles: Sequence[AccountingEntityProfile] = (),
) -> ProcessedBillingRecord:
    """Merge a selected historical bill and compute its business-field changes."""
    if not entity_matches(bill.accounting_entity, record.accounting_entity, profiles):
        raise ValueError("历史账单不属于当前记账主体")
    return _inherit_and_compare(_prepare_record(record), bill, match_level)


def match_and_inherit_batch(
    records: Sequence[ProcessedBillingRecord],
    existing_bills: Sequence[ExistingBillRecord],
    profiles: Sequence[AccountingEntityProfile] = (),
) -> list[ProcessedBillingRecord]:
    """Match within each source document while preserving the input line order.

    Bind profiles with functools.partial when using the two-argument entity callback.
    """
    sorted_bills = sorted(existing_bills, key=lambda bill: bill.id)
    bills_by_entity = {
        entity: [
            bill
            for bill in sorted_bills
            if entity_matches(bill.accounting_entity, entity, profiles)
        ]
        for entity in dict.fromkeys(record.accounting_entity for record in records)
    }
    prepared = [_prepare_record(record) for record in records]
    results = prepared.copy()
    groups: dict[
        tuple[str, str | None, MatchLevel, tuple[str, ...]], tuple[_BillPool, list[int]]
    ] = {}
    manual_claims: dict[tuple[str, str], list[tuple[int, ExistingBillRecord]]] = defaultdict(list)
    ambiguity = "无法为当前费用行唯一确定历史账单，请人工选择"

    for index, record in enumerate(prepared):
        if record.human_overrides.get("force_create_new") is True:
            results[index] = _with_decision(record, OperationType.CREATE)
            continue

        bills = bills_by_entity[record.accounting_entity]
        selected_id = record.human_overrides.get("matched_existing_bill_id")
        if selected_id:
            selected = next((bill for bill in bills if bill.id == selected_id), None)
            if selected is not None:
                manual_claims[record.source_doc_id, selected.id].append((index, selected))
                continue

        charge_item, _ = normalize_charge_item(record.charge_item)
        level, pool = _find_bill_pool(record, bills, charge_item)
        if not pool:
            results[index] = _with_decision(record, OperationType.CREATE)
        elif (
            level in (MatchLevel.L2_BL_CHARGE_CURR, MatchLevel.L3_BOOKING_CHARGE_CURR)
            and record.currency is None
            and len({bill.currency for bill in pool}) > 1
        ):
            results[index] = _ambiguous(record, level, pool, "源单省略币种，历史候选含多种币种")
        else:
            group = (record.source_doc_id, charge_item, level, tuple(bill.id for bill in pool))
            groups.setdefault(group, (pool, []))[1].append(index)

    for claimed in manual_claims.values():
        for index, bill in claimed:
            if len(claimed) == 1:
                results[index] = _inherit_and_compare(prepared[index], bill, MatchLevel.NONE)
            else:
                results[index] = _ambiguous(prepared[index], MatchLevel.NONE, (bill,), ambiguity)

    for (doc_id, _, level, _), (pool, indexes) in groups.items():
        available = tuple(bill for bill in pool if (doc_id, bill.id) not in manual_claims)
        pairs = _pair_group(prepared, indexes, available)
        for index in indexes:
            if bill := pairs.get(index):
                results[index] = _inherit_and_compare(prepared[index], bill, level)
            else:
                results[index] = _ambiguous(prepared[index], level, pool, ambiguity)
    return results


def match_record(
    record: ProcessedBillingRecord,
    existing_bills: Sequence[ExistingBillRecord],
    profiles: Sequence[AccountingEntityProfile] = (),
) -> ProcessedBillingRecord:
    """Match a single line using the same rules as the batch matcher."""
    return match_and_inherit_batch([record], existing_bills, profiles)[0]
