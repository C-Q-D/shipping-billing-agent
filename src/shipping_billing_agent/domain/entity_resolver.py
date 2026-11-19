"""Determine the ledger perspective from the document's stated parties."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from unicodedata import normalize

from .models import (
    AccountingEntityProfile,
    AlertCode,
    ExistingBillRecord,
    MatchLevel,
    OperationType,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    ValidationAlert,
)

_BRANCH_SUFFIX = re.compile(r"(?:\s*\([^()]*\))+[\W_]*$")
_PARTY_WORDS = re.compile(r"[^\W_]+")
_CREDIT_DOCUMENT = re.compile(r"\bcredit\b|\bc\s*/\s*n\b|贷项|貸項", re.IGNORECASE)
_SOURCE_FIELDS = {
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "payer_hint",
    "payee_hint",
    "charge_item",
    "raw_charge_name",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "payment_term",
    "debit_credit",
    "shipment_refs",
}
_TRANSPORT_OVERRIDES = {"mbl_no", "hbl_no", "booking_no", "container_nos"}


def normalize_party_name(name: str | None) -> str:
    normalized = normalize("NFKC", name or "").casefold()
    without_branch = _BRANCH_SUFFIX.sub("", normalized)
    return " ".join(_PARTY_WORDS.findall(without_branch.replace(".", "")))


def _canonical_party(name: str, profiles: Sequence[AccountingEntityProfile]) -> str | None:
    if not name:
        return None
    matching_aliases = set()
    for profile in profiles:
        canonical = normalize_party_name(profile.canonical_name)
        if name == canonical:
            return name
        if canonical and any(name == normalize_party_name(alias) for alias in profile.aliases):
            matching_aliases.add(canonical)
    if len(matching_aliases) > 1:
        return None
    return next(iter(matching_aliases), name)


def entity_matches(
    party_name: str | None,
    target_entity: str,
    profiles: Sequence[AccountingEntityProfile] = (),
) -> bool:
    party = _canonical_party(normalize_party_name(party_name), profiles)
    target = _canonical_party(normalize_party_name(target_entity), profiles)
    return party is not None and party == target


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

    normalized_type = normalize("NFKC", document_type or "").replace("_", " ")
    is_credit = debit_credit == "CREDIT" or bool(_CREDIT_DOCUMENT.search(normalized_type))
    raw_amount_abs = raw_amount.copy_abs() if raw_amount is not None else None
    amount = raw_amount
    if is_credit and posting_type is not None and raw_amount_abs is not None:
        amount = raw_amount_abs.copy_negate()
        reasoning.append(f"贷项冲减：对应台账余额减少 {raw_amount_abs:f}")
    if not is_credit and raw_amount is not None and raw_amount < 0:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
                "非贷项单据含负数金额，待复核",
                field="amount",
                actual=format(raw_amount, "f"),
            )
        )

    return EntityPerspective(posting_type, is_credit, raw_amount_abs, amount, alerts, reasoning)


def _restore_source_record(
    record: ProcessedBillingRecord, new_entity: str, profiles: Sequence[AccountingEntityProfile]
) -> ProcessedBillingRecord:
    values = record.model_dump()
    values.update(
        {key: value for key, value in record.inherited_fields.items() if key in _SOURCE_FIELDS}
    )
    if record.raw_explicit_amount is not None:
        values["amount"] = record.raw_explicit_amount
    for field in record.raw_invalid_fields.keys() & _SOURCE_FIELDS:
        values.pop(field, None)

    overrides = {
        key: value
        for key, value in record.human_overrides.items()
        if key in _SOURCE_FIELDS or key in _TRANSPORT_OVERRIDES
    }
    values.update({key: value for key, value in overrides.items() if key in _SOURCE_FIELDS})
    if values.get("shipment_refs") is None:
        values.pop("shipment_refs", None)
    if _TRANSPORT_OVERRIDES.intersection(overrides):
        refs = dict(values.get("shipment_refs", {}))
        for field in ("hbl_no", "booking_no", "container_nos"):
            if field in overrides:
                refs[field] = overrides[field]
        if refs.get("container_nos") is None:
            refs["container_nos"] = []
        if "mbl_no" in overrides:
            mbl_no = overrides["mbl_no"]
            refs["mbl_nos"] = [{"value": mbl_no, "source": "人工修订"}] if mbl_no else []
        values["shipment_refs"] = refs

    values.update(
        accounting_entity=new_entity,
        operation=OperationType.PENDING_REVIEW,
        review_status=ReviewStatus.NEEDS_REVIEW,
        match_level=MatchLevel.NONE,
        posting_type=None,
        matched_existing_bill_id=None,
        matched_bill_ref=None,
        candidate_bills=[],
        field_diffs=[],
        validation_alerts=[],
        ai_reasoning=[],
        inherited_fields={},
        human_overrides=overrides,
        reject_reason=None,
    )
    source = ProcessedBillingRecord.model_validate(values)
    perspective = resolve_entity_perspective(
        new_entity,
        issuer=source.issuer,
        recipient=source.recipient,
        payer_hint=source.payer_hint,
        payee_hint=source.payee_hint,
        document_type=source.document_type,
        debit_credit=source.debit_credit,
        raw_amount=source.amount,
        profiles=profiles,
    )
    return source.model_copy(
        update={
            "posting_type": perspective.posting_type,
            "amount": perspective.amount,
            "is_credit": perspective.is_credit,
            "raw_amount_abs": perspective.raw_amount_abs,
            "validation_alerts": [*source.raw_parse_issues, *perspective.validation_alerts],
            "ai_reasoning": perspective.ai_reasoning,
        }
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
) -> tuple[list[ProcessedBillingRecord], int]:
    """Restore source values, then call the batch matcher and the batch validator in order."""
    if all(entity_matches(record.accounting_entity, new_entity, profiles) for record in records):
        return list(records), 0

    reset_approval_count = sum(
        record.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED) for record in records
    )
    updated = [_restore_source_record(record, new_entity, profiles) for record in records]
    if matcher_fn is not None:
        scoped_bills = [
            bill
            for bill in existing_bills
            if entity_matches(bill.accounting_entity, new_entity, profiles)
        ]
        updated = matcher_fn(updated, scoped_bills)
    if validator_fn is not None:
        updated = validator_fn(updated)
    return updated, reset_approval_count
