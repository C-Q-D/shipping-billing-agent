"""Match source billing lines to historical bills and inherit omitted values."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Any
from unicodedata import normalize

from .charge_normalizer import normalize_charge_item, normalize_document_date
from .currency import is_valid_iso4217
from .entity_resolver import (
    FIELD_FORMAT_ALERT_CODES,
    PARTY_FIELDS,
    TRANSPORT_FIELDS,
    apply_human_overrides,
    entity_matches,
    has_explicit_parties,
    is_credit_document,
    resolve_entity_perspective,
    source_format_alerts,
)
from .models import (
    DECIMAL_TOLERANCE,
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    CandidateBillSummary,
    ChargeDictionaryItem,
    ExistingBillRecord,
    FieldDiff,
    MatchLevel,
    OperationType,
    PostingType,
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
_AUTOMATIC_LEVELS = (
    MatchLevel.L1_DOC_CHARGE,
    MatchLevel.L2_BL_CHARGE_CURR,
    MatchLevel.L3_BOOKING_CHARGE_CURR,
    MatchLevel.L4_CNTR_PARTY_DATE_AMT,
)
_AMOUNT_TOLERANCE = Fraction(DECIMAL_TOLERANCE)
_MATCH_LEVEL_LABELS = {
    MatchLevel.L1_DOC_CHARGE: "单据编号和费用名称",
    MatchLevel.L2_BL_CHARGE_CURR: "提单号、费用名称和币种",
    MatchLevel.L3_BOOKING_CHARGE_CURR: "订舱号、费用名称和币种",
    MatchLevel.L4_CNTR_PARTY_DATE_AMT: "箱号、交易双方、日期和金额",
}
_MATCH_REASON_PREFIXES = (
    *(f"按 {level.value}（" for level in _MATCH_LEVEL_LABELS),
    "已人工关联历史账单 ",
    "字段变更：",
    "未明确修改字段继承历史账单原值",
    "核心字段与历史账单一致，无变更",
)
_ENTITY_REASONING_MESSAGES = frozenset(
    {
        "记账主体匹配收款方，归入应收台账",
        "记账主体匹配付款方，归入应付台账",
        "当前记账主体无法唯一匹配单据的收款方或付款方",
        "人工确认记账方向，归入应收台账",
        "人工确认记账方向，归入应付台账",
    }
)
_BillPool = tuple[ExistingBillRecord, ...]


def _format_diff_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _reference_key(value: str | None) -> str:
    key = "".join(normalize("NFKC", value or "").upper().split())
    return key if any(character.isalnum() for character in key) else ""


def _container_keys(values: Sequence[str]) -> set[str]:
    return {key for value in values if (key := _reference_key(value))}


def _source_alerts(
    record: ProcessedBillingRecord, corrected: set[str], override_issues: list[ValidationAlert]
) -> list[ValidationAlert]:
    raw_issues = source_format_alerts(record, corrected)
    rebuilt_fields = record.raw_invalid_fields.keys() | record.human_overrides.keys()
    alerts = [
        alert
        for alert in record.validation_alerts
        if alert.severity is AlertSeverity.RESOLVED
        or (
            alert.code
            not in (AlertCode.AMBIGUOUS_EXISTING_BILL, AlertCode.NEGATIVE_NON_CREDIT_AMOUNT)
            and not (alert.field in rebuilt_fields and alert.code in FIELD_FORMAT_ALERT_CODES)
            and alert not in record.raw_parse_issues
        )
    ]
    return [*alerts, *raw_issues, *override_issues]


def _prepare_record(
    record: ProcessedBillingRecord,
    profiles: Sequence[AccountingEntityProfile],
    charge_dictionary: Sequence[ChargeDictionaryItem] | None,
    *,
    recompute_entity: bool = False,
) -> ProcessedBillingRecord:
    values = record.model_dump(warnings=False)
    values.update(values["inherited_fields"])
    values.update(values["source_values_before_overrides"])
    invalid = record.raw_invalid_fields
    for field in invalid.keys() & {*_BILL_FIELDS, *PARTY_FIELDS}:
        values[field] = None
    if "debit_credit" in invalid:
        values["debit_credit"] = "UNKNOWN"
    if recompute_entity:
        values["posting_type"] = None
    if (
        (recompute_entity or record.is_credit)
        and record.raw_explicit_amount is not None
        and "amount" not in invalid
    ):
        values["amount"] = record.raw_explicit_amount
    if (
        record.raw_explicit_amount is None
        and values["amount"] is not None
        and ("amount" in record.explicit_fields or not record.explicit_fields)
    ):
        try:
            values["raw_explicit_amount"] = parse_decimal_strict(
                record.raw_amount_text or values["amount"]
            )
        except ValueError:
            # Malformed raw text remains available to the OCR validator.
            values["raw_explicit_amount"] = None
    refs = dict(values["shipment_refs"] or {})
    if "shipment_refs" in invalid:
        refs = {}
    else:
        for field, attribute in TRANSPORT_FIELDS.items():
            if field in invalid:
                refs.pop(attribute, None)

    values.update(
        shipment_refs=refs,
        match_level=MatchLevel.NONE,
        matched_existing_bill_id=None,
        matched_bill_ref=None,
        candidate_bills=[],
        field_diffs=[],
        inherited_fields={},
        source_values_before_overrides={},
        ai_reasoning=[
            reason
            for reason in values.get("ai_reasoning", ())
            if isinstance(reason, str) and not reason.startswith(_MATCH_REASON_PREFIXES)
        ],
    )
    prepared = ProcessedBillingRecord.model_validate(values)
    prepared, corrected, override_issues = apply_human_overrides(prepared, charge_dictionary)
    alerts = _source_alerts(prepared, corrected, override_issues)
    party_edits = PARTY_FIELDS & (prepared.human_overrides.keys() | invalid.keys())
    manual_posting = "posting_type" in prepared.human_overrides
    unresolved_entity = any(alert.code is AlertCode.ENTITY_NOT_MATCHED for alert in alerts)
    needs_initial_resolution = (
        values["posting_type"] is None and not unresolved_entity and has_explicit_parties(prepared)
    )
    if recompute_entity or party_edits or needs_initial_resolution or manual_posting:
        perspective = resolve_entity_perspective(
            prepared.accounting_entity,
            issuer=prepared.issuer,
            recipient=prepared.recipient,
            payer_hint=prepared.payer_hint,
            payee_hint=prepared.payee_hint,
            profiles=profiles,
        )
        if not manual_posting:
            prepared = prepared.model_copy(update={"posting_type": perspective.posting_type})
        alerts = [alert for alert in alerts if alert.code is not AlertCode.ENTITY_NOT_MATCHED]
        if perspective.posting_type is None and (
            has_explicit_parties(prepared) or prepared.posting_type is None
        ):
            alerts.extend(perspective.validation_alerts)
        if recompute_entity:
            prepared = prepared.model_copy(update={"ai_reasoning": perspective.ai_reasoning})
        else:
            existing = [
                reason
                for reason in prepared.ai_reasoning
                if reason not in _ENTITY_REASONING_MESSAGES
            ]
            entity_unresolved = any(
                alert.code is AlertCode.ENTITY_NOT_MATCHED for alert in alerts
            )
            if prepared.posting_type is not None and not entity_unresolved:
                if perspective.posting_type is not None:
                    entity_reasons = perspective.ai_reasoning
                else:
                    direction_label = "应收" if prepared.posting_type is PostingType.AR else "应付"
                    entity_reasons = [f"人工确认记账方向，归入{direction_label}台账"]
            else:
                entity_reasons = perspective.ai_reasoning
            prepared = prepared.model_copy(
                update={"ai_reasoning": [*entity_reasons, *existing]}
            )
    return _with_credit(prepared.model_copy(update={"validation_alerts": alerts}))


def _with_credit(record: ProcessedBillingRecord) -> ProcessedBillingRecord:
    is_credit = is_credit_document(record.document_type, record.debit_credit)
    amount = record.amount
    reasoning = [
        reason
        for reason in record.ai_reasoning
        if not reason.startswith("贷项冲减：对应台账余额减少 ")
    ]
    if is_credit and amount is not None and record.posting_type is not None:
        amount = amount.copy_abs().copy_negate()
        reasoning.append(f"贷项冲减：对应台账余额减少 {amount.copy_abs():f}")
    raw = record.raw_explicit_amount
    alerts = [
        alert
        for alert in record.validation_alerts
        if alert.code is not AlertCode.NEGATIVE_NON_CREDIT_AMOUNT
        or alert.severity is AlertSeverity.RESOLVED
    ]
    if not is_credit and amount is not None and amount < 0:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
                "非贷项单据含负数金额，待复核",
                field="amount",
                actual=str(amount),
            )
        )
    return record.model_copy(
        update={
            "amount": amount,
            "is_credit": is_credit,
            "raw_amount_abs": raw.copy_abs() if raw is not None else record.raw_amount_abs,
            "ai_reasoning": reasoning,
            "validation_alerts": alerts,
        }
    )


def _find_bill_pool(
    record: ProcessedBillingRecord,
    bills: Sequence[ExistingBillRecord],
    charge_item: str | None,
    profiles: Sequence[AccountingEntityProfile],
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
        if is_valid_iso4217(currency) or (
            currency is None
            and "currency" not in (record.raw_invalid_fields.keys() | record.human_overrides.keys())
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
    issuer, recipient = record.issuer, record.recipient
    date, _ = normalize_document_date(record.document_date)
    if containers and issuer and recipient and date and record.amount is not None:
        amount = Fraction(record.amount)
        pool = tuple(
            bill
            for bill in bills
            if containers.intersection(_container_keys(bill.container_nos))
            and entity_matches(bill.issuer, issuer, profiles)
            and entity_matches(bill.recipient, recipient, profiles)
            and normalize_document_date(bill.document_date)[0] == date
            and abs(amount - Fraction(bill.amount)) <= _AMOUNT_TOLERANCE
        )
        if pool:
            return MatchLevel.L4_CNTR_PARTY_DATE_AMT, pool
    return MatchLevel.NONE, ()


def _pairing_currency(record: ProcessedBillingRecord, currencies: set[str]) -> str | None:
    if record.currency is not None:
        return record.currency if is_valid_iso4217(record.currency) else None
    if "currency" in record.raw_invalid_fields or "currency" in record.human_overrides:
        return None
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
                    f"{bill.reference_no or bill.external_bill_key or bill.id} | "
                    f"{shipment_ref} | {bill.charge_item} | "
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


def _same_bill_value(
    field: str, old: Any, new: Any, profiles: Sequence[AccountingEntityProfile]
) -> bool:
    if field in ("issuer", "recipient"):
        return (
            old == new or (not old and not new) or bool(new and entity_matches(old, new, profiles))
        )
    if field == "document_date":
        return normalize_document_date(old)[0] == normalize_document_date(new)[0]
    return old == new


def _inherit_and_compare(
    record: ProcessedBillingRecord,
    bill: ExistingBillRecord,
    level: MatchLevel,
    profiles: Sequence[AccountingEntityProfile],
) -> ProcessedBillingRecord:
    locked = record.human_overrides.keys() | record.raw_invalid_fields.keys()
    missing_parties = not has_explicit_parties(record) and not (locked & PARTY_FIELDS)
    entity_alert = any(
        alert.code is AlertCode.ENTITY_NOT_MATCHED for alert in record.validation_alerts
    )
    updates: dict[str, Any] = {}
    originals: dict[str, Any] = {}
    for field in _BILL_FIELDS:
        if field in locked or field == "posting_type" and entity_alert and not missing_parties:
            continue
        current, historical = getattr(record, field), getattr(bill, field)
        if (
            historical is not None
            and not _source_supplied(record, field, current)
            and (historical != current or field in ("amount", "quantity", "unit_price"))
        ):
            updates[field] = historical
            originals[field] = current
    refs = _inherit_shipment_refs(record, bill)
    if refs != record.shipment_refs:
        updates["shipment_refs"] = refs
        originals["shipment_refs"] = record.shipment_refs.model_dump()

    merged = _with_credit(record.model_copy(update=updates))
    if entity_alert and missing_parties and merged.posting_type is not None:
        merged = merged.model_copy(
            update={
                "validation_alerts": [
                    alert
                    for alert in merged.validation_alerts
                    if alert.code is not AlertCode.ENTITY_NOT_MATCHED
                ],
                "ai_reasoning": [
                    reason
                    for reason in merged.ai_reasoning
                    if reason not in _ENTITY_REASONING_MESSAGES
                ]
                + ["沿用当前记账主体历史账单的交易双方和应收或应付台账"],
            }
        )

    diffs = []
    for field in _DIFF_FIELDS:
        old = getattr(bill, field)
        new = getattr(merged, field)
        if not _same_bill_value(field, old, new, profiles):
            diffs.append(FieldDiff(field=field, old_value=old, new_value=new, changed=True))
    bill_ref = bill.external_bill_key or bill.id
    match_reasons: list[str] = []
    if level in _MATCH_LEVEL_LABELS:
        match_reasons.append(
            f"按 {level.value}（{_MATCH_LEVEL_LABELS[level]}）匹配历史账单 {bill_ref}"
        )
    else:
        match_reasons.append(f"已人工关联历史账单 {bill_ref}")
    if diffs:
        diff_summary = "；".join(
            f"{diff.field} {_format_diff_value(diff.old_value)}"
            f" -> {_format_diff_value(diff.new_value)}"
            for diff in diffs
        )
        match_reasons.append(f"字段变更：{diff_summary}")
        if originals:
            match_reasons.append("未明确修改字段继承历史账单原值")
    else:
        match_reasons.append("核心字段与历史账单一致，无变更")
    reasoning = [
        reason
        for reason in merged.ai_reasoning
        if not reason.startswith(_MATCH_REASON_PREFIXES)
    ]
    for reason in match_reasons:
        if reason not in reasoning:
            reasoning.append(reason)
    return _with_decision(
        merged,
        OperationType.UPDATE if diffs else OperationType.NO_CHANGE,
        inherited_fields=originals,
        field_diffs=diffs,
        ai_reasoning=reasoning,
        match_level=level,
        matched_existing_bill_id=bill.id,
        matched_bill_ref=bill_ref,
    )


def inherit_existing_bill(
    record: ProcessedBillingRecord,
    bill: ExistingBillRecord,
    *,
    match_level: MatchLevel = MatchLevel.NONE,
    profiles: Sequence[AccountingEntityProfile] = (),
    charge_dictionary: Sequence[ChargeDictionaryItem] | None = None,
) -> ProcessedBillingRecord:
    """Merge a selected historical bill and compute its business-field changes."""
    if not entity_matches(bill.accounting_entity, record.accounting_entity, profiles):
        raise ValueError("历史账单不属于当前记账主体")
    return _inherit_and_compare(
        _prepare_record(record, profiles, charge_dictionary), bill, match_level, profiles
    )


def match_and_inherit_batch(
    records: Sequence[ProcessedBillingRecord],
    existing_bills: Sequence[ExistingBillRecord],
    profiles: Sequence[AccountingEntityProfile] = (),
    charge_dictionary: Sequence[ChargeDictionaryItem] | None = None,
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
    prepared = [_prepare_record(record, profiles, charge_dictionary) for record in records]
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
        level, pool = _find_bill_pool(record, bills, charge_item, profiles)
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
                results[index] = _inherit_and_compare(
                    prepared[index], bill, MatchLevel.NONE, profiles
                )
            else:
                results[index] = _ambiguous(prepared[index], MatchLevel.NONE, (bill,), ambiguity)

    occupied = set(manual_claims)
    for level in _AUTOMATIC_LEVELS:
        proposals: dict[tuple[str, str], list[int]] = defaultdict(list)
        pending = {}
        for (doc_id, _, group_level, _), (pool, indexes) in groups.items():
            if group_level is not level:
                continue
            available = tuple(bill for bill in pool if (doc_id, bill.id) not in occupied)
            pairs = _pair_group(prepared, indexes, available)
            for index in indexes:
                bill = pairs.get(index)
                if bill is not None:
                    proposals[doc_id, bill.id].append(index)
                pending[index] = (doc_id, available, bill)

        for index, (doc_id, available, bill) in pending.items():
            if bill is not None and len(proposals[doc_id, bill.id]) == 1:
                results[index] = _inherit_and_compare(prepared[index], bill, level, profiles)
                occupied.add((doc_id, bill.id))
                continue
            message = ambiguity
            if not available:
                message = "符合条件的历史账单已由同一源单的其他费用行占用"
            results[index] = _ambiguous(prepared[index], level, available, message)
    return results


def match_record(
    record: ProcessedBillingRecord,
    existing_bills: Sequence[ExistingBillRecord],
    profiles: Sequence[AccountingEntityProfile] = (),
    charge_dictionary: Sequence[ChargeDictionaryItem] | None = None,
) -> ProcessedBillingRecord:
    """Match a single line using the same rules as the batch matcher."""
    return match_and_inherit_batch([record], existing_bills, profiles, charge_dictionary)[0]
