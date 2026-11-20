"""Apply review decisions and repair individual sources within a persisted batch."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, ValidationError

from ..domain.charge_normalizer import normalize_charge_item, normalize_payment_term
from ..domain.currency import is_valid_iso4217, normalize_currency
from ..domain.entity_resolver import (
    PARTY_FIELDS,
    TRANSPORT_FIELDS,
    _name_with_punctuation,
    entity_matches,
    recalculate_for_entity,
    resolve_party_profile,
)
from ..domain.matcher import match_and_inherit_batch
from ..domain.models import (
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    BatchStatus,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    DocumentStatus,
    ExtractionChannel,
    FailedDocumentAlert,
    FieldDiff,
    HumanReviewAction,
    OcrMode,
    OperationType,
    ParsedDocument,
    ProcessedBillingRecord,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ReviewStatus,
    ShipmentRefs,
    SourceEvidence,
    SourceKind,
    ValidationAlert,
    parse_decimal_strict,
)
from ..domain.validators import deduplicate_same_source_documents, validate_batch
from ..parsers.dispatcher import compute_retry_context
from ..storage import BillingRepository, OptimisticLockError, ResourceNotFoundError
from ..storage.db import utc_timestamp
from .batch_service import (
    BatchService,
    ServiceValidationError,
    Upload,
    _batch_detail,
    _batch_outcome,
    _batch_scope_changes,
    _BatchScope,
    _document_bundle,
    _entity_name,
    _load_batch_scope,
    _ocr_mode,
    _require_batch,
    _root_documents,
)

_LOGGER = logging.getLogger(__name__)
_DECISIONS = {ReviewStatus.APPROVED, ReviewStatus.REJECTED}
_ENTITY_BLOCK_CODES = {AlertCode.ENTITY_NOT_MATCHED, "ENTITY_AMBIGUOUS"}
_TOTAL_MAPS = {
    "subtotal_amount": "subtotals_by_currency",
    "tax_amount": "taxes_by_currency",
    "total_amount": "totals_by_currency",
}
_TOTAL_FIELDS = {*_TOTAL_MAPS, "total_currency"}
_TOTAL_REVIEW = "document_totals_review"
_MANUAL_ADD = "ADD_MANUAL_RECORD"


def _json_amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("金额和数量须为 JSON 数字")
    return parse_decimal_strict(value)


def _require_currency_code(value: str) -> None:
    if not is_valid_iso4217(value):
        raise ServiceValidationError("INVALID_CURRENCY", "币种须为有效的大写 ISO 4217 代码")


JsonAmount = Annotated[Decimal, BeforeValidator(_json_amount)]


class _ShipmentRefsPatch(ShipmentRefs):
    model_config = ConfigDict(extra="forbid")


class _ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return cls.model_validate(values).model_dump(exclude_unset=True)
        except ValidationError as exc:
            fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
            raise ServiceValidationError(
                "INVALID_REVIEW_FIELDS",
                "复核请求含有未知字段或字段格式错误",
                details={"fields": fields},
            ) from exc


class _RecordPatch(_ReviewRequest):
    issuer: str | None = None
    recipient: str | None = None
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    charge_item: str | None = None
    quantity: JsonAmount | None = None
    unit_price: JsonAmount | None = None
    amount: JsonAmount | None = None
    currency: str | None = None
    payment_term: Literal["PP", "CC"] | None = None
    posting_type: Literal["AR", "AP"] | None = None
    mbl_no: str | None = None
    hbl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] | None = None
    shipment_refs: _ShipmentRefsPatch | None = None
    matched_existing_bill_id: str | None = None
    force_create_new: StrictBool = False
    review_note: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    expected_updated_at: str | None = None


class _ManualRecord(_ReviewRequest):
    source_doc_id: str
    charge_item: str = Field(min_length=1)
    raw_charge_name: str | None = None
    amount: JsonAmount
    currency: str
    payment_term: Literal["PP", "CC"] | None = None
    posting_type: Literal["AR", "AP"] | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    reason: str = "人工补录漏提费用行"


class _DocumentTotalsPatch(_ReviewRequest):
    subtotal_amount: JsonAmount | None = None
    tax_amount: JsonAmount | None = None
    total_amount: JsonAmount | None = None
    total_currency: str | None = None
    review_note: str | None = None


_REVIEW_BASIS_FIELDS = (
    "issuer",
    "recipient",
    "document_type",
    "reference_no",
    "document_date",
    "charge_item",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "payment_term",
    "posting_type",
    "shipment_refs",
    "matched_existing_bill_id",
    "matched_bill_ref",
    "field_diffs",
    "is_credit",
)
_EXTRACTED_REVIEW_FIELDS = (set(_REVIEW_BASIS_FIELDS) - {
    "matched_existing_bill_id",
    "matched_bill_ref",
    "field_diffs",
}) | {"debit_credit", "raw_explicit_amount", "raw_invalid_fields"}


def _extracted_review_basis(
    record: ProcessedBillingRecord,
    *,
    include_totals: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source = record.model_dump(include=_EXTRACTED_REVIEW_FIELDS)
    original = record.inherited_fields | record.source_values_before_overrides
    source.update({field: original[field] for field in source.keys() & original.keys()})
    totals = record.source_doc_totals if include_totals else None
    return source, totals.model_dump(exclude={"source_doc_id"}) if totals is not None else None


def _is_reextraction_warning(alert: ValidationAlert) -> bool:
    return alert.code is AlertCode.OCR_SUSPICIOUS_FLAG and alert.field == "extraction_snapshot"


def _require_reextraction_review(
    record: ProcessedBillingRecord, reason: str
) -> ProcessedBillingRecord:
    warning = ValidationAlert.from_code(
        AlertCode.OCR_SUSPICIOUS_FLAG, reason, field="extraction_snapshot"
    )
    return record.model_copy(
        update={
            "operation": OperationType.PENDING_REVIEW,
            "review_status": ReviewStatus.NEEDS_REVIEW,
            "reject_reason": None,
            "validation_alerts": [
                alert for alert in record.validation_alerts if not _is_reextraction_warning(alert)
            ] + [warning],
            # Revalidation reads the warning from source evidence until it is reviewed.
            "raw_parse_issues": [
                issue for issue in record.raw_parse_issues if not _is_reextraction_warning(issue)
            ] + [warning],
        }
    )


def _require_editable(batch: Mapping[str, Any], expected_version: int | None) -> None:
    if expected_version is not None and batch["version"] != expected_version:
        raise OptimisticLockError("BATCH_CONFLICT", batch["id"], expected_version, batch["version"])
    if BatchStatus(batch["status"]) is BatchStatus.PROCESSING:
        raise ServiceValidationError(
            "BATCH_PROCESSING", "批次仍在处理，请完成后再复核", status_code=409
        )


def _require_record(
    batch: Mapping[str, Any], record_id: str, expected_updated_at: str | None = None
) -> ProcessedBillingRecord:
    for record in batch["records"]:
        if record.id != record_id:
            continue
        if expected_updated_at is not None and record.updated_at != expected_updated_at:
            raise OptimisticLockError(
                "RECORD_CONFLICT", record_id, expected_updated_at, record.updated_at
            )
        return record
    raise ResourceNotFoundError("当前批次中没有该账单记录")


def _require_document(batch: Mapping[str, Any], doc_id: str) -> dict[str, Any]:
    for document in batch["documents"]:
        if document["id"] == doc_id:
            return document
    raise ResourceNotFoundError("当前批次中没有该来源单据")


def _reason(value: str | None, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 500:
        raise ServiceValidationError(
            "INVALID_REVIEW_REASON", "复核理由须为 2–500 字符", status_code=422
        )
    return value.strip()


def _warning_key(alert: ValidationAlert) -> tuple[AlertCode, str | None, str | None]:
    return alert.code, alert.field, alert.actual


def _resolve_warnings(
    alerts: Sequence[ValidationAlert], keys: set[tuple[AlertCode, str | None, str | None]]
) -> list[ValidationAlert]:
    return [
        alert.model_copy(update={"severity": AlertSeverity.RESOLVED})
        if alert.severity is AlertSeverity.WARNING and _warning_key(alert) in keys
        else alert
        for alert in alerts
    ]


def _ready_operation(record: ProcessedBillingRecord) -> OperationType:
    if record.operation is not OperationType.PENDING_REVIEW:
        return record.operation
    if record.matched_existing_bill_id:
        return OperationType.UPDATE if record.field_diffs else OperationType.NO_CHANGE
    return OperationType.CREATE


def _for_revalidation(record: ProcessedBillingRecord) -> ProcessedBillingRecord:
    return record.model_copy(
        update={
            "review_status": ReviewStatus.NEEDS_REVIEW,
            "reject_reason": None,
            "validation_alerts": [
                alert.model_copy(update={"severity": AlertSeverity.WARNING})
                if alert.severity is AlertSeverity.RESOLVED
                else alert
                for alert in record.validation_alerts
            ],
        }
    )


def _entity_blockers(record: ProcessedBillingRecord) -> list[ValidationAlert]:
    if record.posting_type is not None:
        return []
    return [
        alert
        for alert in record.validation_alerts
        if alert.code in _ENTITY_BLOCK_CODES and alert.severity is not AlertSeverity.RESOLVED
    ]


def _source_party_names(record: ProcessedBillingRecord) -> dict[str, str]:
    names = {}
    for field in PARTY_FIELDS:
        text = record.source_values_before_overrides.get(field, getattr(record, field))
        names[field] = _name_with_punctuation(text) if isinstance(text, str) else ""
    return names


def _retain_entity_block(
    record: ProcessedBillingRecord, old: ProcessedBillingRecord, scope: _BatchScope
) -> ProcessedBillingRecord:
    if (
        old.accounting_entity.strip() != record.accounting_entity.strip()
        or "posting_type" in record.human_overrides
        or not (blockers := _entity_blockers(old))
    ):
        return record

    source_names = _source_party_names(record)
    if source_names != _source_party_names(old):
        return record
    exact_names = {_name_with_punctuation(record.accounting_entity)}
    if profile := scope.entity_profile:
        exact_names.update(map(_name_with_punctuation, (profile.canonical_name, *profile.aliases)))
    if exact_names.intersection(source_names.values()):
        return record

    # A new database can resolve the same printed name fuzzily once a sibling is absent.
    # Keep the original evidence until the source or its exact identity changes.
    original_alerts = {_warning_key(alert): alert for alert in blockers}
    return record.model_copy(
        update={
            "posting_type": None,
            "operation": OperationType.PENDING_REVIEW,
            "review_status": ReviewStatus.NEEDS_REVIEW,
            "validation_alerts": [
                alert
                for alert in record.validation_alerts
                if _warning_key(alert) not in original_alerts
            ] + list(original_alerts.values()),
        }
    )


def _scope_with_current_entity_aliases(
    repository: BillingRepository,
    scope: _BatchScope,
    previous: Sequence[ProcessedBillingRecord],
) -> _BatchScope:
    profile = scope.entity_profile
    if profile is None:
        return scope
    blocked_names = {
        name
        for record in previous
        if _entity_blockers(record)
        for name in _source_party_names(record).values()
        if name
    }
    frozen_names = set(map(_name_with_punctuation, (profile.canonical_name, *profile.aliases)))
    eligible_names = blocked_names - frozen_names
    if not eligible_names:
        return scope
    current_profiles = [
        AccountingEntityProfile.model_validate(row) for row in repository.list_accounting_entities()
    ]
    current = resolve_party_profile(profile.canonical_name, current_profiles)
    if current is None or (
        _name_with_punctuation(current.canonical_name)
        != _name_with_punctuation(profile.canonical_name)
    ):
        return scope
    owned_aliases = [
        alias
        for alias in current.aliases
        if _name_with_punctuation(alias) in eligible_names
        and resolve_party_profile(alias, current_profiles) is current
    ]
    if not owned_aliases:
        return scope

    # Use only exact aliases for the blocked source names during repair. Keep the saved
    # baseline and historical profile for export and subsequent approvals.
    updated = profile.model_copy(update={"aliases": [*profile.aliases, *owned_aliases]})
    return replace(
        scope,
        entity_profile=updated,
        profiles=[updated if entry is profile else entry for entry in scope.profiles],
    )


def _restore_decisions(
    verified: Sequence[ProcessedBillingRecord],
    previous: Sequence[ProcessedBillingRecord],
    scope: _BatchScope,
    *,
    same_source_ids: Collection[str] = (),
    content_matched_ids: Collection[str] = (),
    ambiguous_source_ids: Collection[str] = (),
) -> tuple[list[ProcessedBillingRecord], int]:
    old_by_id = {record.id: record for record in previous}
    restored = []
    for record in verified:
        old = old_by_id.get(record.id)
        same_source = record.id in same_source_ids
        if old is not None and same_source:
            record = _retain_entity_block(record, old, scope)
        if record.id in ambiguous_source_ids:
            restored.append(
                _require_reextraction_review(
                    record, "同源单据重新提取后无法唯一对应原费用行，请重新核对原件"
                )
            )
            continue
        if old is not None and entity_matches(
            old.accounting_entity, record.accounting_entity, scope.profiles
        ):
            acknowledged = {
                _warning_key(alert)
                for alert in old.validation_alerts
                if alert.severity is AlertSeverity.RESOLVED
            }
            alerts = _resolve_warnings(record.validation_alerts, acknowledged)
            review_status = record.review_status
            operation = record.operation
            same_basis = all(
                getattr(old, field) == getattr(record, field) for field in _REVIEW_BASIS_FIELDS
            )
            active = any(alert.severity is not AlertSeverity.RESOLVED for alert in alerts)
            if not active:
                operation = _ready_operation(record)
                review_status = (
                    ReviewStatus.APPROVED
                    if same_basis and old.review_status is ReviewStatus.APPROVED
                    else ReviewStatus.AUTO_READY
                )
            # A uniquely identified rejected fee stays rejected when other fees change the total.
            include_totals = not (
                old.review_status is ReviewStatus.REJECTED and record.id in content_matched_ids
            )
            if (
                same_source
                and old.review_status in _DECISIONS
                and _extracted_review_basis(old, include_totals=include_totals)
                != _extracted_review_basis(record, include_totals=include_totals)
            ):
                restored.append(
                    _require_reextraction_review(
                        record.model_copy(update={"validation_alerts": alerts}),
                        "同源单据重新提取后复核依据已变化，请重新核对原件",
                    )
                )
                continue
            if old.review_status is ReviewStatus.REJECTED:
                same_warnings = set(map(_warning_key, old.validation_alerts)) == set(
                    map(_warning_key, alerts)
                )
                if same_source or (same_basis and same_warnings):
                    review_status = ReviewStatus.REJECTED
            elif (
                same_source
                and old.review_status is ReviewStatus.APPROVED
                and review_status is not ReviewStatus.APPROVED
            ):
                review_status = ReviewStatus.NEEDS_REVIEW
            record = record.model_copy(
                update={
                    "validation_alerts": alerts,
                    "operation": operation,
                    "review_status": review_status,
                    "reject_reason": (
                        old.reject_reason if review_status is ReviewStatus.REJECTED else None
                    ),
                }
            )
        restored.append(record)
    retained = {record.id for record in restored if record.review_status in _DECISIONS}
    reset_count = sum(
        record.review_status in _DECISIONS and record.id not in retained for record in previous
    )
    return restored, reset_count


def _recheck(
    records: Sequence[ProcessedBillingRecord],
    bundles: Sequence[DocumentExtractionBundle],
    scope: _BatchScope,
    previous: Sequence[ProcessedBillingRecord],
    *,
    reapply_dictionary: bool = False,
    same_source_ids: Collection[str] = (),
    content_matched_ids: Collection[str] = (),
    ambiguous_source_ids: Collection[str] = (),
) -> list[ProcessedBillingRecord]:
    prepared = match_and_inherit_batch(
        [_for_revalidation(record) for record in records],
        scope.existing_bills,
        scope.profiles,
        scope.charge_dictionary if reapply_dictionary else None,
    )
    checked = validate_batch(prepared, bundles)
    return _restore_decisions(
        checked,
        previous,
        scope,
        same_source_ids=same_source_ids,
        content_matched_ids=content_matched_ids,
        ambiguous_source_ids=ambiguous_source_ids,
    )[0]


def _with_action(
    record: ProcessedBillingRecord,
    action: str,
    operator: str,
    reason: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> ProcessedBillingRecord:
    history = HumanReviewAction(
        action=action,
        operator=operator,
        reason=reason,
        timestamp=utc_timestamp(),
        overrides=dict(overrides or {}),
    )
    return record.model_copy(
        update={"human_review_history": [*record.human_review_history, history]}
    )


def _audit(
    transaction: BillingRepository,
    after: ProcessedBillingRecord,
    before: ProcessedBillingRecord | None = None,
) -> None:
    action = after.human_review_history[-1]
    diff = []
    for field in (*_REVIEW_BASIS_FIELDS, "operation", "review_status", "reject_reason"):
        if before is not None and getattr(before, field) != getattr(after, field):
            old = before.model_dump(include={field})[field]
            new = after.model_dump(include={field})[field]
            diff.append(FieldDiff(field=field, old_value=old, new_value=new, changed=True))
    transaction.insert_review_audit_log(
        after.batch_id,
        after.id,
        action=action.action,
        operator=action.operator,
        reason=action.reason,
        before=before.model_dump() if before is not None else None,
        after=after.model_dump(),
        diff=diff,
        timestamp=action.timestamp,
    )


def _save_review(
    transaction: BillingRepository,
    batch: Mapping[str, Any],
    records: Sequence[ProcessedBillingRecord],
    *,
    scope: _BatchScope | None = None,
    **batch_changes: Any,
) -> dict[str, Any]:
    batch_id = batch["id"]
    transaction.upsert_billing_records(batch_id, records)
    changes = {"stage_message": batch["stage_message"], **batch_changes}
    if scope is not None:
        changes.update(_batch_scope_changes(batch, scope))
    transaction.update_batch_with_version_check(batch_id, batch["version"], **changes)
    return _require_batch(transaction, batch_id)


def _combine_lock(supplied: Any, from_body: Any) -> Any:
    if supplied is not None and from_body is not None and supplied != from_body:
        raise ServiceValidationError("INVALID_REVIEW_VERSION", "请求提供了不同的复核版本")
    return supplied if supplied is not None else from_body


def _replace_row(
    records: Sequence[ProcessedBillingRecord], replacement: ProcessedBillingRecord
) -> list[ProcessedBillingRecord]:
    return [replacement if record.id == replacement.id else record for record in records]


def _without_total_issues(
    record: ProcessedBillingRecord, fields: set[str]
) -> ProcessedBillingRecord:
    return record.model_copy(
        update={
            "raw_invalid_fields": {
                field: value
                for field, value in record.raw_invalid_fields.items()
                if field not in fields
            },
            "raw_parse_issues": [
                issue for issue in record.raw_parse_issues if issue.field not in fields
            ],
            "validation_alerts": [
                alert for alert in record.validation_alerts if alert.field not in fields
            ],
        }
    )


def _update_totals(
    snapshot: RawExtractionEnvelope, updates: Mapping[str, Any], line_currencies: set[str]
) -> tuple[RawExtractionEnvelope, set[str]]:
    raw = snapshot.extraction
    scalar_updates = dict(updates)
    maps = {name: dict(getattr(snapshot, name)) for name in _TOTAL_MAPS.values()}
    currencies = {currency for amounts in maps.values() for currency in amounts}
    corrected = set(updates)
    if currencies or len(line_currencies) > 1:
        selected = updates.get("total_currency") or raw.total_currency
        if selected is None and len(currencies) == 1:
            selected = next(iter(currencies))
        amounts_changed = bool(_TOTAL_MAPS.keys() & updates.keys())
        if selected is None and amounts_changed:
            raise ServiceValidationError(
                "TOTAL_CURRENCY_REQUIRED", "修订分币种汇总时请指定要修改的 total_currency"
            )
        if not currencies and selected is not None and updates:
            # Start a currency map from the declared scalar when a multi-currency
            # document has only one known summary so far.
            original_currency = (raw.total_currency or selected) if amounts_changed else selected
            for field, map_name in _TOTAL_MAPS.items():
                if (amount := getattr(raw, field)) is not None:
                    maps[map_name][original_currency] = amount
                    corrected.add(f"{map_name}.{original_currency}")
        if selected is not None:
            for field, map_name in _TOTAL_MAPS.items():
                amounts = maps[map_name]
                if field in updates:
                    amount = updates[field]
                    if amount is None:
                        amounts.pop(selected, None)
                    else:
                        amounts[selected] = amount
                    corrected.add(f"{map_name}.{selected}")
                elif updates.get("total_currency") and selected != raw.total_currency:
                    scalar_updates[field] = amounts.get(selected)
                    if scalar_updates[field] != getattr(raw, field):
                        corrected.add(field)
    raw = raw.model_copy(update=scalar_updates)
    has_total = bool(raw.total_amount is not None or maps["totals_by_currency"])
    if "total_amount" not in updates:
        has_total |= snapshot.has_explicit_total
    return snapshot.model_copy(
        update={
            "extraction": raw,
            **maps,
            "has_explicit_total": has_total,
            "explicit_fields": snapshot.explicit_fields | corrected,
            "raw_invalid_fields": {
                field: value
                for field, value in snapshot.raw_invalid_fields.items()
                if field not in corrected
            },
            "raw_parse_issues": [
                issue for issue in snapshot.raw_parse_issues if issue.field not in corrected
            ],
        }
    ), corrected


def _restore_reviewed_totals(
    current: DocumentExtractionBundle, previous: DocumentExtractionBundle
) -> set[str]:
    review = previous.document.metadata.get(_TOTAL_REVIEW, {})
    fields = set(review.get("corrected_fields", ()))
    old, new = previous.extraction, current.extraction
    if not fields or old is None or new is None:
        return set()
    scalar_fields = fields & _TOTAL_FIELDS
    maps = {}
    for map_name in _TOTAL_MAPS.values():
        amounts = dict(getattr(new, map_name))
        for field in fields:
            name, _, currency = field.partition(".")
            if name == map_name:
                if currency in getattr(old, map_name):
                    amounts[currency] = getattr(old, map_name)[currency]
                else:
                    amounts.pop(currency, None)
        maps[map_name] = amounts
    raw = new.extraction.model_copy(update={field: getattr(old, field) for field in scalar_fields})
    current.extraction = new.model_copy(
        update={
            **maps,
            "extraction": raw,
            "has_explicit_total": (
                old.has_explicit_total
                if "total_amount" in fields
                else bool(new.has_explicit_total or maps["totals_by_currency"])
            ),
            "explicit_fields": new.explicit_fields | fields,
            "raw_invalid_fields": {
                field: value
                for field, value in new.raw_invalid_fields.items()
                if field not in fields
            },
            "raw_parse_issues": [
                issue for issue in new.raw_parse_issues if issue.field not in fields
            ],
        }
    )
    current.document.metadata = {**current.document.metadata, _TOTAL_REVIEW: review}
    return fields


def _descendant_ids(documents: Sequence[ParsedDocument], doc_id: str) -> set[str]:
    children = defaultdict(list)
    for document in documents:
        children[document.parent_doc_id].append(document.doc_id)
    pending = [doc_id]
    descendants = set()
    while pending:
        child = pending.pop()
        if child not in descendants:
            descendants.add(child)
            pending.extend(children[child])
    return descendants


def _retain_child_ids(
    parsed: Sequence[ParsedDocument], previous: Sequence[ParsedDocument], target_id: str
) -> list[ParsedDocument]:
    old_children = defaultdict(list)
    for doc in previous:
        old_children[doc.parent_doc_id, doc.original_filename, doc.content_sha256].append(
            doc.doc_id
        )
    ids = {target_id: target_id}
    retained = []
    for document in parsed:
        parent = ids.get(document.parent_doc_id, document.parent_doc_id)
        candidates = old_children[parent, document.original_filename, document.content_sha256]
        doc_id = (
            candidates.pop(0) if candidates and document.doc_id != target_id else document.doc_id
        )
        ids[document.doc_id] = doc_id
        retained.append(document.model_copy(update={"doc_id": doc_id, "parent_doc_id": parent}))
    return retained


def _replacement_line_key(record: ProcessedBillingRecord) -> tuple[str, Decimal] | None:
    source = record.inherited_fields | record.source_values_before_overrides
    charge = source.get("charge_item", record.charge_item)
    amount = source.get("amount", record.amount)
    if charge is None or amount is None:
        return None
    amount = parse_decimal_strict(amount)
    return charge, amount.copy_abs().copy_negate() if record.is_credit else amount


def _source_lines_by_charge(
    records: Sequence[ProcessedBillingRecord],
    origins: Mapping[str, str],
    dictionary: Sequence[ChargeDictionaryItem],
) -> dict[tuple[str, str], list[ProcessedBillingRecord]]:
    groups = defaultdict(list)
    for record in records:
        if (origin := origins.get(record.source_doc_id)) is None:
            continue
        source = record.inherited_fields | record.source_values_before_overrides
        raw_charge = source.get("raw_charge_name", record.raw_charge_name)
        charge, _ = normalize_charge_item(
            raw_charge or source.get("charge_item", record.charge_item), dictionary
        )
        if charge:
            groups[origin, charge.casefold()].append(record)
    return groups


def _source_line_signature(record: ProcessedBillingRecord) -> tuple[str | Decimal | None, ...]:
    source = record.inherited_fields | record.source_values_before_overrides
    numbers = [
        source.get(name, getattr(record, name)) for name in ("amount", "unit_price", "quantity")
    ]
    return (
        normalize_currency(source.get("currency", record.currency))[0],
        *(parse_decimal_strict(value) if value is not None else None for value in numbers),
        normalize_payment_term(source.get("payment_term", record.payment_term)),
        source.get("debit_credit", record.debit_credit),
    )


@dataclass(slots=True)
class _SourceLineAlignment:
    matches: dict[str, ProcessedBillingRecord]
    content_ids: set[str]
    ambiguous_ids: set[str]


def _same_source_line_matches(
    previous: Sequence[ProcessedBillingRecord],
    extracted: Sequence[ProcessedBillingRecord],
    same_sources: Mapping[str, str],
    dictionary: Sequence[ChargeDictionaryItem],
) -> _SourceLineAlignment:
    old_groups = _source_lines_by_charge(
        previous, {origin: origin for origin in same_sources.values()}, dictionary
    )
    new_groups = _source_lines_by_charge(extracted, same_sources, dictionary)
    alignment = _SourceLineAlignment(matches={}, content_ids=set(), ambiguous_ids=set())
    for key, new_lines in new_groups.items():
        old_lines = old_groups.get(key, [])
        if not old_lines:
            continue
        if len(old_lines) == len(new_lines) == 1:
            alignment.matches[new_lines[0].id] = old_lines[0]
            continue

        old_by_signature = defaultdict(list)
        for line in old_lines:
            old_by_signature[_source_line_signature(line)].append(line)
        new_signatures = {line.id: _source_line_signature(line) for line in new_lines}
        signature_counts = Counter(new_signatures.values())
        paired_old_ids = set()
        remaining_new = []
        for line in new_lines:
            signature = new_signatures[line.id]
            candidates = old_by_signature[signature]
            if len(candidates) == signature_counts[signature] == 1:
                alignment.matches[line.id] = candidates[0]
                alignment.content_ids.add(line.id)
                paired_old_ids.add(candidates[0].id)
            else:
                remaining_new.append(line)

        remaining_old = [line for line in old_lines if line.id not in paired_old_ids]
        if not remaining_new or not remaining_old:
            continue
        if len(remaining_old) == len(remaining_new) == 1:
            alignment.matches[remaining_new[0].id] = remaining_old[0]
            continue

        alignment.ambiguous_ids.update(line.id for line in remaining_new)
        old_by_index = defaultdict(list)
        for line in remaining_old:
            old_by_index[line.source_line_index].append(line)
        new_index_counts = Counter(line.source_line_index for line in remaining_new)
        # Keep a decided fee's audit history at a unique position and require a new review.
        # An automatic line has no audit to retain and its ID may carry a sync receipt.
        for line in remaining_new:
            index = line.source_line_index
            if len(old_by_index[index]) == new_index_counts[index] == 1:
                old = old_by_index[index][0]
                if old.review_status in _DECISIONS:
                    alignment.matches[line.id] = old
    return alignment


def _carry_line_edits(
    previous: Sequence[ProcessedBillingRecord],
    extracted: Sequence[ProcessedBillingRecord],
    same_sources: Mapping[str, str],
    documents: Mapping[str, DocumentExtractionBundle],
    alignment: _SourceLineAlignment,
) -> tuple[list[ProcessedBillingRecord], dict[str, str]]:
    # Reserve lines from surviving content before matching edits to changed files.
    destinations = {origin: doc_id for doc_id, origin in same_sources.items()}
    old_by_key = defaultdict(list)
    new_by_key = defaultdict(list)
    for row in previous:
        if row.source_doc_id not in destinations:
            old_by_key[_replacement_line_key(row)].append(row)
    for row in extracted:
        if row.source_doc_id not in same_sources:
            new_by_key[_replacement_line_key(row)].append(row)

    carried = []
    ids = {}
    used = set()
    for row in extracted:
        same_source = row.source_doc_id in same_sources
        old = alignment.matches.get(row.id)
        if not same_source:
            key = _replacement_line_key(row)
            candidates = old_by_key[key]
            old = (
                candidates[0]
                if key is not None and len(candidates) == len(new_by_key[key]) == 1
                else None
            )
        changes = {"id": uuid4().hex}
        if old is not None and old.id not in used:
            used.add(old.id)
            changes.update(
                id=old.id,
                created_at=old.created_at,
                updated_at=old.updated_at,
                human_review_history=old.human_review_history,
            )
            if row.id not in alignment.ambiguous_ids:
                changes["human_overrides"] = old.human_overrides
            if same_source:
                changes["raw_parse_issues"] = [
                    *row.raw_parse_issues,
                    *(issue for issue in old.raw_parse_issues if _is_reextraction_warning(issue)),
                ]
        repaired = row.model_copy(update=changes)
        carried.append(repaired)
        ids[row.id] = repaired.id
    # OCR cannot reproduce a genuinely missing line that a reviewer supplied for the same source.
    for row in previous:
        doc_id = destinations.get(row.source_doc_id)
        if (
            row.id in used
            or doc_id is None
            or not any(action.action == _MANUAL_ADD for action in row.human_review_history)
        ):
            continue
        if doc_id != row.source_doc_id:
            doc = documents[doc_id].document
            evidence = row.source_evidence.model_copy(
                update={
                    "source_doc_id": doc_id,
                    "source_file": doc.original_filename,
                    "source_paths": [doc.original_filename],
                    "extraction_channel": doc.extraction_channel,
                }
            )
            row = row.model_copy(update={"source_doc_id": doc_id, "source_evidence": evidence})
        carried.append(row)
    return carried, ids


class ReviewService:
    """Use one transaction for every review write and its batch version change."""

    def __init__(self, batch_service: BatchService) -> None:
        self.batch_service = batch_service
        self.repository = batch_service.repository

    def switch_entity(
        self, batch_id: str, accounting_entity: str, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        entity = _entity_name(accounting_entity)
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            scope = _load_batch_scope(transaction, batch, entity)
            prior = batch["records"]
            records, _ = recalculate_for_entity(
                [_for_revalidation(record) for record in prior],
                entity,
                profiles=scope.profiles,
                existing_bills=scope.existing_bills,
                charge_dictionary=scope.charge_dictionary,
                bundles=[_document_bundle(row) for row in batch["documents"]],
            )
            records, reset = _restore_decisions(records, prior, scope)
            updated = _save_review(
                transaction, batch, records, scope=scope, accounting_entity=entity
            )
        return {
            "batch": _batch_detail(updated),
            "reset_approval_count": reset,
            "notice": f"记账主体已重算，重置 {reset} 条人工复核决定",
        }

    def reapply_dictionary(
        self, batch_id: str, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            scope = _load_batch_scope(transaction, batch, batch["accounting_entity"])
            records = _recheck(
                batch["records"],
                [_document_bundle(row) for row in batch["documents"]],
                scope,
                batch["records"],
                reapply_dictionary=True,
            )
            updated = _save_review(transaction, batch, records, scope=scope)
        return _batch_detail(updated)

    def patch_record(
        self,
        batch_id: str,
        record_id: str,
        patch: Mapping[str, Any],
        *,
        expected_version: int | None = None,
        expected_updated_at: str | None = None,
        operator: str = "local",
    ) -> dict[str, Any]:
        fields = _RecordPatch.parse(patch)
        reason = fields.pop("review_note", None)
        version = _combine_lock(expected_version, fields.pop("expected_version", None))
        updated_at = _combine_lock(expected_updated_at, fields.pop("expected_updated_at", None))
        if "shipment_refs" in fields and TRANSPORT_FIELDS.keys() & fields.keys():
            raise ServiceValidationError(
                "CONFLICTING_SHIPMENT_REFS", "shipment_refs 和平铺的运输号字段须分开提交"
            )
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, version)
            before = _require_record(batch, record_id, updated_at)
            scope = _load_batch_scope(transaction, batch, batch["accounting_entity"])
            overrides = before.human_overrides | fields
            if overrides.get("matched_existing_bill_id") is not None and overrides.get(
                "force_create_new"
            ):
                raise ServiceValidationError(
                    "CONFLICTING_MATCH_SELECTION", "关联历史账单与强制新建不能同时选择"
                )
            if (selected := fields.get("matched_existing_bill_id")) is not None and not any(
                bill.id == selected for bill in scope.existing_bills
            ):
                raise ServiceValidationError(
                    "EXISTING_BILL_OUT_OF_SCOPE", "请选择当前批次和记账主体范围内的历史账单"
                )
            if "shipment_refs" in fields:
                for field in TRANSPORT_FIELDS:
                    overrides.pop(field, None)
            edited = _with_action(
                before.model_copy(update={"human_overrides": overrides}),
                "PATCH_RECORD",
                operator,
                reason,
                fields,
            )
            if "posting_type" in fields and (blockers := _entity_blockers(before)):
                # Revalidate a deliberate ledger edit against the current printed parties.
                edited = edited.model_copy(
                    update={
                        "validation_alerts": [
                            alert for alert in edited.validation_alerts if alert not in blockers
                        ],
                    }
                )
            records = _recheck(
                _replace_row(batch["records"], edited),
                [_document_bundle(row) for row in batch["documents"]],
                scope,
                batch["records"],
            )
            updated = _save_review(transaction, batch, records, scope=scope)
            _audit(transaction, _require_record(updated, record_id), before)
        return _batch_detail(updated)

    def add_manual_record(
        self,
        batch_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
        operator: str = "local",
    ) -> dict[str, Any]:
        fields = _ManualRecord.parse(payload)
        doc_id = fields.pop("source_doc_id")
        reason = fields.pop("reason", "人工补录漏提费用行")
        _require_currency_code(fields["currency"])
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            row = _require_document(batch, doc_id)
            bundles = [_document_bundle(document) for document in batch["documents"]]
            scope = _load_batch_scope(transaction, batch, batch["accounting_entity"])
            canonical, _ = deduplicate_same_source_documents(bundles)
            source = next(
                bundle
                for bundle in canonical
                if bundle.document.doc_id == doc_id
                or row["content_sha256"] is not None
                and bundle.document.content_sha256 == row["content_sha256"]
            )
            doc = source.document
            source_data = (
                source.extraction.extraction
                if source.extraction is not None
                else RawDocumentExtraction()
            )
            raw_charge_name = fields.pop("raw_charge_name", fields["charge_item"])
            charge, _ = normalize_charge_item(fields["charge_item"], scope.charge_dictionary)
            if charge is None:
                raise ServiceValidationError("INVALID_CHARGE_ITEM", "费用名称须为非空文本")
            fields["charge_item"] = charge
            records = batch["records"]
            next_line = 1 + max(
                (
                    record.source_line_index
                    for record in records
                    if record.source_doc_id == doc.doc_id
                ),
                default=-1,
            )
            manual = ProcessedBillingRecord(
                id=uuid4().hex,
                batch_id=batch_id,
                source_doc_id=doc.doc_id,
                source_line_index=next_line,
                accounting_entity=batch["accounting_entity"],
                raw_charge_name=raw_charge_name,
                human_overrides=fields,
                source_evidence=SourceEvidence(
                    source_doc_id=doc.doc_id,
                    source_file=doc.original_filename,
                    source_paths=[doc.original_filename],
                    extraction_channel=doc.extraction_channel,
                ),
                **source_data.model_dump(
                    include={
                        "document_type",
                        "reference_no",
                        "document_date",
                        "issuer",
                        "recipient",
                        "payer_hint",
                        "payee_hint",
                        "shipment_refs",
                        "excluded_clauses",
                    }
                ),
            )
            manual = _with_action(manual, _MANUAL_ADD, operator, reason, fields)
            verified = _recheck([*records, manual], bundles, scope, records)
            outcome = _batch_outcome(
                [bundle.document for bundle in bundles], verified, batch["failed_document_alerts"]
            )
            updated = _save_review(transaction, batch, verified, scope=scope, **outcome)
            _audit(transaction, _require_record(updated, manual.id))
        return _batch_detail(updated)

    def approve_record(
        self,
        batch_id: str,
        record_id: str,
        reason: str | None = None,
        *,
        expected_version: int | None = None,
        expected_updated_at: str | None = None,
        operator: str = "local",
    ) -> dict[str, Any]:
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            before = _require_record(batch, record_id, expected_updated_at)
            blocker_codes = list(
                dict.fromkeys(
                    alert.code.value
                    for alert in before.validation_alerts
                    if alert.severity is AlertSeverity.BLOCKER
                )
            )
            if blocker_codes:
                raise ServiceValidationError(
                    "HARD_BLOCKER_CANNOT_APPROVE",
                    "该记录仍有硬性阻断，请先修正对应字段或选择驳回",
                    status_code=422,
                    details={"blocker_codes": blocker_codes},
                )
            if before.review_status is ReviewStatus.REJECTED:
                raise ServiceValidationError("RECORD_REJECTED", "请修订并重验已驳回的记录")
            warnings = {
                _warning_key(alert)
                for alert in before.validation_alerts
                if alert.severity is AlertSeverity.WARNING
            }
            reason = _reason(reason, required=bool(warnings))
            if before.review_status is ReviewStatus.APPROVED and not warnings:
                return _batch_detail(batch)
            edited = before.model_copy(
                update={
                    "validation_alerts": _resolve_warnings(before.validation_alerts, warnings),
                    "review_status": ReviewStatus.APPROVED,
                    "operation": _ready_operation(before),
                    "reject_reason": None,
                }
            )
            return self._save_decision(
                transaction, batch, before, edited, "APPROVE", operator, reason
            )

    def reject_record(
        self,
        batch_id: str,
        record_id: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_updated_at: str | None = None,
        operator: str = "local",
    ) -> dict[str, Any]:
        reason = _reason(reason)
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            before = _require_record(batch, record_id, expected_updated_at)
            if before.review_status is ReviewStatus.REJECTED and before.reject_reason == reason:
                return _batch_detail(batch)
            edited = before.model_copy(
                update={"review_status": ReviewStatus.REJECTED, "reject_reason": reason}
            )
            return self._save_decision(
                transaction, batch, before, edited, "REJECT", operator, reason
            )

    def _save_decision(
        self,
        transaction: BillingRepository,
        batch: Mapping[str, Any],
        before: ProcessedBillingRecord,
        edited: ProcessedBillingRecord,
        action: str,
        operator: str,
        reason: str | None,
    ) -> dict[str, Any]:
        edited = _with_action(edited, action, operator, reason)
        updated = _save_review(transaction, batch, [edited])
        _audit(transaction, _require_record(updated, edited.id), before)
        return _batch_detail(updated)

    def patch_document_totals(
        self,
        batch_id: str,
        doc_id: str,
        patch: Mapping[str, Any],
        *,
        expected_version: int | None = None,
        operator: str = "local",
    ) -> dict[str, Any]:
        fields = _DocumentTotalsPatch.parse(patch)
        reason = fields.pop("review_note", None)
        if fields.get("total_currency") is not None:
            currency, error = normalize_currency(fields["total_currency"])
            if currency is None:
                raise ServiceValidationError("INVALID_CURRENCY", error or "请指定有效的汇总币种")
            fields["total_currency"] = currency
        with self.repository.transaction() as transaction:
            batch = _require_batch(transaction, batch_id)
            _require_editable(batch, expected_version)
            row = _require_document(batch, doc_id)
            bundle = _document_bundle(row)
            snapshot = bundle.extraction or RawExtractionEnvelope(
                extraction=RawDocumentExtraction()
            )
            line_currencies = {
                record.currency
                for record in batch["records"]
                if record.source_doc_id == doc_id and record.currency is not None
            }
            updated_snapshot, corrected = _update_totals(snapshot, fields, line_currencies)
            doc = bundle.document
            old_review = doc.metadata.get(_TOTAL_REVIEW, {})
            original = old_review.get(
                "original",
                {
                    **{field: getattr(snapshot, field) for field in _TOTAL_FIELDS},
                    **{field: getattr(snapshot, field) for field in _TOTAL_MAPS.values()},
                    "raw_invalid_fields": {
                        field: value
                        for field, value in snapshot.raw_invalid_fields.items()
                        if field in corrected
                    },
                },
            )
            doc.metadata[_TOTAL_REVIEW] = {
                "original": original,
                "operator": operator,
                "reason": reason,
                "corrected_fields": sorted(set(old_review.get("corrected_fields", ())) | corrected),
                "timestamp": utc_timestamp(),
            }
            bundle.extraction = updated_snapshot
            transaction.upsert_source_documents(batch_id, [bundle])
            bundles = [
                bundle if document["id"] == doc_id else _document_bundle(document)
                for document in batch["documents"]
            ]
            scope = _load_batch_scope(transaction, batch, batch["accounting_entity"])
            records = [
                _without_total_issues(record, corrected)
                if record.source_doc_id == doc_id
                else record
                for record in batch["records"]
            ]
            checked = _recheck(records, bundles, scope, batch["records"])
            updated = _save_review(transaction, batch, checked, scope=scope)
        return _batch_detail(updated)

    def retry_or_replace_document(
        self,
        batch_id: str,
        doc_id: str,
        *,
        file: Upload | None = None,
        public_url: str | None = None,
        ocr_mode: OcrMode | str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self._repair_document(
            batch_id, doc_id, file, public_url, ocr_mode, expected_version, force_parse=False
        )

    def force_parse_skipped_document(
        self,
        batch_id: str,
        doc_id: str,
        *,
        ocr_mode: OcrMode | str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self._repair_document(
            batch_id, doc_id, None, None, ocr_mode, expected_version, force_parse=True
        )

    def _repair_document(
        self,
        batch_id: str,
        doc_id: str,
        file: Upload | None,
        public_url: str | None,
        ocr_mode: OcrMode | str | None,
        expected_version: int | None,
        *,
        force_parse: bool,
    ) -> dict[str, Any]:
        batch = _require_batch(self.repository, batch_id)
        _require_editable(batch, expected_version)
        row = _require_document(batch, doc_id)
        original = _document_bundle(row).document
        if force_parse and (
            original.status is not DocumentStatus.SKIPPED_NOISE
            or original.source_kind is not SourceKind.IMAGE
        ):
            raise ServiceValidationError("DOCUMENT_NOT_SKIPPED_IMAGE", "请选择已跳过的邮件内嵌图")
        if file is not None and public_url is not None:
            raise ServiceValidationError("CONFLICTING_SOURCE_INPUTS", "请上传文件或提供公网 URL")
        mode = _ocr_mode(batch["ocr_mode"] if ocr_mode is None else ocr_mode)
        bundles = [_document_bundle(document) for document in batch["documents"]]
        documents = [bundle.document for bundle in bundles]
        depth, slots = compute_retry_context(documents, doc_id, self.batch_service.limits)
        replaced_ids = _descendant_ids(documents, doc_id)
        dispatcher = self.batch_service._dispatcher(mode)
        filename = original.original_filename
        media_type = original.media_type
        source_url = None
        data = None
        if file is not None:
            self.batch_service._validate_inputs([file], ())
            filename, media_type = file[0], None
            data = self.batch_service._upload_bytes(file)
        elif public_url is not None or original.source_kind is SourceKind.URL:
            source_url = public_url if public_url is not None else original.public_url
            if source_url is None:
                raise ServiceValidationError(
                    "ORIGINAL_FILE_UNAVAILABLE", "复核包没有可用原件，请补传文件或提供公网 URL。"
                )
            self.batch_service._validate_inputs((), [source_url])
        else:
            data = self.batch_service._read_original(batch_id, row)
        args = {
            "existing_doc_id": doc_id,
            "parent_doc_id": original.parent_doc_id,
            "media_type": media_type,
            "depth": depth,
        }
        if force_parse:
            parsed = [dispatcher.force_parse_skipped_document(filename, data, **args)]
        elif file is None and public_url is None and original.source_kind is SourceKind.EML_BODY:
            parsed = [
                original.model_copy(
                    update={
                        "status": DocumentStatus.PARSED,
                        "error_message": None,
                        "extraction_channel": ExtractionChannel.EML_TEXT_PARSER,
                    }
                )
            ]
        else:
            parsed = dispatcher.retry_single_document(
                filename, data, public_url=source_url, remaining_expanded_slots=slots, **args
            )
        parsed = _retain_child_ids(
            parsed, [document for document in documents if document.doc_id in replaced_ids], doc_id
        )
        previous_bundles = {bundle.document.doc_id: bundle for bundle in bundles}
        unchanged = {}
        if file is None and public_url is None:
            for index, doc in enumerate(parsed):
                previous_bundle = previous_bundles.get(doc.doc_id)
                if (
                    previous_bundle is not None
                    and doc.content_sha256 == previous_bundle.document.content_sha256
                    and doc.public_url == previous_bundle.document.public_url
                ):
                    unchanged[doc.doc_id] = previous_bundle
                    previous_document = previous_bundle.document
                    if doc.status is DocumentStatus.SKIPPED_NOISE and previous_document.status in {
                        DocumentStatus.PARSED,
                        DocumentStatus.METADATA_ONLY,
                    }:
                        # Retrying a parent email retains a prior decision to force this image.
                        parsed[index] = previous_document.model_copy(
                            update={"raw_bytes": doc.raw_bytes}
                        )
        targets = {document.doc_id for document in parsed}
        roots = _root_documents(documents)
        retained = [bundle for bundle in bundles if bundle.document.doc_id not in replaced_ids]
        previous = batch["records"]
        old_lines = [record for record in previous if record.source_doc_id in replaced_ids]
        candidates = [record for record in previous if record.source_doc_id not in replaced_ids]

        hashes = {document.doc_id: document.content_sha256 for document in documents}
        retained_owners = {
            hashes[record.source_doc_id]: previous_bundles[record.source_doc_id]
            for record in candidates
            if hashes[record.source_doc_id]
        }
        replaced_owners = {
            hashes[record.source_doc_id]: previous_bundles[record.source_doc_id]
            for record in old_lines
            if hashes[record.source_doc_id]
        }
        covered_hashes = set(retained_owners)
        covered_hashes.update(
            document.content_sha256
            for document in parsed
            if document.status is DocumentStatus.PARSED
        )
        lost_hashes = replaced_owners.keys() - covered_hashes
        restoring = {
            bundle.document.doc_id: bundle
            for bundle in retained
            if bundle.document.content_sha256 in lost_hashes
        }
        # Rebuild duplicate rows using the surviving copies' own email context.
        affected_roots = {roots[doc_id], *(roots[copy_id] for copy_id in restoring)}
        package = [
            bundle.document
            for bundle in retained
            if roots[bundle.document.doc_id] in affected_roots
        ]
        scope = _load_batch_scope(self.repository, batch, batch["accounting_entity"])
        result = self.batch_service._run_pipeline(
            [*package, *parsed],
            batch["accounting_entity"],
            _scope_with_current_entity_aliases(self.repository, scope, old_lines),
            batch_id,
            targets=targets | restoring.keys(),
        )
        replacement_lines = [record for record in result.records if record.source_doc_id in targets]
        replacement_failures = [
            alert for alert in result.failed_document_alerts if alert.doc_id in targets
        ]
        if old_lines and not replacement_lines and replacement_failures:
            return self._save_failed_repair(batch, documents, doc_id, replacement_failures[0])

        restored_lines = [record for record in result.records if record.source_doc_id in restoring]
        restored_hashes = {hashes[record.source_doc_id] for record in restored_lines}
        if any(hashes[copy_id] not in restored_hashes for copy_id in restoring):
            failure = FailedDocumentAlert(
                doc_id=doc_id,
                filename=original.original_filename,
                error_message="保留的同源副本未能重新生成费用明细。",
            )
            return self._save_failed_repair(batch, documents, doc_id, failure)

        unchanged.update(restoring)
        fresh_documents = {bundle.document.doc_id: bundle for bundle in result.documents}
        duplicate_targets = set()
        for fresh_id, bundle in fresh_documents.items():
            content_hash = bundle.document.content_sha256
            if retained_owner := retained_owners.get(content_hash):
                # The retained rows may move to this document during final deduplication.
                # Keep their extraction and document corrections with them.
                duplicate_targets.add(fresh_id)
                bundle.extraction = retained_owner.extraction
                if totals := retained_owner.document.metadata.get(_TOTAL_REVIEW):
                    bundle.document.metadata[_TOTAL_REVIEW] = totals
            elif replaced_owner := replaced_owners.get(content_hash):
                unchanged[fresh_id] = replaced_owner

        corrected_totals = {
            bundle.document.doc_id: _restore_reviewed_totals(
                bundle, unchanged[bundle.document.doc_id]
            )
            for bundle in result.documents
            if bundle.document.doc_id in unchanged
        }
        canonical, _ = deduplicate_same_source_documents(result.documents)
        same_sources = {
            fresh_id: unchanged[fresh_id].document.doc_id
            for bundle in canonical
            if (fresh_id := bundle.document.doc_id) in unchanged
            and fresh_id not in duplicate_targets
        }
        fresh_lines = [
            record
            for record in [*replacement_lines, *restored_lines]
            if record.source_doc_id not in duplicate_targets
        ]
        alignment = _same_source_line_matches(
            old_lines, fresh_lines, same_sources, scope.charge_dictionary
        )
        carried, ids = _carry_line_edits(
            old_lines, fresh_lines, same_sources, fresh_documents, alignment
        )
        prior_source_ids = set(same_sources.values())
        prior_lines = [record for record in old_lines if record.source_doc_id in prior_source_ids]
        carried_ids = {record.id for record in carried}
        if any(
            record.id not in carried_ids
            and (record.review_status in _DECISIONS or record.human_review_history)
            for record in prior_lines
        ):
            failure = FailedDocumentAlert(
                doc_id=doc_id,
                filename=original.original_filename,
                error_message="同源单据的已复核费用行未能对齐。",
            )
            return self._save_failed_repair(batch, documents, doc_id, failure)
        carried = [
            _without_total_issues(record, fields)
            if (fields := corrected_totals.get(record.source_doc_id))
            else record
            for record in carried
        ]
        replacements = [bundle for bundle in result.documents if bundle.document.doc_id in targets]
        restored = {
            bundle.document.doc_id: bundle
            for bundle in result.documents
            if bundle.document.doc_id in restoring
        }
        merged = []
        for bundle in bundles:
            current_id = bundle.document.doc_id
            if current_id == doc_id:
                merged.extend(replacements)
            elif current_id not in replaced_ids:
                merged.append(restored.get(current_id, bundle))
        duplicate_hashes = {
            fresh_documents[fresh_id].document.content_sha256 for fresh_id in duplicate_targets
        }
        same_source_ids = {record.id for record in prior_lines if hashes[record.source_doc_id]}
        same_source_ids.update(
            record.id for record in candidates if hashes[record.source_doc_id] in duplicate_hashes
        )
        failures = [
            alert
            for alert in batch["failed_document_alerts"]
            if alert.doc_id not in replaced_ids and alert.doc_id not in restoring
        ]
        failures.extend(
            alert.model_copy(
                update={
                    "affected_record_ids": [
                        ids[record_id]
                        for record_id in alert.affected_record_ids
                        if record_id in ids
                    ]
                }
            )
            for alert in result.failed_document_alerts
        )
        reused = {doc_id: row["stored_path"]} if data is not None and file is None else {}
        with self.batch_service._staged_originals(batch_id) as new_paths:
            stored = self.batch_service._store_child_originals(batch_id, parsed, new_paths, reused)
            with self.repository.transaction() as transaction:
                current = _require_batch(transaction, batch_id)
                _require_editable(current, batch["version"])
                scope = _load_batch_scope(transaction, current, current["accounting_entity"])
                previous_lines = [*candidates, *prior_lines]
                repair_scope = _scope_with_current_entity_aliases(
                    transaction,
                    scope,
                    [record for record in previous_lines if record.id in same_source_ids],
                )
                checked = _recheck(
                    [*candidates, *carried],
                    merged,
                    repair_scope,
                    previous_lines,
                    same_source_ids=same_source_ids,
                    content_matched_ids={ids[line_id] for line_id in alignment.content_ids},
                    ambiguous_source_ids={ids[line_id] for line_id in alignment.ambiguous_ids},
                )
                outcome = _batch_outcome([bundle.document for bundle in merged], checked, failures)
                outcome.update(_batch_scope_changes(current, scope))
                transaction.upsert_source_documents(batch_id, result.documents, stored_paths=stored)
                transaction.upsert_billing_records(batch_id, checked)
                live_ids = {record.id for record in checked}
                transaction.delete_billing_records(
                    batch_id, [record.id for record in previous if record.id not in live_ids]
                )
                if obsolete := replaced_ids - targets:
                    # Keep removal of obsolete descendants in the same transaction as the repair.
                    connection = transaction._connection
                    assert connection is not None
                    connection.executemany(
                        "DELETE FROM source_documents WHERE batch_id = ? AND id = ?",
                        [(batch_id, child_id) for child_id in obsolete],
                    )
                transaction.update_batch_with_version_check(batch_id, current["version"], **outcome)
                updated = _require_batch(transaction, batch_id)
        self._discard_replaced_originals(batch, updated, replaced_ids)
        return _batch_detail(updated)

    def _save_failed_repair(
        self,
        batch: Mapping[str, Any],
        documents: Sequence[ParsedDocument],
        doc_id: str,
        error: FailedDocumentAlert,
    ) -> dict[str, Any]:
        with self.repository.transaction() as transaction:
            current = _require_batch(transaction, batch["id"])
            _require_editable(current, batch["version"])
            row = _require_document(current, doc_id)
            alerts = [
                alert for alert in current["failed_document_alerts"] if alert.doc_id != doc_id
            ]
            alerts.append(
                FailedDocumentAlert(
                    doc_id=doc_id,
                    filename=row["original_filename"],
                    error_message=f"本次重试未能读取费用，已保留上次结果。{error.error_message}",
                )
            )
            outcome = _batch_outcome(documents, current["records"], alerts)
            updated = _save_review(transaction, current, [], **outcome)
        return _batch_detail(updated)

    def _discard_replaced_originals(
        self, before: Mapping[str, Any], after: Mapping[str, Any], replaced_ids: set[str]
    ) -> None:
        live = {row["stored_path"] for row in after["documents"]}
        for row in before["documents"]:
            stored_path = row["stored_path"]
            if row["id"] not in replaced_ids or not stored_path or stored_path in live:
                continue
            try:
                path = Path(stored_path)
                if (
                    path.resolve(strict=True) == path
                    and path.is_relative_to(self.batch_service._upload_dir(before["id"]))
                    and path.is_file()
                ):
                    path.unlink()
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError, ValueError):
                _LOGGER.warning("Could not remove unused upload for batch %s", before["id"])
