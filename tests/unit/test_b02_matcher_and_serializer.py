import json
from decimal import Decimal, localcontext
from functools import partial
from itertools import permutations
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

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
    PaymentTerm,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    ShipmentRefs,
    SourceEvidence,
    SourceRefValue,
    ValidationAlert,
    match_and_inherit_batch,
    match_record,
    recalculate_for_entity,
    serialize_export_bundle,
    serialize_records,
    serialize_records_json,
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
    )
    first_baseline = _bill(
        reference_no="DN-202609-001", payment_term="PP", external_bill_key="DN-202609-001-HC"
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
        assert result.raw_explicit_amount is None
        assert result.inherited_fields == {"amount": None, "currency": None, "posting_type": None}
    assert updated.operation is OperationType.UPDATE
    assert updated.field_diffs == [
        FieldDiff(
            field="payment_term", old_value=PaymentTerm.PP, new_value=PaymentTerm.CC, changed=True
        )
    ]
    assert repeated.operation is OperationType.NO_CHANGE
    assert repeated.field_diffs == []
    assert source.amount is source.currency is source.posting_type is None


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
    rejected = _line("rejected", review_status="REJECTED")
    needs_review = created.model_copy(
        update={"id": "status-pending", "review_status": ReviewStatus.NEEDS_REVIEW}
    )
    records = [created, updated, unchanged, pending, rejected, needs_review]

    items = serialize_records(records, mode)
    export = serialize_export_bundle(records, mode)
    assert export == {
        "mode": mode,
        "total_records": 6,
        "included_count": 3 if mode == "official" else 5,
        "excluded_pending_count": 2 if mode == "official" else 0,
        "excluded_rejected_count": 1,
        "items": items,
    }
    output_validator.validate(items)
    assert [item["operation"] for item in items] == ["CREATE", "UPDATE", "NO_CHANGE"] + (
        [] if mode == "official" else ["PENDING_REVIEW", "CREATE"]
    )
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
