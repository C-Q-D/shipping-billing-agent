import json
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from functools import partial
from itertools import permutations
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shipping_billing_agent.agent import run_billing_pipeline
from shipping_billing_agent.domain import (
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    ExcludedClause,
    ExistingBillRecord,
    ExportMode,
    FieldDiff,
    HumanReviewAction,
    MatchLevel,
    OperationType,
    ParsedDocument,
    PaymentTerm,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    ShipmentRefs,
    SourceEvidence,
    SourceRefValue,
    ValidationAlert,
    dumps_decimal_json,
    match_and_inherit_batch,
    match_record,
    recalculate_for_entity,
    serialize_export_bundle,
    serialize_records,
    serialize_records_json,
    validate_record,
)

ENTITY = "NEPTUNE SHIPPING LIMITED"
RECIPIENT = "ALL AMERICA FORWARDING"
_STANDARD_KEYS = {
    "operation",
    "document_info",
    "transaction_party",
    "charge_details",
    "financial_attributes",
    "audit_trail",
}


def _bill(bill_id: str = "old", **fields) -> ExistingBillRecord:
    return ExistingBillRecord.model_validate(
        {
            "id": bill_id,
            "accounting_entity": ENTITY,
            "charge_item": "Handling Charge",
            "amount": "30.00",
            "currency": "USD",
            "posting_type": "AR",
            **fields,
        }
    )


def _line(line_id: str = "new", **fields) -> ProcessedBillingRecord:
    return ProcessedBillingRecord.model_validate(
        {
            "id": line_id,
            "source_doc_id": "document",
            "accounting_entity": ENTITY,
            "charge_item": "Handling Charge",
            **fields,
        }
    )


def _assert_ambiguous(record: ProcessedBillingRecord, expected_ids: list[str]) -> None:
    assert record.operation is OperationType.PENDING_REVIEW
    assert record.review_status is ReviewStatus.NEEDS_REVIEW
    assert record.match_level is MatchLevel.AMBIGUOUS_MULTI
    assert record.matched_existing_bill_id is record.matched_bill_ref is None
    assert [candidate.id for candidate in record.candidate_bills] == expected_ids
    assert [(alert.code, alert.severity) for alert in record.validation_alerts] == [
        (AlertCode.AMBIGUOUS_EXISTING_BILL, AlertSeverity.BLOCKER)
    ]


def test_dn_202609_001_payment_only_update_vs_already_updated_baseline() -> None:
    source = _line(
        reference_no="DN-202609-001",
        charge_item="H/C",
        explicit_fields={"reference_no", "raw_charge_name", "payment_term"},
        payment_term="CC",
        validation_alerts=[ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "尚无交易双方")],
    )
    first_baseline = _bill(
        reference_no="DN-202609-001",
        payment_term="PP",
        external_bill_key="DN-202609-001-HC",
        issuer=ENTITY,
        recipient=RECIPIENT,
    )
    second_baseline = first_baseline.model_copy(
        update={"payment_term": PaymentTerm.CC, "version": 2}
    )

    updated = match_record(source, [first_baseline])
    repeated = match_record(updated, [second_baseline])
    for result in (updated, repeated):
        assert result.match_level is MatchLevel.L1_DOC_CHARGE
        assert result.review_status is ReviewStatus.AUTO_READY
        assert result.matched_existing_bill_id == first_baseline.id
        assert result.matched_bill_ref == "DN-202609-001-HC"
        assert result.amount == Decimal("30.00")
        assert result.currency == "USD"
        assert result.posting_type is PostingType.AR
        assert (result.issuer, result.recipient) == (ENTITY, RECIPIENT)
        assert result.validation_alerts == []
        assert result.raw_explicit_amount is None
        assert result.inherited_fields == dict.fromkeys(
            ("amount", "currency", "posting_type", "issuer", "recipient")
        )
    assert updated.operation is OperationType.UPDATE
    assert updated.field_diffs == [
        FieldDiff(
            field="payment_term", old_value=PaymentTerm.PP, new_value=PaymentTerm.CC, changed=True
        )
    ]
    assert repeated.operation is OperationType.NO_CHANGE
    assert repeated.field_diffs == []
    assert source.amount is source.currency is source.posting_type is None


@pytest.mark.parametrize("term, operation", [("PP", "UPDATE"), ("CC", "NO_CHANGE")])
def test_pipeline_inherits_omitted_parties_and_historical_amounts_without_blocking(
    term: str, operation: str
) -> None:
    doc = ParsedDocument(
        doc_id="notice",
        original_filename="notice.eml",
        sanitized_filename="notice.eml",
        source_kind="EML_BODY",
        extraction_channel="EML_TEXT_PARSER",
        status="PARSED",
        content_sha256="a" * 64,
        raw_text="Debit Note DN-202609-001\nCharge: Handling Charge\nChange payment term to CC",
    )
    old = _bill(
        reference_no="DN-202609-001",
        issuer="Vendor",
        recipient="Buyer",
        accounting_entity="Vendor",
        quantity="2",
        unit_price="10",
        payment_term=term,
    )

    (record,) = run_billing_pipeline([doc], "Vendor", existing_bills=[old]).records

    assert record.explicit_fields.isdisjoint(
        {"issuer", "recipient", "payer_hint", "payee_hint", "amount", "currency"}
    )
    assert (record.issuer, record.recipient, record.posting_type) == ("Vendor", "Buyer", "AR")
    assert (record.operation, record.review_status) == (operation, ReviewStatus.AUTO_READY)
    assert record.raw_explicit_amount is None and record.amount == Decimal("30.00")
    assert record.validation_alerts == []
    (item,) = serialize_records([record])
    assert item["operation"] == operation


@pytest.mark.parametrize(
    "parties, overrides",
    [
        ({"issuer": "Other Vendor", "recipient": "Other Buyer"}, {}),
        ({"payee_hint": "Other Vendor", "payer_hint": "Other Buyer"}, {}),
        ({"issuer": "Other Vendor"}, {"posting_type": "AR"}),
        ({}, {"issuer": None}),
        ({}, {"posting_type": "unrecognized"}),
    ],
)
def test_matched_notice_keeps_actual_party_conflicts_or_invalid_manual_posting(
    parties: dict, overrides: dict
) -> None:
    source = _line(
        reference_no="DN-202609-001",
        payment_term="CC",
        **parties,
        explicit_fields={"reference_no", "payment_term", *parties},
        human_overrides=overrides,
        validation_alerts=[ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "主体未匹配")],
    )
    old = _bill(reference_no=source.reference_no, issuer=ENTITY, recipient=RECIPIENT)

    matched = match_record(source, [old])
    validated = validate_record(matched)

    assert matched.matched_existing_bill_id == old.id
    assert matched.operation is validated.operation is OperationType.PENDING_REVIEW
    assert any(alert.code is AlertCode.ENTITY_NOT_MATCHED for alert in validated.validation_alerts)
    assert serialize_records([validated]) == []


def test_historical_bill_search_never_crosses_registered_atlas_branches() -> None:
    uk = AccountingEntityProfile(canonical_name="Atlas Shipping (UK)", aliases=["Atlas London"])
    hk = AccountingEntityProfile(canonical_name="Atlas Shipping (HK)", aliases=["Atlas Hong Kong"])
    profiles = [uk, hk]
    bills = [
        _bill("uk", accounting_entity=uk.canonical_name, reference_no="shared"),
        _bill("hk", accounting_entity=hk.aliases[0], reference_no="shared"),
    ]

    for profile, other, expected in ((uk, hk, "uk"), (hk, uk, "hk")):
        source = _line(accounting_entity=profile.canonical_name, reference_no="shared")
        assert match_record(source, bills, profiles).matched_existing_bill_id == expected
        foreign = [
            bill
            for bill in bills
            if bill.accounting_entity in {other.canonical_name, *other.aliases}
        ]
        assert match_record(source, foreign, profiles).matched_existing_bill_id is None


@pytest.mark.parametrize("new_amount", [None, "30.00", "-30.00"])
def test_matching_inherits_credit_note_and_recalculates_amount_sign(new_amount: str | None) -> None:
    old = _bill(
        reference_no="CN-1", document_type="Credit Note", amount="-30.00", payment_term="PP"
    )
    source = _line(
        reference_no="CN-1",
        amount=new_amount,
        payment_term="CC",
        explicit_fields={"reference_no", "payment_term", "amount"},
    )

    record = validate_record(match_record(source, [old]))

    assert record.document_type == "Credit Note"
    assert record.is_credit and record.amount == Decimal("-30.00")
    assert record.operation is OperationType.UPDATE and record.validation_alerts == []


def test_dc_108478_exact_and_changed_lines_keep_distinct_references_under_every_bill_order() -> (
    None
):
    records = [
        _line("changed", reference_no="DC-108478", amount="70.00", currency="USD"),
        _line("exact", reference_no="DC-108478", amount="35.0", currency="USD"),
    ]
    bills = [
        _bill("z-65", reference_no="DC-108478", amount="65.00"),
        _bill("a-35", reference_no="DC-108478", amount="35.00", external_bill_key="DC-108478-35"),
        _bill("b-foreign", reference_no="DC-108478", amount="35", accounting_entity=RECIPIENT),
    ]
    expected = match_and_inherit_batch(records, bills)
    for bill_order in permutations(bills):
        assert match_and_inherit_batch(records, bill_order) == expected

    changed, exact = expected
    assert changed.operation is OperationType.UPDATE
    assert changed.field_diffs == [
        FieldDiff(
            field="amount", old_value=Decimal("65.00"), new_value=Decimal("70.00"), changed=True
        )
    ]
    assert exact.operation is OperationType.NO_CHANGE
    assert exact.field_diffs == []
    assert [(line.matched_existing_bill_id, line.matched_bill_ref) for line in expected] == [
        ("z-65", "z-65"),
        ("a-35", "DC-108478-35"),
    ]
    assert [line.raw_explicit_amount for line in expected] == [Decimal("70.00"), Decimal("35.0")]


@pytest.mark.parametrize("source_count, old_count", [(2, 1), (1, 2), (2, 2)])
def test_duplicate_amounts_block_the_whole_unpaired_group(
    source_count: int, old_count: int
) -> None:
    records = [
        _line(str(index), reference_no="notice", amount="30.0") for index in range(source_count)
    ]
    bills = [_bill(f"old-{index}", reference_no="notice") for index in range(old_count)]
    for result in match_and_inherit_batch(records, list(reversed(bills))):
        _assert_ambiguous(result, [bill.id for bill in bills])


def test_l1_ambiguity_does_not_fall_to_an_exact_l2_candidate() -> None:
    source = _line(
        reference_no="notice",
        shipment_refs=ShipmentRefs(hbl_no="HBL-1"),
        amount="70",
        currency="USD",
    )
    bills = [
        _bill("b", reference_no="notice", amount="65"),
        _bill("a", reference_no="notice", amount="35"),
        _bill("lower", hbl_no="HBL-1", amount="70"),
    ]
    result = match_record(source, bills)
    _assert_ambiguous(result, ["a", "b"])
    assert all(
        candidate.match_level is MatchLevel.L1_DOC_CHARGE for candidate in result.candidate_bills
    )


def test_l1_pairs_remaining_rows_within_each_explicit_currency() -> None:
    bills = [
        _bill("exact", reference_no="notice", amount="35"),
        _bill("usd", reference_no="notice", amount="65"),
        _bill("eur", reference_no="notice", amount="35", currency="EUR"),
    ]
    records = [
        _line("euros", reference_no="notice", amount="90", currency="EUR"),
        _line("unchanged", reference_no="notice", amount="35.00", currency="USD"),
        _line("dollars", reference_no="notice", amount="70", currency="USD"),
    ]
    results = match_and_inherit_batch(records, bills)
    assert [(row.matched_existing_bill_id, row.operation) for row in results] == [
        ("eur", OperationType.UPDATE),
        ("exact", OperationType.NO_CHANGE),
        ("usd", OperationType.UPDATE),
    ]


@pytest.mark.parametrize("use_bl", [True, False], ids=["L2", "L3"])
def test_omitted_currency_with_multiple_legacy_currencies_stops_before_amount_pairing(
    use_bl: bool,
) -> None:
    refs = ShipmentRefs(
        mbl_nos=[
            SourceRefValue(value=" ", source="源单"),
            SourceRefValue(value="ｍｂｌ-01", source="源单"),
        ]
        if use_bl
        else [],
        booking_no="booking 1",
        container_nos=["MRSU2571577"],
    )
    source = _line(
        shipment_refs=refs,
        amount="30.00",
        issuer=ENTITY,
        recipient=RECIPIENT,
        document_date="2026-10-24",
    )
    usd_bill = _bill(
        "z-usd",
        external_bill_key="old-usd",
        mbl_no="MBL-01" if use_bl else None,
        booking_no="BOOKING1",
        container_nos=["MRSU2571577"],
        document_date="2026-10-24",
        issuer=ENTITY,
        recipient=RECIPIENT,
    )
    eur_bill = _bill(
        "a-eur",
        currency="EUR",
        amount="99.00",
        mbl_no="MBL-01" if use_bl else None,
        booking_no=None if use_bl else "BOOKING1",
    )
    bills = [usd_bill, eur_bill]
    level = MatchLevel.L2_BL_CHARGE_CURR if use_bl else MatchLevel.L3_BOOKING_CHARGE_CURR

    result = match_record(source, bills)
    _assert_ambiguous(result, ["a-eur", "z-usd"])
    assert result.currency is None
    assert all(candidate.match_level is level for candidate in result.candidate_bills)
    shipment_label = "MBL-01" if use_bl else "BOOKING1"
    assert [candidate.label for candidate in result.candidate_bills] == [
        f"a-eur | {shipment_label} | Handling Charge | 99.00 EUR | -",
        f"old-usd | {shipment_label} | Handling Charge | 30.00 USD | 2026-10-24",
    ]

    explicit = match_record(source.model_copy(update={"currency": "USD"}), bills)
    assert explicit.matched_existing_bill_id == usd_bill.id
    assert explicit.match_level is level
    assert explicit.operation is OperationType.NO_CHANGE


@pytest.mark.parametrize(
    "field, level",
    [("hbl_no", MatchLevel.L2_BL_CHARGE_CURR), ("booking_no", MatchLevel.L3_BOOKING_CHARGE_CURR)],
)
def test_only_one_currency_can_be_inherited_for_bl_or_booking(
    field: str, level: MatchLevel
) -> None:
    source = _line(shipment_refs=ShipmentRefs.model_validate({field: " ref-1 "}))
    bill = _bill(**{field: "REF-1", "charge_item": " Handling Fee "})
    result = match_record(source, [bill])
    assert result.match_level is level
    assert result.operation is OperationType.NO_CHANGE
    assert result.currency == "USD"
    assert result.inherited_fields["currency"] is None
    assert result.raw_explicit_amount is None

    invalid = source.model_copy(update={"raw_invalid_fields": {"currency": "ZZZ"}})
    blocked = match_record(invalid, [bill])
    assert blocked.match_level is MatchLevel.NONE
    assert blocked.matched_existing_bill_id is None
    assert blocked.operation is OperationType.PENDING_REVIEW
    assert blocked.validation_alerts[0].code is AlertCode.INVALID_OR_MISSING_CURRENCY


@pytest.mark.parametrize("blank", [None, "", " \t "])
def test_missing_references_are_never_equal_matching_keys(blank: str | None) -> None:
    tokens = [blank] if blank is not None else []
    source = _line(
        reference_no=blank,
        issuer=ENTITY,
        recipient=RECIPIENT,
        document_date="2026-10-24",
        amount="30.00",
        currency="USD",
        shipment_refs=ShipmentRefs(
            mbl_nos=[SourceRefValue(value=token, source="源单") for token in tokens],
            hbl_no=blank,
            booking_no=blank,
            container_nos=tokens,
        ),
    )
    bill = _bill(
        reference_no=blank,
        mbl_no=blank,
        hbl_no=blank,
        booking_no=blank,
        document_date="2026-10-24",
        issuer=ENTITY,
        recipient=RECIPIENT,
        container_nos=tokens,
    )
    result = match_record(source, [bill])
    assert result.operation is OperationType.CREATE
    assert result.match_level is MatchLevel.NONE


@pytest.mark.parametrize(
    "amount, matched", [("9007199254740991.03", True), ("9007199254740991.0301", False)]
)
def test_l4_requires_both_parties_date_and_container_with_exact_tolerance(
    amount: str, matched: bool
) -> None:
    source = _line(
        shipment_refs=ShipmentRefs(container_nos=[" mrsu2571577 "]),
        issuer=f"{ENTITY.lower()} (LAX)",
        recipient=RECIPIENT,
        document_date="2026/10/24",
        amount=amount,
        currency="USD",
    )
    bill = _bill(
        container_nos=["MRSU2571577"],
        issuer=ENTITY,
        recipient=RECIPIENT,
        document_date="2026-10-24",
        amount="9007199254740991.01",
    )
    other_bill = bill.model_copy(update={"id": "a-wrong-party", "recipient": "OTHER COMPANY"})
    with localcontext() as context:
        context.prec = 6
        result = match_record(source, [other_bill, bill])
    assert result.match_level is (MatchLevel.L4_CNTR_PARTY_DATE_AMT if matched else MatchLevel.NONE)
    assert result.operation is (OperationType.UPDATE if matched else OperationType.CREATE)
    assert result.matched_existing_bill_id == (bill.id if matched else None)
    assert [diff.field for diff in result.field_diffs] == (["amount"] if matched else [])


def test_human_target_and_force_new_take_precedence_and_keep_bills_scoped() -> None:
    profiles = [AccountingEntityProfile(canonical_name=ENTITY, aliases=["NEPTUNE"])]
    bills = [
        _bill(
            "a", accounting_entity="NEPTUNE", reference_no="notice", amount="30", payment_term="PP"
        ),
        _bill("b", reference_no="notice", amount="35"),
        _bill("foreign", reference_no="notice", accounting_entity=RECIPIENT, amount="30"),
    ]
    explicit = _line(
        "picked",
        reference_no="notice",
        amount="32",
        explicit_fields={"reference_no", "amount"},
        human_overrides={
            "matched_existing_bill_id": "a",
            "amount": "30.00",
            "payment_term": None,
        },
    )
    other = _line(
        "other",
        reference_no="notice",
        amount="37",
        human_overrides={"matched_existing_bill_id": "foreign"},
    )
    forced = _line(
        "forced",
        reference_no="notice",
        amount="35",
        human_overrides={
            "force_create_new": True,
            "matched_existing_bill_id": "b",
        },
    )

    picked, matched, created = match_and_inherit_batch([explicit, other, forced], bills, profiles)
    assert picked.matched_existing_bill_id == "a"
    assert picked.raw_explicit_amount == Decimal("32")
    assert picked.amount == Decimal("30.00")
    assert picked.payment_term is None
    assert [diff.field for diff in picked.field_diffs] == ["payment_term"]
    assert picked.operation is OperationType.UPDATE
    assert matched.matched_existing_bill_id == "b"
    assert matched.operation is OperationType.UPDATE
    assert created.matched_existing_bill_id is None
    assert created.operation is OperationType.CREATE
    assert match_record(explicit, [bills[2]], profiles).operation is OperationType.CREATE


def test_human_choice_resolves_matching_ambiguity_on_retry() -> None:
    bills = [_bill("a", reference_no="notice"), _bill("b", reference_no="notice", amount="35")]
    source = _line(reference_no="notice", amount="80")
    ambiguous = match_record(source, bills)
    _assert_ambiguous(ambiguous, ["a", "b"])

    chosen = ambiguous.model_copy(update={"human_overrides": {"matched_existing_bill_id": "b"}})
    result = match_record(chosen, bills)
    assert result.matched_existing_bill_id == "b"
    assert result.operation is OperationType.UPDATE
    assert result.validation_alerts == []


@pytest.mark.parametrize("higher_priority", ["manual", "L1"])
def test_same_document_reserves_bill_before_a_lower_level_can_claim_it(
    higher_priority: str,
) -> None:
    party_fields = dict(issuer=ENTITY, recipient=RECIPIENT, document_date="2026-10-24")
    old = _bill(reference_no="notice", container_nos=["JXLU4504463"], **party_fields)
    higher = _line(
        "higher",
        reference_no="notice",
        human_overrides={"matched_existing_bill_id": old.id} if higher_priority == "manual" else {},
    )
    lower = _line(
        "lower",
        charge_item="Storage Charge",
        amount="30.01",
        currency="USD",
        shipment_refs=ShipmentRefs(container_nos=["JXLU4504463"]),
        **party_fields,
    )
    independent = lower.model_copy(update={"id": "independent", "source_doc_id": "another"})

    for rows in ([higher, lower, independent], [independent, lower, higher]):
        by_id = {row.id: row for row in match_and_inherit_batch(rows, [old])}
        assert by_id["higher"].matched_existing_bill_id == old.id
        _assert_ambiguous(by_id["lower"], [])
        assert by_id["independent"].matched_existing_bill_id == old.id
        assert by_id["independent"].match_level is MatchLevel.L4_CNTR_PARTY_DATE_AMT


def test_same_level_competing_groups_are_ambiguous_and_show_business_reference() -> None:
    party_fields = dict(issuer=ENTITY, recipient=RECIPIENT, document_date="2026-10-24")
    old = _bill(
        reference_no="DN-RECEIVED",
        external_bill_key="external-key",
        container_nos=["JXLU4504463"],
        **party_fields,
    )
    lines = [
        _line(
            charge,
            charge_item=charge,
            amount="30",
            currency="USD",
            shipment_refs=ShipmentRefs(container_nos=["JXLU4504463"]),
            **party_fields,
        )
        for charge in ("Storage Charge", "Trucking Charge")
    ]
    for line in match_and_inherit_batch(lines, [old]):
        _assert_ambiguous(line, [old.id])
        assert line.candidate_bills[0].label.startswith("DN-RECEIVED | ")


@pytest.mark.parametrize("source_amount", ["32.00", None])
def test_human_amount_override_does_not_change_the_original_amount(
    source_amount: str | None,
) -> None:
    bill = _bill(reference_no="other-notice")
    source = _line(
        reference_no="notice",
        amount=source_amount,
        human_overrides={
            "amount": "30",
            "matched_existing_bill_id": bill.id,
        },
    )
    result = match_record(source, [bill])
    assert result.matched_existing_bill_id == bill.id
    assert result.amount == Decimal("30")
    assert result.raw_explicit_amount == (
        Decimal(source_amount) if source_amount is not None else None
    )
    assert match_record(result, [bill]) == result


@pytest.mark.parametrize(
    "field, raw, old_value, code",
    [
        ("currency", "ZZZ", "USD", AlertCode.INVALID_OR_MISSING_CURRENCY),
        ("document_date", "2026-02-30", "2026-02-28", AlertCode.INVALID_DOCUMENT_DATE),
    ],
)
def test_raw_invalid_fields_cannot_be_replaced_by_legal_history(
    field: str,
    raw: str,
    old_value: str,
    code: AlertCode,
) -> None:
    issue = ValidationAlert.from_code(code, "原始单据值非法", field=field, actual=raw)
    source = _line(
        reference_no="notice",
        raw_invalid_fields={field: raw},
        raw_parse_issues=[issue],
        explicit_fields={"reference_no", field},
        **{field: old_value},
    )
    result = match_record(source, [_bill(reference_no="notice", **{field: old_value})])
    assert result.matched_existing_bill_id == "old"
    assert result.match_level is MatchLevel.L1_DOC_CHARGE
    assert result.amount == Decimal("30.00")
    assert result.raw_explicit_amount is None
    assert getattr(result, field) is None
    assert field not in result.inherited_fields
    assert result.raw_invalid_fields == {field: raw}
    assert result.raw_parse_issues == result.validation_alerts == [issue]
    assert result.operation is OperationType.PENDING_REVIEW
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


@pytest.mark.parametrize("resolved", [True, False])
def test_matcher_blocks_on_unresolved_warnings(resolved: bool) -> None:
    warning = ValidationAlert.from_code(AlertCode.OCR_SUSPICIOUS_FLAG, "OCR 内容待复核")
    if resolved:
        warning = warning.model_copy(update={"severity": AlertSeverity.RESOLVED})
    source = _line(reference_no="notice", validation_alerts=[warning])
    result = match_record(source, [_bill(reference_no="notice")])
    assert result.validation_alerts == [warning]
    assert result.operation is (
        OperationType.NO_CHANGE if resolved else OperationType.PENDING_REVIEW
    )
    assert result.review_status is (
        ReviewStatus.AUTO_READY if resolved else ReviewStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize("use_alias", [False, True])
def test_entity_recalculation_restores_source_before_matching_the_new_ledger(
    use_alias: bool,
) -> None:
    profiles = (
        [AccountingEntityProfile(canonical_name=RECIPIENT, aliases=["AAF"])] if use_alias else []
    )
    source = _line(reference_no="notice", issuer=ENTITY, recipient=RECIPIENT)
    old = _bill("ar", reference_no="notice", mbl_no="OLD-BL", issuer=ENTITY, recipient=RECIPIENT)
    new = _bill(
        "ap",
        reference_no="notice",
        mbl_no="NEW-BL",
        issuer=ENTITY,
        recipient=RECIPIENT,
        accounting_entity="AAF" if use_alias else RECIPIENT,
        amount="65.00",
        currency="EUR",
        posting_type="AP",
    )
    previous = match_record(source, [old, new]).model_copy(
        update={"review_status": ReviewStatus.APPROVED}
    )
    assert previous.shipment_refs.mbl_nos[0].value == "OLD-BL"

    (current,), reset_count = recalculate_for_entity(
        [previous],
        RECIPIENT,
        profiles,
        [old, new],
        matcher_fn=partial(match_and_inherit_batch, profiles=profiles)
        if use_alias
        else match_and_inherit_batch,
    )
    assert reset_count == 1
    assert current.matched_existing_bill_id == "ap"
    assert current.shipment_refs.mbl_nos[0].value == "NEW-BL"
    assert current.amount == Decimal("65.00")
    assert current.currency == "EUR"
    assert current.posting_type is PostingType.AP
    assert current.raw_explicit_amount is None
    assert current.operation is OperationType.NO_CHANGE


@pytest.fixture(scope="module")
def output_validator() -> Draft202012Validator:
    schema_path = (
        Path(__file__).resolve().parents[2] / "docs/workflow/contracts/billing-output.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("mode", ["official", "full_standard", "audit"])
def test_three_export_modes_pass_the_real_schema_and_keep_exact_json_numbers(
    mode: ExportMode,
    output_validator: Draft202012Validator,
) -> None:
    created = _line(
        "created",
        operation="CREATE",
        review_status="AUTO_READY",
        amount="9007199254740991.01",
        unit_price="0.0025",
        currency="USD",
        posting_type="AR",
        ai_reasoning=["", " "],
    )
    update_source = _line(
        "updated",
        reference_no="notice",
        amount="40.00",
        shipment_refs=ShipmentRefs(booking_no="B-1"),
        source_evidence=SourceEvidence(source_file="notice.pdf", extraction_channel="PLTPLT_OCR"),
        validation_alerts=[
            ValidationAlert(
                code=AlertCode.OCR_SUSPICIOUS_FLAG, severity="RESOLVED", message="已核对 OCR 文本"
            )
        ],
        excluded_clauses=[ExcludedClause(text="逾期条款", reason="费用区外")],
        human_overrides={"values": {"amount": Decimal("0.0025"), "payment_term": PaymentTerm.CC}},
        human_review_history=[
            HumanReviewAction(
                action="APPROVED",
                operator="reviewer",
                timestamp="2026-11-19T08:00:00Z",
                overrides={"amount": Decimal("40.00")},
            )
        ],
        retry_count=1,
    )
    updated = match_record(
        update_source, [_bill(reference_no="notice", external_bill_key="bill-ref")]
    )
    updated = updated.model_copy(update={"review_status": ReviewStatus.APPROVED})
    unchanged = match_record(
        _line("unchanged", reference_no="notice"), [_bill(reference_no="notice")]
    )
    pending = _line(
        "pending",
        currency="ZZZ",
        document_date="2026-02-30",
        charge_item=None,
        review_status="APPROVED",
    )
    rejected = _line(
        "rejected",
        operation="UPDATE",
        review_status="REJECTED",
        currency="ZZZ",
        raw_invalid_fields={"currency": "ZZZ"},
        reject_reason="原始单据作废",
    )
    needs_review = created.model_copy(
        update={"id": "status-pending", "review_status": ReviewStatus.NEEDS_REVIEW}
    )
    records = [created, updated, unchanged, pending, rejected, needs_review]

    items = serialize_records(records, mode)
    export = serialize_export_bundle(records, mode)
    assert export == {
        "mode": mode,
        "total_records": 6,
        "included_count": {"official": 3, "full_standard": 5, "audit": 6}[mode],
        "excluded_pending_count": 2 if mode == "official" else 0,
        "excluded_rejected_count": int(mode != "audit"),
        "items": items,
    }
    output_validator.validate(items)
    assert [item["operation"] for item in items] == ["CREATE", "UPDATE", "NO_CHANGE"] + {
        "official": [],
        "full_standard": ["PENDING_REVIEW", "CREATE"],
        "audit": ["PENDING_REVIEW", "PENDING_REVIEW", "CREATE"],
    }[mode]
    for item in items:
        assert set(item) == _STANDARD_KEYS
        assert item["audit_trail"]["ai_reasoning"]
        assert all(reason.strip() for reason in item["audit_trail"]["ai_reasoning"])
    assert items[1]["audit_trail"]["matched_bill_ref"] == "bill-ref"
    if mode != "official":
        assert items[3]["charge_details"]["currency"] is None
        assert items[3]["document_info"]["document_date"] is None

    encoded = serialize_records_json(records, mode).encode("utf-8")
    assert b'"amount":9007199254740991.01' in encoded
    assert b'"unit_price":0.0025' in encoded
    decoded = json.loads(encoded, parse_float=Decimal)
    output_validator.validate(decoded)
    assert (
        decoded[0]["charge_details"]["amount"].as_tuple()
        == Decimal("9007199254740991.01").as_tuple()
    )
    assert decoded[0]["charge_details"]["unit_price"].as_tuple() == Decimal("0.0025").as_tuple()

    trail = decoded[1]["audit_trail"]
    if mode == "audit":
        rejected_item = decoded[4]
        assert rejected_item["audit_trail"]["review_status"] == "REJECTED"
        assert rejected_item["audit_trail"]["reject_reason"] == "原始单据作废"
        assert rejected_item["audit_trail"]["raw_invalid_fields"] == {"currency": "ZZZ"}
        assert rejected_item["charge_details"]["currency"] is None
        assert trail["record_id"] == "updated"
        assert trail["source_doc_id"] == "document"
        assert trail["review_status"] == "APPROVED"
        assert trail["match_level"] == "L1_DOC_CHARGE"
        assert trail["matched_existing_bill_id"] == "old"
        assert trail["shipment_refs"]["booking_no"] == "B-1"
        assert trail["source_evidence"]["extraction_channel"] == "PLTPLT_OCR"
        assert trail["field_diffs"] == [
            {
                "field": "amount",
                "old_value": Decimal("30.00"),
                "new_value": Decimal("40.00"),
                "changed": True,
            }
        ]
        assert trail["validation_alerts"][0]["severity"] == "RESOLVED"
        assert trail["excluded_clauses"][0]["text"] == "逾期条款"
        assert trail["human_overrides"]["values"] == {
            "amount": Decimal("0.0025"),
            "payment_term": "CC",
        }
        assert trail["human_review_history"][0]["overrides"]["amount"] == Decimal("40.00")
        assert trail["reject_reason"] is None
        assert trail["retry_count"] == 1
        assert b'"old_value":30.00,"new_value":40.00' in encoded
    else:
        assert "field_diffs" not in trail


def test_update_export_uses_a_real_historical_id_when_the_reference_is_unset() -> None:
    record = _line(
        operation="UPDATE",
        review_status="AUTO_READY",
        posting_type="AR",
        amount="30",
        currency="USD",
        matched_existing_bill_id="old-id",
    )
    (item,) = serialize_records([record])
    assert item["audit_trail"]["matched_bill_ref"] == "old-id"
    with pytest.raises(ValueError, match="缺少历史账单引用"):
        serialize_records([record.model_copy(update={"matched_existing_bill_id": None})])


def test_audit_serializes_dates_and_invalid_numbers_without_invalid_json(
    output_validator: Draft202012Validator,
) -> None:
    corrected_on = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    record = _line(
        posting_type="AR",
        currency="USD",
        review_status="AUTO_READY",
        operation="CREATE",
        human_overrides={"metadata": {"date": date(2026, 11, 19), "raw_amount": Decimal("NaN")}},
        field_diffs=[
            FieldDiff(
                field="date", old_value=Decimal("Infinity"), new_value=corrected_on, changed=True
            )
        ],
    ).model_copy(
        update={
            "amount": Decimal("NaN"),
            "quantity": "malformed",
            "unit_price": Decimal("-Infinity"),
        }
    )
    with pytest.raises(ValueError):
        serialize_records_json([record], "official")
    with pytest.raises(ValueError, match="有限"):
        dumps_decimal_json(Decimal("NaN"))

    pending = validate_record(record)
    assert pending.operation is OperationType.PENDING_REVIEW
    for mode in ("full_standard", "audit"):
        data = json.loads(serialize_records_json([pending], mode))
        output_validator.validate(data)
        (item,) = data
        assert {
            field: item["charge_details"][field] for field in ("amount", "quantity", "unit_price")
        } == dict.fromkeys(("amount", "quantity", "unit_price"))
    audit = item["audit_trail"]
    assert audit["human_overrides"]["metadata"] == {"date": "2026-11-19", "raw_amount": "NaN"}
    assert audit["field_diffs"][0]["old_value"] == "Infinity"
    assert audit["field_diffs"][0]["new_value"] == corrected_on.isoformat()


def test_export_strips_signed_query_and_fragment_from_both_source_locations() -> None:
    signed = "https://example.test/invoice.pdf?signature=synthetic-secret#private-evidence"
    path = "https://example.test/invoice.pdf"
    record = _line(
        currency="USD",
        amount="30",
        posting_type="AR",
        review_status="AUTO_READY",
        operation="CREATE",
        source_evidence=SourceEvidence(
            source_file=signed, source_paths=[signed, "Mail.eml/bill#2.pdf"]
        ),
    )
    for mode in ("official", "full_standard", "audit"):
        encoded = serialize_records_json([record], mode)
        assert "synthetic-secret" not in encoded and "private-evidence" not in encoded
        assert json.loads(encoded)[0]["audit_trail"]["source_file"] == path
    assert json.loads(encoded)[0]["audit_trail"]["source_evidence"]["source_paths"] == [
        path,
        "Mail.eml/bill#2.pdf",
    ]
