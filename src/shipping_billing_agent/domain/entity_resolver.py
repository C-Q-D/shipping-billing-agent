"""Determine the ledger perspective from the document's stated parties."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from typing import Any
from unicodedata import normalize

from .charge_normalizer import (
    normalize_charge_item,
    normalize_document_date,
    normalize_payment_term,
)
from .currency import normalize_currency
from .models import (
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    ExistingBillRecord,
    OperationType,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    ShipmentRefs,
    SourceRefValue,
    ValidationAlert,
    parse_decimal_strict,
)

_BRANCH_SUFFIX = re.compile(r"(?:\s*\([^()]*\))+[\W_]*$")
_PARTY_WORDS = re.compile(r"[^\W_]+")
CREDIT_DOCUMENT_PATTERN = re.compile(
    r"(?:\bcredit\s+(?:note|memo|invoice|advice|voucher|adjustment)\b|\bc\s*/\s*n\b|\bcn\b|"
    r"贷项(?:通知|凭证)?|貸項(?:通知|憑證)?|红字|紅字)",
    re.IGNORECASE,
)
PARTY_FIELDS = {"issuer", "recipient", "payer_hint", "payee_hint"}
_NUMERIC_FIELDS = {"quantity", "unit_price", "amount"}
_TEXT_OVERRIDES = {"document_type", "reference_no", "raw_charge_name", "charge_item", *PARTY_FIELDS}
_OVERRIDE_CODES = {
    "currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
    "document_date": AlertCode.INVALID_DOCUMENT_DATE,
    **dict.fromkeys(_NUMERIC_FIELDS, AlertCode.OCR_NUMERIC_CONFUSION),
}
FIELD_FORMAT_ALERT_CODES = {*_OVERRIDE_CODES.values(), AlertCode.LLM_EXTRACTION_FAILED}
PRINTED_CURRENCY_CONFLICT_MESSAGE = "费用明细行标明了不同的币种"
TRANSPORT_FIELDS = {
    "mbl_no": "mbl_nos",
    "hbl_no": "hbl_no",
    "booking_no": "booking_no",
    "container_nos": "container_nos",
}
_SCALAR_OVERRIDES = (
    _TEXT_OVERRIDES | _OVERRIDE_CODES.keys() | {"payment_term", "posting_type", "debit_credit"}
)
_REQUIRED_OVERRIDES = {"amount", "currency", "charge_item", "posting_type"}
_ENTITY_OVERRIDES = {"posting_type", "matched_existing_bill_id", "force_create_new"}
_REVIEW_FIELDS = (
    "charge_item",
    "raw_charge_name",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "payment_term",
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "payer_hint",
    "payee_hint",
    "posting_type",
    "debit_credit",
    "is_credit",
    "matched_existing_bill_id",
    "matched_bill_ref",
    "match_level",
)
_HUMAN_DECISIONS = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)
_PartyKey = tuple[str, tuple[str, ...]]


def _party_words(name: str) -> str:
    return " ".join(_PARTY_WORDS.findall(name.replace(".", "")))


def _party_key(name: str | None) -> _PartyKey:
    text = normalize("NFKC", name or "").casefold()
    if branch := _BRANCH_SUFFIX.search(text):
        suffixes = re.findall(r"\(([^()]*)\)", branch[0])
        return _party_words(text[: branch.start()]), tuple(map(_party_words, suffixes))
    return _party_words(text), ()


def normalize_party_name(name: str | None) -> str:
    stem, branches = _party_key(name)
    return " ".join((stem, *(f"({branch})" for branch in branches))).strip()


@dataclass(frozen=True, slots=True)
class _PartyMatch:
    profile_index: int | None = None
    matched_name: bool = False
    ambiguous: bool = False


def _name_with_punctuation(name: str) -> str:
    return " ".join(normalize("NFKC", name).casefold().split())


def _resolve_party_key(name: str, profiles: Sequence[AccountingEntityProfile]) -> _PartyMatch:
    name = name.strip()
    if not name or not profiles:
        return _PartyMatch()

    names = [
        tuple(text.strip() for text in (profile.canonical_name, *profile.aliases))
        for profile in profiles
    ]
    matches = [index for index, entries in enumerate(names) if name == entries[0]]
    if not matches:
        matches = [index for index, entries in enumerate(names) if name in entries[1:]]
    if not matches:
        normalized = _name_with_punctuation(name)
        matches = [
            index
            for index, entries in enumerate(names)
            if any(_name_with_punctuation(entry) == normalized for entry in entries)
        ]
    if matches:
        if len(matches) == 1:
            return _PartyMatch(matches[0], matched_name=True)
        return _PartyMatch(ambiguous=True)

    stem, branches = _party_key(name)
    if not stem:
        return _PartyMatch()
    by_stem = {}
    for index, entries in enumerate(names):
        suffixes = {suffix for entry_stem, suffix in map(_party_key, entries) if entry_stem == stem}
        if suffixes:
            by_stem[index] = suffixes
    matches = [index for index, suffixes in by_stem.items() if branches in suffixes]
    if matches:
        if len(matches) == 1:
            return _PartyMatch(matches[0], matched_name=True)
        return _PartyMatch(ambiguous=True)
    if len(by_stem) == 1:
        index, suffixes = next(iter(by_stem.items()))
        if not branches or suffixes == {()}:
            return _PartyMatch(index)
    return _PartyMatch(ambiguous=not branches and len(by_stem) > 1)


def resolve_party_profile(
    name: str, profiles: Sequence[AccountingEntityProfile]
) -> AccountingEntityProfile | None:
    index = _resolve_party_key(name, profiles).profile_index
    return profiles[index] if index is not None else None


def entity_matches(
    party_name: str | None,
    target_entity: str,
    profiles: Sequence[AccountingEntityProfile] = (),
    *,
    allow_unconfigured_fallback: bool = True,
) -> bool:
    if not party_name or not party_name.strip() or not target_entity.strip():
        return False
    party_match = _resolve_party_key(party_name, profiles)
    target_match = _resolve_party_key(target_entity, profiles)
    if party_match.ambiguous or target_match.ambiguous:
        return False
    if party_match.profile_index != target_match.profile_index:
        return False
    if party_match.matched_name and target_match.matched_name:
        return True
    if party_match.profile_index is None and not allow_unconfigured_fallback:
        return party_name.strip() == target_entity.strip()

    party_stem, party_branch = _party_key(party_name)
    target_stem, target_branch = _party_key(target_entity)
    if party_match.profile_index is None:
        return bool(party_stem and party_stem == target_stem) and (
            party_branch == target_branch or not (party_branch and target_branch)
        )
    return not (
        party_stem == target_stem
        and party_branch
        and target_branch
        and party_branch != target_branch
    )


def is_credit_document_type(text: str | None) -> bool:
    normalized_type = normalize("NFKC", text or "").replace("_", " ")
    return bool(CREDIT_DOCUMENT_PATTERN.search(normalized_type))


def is_credit_document(document_type: str | None, debit_credit: str) -> bool:
    return debit_credit == "CREDIT" or is_credit_document_type(document_type)


@dataclass(slots=True)
class EntityPerspective:
    posting_type: PostingType | None
    is_credit: bool
    raw_amount_abs: Decimal | None
    amount: Decimal | None
    validation_alerts: list[ValidationAlert]
    ai_reasoning: list[str]

    @property
    def operation(self) -> OperationType | None:
        return OperationType.PENDING_REVIEW if self.validation_alerts else None


def resolve_entity_perspective(
    target_entity: str,
    *,
    issuer: str | None = None,
    recipient: str | None = None,
    payer_hint: str | None = None,
    payee_hint: str | None = None,
    document_type: str | None = None,
    debit_credit: str = "UNKNOWN",
    raw_amount: Decimal | None = None,
    profiles: Sequence[AccountingEntityProfile] = (),
) -> EntityPerspective:
    creditor = (payee_hint or "").strip()
    debtor = (payer_hint or "").strip()
    if not creditor and not debtor:
        creditor, debtor = issuer, recipient
    matches_creditor = entity_matches(creditor, target_entity, profiles)
    matches_debtor = entity_matches(debtor, target_entity, profiles)

    alerts = []
    reasoning = []
    posting_type = None
    if matches_creditor and not matches_debtor:
        posting_type = PostingType.AR
        reasoning.append("记账主体匹配收款方，归入应收台账")
    elif matches_debtor and not matches_creditor:
        posting_type = PostingType.AP
        reasoning.append("记账主体匹配付款方，归入应付台账")
    else:
        reason = "当前记账主体无法唯一匹配单据的收款方或付款方"
        alerts.append(ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, reason))
        reasoning.append(reason)

    is_credit = is_credit_document(document_type, debit_credit)
    raw_amount_abs = raw_amount.copy_abs() if raw_amount is not None else None
    amount = raw_amount
    if is_credit and posting_type is not None and raw_amount_abs is not None:
        amount = raw_amount_abs.copy_negate()
        reasoning.append(f"贷项冲减：对应台账余额减少 {raw_amount_abs:f}")
    if not is_credit and raw_amount is not None and raw_amount.is_finite() and raw_amount < 0:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
                "非贷项单据含负数金额，待复核",
                field="amount",
                actual=format(raw_amount, "f"),
            )
        )

    return EntityPerspective(posting_type, is_credit, raw_amount_abs, amount, alerts, reasoning)


def _override_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("需要非空文本")
    return value.strip()


def _scalar_override(field: str, value: Any) -> Any:
    if value is None and field not in _REQUIRED_OVERRIDES and field != "debit_credit":
        return None
    if field in _NUMERIC_FIELDS:
        return parse_decimal_strict(value)
    if field == "document_date":
        if isinstance(value, datetime):
            value = value.date()
        day, error = normalize_document_date(
            value.isoformat() if isinstance(value, date) else _override_text(value)
        )
        if error:
            raise ValueError(error)
        return day
    text = _override_text(value)
    if field == "currency":
        currency, reason = normalize_currency(text)
        if currency is None:
            raise ValueError(reason or "需要有效币种")
        return currency
    if field == "payment_term":
        term = normalize_payment_term(text)
        if term is None:
            raise ValueError("需要有效付款方式")
        return term
    if field == "posting_type":
        return PostingType(text.upper())
    if field == "debit_credit" and text not in {"DEBIT", "CREDIT", "UNKNOWN"}:
        raise ValueError("需要有效借贷方向")
    return text


def _transport_override(field: str, value: Any) -> Any:
    if field == "shipment_refs":
        if isinstance(value, dict) and value.keys() - ShipmentRefs.model_fields.keys():
            raise ValueError("运输引用含未知字段")
        refs = ShipmentRefs.model_validate({} if value is None else value)
        for token in (refs.hbl_no, refs.booking_no, *(ref.value for ref in refs.mbl_nos)):
            if token is not None:
                _override_text(token)
        return refs
    if field == "container_nos":
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("需要箱号列表")
        return [_override_text(item) for item in value]
    token = None if value is None else _override_text(value)
    if field == "mbl_no":
        return [SourceRefValue(value=token, source="人工修订")] if token else []
    return token


def apply_human_overrides(
    record: ProcessedBillingRecord, charge_dictionary: Sequence[ChargeDictionaryItem] | None = None
) -> tuple[ProcessedBillingRecord, set[str], list[ValidationAlert]]:
    updates: dict[str, Any] = {}
    corrected = set()
    issues = []
    original_values = record.source_values_before_overrides.copy()
    refs = record.shipment_refs
    overrides = record.human_overrides
    for field, value in overrides.items():
        if field not in _SCALAR_OVERRIDES:
            continue
        try:
            updates[field] = _scalar_override(field, value)
            corrected.add(field)
        except ValueError:
            updates[field] = None if field != "debit_credit" else "UNKNOWN"
            issues.append(_invalid_override(field, value))

    for field in ("shipment_refs", *TRANSPORT_FIELDS):
        if field not in overrides:
            continue
        attr = TRANSPORT_FIELDS.get(field, field)
        try:
            effective = _transport_override(field, overrides[field])
            corrected.add(field)
            if field == "shipment_refs":
                corrected.update(
                    field
                    for field, attr in TRANSPORT_FIELDS.items()
                    if overrides["shipment_refs"] is None or attr in effective.model_fields_set
                )
        except ValueError:
            corrected.discard(field)
            effective = (
                getattr(ShipmentRefs(), attr) if field != "shipment_refs" else ShipmentRefs()
            )
            issues.append(_invalid_override(field, overrides[field]))
        refs = effective if field == "shipment_refs" else refs.model_copy(update={attr: effective})
    if refs != record.shipment_refs:
        updates["shipment_refs"] = refs

    raw_name = updates.get("raw_charge_name", record.raw_charge_name)
    edited_raw_name = "raw_charge_name" in overrides
    normalize_charge = (
        charge_dictionary is not None
        and raw_name is not None
        or edited_raw_name
        and (raw_name != record.raw_charge_name or "raw_charge_name" not in original_values)
    )
    if "charge_item" not in overrides and normalize_charge:
        updates["charge_item"], updates["matched_from_dictionary"] = normalize_charge_item(
            raw_name, charge_dictionary or ()
        )
    if (
        "charge_item" not in overrides
        and "raw_charge_name" in corrected
        and updates.get("charge_item", record.charge_item)
    ):
        corrected.add("charge_item")
    if "charge_item" in overrides:
        updates["matched_from_dictionary"] = False

    # Store what was present before the first edit so removing an override can restore it.
    edited_fields = set(overrides)
    if edited_fields.intersection(TRANSPORT_FIELDS):
        edited_fields.add("shipment_refs")
    if edited_raw_name or "charge_item" in overrides:
        edited_fields.update(("charge_item", "matched_from_dictionary"))
    for field in updates.keys() & edited_fields:
        original = record.inherited_fields.get(field, getattr(record, field))
        original_values.setdefault(field, original)
    if updates:
        record = record.model_copy(
            update={**updates, "source_values_before_overrides": original_values}
        )
    return record, corrected, issues


def _invalid_override(field: str, value: Any) -> ValidationAlert:
    return ValidationAlert.from_code(
        _OVERRIDE_CODES.get(field, AlertCode.LLM_EXTRACTION_FAILED),
        "人工修订值无效，待复核",
        field=field,
        actual=str(value),
    )


def _is_currency_conflict(issue: ValidationAlert) -> bool:
    return issue.code is AlertCode.CROSS_DOCUMENT_CONFLICT and issue.field == "currency"


def source_format_alerts(
    record: ProcessedBillingRecord, corrected: set[str]
) -> list[ValidationAlert]:
    invalid = record.raw_invalid_fields
    alerts = [
        issue
        for issue in record.raw_parse_issues
        if not (
            (_is_currency_conflict(issue) and "currency" in corrected)
            or (
                issue.code is AlertCode.CROSS_DOCUMENT_CONFLICT
                and issue.field == "payment_term"
                and "payment_term" in corrected
                and record.payment_term is not None
            )
        )
        and (
            issue.code not in FIELD_FORMAT_ALERT_CODES
            or (
                issue.field not in corrected
                and (issue.field not in invalid or issue.actual == invalid[issue.field])
            )
        )
    ]
    blocked = {alert.field for alert in alerts}
    for field, original in invalid.items():
        if field in corrected or field in blocked:
            continue
        alerts.append(
            ValidationAlert.from_code(
                _OVERRIDE_CODES.get(field, AlertCode.LLM_EXTRACTION_FAILED),
                "源单字段值无效，待复核",
                field=field,
                actual=original,
            )
        )
    return alerts


def has_explicit_parties(record: ProcessedBillingRecord) -> bool:
    return bool(record.explicit_fields & PARTY_FIELDS) or (
        not record.explicit_fields and any(getattr(record, field) for field in PARTY_FIELDS)
    )


def _match_ledger_records(
    records: list[ProcessedBillingRecord],
    match: Callable[[list[ProcessedBillingRecord]], list[ProcessedBillingRecord]],
) -> list[ProcessedBillingRecord]:
    """Keep explicitly different branches out of the historical bill matcher."""
    eligible = {}
    for index, record in enumerate(records):
        if record.posting_type is not None:
            eligible[index] = record
            continue
        target_stem, target_branch = _party_key(record.accounting_entity)
        parties = (record.payer_hint, record.payee_hint)
        if not any(party and party.strip() for party in parties):
            parties = (record.issuer, record.recipient)
        if target_stem and any(
            stem == target_stem and branch != target_branch
            for stem, branch in map(_party_key, parties)
        ):
            continue
        eligible[index] = record
    if len(eligible) == len(records):
        return match(records)
    matched = dict(zip(eligible, match(list(eligible.values())) if eligible else (), strict=True))
    return [matched.get(index, record) for index, record in enumerate(records)]


@dataclass(frozen=True, slots=True)
class _ReviewBasis:
    billing_fields: tuple[Any, ...]
    shipment_refs: tuple[str, str, tuple[str, ...], tuple[str, ...]]
    operation: OperationType
    field_diffs: tuple[tuple[Any, ...], ...]


def _diff_value(field: str, value: Any) -> tuple[int, Decimal | str]:
    if field in _NUMERIC_FIELDS and value is not None:
        try:
            return 0, parse_decimal_strict(value)
        except ValueError:
            pass  # Keep a legacy non-numeric value comparable as stored.
    return 1, json.dumps(value, sort_keys=True, ensure_ascii=False)


def _review_basis(record: ProcessedBillingRecord) -> _ReviewBasis:
    def ref_key(value: str | None) -> str:
        return "".join(normalize("NFKC", value or "").upper().split())

    unresolved = frozenset(
        (alert.code, alert.severity, alert.field, alert.actual, alert.expected)
        for alert in record.validation_alerts
        if alert.severity is not AlertSeverity.RESOLVED
    )
    refs = record.shipment_refs
    shipment_refs = (
        ref_key(refs.hbl_no),
        ref_key(refs.booking_no),
        tuple(sorted({key for ref in refs.mbl_nos if (key := ref_key(ref.value))})),
        tuple(sorted({key for value in refs.container_nos if (key := ref_key(value))})),
    )
    diffs = []
    for diff in record.field_diffs:
        values = diff.model_dump(mode="json")
        diffs.append(
            (
                diff.field,
                _diff_value(diff.field, values["old_value"]),
                _diff_value(diff.field, values["new_value"]),
                diff.changed,
            )
        )
    return _ReviewBasis(
        billing_fields=(*(getattr(record, field) for field in _REVIEW_FIELDS), unresolved),
        shipment_refs=shipment_refs,
        operation=record.operation,
        field_diffs=tuple(sorted(diffs)),
    )


def recalculate_for_entity(
    records: Sequence[ProcessedBillingRecord],
    new_entity: str,
    profiles: Sequence[AccountingEntityProfile] = (),
    existing_bills: Sequence[ExistingBillRecord] = (),
    matcher_fn: Callable[
        [list[ProcessedBillingRecord], list[ExistingBillRecord]], list[ProcessedBillingRecord]
    ]
    | None = None,
    validator_fn: Callable[[list[ProcessedBillingRecord]], list[ProcessedBillingRecord]]
    | None = None,
    *,
    charge_dictionary: Sequence[ChargeDictionaryItem] | None = None,
    bundles: Sequence[DocumentExtractionBundle] = (),
) -> tuple[list[ProcessedBillingRecord], int]:
    """Restore source values, then call the batch matcher and the batch validator in order."""
    from .matcher import _prepare_record, match_and_inherit_batch
    from .validators import validate_batch

    previous_by_id = {record.id: record for record in records}
    updated = []
    for record in records:
        same_ledger = entity_matches(record.accounting_entity, new_entity, profiles)
        overrides = record.human_overrides
        if not same_ledger:
            overrides = {
                field: value for field, value in overrides.items() if field not in _ENTITY_OVERRIDES
            }
        source = record.model_copy(
            update={
                "accounting_entity": new_entity,
                "operation": OperationType.PENDING_REVIEW,
                "review_status": ReviewStatus.NEEDS_REVIEW,
                "requires_reapproval": (
                    same_ledger
                    and record.requires_reapproval
                    and record.review_status not in _HUMAN_DECISIONS
                ),
                "human_overrides": overrides,
                "ai_reasoning": [],
                "reject_reason": None,
            }
        )
        updated.append(_prepare_record(source, profiles, charge_dictionary, recompute_entity=True))
    if matcher_fn is None or matcher_fn is match_and_inherit_batch:
        match = partial(
            match_and_inherit_batch,
            existing_bills=existing_bills,
            profiles=profiles,
            charge_dictionary=charge_dictionary,
        )
    else:
        scoped_bills = [
            bill
            for bill in existing_bills
            if entity_matches(bill.accounting_entity, new_entity, profiles)
        ]

        def match(rows: list[ProcessedBillingRecord]) -> list[ProcessedBillingRecord]:
            return matcher_fn(rows, scoped_bills)

    updated = _match_ledger_records(updated, match)
    updated = validate_batch(updated, bundles) if validator_fn is None else validator_fn(updated)
    reset_approval_count = 0
    for index, record in enumerate(updated):
        previous = previous_by_id.get(record.id)
        if previous is None:
            continue
        same_ledger = entity_matches(previous.accounting_entity, record.accounting_entity, profiles)
        if previous.review_status not in _HUMAN_DECISIONS:
            if same_ledger and previous.requires_reapproval:
                updated[index] = record.model_copy(
                    update={"review_status": ReviewStatus.NEEDS_REVIEW, "requires_reapproval": True}
                )
            continue
        before, after = _review_basis(previous), _review_basis(record)
        if same_ledger and before == after:
            updated[index] = record.model_copy(
                update={
                    "review_status": previous.review_status,
                    "requires_reapproval": False,
                    "reject_reason": previous.reject_reason,
                    "human_review_history": previous.human_review_history,
                }
            )
        else:
            reset_approval_count += 1
            same_bill = previous.matched_existing_bill_id == record.matched_existing_bill_id
            changed_action = (before.operation, before.field_diffs) != (
                after.operation,
                after.field_diffs,
            )
            if same_ledger and (
                previous.review_status is ReviewStatus.APPROVED
                or before.shipment_refs != after.shipment_refs
                or same_bill
                and changed_action
            ):
                updated[index] = record.model_copy(
                    update={
                        "review_status": ReviewStatus.NEEDS_REVIEW,
                        "requires_reapproval": previous.review_status is ReviewStatus.APPROVED,
                    }
                )
    return updated, reset_approval_count
