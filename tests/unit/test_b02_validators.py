"""B02 deterministic billing gates through the domain entry points."""

from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256

import pytest

from shipping_billing_agent.domain import (
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    ExistingBillRecord,
    HumanReviewAction,
    MatchLevel,
    OperationType,
    ParsedDocument,
    ProcessedBillingRecord,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ReviewStatus,
    ValidationAlert,
    compute_iso6346_check_digit,
    deduplicate_same_source_documents,
    match_and_inherit_batch,
    match_record,
    parse_llm_raw_extraction,
    recalculate_for_entity,
    serialize_records,
    validate_batch,
    validate_container_number,
    validate_record,
    validate_records,
)
from shipping_billing_agent.domain.entity_resolver import PRINTED_CURRENCY_CONFLICT_MESSAGE

ENTITY = "NEPTUNE SHIPPING LIMITED"


def _line(line_id: str = "line", doc_id: str = "invoice", **fields) -> ProcessedBillingRecord:
    return ProcessedBillingRecord.model_validate(
        {
            "id": line_id,
            "source_doc_id": doc_id,
            "accounting_entity": ENTITY,
            "operation": "CREATE",
            "review_status": "AUTO_READY",
            "posting_type": "AR",
            "charge_item": "Handling Charge",
            "amount": "100.00",
            "raw_explicit_amount": fields.get("amount", "100.00"),
            "currency": "USD",
            **fields,
        }
    )


def _bundle(
    doc_id: str = "invoice",
    *,
    filename: str | None = None,
    parent_doc_id: str | None = None,
    digest: str | None = None,
    public_url: str | None = None,
    source_kind: str = "PDF",
    subtotals_by_currency: dict[str, str] | None = None,
    taxes_by_currency: dict[str, str] | None = None,
    totals_by_currency: dict[str, str] | None = None,
    ocr_payload: dict | None = None,
    **fields,
) -> DocumentExtractionBundle:
    name = filename or f"{doc_id}.pdf"
    return DocumentExtractionBundle(
        document=ParsedDocument(
            doc_id=doc_id,
            parent_doc_id=parent_doc_id,
            original_filename=name,
            sanitized_filename=name,
            source_kind=source_kind,
            content_sha256=None
            if source_kind == "URL"
            else digest or sha256(doc_id.encode()).hexdigest(),
            public_url=public_url,
            extraction_channel="PLTPLT_OCR",
            status="PARSED",
            ocr_payload=ocr_payload,
        ),
        extraction=RawExtractionEnvelope(
            extraction=RawDocumentExtraction.model_validate(fields),
            subtotals_by_currency=subtotals_by_currency or {},
            taxes_by_currency=taxes_by_currency or {},
            totals_by_currency=totals_by_currency or {},
        ),
    )


def _bill(**fields) -> ExistingBillRecord:
    return ExistingBillRecord.model_validate(
        {
            "id": "old",
            "accounting_entity": ENTITY,
            "reference_no": "NOTICE-1",
            "charge_item": "Handling Charge",
            "amount": "30.00",
            "currency": "USD",
            "document_date": "2026-02-20",
            "posting_type": "AR",
            **fields,
        }
    )


def _codes(record: ProcessedBillingRecord) -> set[AlertCode]:
    return {alert.code for alert in record.validation_alerts}


def _assert_review(record: ProcessedBillingRecord, code: AlertCode) -> None:
    assert record.operation is OperationType.PENDING_REVIEW
    assert record.review_status is ReviewStatus.NEEDS_REVIEW
    assert any(
        alert.code is code and alert.severity is AlertSeverity.BLOCKER
        for alert in record.validation_alerts
    )


@pytest.mark.parametrize(
    ("raw_currencies", "pairs", "snippets"),
    [
        (("EUR", "GBP"), (("USD", "EUR"), ("USD", "GBP")), (None, None)),
        (("US$ / EUR", "US$ / GBP"), (("USD", "EUR"), ("USD", "GBP")), (None, None)),
        (("EUR / USD", "GBP / USD"), (("EUR", "USD"), ("GBP", "USD")), (None, None)),
        (("CA$ / USD", "GBP / USD"), (("CAD", "USD"), ("GBP", "USD")), (None, None)),
        (("USD", "USD"), (("EUR", "USD"), ("GBP", "USD")), ("EUR 30 USD", "GBP 15 USD")),
    ],
    ids=["single-token", "shared-expected", "shared-actual", "symbol", "snippet-pair"],
)
def test_legacy_currency_override_keeps_only_other_lines_own_conflicts(
    raw_currencies: tuple[str, str],
    pairs: tuple[tuple[str, str], tuple[str, str]],
    snippets: tuple[str | None, str | None],
) -> None:
    eur, gbp = [
        ValidationAlert.from_code(
            AlertCode.CROSS_DOCUMENT_CONFLICT,
            PRINTED_CURRENCY_CONFLICT_MESSAGE,
            field="currency",
            expected=expected,
            actual=actual,
        )
        for expected, actual in pairs
    ]
    first = _line(
        "handling", currency=None, raw_invalid_fields={"currency": raw_currencies[0]},
        source_evidence={"snippet": snippets[0]},
        raw_parse_issues=[eur, gbp], validation_alerts=[eur, gbp],
        human_overrides={"currency": "USD"},
    )
    second = _line(
        "storage", charge_item="Storage Charge", currency=None,
        raw_invalid_fields={"currency": raw_currencies[1]},
        source_evidence={"snippet": snippets[1]},
        raw_parse_issues=[eur, gbp], validation_alerts=[eur, gbp],
    )

    checked = validate_batch([first, second])

    assert [row.raw_parse_issues for row in checked] == [[eur], [gbp]]
    assert [row.validation_alerts for row in checked] == [[gbp], [gbp]]
    assert serialize_records(checked, "official") == []
    edited = second.model_copy(update={"human_overrides": {"currency": "USD"}})
    ready = validate_batch([first, edited])
    assert [row.validation_alerts for row in ready] == [[], []]
    assert len(serialize_records(ready, "official")) == 2


@pytest.mark.parametrize(("amount", "passes"), [("1.12", True), ("1.121", False)])
def test_line_arithmetic_cent_boundary(amount: str, passes: bool) -> None:
    validated = validate_record(_line(amount=amount, quantity="2", unit_price="0.55"))

    if passes:
        assert validated.operation is OperationType.CREATE
        assert validated.validation_alerts == []
    else:
        _assert_review(validated, AlertCode.LINE_ARITHMETIC_MISMATCH)


@pytest.mark.parametrize(
    ("explicit", "overrides", "check_arithmetic"),
    [
        ({"payment_term"}, {"amount": "35"}, False),
        (set(), {"amount": "35"}, True),
        ({"quantity", "unit_price"}, {"amount": "35"}, True),
        ({"payment_term"}, {"amount": "35", "quantity": "2", "unit_price": "15"}, True),
    ],
    ids=["legacy-partial", "legacy-unannotated", "printed-rate", "edited-all-three"],
)
def test_arithmetic_checks_amount_override_against_stated_rate_fields(
    explicit: set[str], overrides: dict[str, str], check_arithmetic: bool
) -> None:
    line = _line(
        amount="30",
        quantity="2",
        unit_price="15",
        explicit_fields=explicit,
        human_overrides=overrides,
    )

    checked = validate_record(line)

    assert checked.amount == Decimal("35")
    if check_arithmetic:
        _assert_review(checked, AlertCode.LINE_ARITHMETIC_MISMATCH)
        assert not serialize_records([checked], "official")
    else:
        assert checked.review_status is ReviewStatus.AUTO_READY
        assert len(serialize_records([checked], "official")) == 1


def test_large_decimal_arithmetic_retains_cents_under_low_caller_precision() -> None:
    line = _line(
        amount="123456789012345678901234567890.021",
        quantity="1",
        unit_price="123456789012345678901234567890.000",
    )
    with localcontext(prec=4):
        result = validate_record(line)

    _assert_review(result, AlertCode.LINE_ARITHMETIC_MISMATCH)
    assert result.amount == line.amount


def test_match_then_validate_preserves_explicit_invalid_values_and_parser_alerts() -> None:
    parsed = parse_llm_raw_extraction("""{
        "reference_no": "NOTICE-1", "document_date": "2026-02-30",
        "charges": [{"raw_charge_name": "Handling Charge", "amount": "1.310.40", "currency": "ZZZ"}]
    }""")
    (raw,) = parsed.charges
    line = _line(
        reference_no=parsed.extraction.reference_no,
        document_date=parsed.extraction.document_date,
        amount=raw.charge.amount,
        currency=raw.charge.currency,
        raw_amount_text=raw.raw_amount_text,
        explicit_fields=raw.explicit_fields | parsed.explicit_fields,
        raw_invalid_fields=raw.raw_invalid_fields | parsed.raw_invalid_fields,
        raw_parse_issues=[*parsed.raw_parse_issues, *raw.raw_parse_issues],
    )

    matched = match_record(line, [_bill()])
    result = validate_record(matched)

    assert result.matched_existing_bill_id == "old"
    assert result.currency is result.amount is result.document_date is None
    assert result.validation_alerts == matched.validation_alerts
    assert {(alert.code, alert.actual) for alert in result.validation_alerts} == {
        (AlertCode.INVALID_OR_MISSING_CURRENCY, "ZZZ"),
        (AlertCode.INVALID_DOCUMENT_DATE, "2026-02-30"),
        (AlertCode.OCR_NUMERIC_CONFUSION, "1.310.40"),
    }
    _assert_review(result, AlertCode.OCR_NUMERIC_CONFUSION)


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("currency", "ZZZ", AlertCode.INVALID_OR_MISSING_CURRENCY),
        ("document_date", "2026-02-30", AlertCode.INVALID_DOCUMENT_DATE),
        ("amount", "?", AlertCode.OCR_NUMERIC_CONFUSION),
        ("quantity", "?", AlertCode.OCR_NUMERIC_CONFUSION),
        ("unit_price", "?", AlertCode.OCR_NUMERIC_CONFUSION),
    ],
)
def test_explicit_invalid_metadata_alone_blocks(field: str, bad: str, code: AlertCode) -> None:
    result = validate_record(_line(raw_invalid_fields={field: bad}))

    _assert_review(result, code)
    assert any(alert.field == field and alert.actual == bad for alert in result.validation_alerts)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"charge_item": " "}, AlertCode.LLM_EXTRACTION_FAILED),
        ({"amount": None}, AlertCode.LLM_EXTRACTION_FAILED),
        ({"currency": None}, AlertCode.INVALID_OR_MISSING_CURRENCY),
        ({"currency": "ZZZ"}, AlertCode.INVALID_OR_MISSING_CURRENCY),
        ({"document_date": "2026-02-30"}, AlertCode.INVALID_DOCUMENT_DATE),
        ({"posting_type": None}, AlertCode.ENTITY_NOT_MATCHED),
        ({"amount": "-40.00"}, AlertCode.NEGATIVE_NON_CREDIT_AMOUNT),
        (
            {"quantity": "0", "unit_price": "-1", "amount": "0.00"},
            AlertCode.LINE_ARITHMETIC_MISMATCH,
        ),
    ],
)
def test_line_mandatory_fields_and_sign_gates(fields: dict, code: AlertCode) -> None:
    _assert_review(validate_record(_line(**fields)), code)


@pytest.mark.parametrize("nonfinite", ["Infinity", "sNaN"])
def test_nonfinite_amount_is_blocked_even_if_runtime_model_validation_was_bypassed(
    nonfinite: str,
) -> None:
    line = _line().model_copy(update={"amount": Decimal(nonfinite)})

    _assert_review(validate_record(line), AlertCode.OCR_NUMERIC_CONFUSION)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("raw_amount_text", "1O0.00"),
        ("raw_amount_text", "1.310.40"),
        ("raw_unit_price_text", "2S.00"),
        ("raw_quantity_text", "l1"),
        ("raw_quantity_text", "I.0"),
    ],
)
def test_finite_normalized_number_does_not_hide_ocr_confusion(field: str, text: str) -> None:
    result = validate_record(_line(**{field: text}))

    _assert_review(result, AlertCode.OCR_NUMERIC_CONFUSION)
    assert result.validation_alerts[0].actual == text


def test_ordinary_grouped_and_scientific_number_text_does_not_trigger_ocr_alerts() -> None:
    line = _line(raw_amount_text="1,310.40", raw_unit_price_text="1.2e+3")

    assert validate_record(line).validation_alerts == []


@pytest.mark.parametrize(("amount", "raw_amount"), [("40.00", "40.00"), ("-40.00", "-40.00")])
def test_credit_line_checks_magnitude_and_keeps_negative_posting(
    amount: str, raw_amount: str
) -> None:
    line = _line(
        amount=amount, raw_explicit_amount=raw_amount, quantity="2", unit_price="20", is_credit=True
    )

    result = validate_record(line)

    assert result.amount == Decimal("-40.00")
    assert result.raw_explicit_amount == Decimal(raw_amount)
    assert result.operation is OperationType.CREATE
    assert result.validation_alerts == []


def test_partial_update_only_multiplies_three_current_or_manually_edited_values() -> None:
    old = _bill(quantity="2", unit_price="15.00")
    source = _line(
        reference_no="NOTICE-1", amount="35.00", explicit_fields={"reference_no", "amount"}
    )
    matched = match_record(source, [old])

    result = validate_record(matched)

    assert {"quantity", "unit_price"} <= matched.inherited_fields.keys()
    assert result.operation is OperationType.UPDATE
    assert result.review_status is ReviewStatus.AUTO_READY and result.validation_alerts == []

    one_edit = matched.model_copy(update={"human_overrides": {"quantity": "2"}})
    assert validate_record(one_edit).validation_alerts == []

    both_edited = matched.model_copy(
        update={"human_overrides": {"quantity": "2", "unit_price": "15.00"}}
    )
    checked = validate_record(both_edited)
    _assert_review(checked, AlertCode.LINE_ARITHMETIC_MISMATCH)
    assert [(alert.expected, alert.actual) for alert in checked.validation_alerts] == [
        ("30.00", "35.00")
    ]


@pytest.mark.parametrize(
    ("explicit", "overrides", "has_mismatch"),
    [
        ({"amount"}, {}, False),
        ({"amount", "quantity"}, {}, False),
        ({"amount", "quantity"}, {"unit_price": "15"}, True),
        ({"amount", "quantity", "unit_price"}, {}, True),
        (set(), {}, True),
    ],
    ids=["legacy-amount", "legacy-quantity", "manual-rate", "all-explicit", "unannotated"],
)
def test_direct_arithmetic_checks_only_current_values_when_amount_is_explicit(
    explicit: set[str], overrides: dict[str, str], has_mismatch: bool
) -> None:
    legacy = _line(
        amount="35",
        quantity="2",
        unit_price="15",
        explicit_fields=explicit,
        human_overrides=overrides,
    )
    assert not legacy.inherited_fields

    checked = validate_record(legacy)

    if has_mismatch:
        _assert_review(checked, AlertCode.LINE_ARITHMETIC_MISMATCH)
    else:
        assert not checked.validation_alerts and checked.review_status is ReviewStatus.AUTO_READY


def test_fully_inherited_arithmetic_does_not_block_payment_term_only_updates() -> None:
    source = _line(reference_no="DN-202609-001", amount=None, payment_term="CC")
    matched = match_record(
        source,
        [_bill(reference_no="DN-202609-001", quantity="2", unit_price="10.00", payment_term="PP")],
    )

    assert matched.raw_explicit_amount is None
    assert {"quantity", "unit_price", "amount"} <= matched.inherited_fields.keys()
    validated = validate_record(matched)
    assert validated.operation is OperationType.UPDATE
    assert validated.review_status is ReviewStatus.AUTO_READY
    assert validated.validation_alerts == []


@pytest.mark.parametrize("status", [ReviewStatus.APPROVED, ReviewStatus.REJECTED])
def test_resolved_alert_stays_resolved_for_the_same_source_value(status: ReviewStatus) -> None:
    bad_container = "JXLU4504464"
    resolved = ValidationAlert(
        code=AlertCode.CONTAINER_ISO6346_MISMATCH,
        severity=AlertSeverity.RESOLVED,
        message="箱号已复核",
        field="container_nos",
        actual=bad_container,
    )
    line = _line(
        review_status=status,
        shipment_refs={"container_nos": [bad_container]},
        validation_alerts=[resolved],
    )

    result = validate_record(line)

    assert result.validation_alerts == [resolved]
    assert result.operation is OperationType.CREATE
    assert result.review_status is status
    changed = line.model_copy(
        update={
            "shipment_refs": line.shipment_refs.model_copy(
                update={"container_nos": ["MSCU663987O"]}
            )
        }
    )
    (warning,) = validate_record(changed).validation_alerts
    assert (warning.code, warning.severity, warning.actual) == (
        AlertCode.CONTAINER_ISO6346_MISMATCH,
        AlertSeverity.WARNING,
        "MSCU663987O",
    )


def test_validator_recomputes_entity_gate_and_preserves_match_ambiguity() -> None:
    entity = ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "主体未匹配")
    ambiguous = ValidationAlert.from_code(AlertCode.AMBIGUOUS_EXISTING_BILL, "命中多条历史记录")
    line = _line(posting_type=None, validation_alerts=[entity, ambiguous])

    result = validate_record(line)

    assert _codes(result) == {AlertCode.ENTITY_NOT_MATCHED, AlertCode.AMBIGUOUS_EXISTING_BILL}
    assert ambiguous in result.validation_alerts
    _assert_review(result, AlertCode.AMBIGUOUS_EXISTING_BILL)


def test_validate_records_accepts_the_entity_recalculation_callback() -> None:
    candidate = _line(issuer=ENTITY, recipient="New Entity", review_status="APPROVED")
    (result,), reset = recalculate_for_entity(
        [candidate], "New Entity", matcher_fn=match_and_inherit_batch, validator_fn=validate_records
    )

    assert reset == 1
    assert result.accounting_entity == "New Entity"
    assert result.posting_type == "AP"
    assert result.operation is OperationType.CREATE
    assert result.review_status is ReviewStatus.AUTO_READY


def test_same_entity_recalculation_rematches_against_reloaded_history_without_callbacks() -> None:
    notice = _line(
        reference_no="NOTICE-1",
        amount=None,
        currency=None,
        posting_type=None,
        payment_term="CC",
        explicit_fields={"reference_no", "payment_term", "charge_item"},
        validation_alerts=[ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "缺双方")],
    )
    original = _bill(issuer=ENTITY, recipient="Buyer", payment_term="PP")
    matched = match_record(notice, [original])
    approved = matched.model_copy(update={"review_status": ReviewStatus.APPROVED})
    new_baseline = _bill(
        id="updated-baseline",
        issuer=ENTITY,
        recipient="Buyer",
        amount="80",
        payment_term="CC",
    )

    (result,), reset = recalculate_for_entity([approved], ENTITY, existing_bills=[new_baseline])

    assert reset == 1
    assert result.matched_existing_bill_id == "updated-baseline"
    assert (result.amount, result.operation) == (Decimal("80"), OperationType.NO_CHANGE)
    assert result.review_status is ReviewStatus.NEEDS_REVIEW and result.validation_alerts == []
    assert result.requires_reapproval and not serialize_records([result], "official")
    assert approved.matched_existing_bill_id == "old" and approved.amount == Decimal("30")


@pytest.mark.parametrize("status", [ReviewStatus.APPROVED, ReviewStatus.REJECTED])
def test_unchanged_recalculation_keeps_manual_decision_under_an_entity_alias(
    status: ReviewStatus,
) -> None:
    profiles = [AccountingEntityProfile(canonical_name=ENTITY, aliases=["Neptune"])]
    source = _line(reference_no="NOTICE-1", issuer=ENTITY, recipient="Buyer", amount="35")
    bills = [_bill(issuer=ENTITY, recipient="Buyer")]
    history = [HumanReviewAction(action=status, operator="reviewer", timestamp="2026-02-28")]
    reason = "duplicate" if status is ReviewStatus.REJECTED else None
    original = validate_record(match_record(source, bills, profiles)).model_copy(
        update={
            "review_status": status,
            "requires_reapproval": True,
            "reject_reason": reason,
            "human_review_history": history,
        }
    )

    assert not validate_record(original).requires_reapproval
    (recalculated,), reset = recalculate_for_entity([original], "Neptune", profiles, bills)

    assert reset == 0
    assert recalculated.review_status is status
    assert not recalculated.requires_reapproval
    assert recalculated.reject_reason == reason
    assert recalculated.human_review_history == history
    assert recalculated.operation is OperationType.UPDATE
    assert len(serialize_records([recalculated], "official")) == (status is ReviewStatus.APPROVED)


@pytest.mark.parametrize("change", ["hbl", "historical-amount"])
@pytest.mark.parametrize("status", [ReviewStatus.APPROVED, ReviewStatus.REJECTED])
def test_same_ledger_recalculation_requires_review_when_hbl_or_the_historical_delta_changes(
    change: str, status: ReviewStatus
) -> None:
    source = _line(
        reference_no="NOTICE-1",
        issuer=ENTITY,
        recipient="Buyer",
        amount="80",
        shipment_refs={"hbl_no": "HBL-11"},
    )
    historical = _bill(issuer=ENTITY, recipient="Buyer", amount="50")
    previous = validate_record(match_record(source, [historical]))
    history = [HumanReviewAction(action=status, operator="reviewer", timestamp="2026-11-19")]
    previous = previous.model_copy(
        update={
            "review_status": status,
            "reject_reason": "checked",
            "human_review_history": history,
        }
    )
    previous = ProcessedBillingRecord.model_validate_json(previous.model_dump_json())
    if change == "hbl":
        previous = previous.model_copy(update={"human_overrides": {"hbl_no": "HBL-99"}})
    else:
        historical = historical.model_copy(update={"amount": Decimal("30000")})

    (checked,), reset = recalculate_for_entity([previous], ENTITY, existing_bills=[historical])

    assert checked.matched_existing_bill_id == previous.matched_existing_bill_id == historical.id
    assert checked.amount == previous.amount and checked.operation is OperationType.UPDATE
    if change == "hbl":
        assert checked.shipment_refs.hbl_no == "HBL-99"
    else:
        (diff,) = checked.field_diffs
        assert (diff.field, diff.old_value, diff.new_value, diff.changed) == (
            "amount",
            Decimal("30000"),
            Decimal("80"),
            True,
        )
    assert reset == 1 and checked.review_status is ReviewStatus.NEEDS_REVIEW
    assert checked.reject_reason is None and checked.human_review_history == history
    assert serialize_records([checked], "official") == []


@pytest.mark.parametrize("status", [ReviewStatus.APPROVED, ReviewStatus.REJECTED])
def test_recalculation_preserves_review_across_equivalent_refs_and_serialized_diffs(
    status: ReviewStatus,
) -> None:
    refs = {
        "hbl_no": "HBL-11",
        "booking_no": "BK-12",
        "mbl_nos": [{"value": value, "source": "document"} for value in ("MBL-1", "MBL-2")],
        "container_nos": ["MSCU6639870", "CSQU3054383"],
    }
    source = _line(
        reference_no="NOTICE-1",
        issuer=ENTITY,
        recipient="Buyer",
        amount="80.000",
        shipment_refs=refs,
    )
    historical = _bill(issuer=ENTITY, recipient="Buyer", amount="50.00")
    (previous,) = validate_batch(match_and_inherit_batch([source], [historical]))
    assert previous.review_status is ReviewStatus.AUTO_READY
    history = [HumanReviewAction(action=status, operator="reviewer", timestamp="2026-11-19")]
    previous = previous.model_copy(
        update={
            "review_status": status,
            "reject_reason": "checked",
            "human_review_history": history,
            "human_overrides": {
                "shipment_refs": {
                    **refs,
                    "hbl_no": " ｈｂｌ-１１ ",
                    "booking_no": " bk-12 ",
                    "mbl_nos": list(reversed(refs["mbl_nos"])),
                    "container_nos": list(reversed(refs["container_nos"])),
                }
            },
        }
    )
    previous = ProcessedBillingRecord.model_validate_json(previous.model_dump_json())
    historical = historical.model_copy(update={"amount": Decimal("50")})

    (checked,), reset = recalculate_for_entity([previous], ENTITY, existing_bills=[historical])

    assert reset == 0 and checked.review_status is status
    assert checked.reject_reason == "checked" and checked.human_review_history == history


@pytest.mark.parametrize("change", ["amount", "total_gate"])
def test_effective_amount_or_new_total_gate_resets_a_rejection(change: str) -> None:
    source = _line(issuer=ENTITY, recipient="Buyer", amount="30")
    (balanced,) = validate_batch([source], [_bundle(total_amount="30")])
    rejected = balanced.model_copy(
        update={"review_status": ReviewStatus.REJECTED, "reject_reason": "duplicate"}
    )
    bundles = []
    if change == "amount":
        rejected = rejected.model_copy(update={"human_overrides": {"amount": "30.01"}})
    else:
        bundles = [_bundle(total_amount="40")]

    (recalculated,), reset = recalculate_for_entity([rejected], ENTITY, bundles=bundles)

    assert reset == 1
    assert recalculated.reject_reason is None
    if change == "amount":
        assert recalculated.amount == Decimal("30.01")
        assert recalculated.review_status is ReviewStatus.AUTO_READY
    else:
        assert recalculated.amount == balanced.amount
        _assert_review(recalculated, AlertCode.AMOUNT_TOTAL_MISMATCH)


def test_document_total_survives_detached_revalidation_and_updates_with_new_bundles() -> None:
    line = _line(issuer=ENTITY, recipient="Buyer", amount="30")
    original_bundle = _bundle(total_amount="40", total_currency="USD")
    (mismatch,) = validate_batch([line], [original_bundle])
    _assert_review(mismatch, AlertCode.AMOUNT_TOTAL_MISMATCH)
    assert mismatch.source_doc_totals is not None
    assert mismatch.source_doc_totals.source_doc_id == line.source_doc_id
    assert mismatch.source_doc_totals.total_amount == Decimal("40")

    detached = ProcessedBillingRecord.model_validate_json(mismatch.model_dump_json())
    _assert_review(validate_record(detached), AlertCode.AMOUNT_TOTAL_MISMATCH)
    assert serialize_records([validate_record(detached)], "official") == []
    (direct,) = validate_records([detached])
    _assert_review(direct, AlertCode.AMOUNT_TOTAL_MISMATCH)
    (recalculated,), _ = recalculate_for_entity(
        [detached], "Buyer", matcher_fn=match_and_inherit_batch, validator_fn=validate_records
    )
    _assert_review(recalculated, AlertCode.AMOUNT_TOTAL_MISMATCH)
    assert recalculated.posting_type == "AP"

    (balanced,), _ = recalculate_for_entity(
        [recalculated],
        "Buyer",
        bundles=[_bundle(total_amount="30", total_currency="USD")],
    )
    assert balanced.operation is OperationType.CREATE and balanced.validation_alerts == []
    (detached_again,) = validate_records([balanced])
    assert detached_again == balanced


@pytest.mark.parametrize(
    "code",
    [
        AlertCode.AMOUNT_TOTAL_MISMATCH,
        AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
        AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
        AlertCode.DUPLICATE_LINE_EXTRACTED,
        AlertCode.CROSS_DOCUMENT_CONFLICT,
        AlertCode.MULTI_SOURCE_BL_MISMATCH,
        AlertCode.OCR_SUSPICIOUS_FLAG,
    ],
)
def test_single_line_validation_keeps_prior_contextual_alerts(code: AlertCode) -> None:
    alert = ValidationAlert.from_code(code, "依赖完整文档或邮件包复核")
    result = validate_record(_line(validation_alerts=[alert]))

    assert result.validation_alerts == [alert]
    assert result.operation is OperationType.PENDING_REVIEW
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


def test_revalidating_original_bundle_respects_each_rows_valid_edits_and_keeps_audit() -> None:
    parsed = parse_llm_raw_extraction("""{
        "document_date": "2026-02-30",
        "charges": [{"raw_charge_name": "Handling Charge", "amount": "1.310.40", "currency": "ZZZ"}]
    }""")
    (raw,) = parsed.charges
    bundle = _bundle().model_copy(update={"extraction": parsed})
    source = _line(
        amount=raw.charge.amount,
        currency=raw.charge.currency,
        raw_amount_text=raw.raw_amount_text,
        raw_invalid_fields=raw.raw_invalid_fields | parsed.raw_invalid_fields,
        raw_parse_issues=[*raw.raw_parse_issues, *parsed.raw_parse_issues],
        human_overrides={"currency": "USD", "document_date": "2026-02-28", "amount": "30.00"},
    )
    invalid_edit = source.model_copy(
        update={
            "id": "invalid",
            "human_overrides": source.human_overrides | {"document_date": "2026-02-31"},
        }
    )

    fixed, blocked = validate_batch(match_and_inherit_batch([source, invalid_edit], []), [bundle])

    assert (fixed.currency, fixed.document_date, fixed.amount) == (
        "USD",
        "2026-02-28",
        Decimal("30.00"),
    )
    assert (fixed.operation, fixed.review_status) == (OperationType.CREATE, ReviewStatus.AUTO_READY)
    assert fixed.validation_alerts == []
    assert fixed.raw_invalid_fields == source.raw_invalid_fields
    assert fixed.raw_parse_issues == source.raw_parse_issues
    (exported,) = serialize_records([fixed], "audit")
    assert exported["audit_trail"]["raw_invalid_fields"] == {
        "currency": "ZZZ",
        "document_date": "2026-02-30",
        "amount": "1.310.40",
    }
    _assert_review(blocked, AlertCode.INVALID_DOCUMENT_DATE)
    assert {issue.actual for issue in blocked.validation_alerts} == {"2026-02-30", "2026-02-31"}


def test_human_overrides_fix_only_corresponding_original_format_alerts() -> None:
    source = _line(
        reference_no="NOTICE-1",
        amount=None,
        currency=None,
        document_date=None,
        raw_invalid_fields={
            "currency": "ZZZ",
            "document_date": "2026-02-30",
            "amount": "1.310.40",
            "quantity": "l1",
            "unit_price": "I.0",
            "payment_term": "unknown",
        },
        raw_amount_text="1.310.40",
        raw_quantity_text="l1",
        raw_unit_price_text="I.0",
        human_overrides={
            "currency": "usd",
            "document_date": date(2026, 2, 28),
            "amount": "30",
            "quantity": 2,
            "unit_price": "15",
            "payment_term": "collect",
            "posting_type": "AP",
            "shipment_refs": {"hbl_no": "H-30"},
            "container_nos": ["JXLU4504463"],
            "mbl_no": "M-30",
            "booking_no": "B-30",
        },
    )

    result = validate_record(match_record(source, [_bill()]))

    assert result.validation_alerts == [] and result.operation is OperationType.UPDATE
    assert (result.currency, result.document_date, result.amount) == (
        "USD",
        "2026-02-28",
        Decimal("30"),
    )
    assert (result.quantity, result.unit_price, result.payment_term, result.posting_type) == (
        Decimal("2"),
        Decimal("15"),
        "CC",
        "AP",
    )
    assert result.shipment_refs.hbl_no == "H-30"
    assert result.shipment_refs.mbl_nos[0].value == "M-30"
    assert result.shipment_refs.booking_no == "B-30"
    assert result.shipment_refs.container_nos == ["JXLU4504463"]
    assert result.raw_invalid_fields == source.raw_invalid_fields
    (exported,) = serialize_records([result], "audit")
    assert exported["charge_details"]["currency"] == "USD"
    assert exported["audit_trail"]["raw_invalid_fields"] == source.raw_invalid_fields

    partially_fixed = source.model_copy(
        update={"human_overrides": source.human_overrides | {"document_date": "2026-02-31"}}
    )
    blocked = validate_record(match_record(partially_fixed, [_bill()]))
    _assert_review(blocked, AlertCode.INVALID_DOCUMENT_DATE)
    assert {alert.actual for alert in blocked.validation_alerts} == {"2026-02-30", "2026-02-31"}


@pytest.mark.parametrize(
    "field, value",
    [
        ("amount", Decimal("NaN")),
        ("quantity", "bad"),
        ("unit_price", "Inf"),
        ("currency", "XXX"),
        ("payment_term", "unknown"),
        ("posting_type", "DR"),
        ("container_nos", ["JXLU4504464"]),
        ("shipment_refs", {"unexpected": "field"}),
        ("mbl_no", ["M-123"]),
        ("hbl_no", 123),
        ("booking_no", False),
    ],
)
def test_invalid_manual_values_create_new_blockers(field: str, value: object) -> None:
    result = validate_record(_line(human_overrides={field: value}))

    assert result.operation is OperationType.PENDING_REVIEW
    assert any(
        alert.severity is AlertSeverity.BLOCKER and alert.field == field
        for alert in result.validation_alerts
    )


def test_correcting_manual_container_edit_rebuilds_the_format_blocker() -> None:
    bad = validate_record(_line(human_overrides={"container_nos": ["JXLU4504464"]}))
    _assert_review(bad, AlertCode.LLM_EXTRACTION_FAILED)
    corrected = bad.model_copy(
        update={
            "human_overrides": {},
            "shipment_refs": bad.shipment_refs.model_copy(
                update={"container_nos": ["JXLU4504463"]}
            ),
        }
    )
    result = validate_record(corrected)
    assert result.operation is OperationType.CREATE and result.validation_alerts == []


def test_charge_name_override_and_updated_dictionary_recompute_history_matching() -> None:
    original = _line(
        issuer=ENTITY, reference_no="NOTICE-1", raw_charge_name="Handling Charge", amount="30"
    )
    handling = _bill(id="handling")
    trucking = _bill(id="trucking", charge_item="Trucking Charge")
    custom = _bill(id="custom", charge_item="Road Transport")
    edited = original.model_copy(update={"human_overrides": {"raw_charge_name": "Trucking Fee"}})
    (matched,), _ = recalculate_for_entity([edited], ENTITY, existing_bills=[handling, trucking])
    assert matched.charge_item == "Trucking Charge"
    assert matched.matched_existing_bill_id == "trucking"

    (rematched,), _ = recalculate_for_entity(
        [matched],
        ENTITY,
        existing_bills=[handling, trucking, custom],
        charge_dictionary=[
            ChargeDictionaryItem(raw_alias="Trucking Fee", standard_name="Road Transport")
        ],
    )
    assert rematched.charge_item == "Road Transport"
    assert (rematched.match_level, rematched.matched_existing_bill_id) == (
        MatchLevel.L1_DOC_CHARGE,
        "custom",
    )

    locked = edited.model_copy(
        update={"human_overrides": edited.human_overrides | {"charge_item": "Handling Charge"}}
    )
    (fixed,), _ = recalculate_for_entity([locked], ENTITY, existing_bills=[handling, trucking])
    assert fixed.charge_item == "Handling Charge" and fixed.matched_existing_bill_id == "handling"


def test_human_amount_override_recalculates_line_and_source_total() -> None:
    original = _line(issuer=ENTITY, amount="100", quantity="1", unit_price="100")
    (balanced,) = validate_batch([original], [_bundle(total_amount="100")])
    edited = balanced.model_copy(update={"human_overrides": {"amount": "200"}})

    (blocked,), _ = recalculate_for_entity([edited], ENTITY)

    assert blocked.amount == Decimal("200") and blocked.raw_explicit_amount == Decimal("100")
    assert _codes(blocked) == {AlertCode.LINE_ARITHMETIC_MISMATCH, AlertCode.AMOUNT_TOTAL_MISMATCH}
    assert all(alert.actual == "200" for alert in blocked.validation_alerts)
    _assert_review(blocked, AlertCode.AMOUNT_TOTAL_MISMATCH)

    repaired = blocked.model_copy(update={"human_overrides": {"amount": "100"}})
    (ready,) = validate_records([repaired])
    assert ready.amount == Decimal("100")
    assert ready.operation is OperationType.CREATE and ready.validation_alerts == []


def test_human_override_on_inherited_amount_is_counted_until_the_edit_is_removed() -> None:
    partial = _line(
        reference_no="NOTICE-1",
        amount=None,
        payment_term="CC",
        issuer=ENTITY,
        explicit_fields={"reference_no", "payment_term", "issuer", "charge_item"},
    )
    old = _bill(quantity="2", unit_price="15", payment_term="PP")
    (matched,) = validate_batch([match_record(partial, [old])], [_bundle(total_amount="0")])
    assert matched.validation_alerts == [] and matched.raw_explicit_amount is None

    edited = matched.model_copy(update={"human_overrides": {"amount": "60"}})
    (blocked,) = validate_records([edited])
    assert _codes(blocked) == {AlertCode.AMOUNT_TOTAL_MISMATCH}

    removed_edit = blocked.model_copy(update={"human_overrides": {}})
    (reverted,), _ = recalculate_for_entity([removed_edit], ENTITY, existing_bills=[old])
    assert reverted.amount == Decimal("30") and reverted.raw_explicit_amount is None
    assert reverted.operation is OperationType.UPDATE and reverted.validation_alerts == []


def test_iso6346_valid_wrong_check_digit_and_unique_ocr_repair() -> None:
    assert compute_iso6346_check_digit("JXLU450446") == 3
    assert validate_container_number("JXLU4504463") == (True, 3, None)
    assert validate_container_number("JXLU4504464") == (False, 3, None)
    assert validate_container_number("MSKU123456O") == (False, 5, None)
    assert validate_container_number("MSCU663987O") == (False, 0, "MSCU6639870")
    assert validate_container_number("JX1U45O4463") == (False, None, "JXLU4504463")
    assert validate_container_number("111U1234510") == (False, None, None)
    with pytest.raises(ValueError):
        compute_iso6346_check_digit("JXLU45044O")

    valid = validate_record(_line(shipment_refs={"container_nos": ["JXLU4504463"]}))
    assert valid.validation_alerts == []
    result = validate_record(_line(shipment_refs={"container_nos": ["MSCU663987O"]}))
    (alert,) = result.validation_alerts
    assert (alert.code, alert.severity, alert.actual, alert.expected) == (
        AlertCode.CONTAINER_ISO6346_MISMATCH,
        AlertSeverity.WARNING,
        "MSCU663987O",
        "MSCU6639870",
    )
    assert "建议修正为 MSCU6639870" in alert.message
    assert result.operation is OperationType.PENDING_REVIEW
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


def test_resolved_container_warning_does_not_hide_a_different_container() -> None:
    bad = "MSCU663987O"
    line = _line(shipment_refs={"container_nos": [bad]})
    (alert,) = validate_record(line).validation_alerts
    resolved = alert.model_copy(update={"severity": AlertSeverity.RESOLVED})
    confirmed = line.model_copy(
        update={"validation_alerts": [resolved], "review_status": ReviewStatus.APPROVED}
    )
    assert validate_record(confirmed) == confirmed

    newer = confirmed.model_copy(
        update={
            "shipment_refs": confirmed.shipment_refs.model_copy(
                update={"container_nos": [bad, "JXLU4504464"]}
            )
        }
    )
    checked = validate_record(newer)
    assert [alert.severity for alert in checked.validation_alerts] == [
        AlertSeverity.RESOLVED,
        AlertSeverity.WARNING,
    ]
    assert checked.review_status is ReviewStatus.NEEDS_REVIEW


def test_fixed_container_removes_stale_warning_and_recovers_operation() -> None:
    blocked = validate_record(_line(shipment_refs={"container_nos": ["JXLU4504464"]}))
    assert _codes(blocked) == {AlertCode.CONTAINER_ISO6346_MISMATCH}
    confirmed = blocked.model_copy(
        update={
            "validation_alerts": [
                blocked.validation_alerts[0].model_copy(update={"severity": AlertSeverity.RESOLVED})
            ],
            "shipment_refs": blocked.shipment_refs.model_copy(
                update={"container_nos": ["JXLU4504463"]}
            ),
        }
    )

    checked = validate_record(confirmed)

    assert checked.validation_alerts == []
    assert (checked.operation, checked.review_status) == (
        OperationType.CREATE,
        ReviewStatus.AUTO_READY,
    )


@pytest.mark.parametrize(
    "field, actual",
    [
        (None, None),
        ("", "JXLU4504464"),
        (" ", "JXLU4504464"),
        ("container_nos", ""),
        ("container_nos", " "),
    ],
)
def test_incomplete_resolution_never_suppresses_current_container_warnings(
    field: str | None, actual: str | None
) -> None:
    line = _line(
        shipment_refs={"container_nos": ["JXLU4504464", "MSCU663987O"]},
        validation_alerts=[
            ValidationAlert(
                code=AlertCode.CONTAINER_ISO6346_MISMATCH,
                severity=AlertSeverity.RESOLVED,
                message="未绑定原值",
                field=field,
                actual=actual,
            )
        ],
    )
    checked = validate_record(line)
    assert checked.operation is OperationType.PENDING_REVIEW
    assert {(alert.actual, alert.severity) for alert in checked.validation_alerts} == {
        ("JXLU4504464", AlertSeverity.WARNING),
        ("MSCU663987O", AlertSeverity.WARNING),
    }


def test_exact_total_confirmation_cannot_release_the_blocker() -> None:
    (blocked,) = validate_batch([_line()], [_bundle(total_amount="110")])
    (alert,) = blocked.validation_alerts
    with pytest.raises(ValueError, match="告警严重度"):
        ValidationAlert.model_validate(alert.model_dump() | {"severity": AlertSeverity.RESOLVED})

    # model_copy bypasses validation and represents a legacy in-memory confirmation.
    confirmation = alert.model_copy(update={"severity": AlertSeverity.RESOLVED})
    confirmed = blocked.model_copy(update={"validation_alerts": [confirmation]})
    (revalidated,) = validate_records([confirmed])
    _assert_review(revalidated, AlertCode.AMOUNT_TOTAL_MISMATCH)
    assert revalidated.validation_alerts == [alert]
    _assert_review(validate_record(confirmed), AlertCode.AMOUNT_TOTAL_MISMATCH)

    changed = confirmed.model_copy(update={"human_overrides": {"amount": "105"}})
    (fresh,) = validate_records([changed])
    _assert_review(fresh, AlertCode.AMOUNT_TOTAL_MISMATCH)
    assert [(alert.actual, alert.severity) for alert in fresh.validation_alerts] == [
        ("105", AlertSeverity.BLOCKER)
    ]
    repaired = confirmed.model_copy(update={"human_overrides": {"amount": "110"}})
    (ready,) = validate_records([repaired])
    assert ready.operation is OperationType.CREATE
    assert ready.validation_alerts == []


def test_ocr_suspicion_from_payload_item_is_a_warning() -> None:
    bundle = _bundle(
        ocr_payload={
            "items": [{"invoiceNumber": "INV-1"}, {"suspicious": True}],
            "has_charges": True,
            "has_metadata": True,
        }
    )

    (result,) = validate_batch([_line()], [bundle])

    (alert,) = result.validation_alerts
    assert alert.code is AlertCode.OCR_SUSPICIOUS_FLAG
    assert alert.severity is AlertSeverity.WARNING
    assert result.operation is OperationType.PENDING_REVIEW


@pytest.mark.parametrize(
    "edit", [None, "field", "actual"], ids=["exact", "wrong-field", "wrong-value"]
)
def test_ocr_suspicion_confirmation_revalidates_against_the_exact_item(edit: str | None) -> None:
    bundle = _bundle(
        ocr_payload={
            "items": [
                {"suspicious": True, "text": "  fee label is unclear  ", "chargeList": [],
                 "other": {}, "issuer": ""},
                {"suspicious": True},
            ],
            "has_charges": True,
            "has_metadata": True,
        }
    )
    (initial,) = validate_batch([_line()], [bundle])
    with_text, without_text = initial.validation_alerts
    assert all(alert.code is AlertCode.OCR_SUSPICIOUS_FLAG for alert in initial.validation_alerts)
    assert with_text.field and without_text.field and with_text.field != without_text.field
    assert with_text.actual == "fee label is unclear"
    assert without_text.actual == without_text.field
    checked = []
    for alert in (with_text, without_text):
        update = {"severity": AlertSeverity.RESOLVED}
        if edit and alert is with_text:
            update[edit] = "different"
        checked.append(alert.model_copy(update=update))
    confirmed = initial.model_copy(update={"validation_alerts": checked})

    (rechecked,) = validate_batch([confirmed], [bundle])

    first, second = rechecked.validation_alerts
    assert (first.field, first.actual) == (with_text.field, with_text.actual)
    assert first.severity is (AlertSeverity.WARNING if edit else AlertSeverity.RESOLVED)
    assert second == checked[1] and second.severity is AlertSeverity.RESOLVED
    assert len(serialize_records([rechecked], "official")) == (0 if edit else 1)


@pytest.mark.parametrize(
    ("original", "equivalent", "changed"),
    [
        ({"other": "30 or 3O?"}, {"other": "30 or 3O?"}, {"other": "80 or 8O?"}),
        (
            {"chargeList": [{"amount": "30", "currency": "USD"}]},
            {"chargeList": [{"currency": "USD", "amount": "30"}]},
            {"chargeList": [{"currency": "USD", "amount": "30000"}]},
        ),
        (
            {"locations": {"row": 1, "page": 2}},
            {"locations": {"page": 2, "row": 1}},
            {"locations": {"page": 3, "row": 1}},
        ),
        (
            {"pageNumber": 4, "text": "unclear"},
            {"text": "unclear", "pageNumber": 4},
            {"text": "unclear", "pageNumber": 5},
        ),
    ],
    ids=["other", "charge-list", "extra", "text-and-page"],
)
def test_ocr_suspicion_invalidates_confirmation_when_structured_content_changes(
    original: dict, equivalent: dict, changed: dict
) -> None:
    bundles = [
        _bundle(
            ocr_payload={
                "items": [{"suspicious": True, **data}],
                "has_charges": True,
                "has_metadata": True,
            }
        )
        for data in (original, equivalent, changed)
    ]
    (initial,) = validate_batch([_line()], [bundles[0]])
    (warning,) = initial.validation_alerts
    resolved = warning.model_copy(update={"severity": AlertSeverity.RESOLVED})
    confirmed = initial.model_copy(update={"validation_alerts": [resolved]})

    (rechecked,) = validate_batch([confirmed], [bundles[1]])
    (current,) = rechecked.validation_alerts
    assert current.severity is AlertSeverity.RESOLVED and current.actual == warning.actual
    assert len(serialize_records([rechecked], "official")) == 1

    (rechecked,) = validate_batch([rechecked], [bundles[2]])

    (current,) = rechecked.validation_alerts
    assert current.code is AlertCode.OCR_SUSPICIOUS_FLAG
    assert current.severity is AlertSeverity.WARNING
    assert current.field == warning.field and current.actual != warning.actual
    assert serialize_records([rechecked], "official") == []


def test_duplicate_amount_excess_uses_the_total_before_tax() -> None:
    rows = [_line("first", amount="15"), _line("second", amount="15", source_line_index=1)]
    bundle = _bundle(tax_amount="3", total_amount="18", total_currency="USD")

    checked = validate_batch(rows, [bundle])

    for row in checked:
        assert _codes(row) == {
            AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
            AlertCode.DUPLICATE_LINE_EXTRACTED,
        }


@pytest.mark.parametrize(
    ("summary", "code"),
    [
        ({}, None),
        ({"subtotal_amount": "100.02"}, None),
        ({"subtotal_amount": "100.021"}, AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH),
        ({"subtotal_amount": "100.00", "tax_amount": "8.00"}, None),
        ({"subtotal_amount": "100.00", "total_amount": "100.00"}, None),
        ({"subtotal_amount": "100.00", "tax_amount": "8.00", "total_amount": "108.00"}, None),
        (
            {"subtotal_amount": "100.00", "tax_amount": "8.00", "total_amount": "108.021"},
            AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
        ),
        ({"total_amount": "100.02"}, None),
        ({"total_amount": "0.00"}, AlertCode.AMOUNT_TOTAL_MISMATCH),
    ],
)
def test_source_document_summary_modes(summary: dict, code: AlertCode | None) -> None:
    (result,) = validate_batch([_line()], [_bundle(**summary)])

    reason = "单据无汇总行，免汇总对账"
    assert (reason in result.ai_reasoning) is (not summary)
    if code is None:
        assert result.operation is OperationType.CREATE
        assert result.validation_alerts == []
    else:
        _assert_review(result, code)


def test_printed_zero_total_passes_when_explicit_debits_and_credits_offset() -> None:
    lines = [_line("debit"), _line("credit", is_credit=True, charge_item="Rebate")]

    result = validate_batch(lines, [_bundle(total_amount="0.00")])

    assert [line.amount for line in result] == [Decimal("100.00"), Decimal("-100.00")]
    assert all(line.operation is OperationType.CREATE for line in result)


@pytest.mark.parametrize("sign", ["", "-"])
def test_all_credit_document_accepts_both_printed_summary_signs(sign: str) -> None:
    line = _line(is_credit=True, amount="-40.00", raw_explicit_amount="40.00")
    summaries = [
        {"total_amount": f"{sign}40.00"},
        {
            "subtotal_amount": f"{sign}40.00",
            "tax_amount": f"{sign}2.00",
            "total_amount": f"{sign}42.00",
        },
        {"totals_by_currency": {"USD": f"{sign}40.00"}},
    ]

    for summary in summaries:
        (result,) = validate_batch([line], [_bundle(**summary)])
        assert result.operation is OperationType.CREATE
        assert result.validation_alerts == []


def test_currency_totals_are_independent_and_do_not_offset_one_another() -> None:
    lines = [_line("usd"), _line("eur", currency="EUR", amount="60.00")]
    balanced = _bundle(totals_by_currency={"USD": "100.02", "EUR": "59.98"})
    off = _bundle(totals_by_currency={"USD": "101.00", "EUR": "59.00"})

    assert all(line.operation is OperationType.CREATE for line in validate_batch(lines, [balanced]))
    for result in validate_batch(lines, [off]):
        _assert_review(result, AlertCode.AMOUNT_TOTAL_MISMATCH)
        assert {alert.field for alert in result.validation_alerts} == {
            "totals_by_currency.USD",
            "totals_by_currency.EUR",
        }


@pytest.mark.parametrize("declare_subtotals", [True, False])
def test_multicurrency_totals_include_the_tax_for_each_currency(declare_subtotals: bool) -> None:
    lines = [
        _line("usd", amount="30", issuer=ENTITY),
        _line("eur", currency="EUR", amount="40", issuer=ENTITY),
    ]
    subtotals = {"USD": "30", "EUR": "40"} if declare_subtotals else {}
    taxes = {"USD": "3", "EUR": "4"}
    balanced = _bundle(
        subtotals_by_currency=subtotals,
        taxes_by_currency=taxes,
        totals_by_currency={"USD": "33", "EUR": "44"},
    )
    off = _bundle(
        subtotals_by_currency=subtotals,
        taxes_by_currency=taxes,
        totals_by_currency={"USD": "30", "EUR": "40"},
    )

    results = validate_batch(lines, [balanced])
    assert all(row.review_status is ReviewStatus.AUTO_READY for row in results)
    for row in results:
        assert row.source_doc_totals is not None
        assert row.source_doc_totals.subtotals_by_currency == {
            currency: Decimal(amount) for currency, amount in subtotals.items()
        }
        assert row.source_doc_totals.taxes_by_currency == {"USD": Decimal("3"), "EUR": Decimal("4")}
    detached = [
        ProcessedBillingRecord.model_validate_json(row.model_dump_json()) for row in results
    ]
    rechecked, _ = recalculate_for_entity(detached, ENTITY)
    assert all(row.operation is OperationType.CREATE for row in rechecked)

    code = (
        AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH
        if declare_subtotals
        else AlertCode.AMOUNT_TOTAL_MISMATCH
    )
    for row in validate_batch(lines, [off]):
        _assert_review(row, code)
        assert {(alert.field, alert.actual) for alert in row.validation_alerts} == {
            ("totals_by_currency.USD", "33"),
            ("totals_by_currency.EUR", "44"),
        }


def test_each_currency_subtotal_is_checked_even_without_grand_totals() -> None:
    lines = [_line("usd", amount="30"), _line("eur", currency="EUR", amount="40")]
    summary = _bundle(subtotals_by_currency={"USD": "30.02", "EUR": "40.021"})

    for row in validate_batch(lines, [summary]):
        _assert_review(row, AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH)
        assert [alert.field for alert in row.validation_alerts] == ["subtotals_by_currency.EUR"]
        assert "单据无汇总行，免汇总对账" not in row.ai_reasoning


def test_unallocated_total_cannot_combine_different_legal_currencies() -> None:
    lines = [_line("usd"), _line("eur", currency="EUR", amount="60.00")]

    for result in validate_batch(lines, [_bundle(total_amount="160.00")]):
        _assert_review(result, AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY)


def test_partial_update_inherited_amount_stays_out_of_explicit_source_total() -> None:
    notice = _line("notice", reference_no="NOTICE-1", amount=None, currency=None, payment_term="CC")
    explicit = _line(
        "new", reference_no="NOTICE-1", charge_item="Documentation Fee", amount="12.00"
    )
    matched = match_and_inherit_batch([notice, explicit], [_bill(payment_term="PP")])

    current, created = validate_batch(matched, [_bundle(total_amount="12.00")])

    assert current.amount == Decimal("30.00")
    assert current.raw_explicit_amount is None
    assert current.operation is OperationType.UPDATE
    assert created.operation is OperationType.CREATE
    assert current.validation_alerts == created.validation_alerts == []


@pytest.mark.parametrize("has_primary_record", [True, False])
def test_same_hash_keeps_attachment_and_both_source_paths(has_primary_record: bool) -> None:
    digest = "a" * 64
    flat = _bundle("flat", filename="bill.pdf", digest=digest, total_amount="100")
    root = _bundle("eml", filename="message.eml", source_kind="EML")
    child = _bundle(
        "child", filename="bill.pdf", parent_doc_id="eml", digest=digest, total_amount="100"
    )
    if not has_primary_record:
        child = child.model_copy(update={"extraction": None})
    bundles = [flat, root, child]
    paths = ["message.eml/bill.pdf", "bill.pdf"]

    kept, merged = deduplicate_same_source_documents(bundles)
    assert {bundle.document.doc_id for bundle in kept} == {"eml", "child"}
    assert merged == {"child": paths}

    lines = [_line("direct", "flat", reference_no="INV-1")]
    if has_primary_record:
        lines.append(_line("attached", "child", reference_no="INV-1"))
    (result,) = validate_batch(match_and_inherit_batch(lines, []), bundles)

    assert result.id == ("attached" if has_primary_record else "direct")
    assert result.source_doc_id == result.source_evidence.source_doc_id == "child"
    assert result.source_evidence.source_paths == paths
    assert result.ai_reasoning == ["已归并同批重复提交的同源单据：message.eml/bill.pdf、bill.pdf"]
    assert result.operation is OperationType.CREATE
    assert all(line.source_evidence.source_paths == [] for line in lines)
    assert validate_batch([result], bundles) == [result]


def test_same_name_sources_keep_distinct_paths_when_the_mail_wrapper_is_absent() -> None:
    flat = _bundle("flat", filename="bill.pdf", digest="a" * 64)
    child = _bundle("child", filename="bill.pdf", digest="a" * 64, parent_doc_id="mail")

    assert deduplicate_same_source_documents([flat, child]) == (
        [child],
        {"child": ["邮件附件/bill.pdf", "bill.pdf"]},
    )


def test_merging_source_paths_strips_queries_and_fragments_before_creating_audit_reason() -> None:
    signed = "https://example.test/file.pdf?token=private-key#private-fragment"
    path = "https://example.test/file.pdf"
    flat = _bundle("flat", digest="a" * 64, public_url=signed)
    attachment = _bundle("attachment", parent_doc_id="mail", digest="a" * 64)
    mail = _bundle("mail", filename="mail.eml", source_kind="EML")
    bundles = [flat, attachment, mail]
    assert deduplicate_same_source_documents(bundles)[1] == {
        "attachment": ["mail.eml/attachment.pdf", path]
    }
    rows = [
        _line("flat-line", "flat", source_evidence={"source_paths": [signed]}),
        _line("attachment-line", "attachment", source_evidence={"source_file": signed}),
    ]

    (result,) = validate_batch(rows, bundles)

    assert result.source_evidence.source_paths == ["mail.eml/attachment.pdf", path]
    assert result.source_evidence.source_file == path
    assert all("private" not in reason for reason in result.ai_reasoning)


def test_hashless_urls_are_never_grouped_even_if_their_urls_match() -> None:
    bundles = [
        _bundle(doc_id, source_kind="URL", public_url="https://example.com/bill.pdf")
        for doc_id in ("remote-1", "remote-2")
    ]

    assert deduplicate_same_source_documents(bundles) == (bundles, {})
    results = validate_batch(
        [_line(doc_id, doc_id) for doc_id in ("remote-1", "remote-2")], bundles
    )
    assert [result.operation for result in results] == [OperationType.CREATE, OperationType.CREATE]


@pytest.mark.parametrize(
    ("first", "second", "total", "duplicates"),
    [
        ("65.00", "35.00", "100.00", False),
        ("35.00", "35.00", "70.00", False),
        ("35.00", "35.00", "35.00", True),
        ("35.00", "35.00", "35.02", True),
        ("35.00", "35.00", "35.03", False),
        ("35.00", "35.00", "20.00", False),
        ("35.00", "35.00", "105.00", False),
    ],
)
def test_repeated_charge_requires_an_excess_of_about_one_line(
    first: str,
    second: str,
    total: str,
    duplicates: bool,
) -> None:
    lines = [
        _line("a", amount=first, source_evidence={"page": 1, "line_number": 7}),
        _line("b", amount=second, source_evidence={"page": 1, "line_number": 8}),
    ]

    for result in validate_batch(lines, [_bundle(total_amount=total)]):
        if duplicates:
            _assert_review(result, AlertCode.DUPLICATE_LINE_EXTRACTED)
        else:
            assert AlertCode.DUPLICATE_LINE_EXTRACTED not in _codes(result)
        if total in ("70.00", "100.00"):
            assert result.operation is OperationType.CREATE


def test_repeated_explicit_source_location_blocks_even_if_total_balances() -> None:
    evidence = {"page": 2, "line_number": 4}
    lines = [
        _line("a", amount="65", source_evidence=evidence),
        _line("b", amount="35", source_evidence=evidence),
    ]

    for result in validate_batch(lines, [_bundle(total_amount="100")]):
        _assert_review(result, AlertCode.DUPLICATE_LINE_EXTRACTED)


@pytest.mark.parametrize(
    "count, subtotal, duplicated",
    [(2, "35.00", True), (3, "35.04", True), (3, "35.041", False), (2, "20", False)],
)
def test_duplicate_attribution_uses_subtotal_and_number_of_excess_copies(
    count: int, subtotal: str, duplicated: bool
) -> None:
    records = [_line(str(index), amount="35.00") for index in range(count)]
    for result in validate_batch(records, [_bundle(subtotal_amount=subtotal)]):
        _assert_review(result, AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH)
        assert (AlertCode.DUPLICATE_LINE_EXTRACTED in _codes(result)) == duplicated


def test_duplicate_group_cannot_mix_currency_when_only_one_currency_exceeds_its_total() -> None:
    rows = [_line("usd", amount="35"), _line("eur", amount="35", currency="EUR")]
    for result in validate_batch(rows, [_bundle(totals_by_currency={"USD": "0", "EUR": "35"})]):
        _assert_review(result, AlertCode.AMOUNT_TOTAL_MISMATCH)
        assert AlertCode.DUPLICATE_LINE_EXTRACTED not in _codes(result)


@pytest.mark.parametrize(
    "field, first, second",
    [("payment_term", "PP", "CC"), ("debit_credit", "DEBIT", "UNKNOWN")],
)
def test_duplicate_charge_attribution_keeps_distinct_terms_and_directions_separate(
    field: str, first: str, second: str
) -> None:
    rows = [_line("a", amount="35", **{field: first}), _line("b", amount="35", **{field: second})]
    for result in validate_batch(rows, [_bundle(subtotal_amount="35")]):
        _assert_review(result, AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH)
        assert _codes(result) == {AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH}


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("amount", "100.021"),
        ("currency", "EUR"),
        ("payment_term", "CC"),
    ],
)
@pytest.mark.parametrize("source_layout", ["siblings", "body_and_attachment", "independent"])
def test_cross_document_conflicts_block_both_affected_records(
    field: str,
    other: str,
    source_layout: str,
) -> None:
    parent = "mail-a" if source_layout != "independent" else None
    bundles = [
        _bundle(
            "source-a",
            parent_doc_id=parent,
            source_kind="EML_BODY" if source_layout == "body_and_attachment" else "PDF",
        ),
        _bundle("source-b", parent_doc_id=parent),
    ]
    if parent:
        bundles.insert(0, _bundle(parent, source_kind="EML"))
    fields = {"reference_no": "INV-42", "payment_term": "PP"}
    first = _line("a", "source-a", **fields)
    second = _line("b", "source-b", **(fields | {field: other}))

    for result in validate_batch([first, second], bundles):
        _assert_review(result, AlertCode.CROSS_DOCUMENT_CONFLICT)
        (alert,) = result.validation_alerts
        assert alert.field == field


@pytest.mark.parametrize(
    "refs",
    [
        {"mbl_nos": [{"value": "MBL-42", "source": "original"}]},
        {"hbl_no": "HBL-42"},
        {"booking_no": "BOOKING-42"},
    ],
)
def test_shared_transport_reference_detects_conflict_without_an_invoice_number(refs: dict) -> None:
    records = [
        _line("a", "first", shipment_refs=refs),
        _line("b", "second", amount="110", shipment_refs=refs),
    ]

    for result in validate_records(records):
        _assert_review(result, AlertCode.CROSS_DOCUMENT_CONFLICT)


def test_blank_references_and_within_tolerance_amounts_do_not_create_conflicts() -> None:
    records = [
        _line("a", "first", reference_no="  ", shipment_refs={"booking_no": " "}),
        _line("b", "second", reference_no="", amount="200"),
        _line("c", "third", reference_no="INV-1"),
        _line("d", "fourth", reference_no="inv-1", amount="100.02"),
    ]

    assert all(record.operation is OperationType.CREATE for record in validate_records(records))


def test_two_sources_with_same_set_of_legitimate_same_name_lines_do_not_conflict() -> None:
    records = [
        _line(f"{doc_id}-{index}", doc_id, reference_no="DC-108478", amount=amount)
        for doc_id, amounts in (("first", ["65.00", "35.00"]), ("second", ["35.01", "64.99"]))
        for index, amount in enumerate(amounts)
    ]

    results = validate_batch(
        records, [_bundle(doc_id, total_amount="100") for doc_id in ("first", "second")]
    )

    assert all(record.operation is OperationType.CREATE for record in results)


def test_conflicting_repeated_line_is_not_hidden_by_one_equivalent_line() -> None:
    records = [
        _line("first-0", "first", reference_no="INV-1", amount="50"),
        _line("first-1", "first", reference_no="INV-1", amount="50"),
        _line("second-0", "second", reference_no="INV-1", amount="50"),
        _line("second-1", "second", reference_no="INV-1", amount="80"),
    ]

    disputed = [record for record in validate_batch(records) if record.validation_alerts]

    assert {record.id for record in disputed} == {"first-1", "second-1"}
    for record in disputed:
        _assert_review(record, AlertCode.CROSS_DOCUMENT_CONFLICT)


def test_compatible_lines_can_be_reassigned_at_the_tolerance_boundary() -> None:
    records = [
        _line(f"{doc_id}-{index}", doc_id, reference_no="INV-1", amount=amount)
        for doc_id, amounts in (("first", ["1.02", "1.00"]), ("second", ["1.02", "1.04"]))
        for index, amount in enumerate(amounts)
    ]

    assert all(record.operation is OperationType.CREATE for record in validate_batch(records))


def test_history_only_terms_and_amount_are_not_cross_document_evidence() -> None:
    matched = match_record(
        _line("notice", "first", reference_no="NOTICE-1", amount=None), [_bill(payment_term="PP")]
    )
    current = _line("invoice", "second", reference_no="NOTICE-1", amount="35", payment_term="CC")

    results = validate_batch([matched, current])

    assert all(AlertCode.CROSS_DOCUMENT_CONFLICT not in _codes(record) for record in results)
    assert results[0].operation is OperationType.NO_CHANGE


def test_package_with_different_mbls_keeps_evidence_including_metadata_only_attachment() -> None:
    first_mbl = {"value": "MBL-A", "source": "first.pdf p1"}
    second_mbl = {"value": "MBL-B", "source": "second.pdf p2"}
    bundles = [
        _bundle("mail", source_kind="EML"),
        _bundle("first", parent_doc_id="mail", shipment_refs={"mbl_nos": [first_mbl]}),
        _bundle("second", parent_doc_id="mail"),
        _bundle("body", parent_doc_id="mail", source_kind="EML_BODY"),
    ]
    records = [
        _line("charge", "second", shipment_refs={"mbl_nos": [second_mbl]}),
        _line("instruction", "body", charge_item="Documentation Fee"),
    ]

    results = validate_batch(records, bundles)

    for result in results:
        (alert,) = result.validation_alerts
        assert (alert.code, alert.severity) == (
            AlertCode.MULTI_SOURCE_BL_MISMATCH,
            AlertSeverity.WARNING,
        )
        assert {(ref.value, ref.source) for ref in result.shipment_refs.mbl_nos} == {
            ("MBL-A", "first.pdf p1"),
            ("MBL-B", "second.pdf p2"),
        }
        assert result.operation is OperationType.PENDING_REVIEW
    assert validate_batch(results, bundles) == results
    assert validate_batch(records, list(reversed(bundles))) == results


def test_package_warns_about_conflicting_hbls_and_clears_after_realignment() -> None:
    body = _bundle("body", parent_doc_id="mail", source_kind="EML_BODY")
    first = _bundle("first", parent_doc_id="mail", shipment_refs={"hbl_no": "HBL-A"})
    second = _bundle("second", parent_doc_id="mail", shipment_refs={"hbl_no": "HBL-B"})
    line = _line("instruction", "body")

    (checked,) = validate_batch([line], [body, first, second])

    (alert,) = checked.validation_alerts
    assert (alert.code, alert.severity, alert.field, alert.actual) == (
        AlertCode.MULTI_SOURCE_BL_MISMATCH,
        AlertSeverity.WARNING,
        "hbl_no",
        "HBL-A、HBL-B",
    )
    assert checked.operation is OperationType.PENDING_REVIEW

    realigned = _bundle("second", parent_doc_id="mail", shipment_refs={"hbl_no": " ｈｂｌ - ａ "})
    (fixed,) = validate_batch([checked], [body, first, realigned])
    assert fixed.validation_alerts == [] and fixed.operation is OperationType.CREATE


def test_detached_recalculation_keeps_both_package_conflicts_and_the_source_total() -> None:
    body = _bundle("body", parent_doc_id="mail", source_kind="EML_BODY", total_amount="40")
    attachments = [
        _bundle(
            doc_id,
            parent_doc_id="mail",
            shipment_refs={
                "hbl_no": f"HBL-{doc_id}",
                "mbl_nos": [{"value": f"MBL-{doc_id}", "source": f"{doc_id}.pdf"}],
            },
        )
        for doc_id in ("A", "B")
    ]
    source = _line("instruction", "body", amount="30", issuer=ENTITY, recipient="Buyer")
    (checked,) = validate_batch([source], [body, *attachments])
    expected = {
        (AlertCode.AMOUNT_TOTAL_MISMATCH, "total_amount", "30"),
        (AlertCode.MULTI_SOURCE_BL_MISMATCH, "mbl_nos", "MBL-A、MBL-B"),
        (AlertCode.MULTI_SOURCE_BL_MISMATCH, "hbl_no", "HBL-A、HBL-B"),
    }
    assert {
        (alert.code, alert.field, alert.actual) for alert in checked.validation_alerts
    } == expected
    detached = ProcessedBillingRecord.model_validate_json(checked.model_dump_json())
    assert {refs.source_doc_id for refs in detached.package_sibling_refs} == {"body", "A", "B"}

    for new_entity in (ENTITY, "Buyer"):
        (recalculated,), _ = recalculate_for_entity([detached], new_entity)
        assert recalculated.validation_alerts == checked.validation_alerts
        assert recalculated.source_doc_totals == checked.source_doc_totals
        assert recalculated.shipment_refs.mbl_nos == checked.shipment_refs.mbl_nos
        assert serialize_records([recalculated], "official") == []

    legacy = detached.model_copy(update={"package_sibling_refs": [], "source_doc_totals": None})
    (recalculated,), _ = recalculate_for_entity([legacy], ENTITY)
    assert recalculated.validation_alerts == checked.validation_alerts


def test_detached_package_reconciles_a_corrected_hbl_using_original_sibling_evidence() -> None:
    first = _bundle("first", parent_doc_id="mail", shipment_refs={"hbl_no": "HBL-A"})
    second = _bundle("second", parent_doc_id="mail", shipment_refs={"hbl_no": "HBL-B"})
    source = _line("instruction", "second", issuer=ENTITY, shipment_refs={"hbl_no": "HBL-B"})
    (checked,) = validate_batch([source], [first, second])
    edited = checked.model_copy(update={"human_overrides": {"hbl_no": "HBL-A"}})

    (recalculated,), _ = recalculate_for_entity([edited], ENTITY)

    assert recalculated.validation_alerts == []
    assert recalculated.operation is OperationType.CREATE
    assert recalculated.shipment_refs.hbl_no == "HBL-A"
    assert recalculated.package_sibling_refs == checked.package_sibling_refs


def test_shared_fee_checks_for_conflicts_between_mail_packages() -> None:
    digest = sha256(b"shared-fee").hexdigest()
    bundles = [
        bundle
        for mail in ("a", "b")
        for bundle in (
            _bundle(f"fee-{mail}", parent_doc_id=mail, digest=digest),
            _bundle(f"bl-{mail}", parent_doc_id=mail, shipment_refs={"hbl_no": f"HBL-{mail}"}),
        )
    ]
    records = [
        _line(
            mail,
            f"fee-{mail}",
            issuer=ENTITY,
            shipment_refs={"hbl_no": f"HBL-{mail}"},
            package_sibling_refs=[{"source_doc_id": f"fee-{mail}", "parent_doc_id": mail}],
        )
        for mail in ("a", "b")
    ]

    (checked,) = validate_batch(records, bundles)

    assert {source.parent_doc_id for source in checked.package_sibling_refs} == {"a", "b"}
    (conflict,) = checked.validation_alerts
    assert (conflict.code, conflict.field, conflict.actual) == (
        AlertCode.MULTI_SOURCE_BL_MISMATCH, "hbl_no", "HBL-A、HBL-B",
    )
    assert checked.operation is OperationType.PENDING_REVIEW
    assert serialize_records([checked], "official") == []
    detached = ProcessedBillingRecord.model_validate_json(checked.model_dump_json())
    recalculated, reset = recalculate_for_entity([detached], ENTITY)
    assert reset == 0 and recalculated[0].validation_alerts == [conflict]
    assert serialize_records(recalculated, "official") == []
