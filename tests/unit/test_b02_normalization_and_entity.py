from __future__ import annotations

import ast
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from shipping_billing_agent.domain import CREDIT_DOCUMENT_PATTERN, is_credit_document_type
from shipping_billing_agent.domain.charge_normalizer import (
    normalize_charge_item,
    normalize_document_date,
    normalize_payment_term,
)
from shipping_billing_agent.domain.entity_resolver import (
    entity_matches,
    normalize_party_name,
    recalculate_for_entity,
    resolve_entity_perspective,
)
from shipping_billing_agent.domain.models import (
    ALERT_SEVERITY_MAP,
    DECIMAL_TOLERANCE,
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    BatchPipelineResult,
    CandidateBillSummary,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    ExistingBillRecord,
    FailedDocumentAlert,
    MatchLevel,
    OperationType,
    ParsedDocument,
    PaymentTerm,
    PostingType,
    ProcessedBillingRecord,
    RawDocumentExtraction,
    ReviewStatus,
    ValidationAlert,
    dumps_decimal_json,
    loads_decimal_json,
    parse_llm_raw_extraction,
)


@pytest.mark.parametrize(
    ("standard_name", "aliases"),
    [
        (
            "Handling Charge",
            [
                "H/C",
                "hc",
                "Handling",
                "Handling Fee",
                "H/D Fee",
                "H/D Fee Rebate",
                "HANDLING CHARGE",
            ],
        ),
        ("Documentation Fee", ["doc", "doc fee", "Documentation Fee"]),
        (
            "Terminal Handling Charge",
            ["THC", "terminal handling", "Terminal Handling Fee", "Terminal Handling Charge"],
        ),
        ("Trucking Charge", ["trucking", "Trucking Fee", "Trucking Charge"]),
        ("Customs Clearance Fee", ["customs clearance", "Customs Fee", "Customs Clearance Fee"]),
        ("Storage Charge", ["storage", "Storage Fee", "Storage Charge"]),
        ("Wharfage", ["WHARFAGE"]),
        ("Wharfage Administration Fee", ["Wharfage admin fee", "WHARFAGE ADMINISTRATION FEE"]),
        (
            "Detention Free Time Charge",
            [
                "4 ADD'L DETENTION FREE TIME CHARGE",
                "4 Add’l Detention Free Time Charge",
                "Add'l Detention Free Time Charge",
                "Detention Free Time Charge",
                "Detention Charge",
            ],
        ),
        ("On Carriage Additional", ["On Carriage Additional - US Em", "On Carriage Additional"]),
    ],
)
def test_charge_dictionary_maps_the_exam_and_sample_aliases(
    standard_name: str, aliases: list[str]
) -> None:
    for alias in aliases:
        assert normalize_charge_item(alias) == (standard_name, True), alias


def test_charge_normalizer_strips_generic_count_prefixes_and_lane_suffixes() -> None:
    assert normalize_charge_item("7 Add'l Detention Free Time Charge") == (
        "Detention Free Time Charge",
        True,
    )
    assert normalize_charge_item("On Carriage Additional - EU WB") == (
        "On Carriage Additional",
        True,
    )
    assert normalize_charge_item("7 Add'l Detention Free Time Charge - EU WB") == (
        "Detention Free Time Charge",
        True,
    )
    assert normalize_charge_item("7 Fuel Adjustment - EU WB") == (
        "7 Fuel Adjustment - Eu Wb",
        False,
    )


def test_custom_charge_alias_takes_precedence_and_unknown_name_is_kept() -> None:
    custom = ChargeDictionaryItem(raw_alias=" … HANDLING\tFee ! ", standard_name="Agent Fee")
    assert normalize_charge_item(" (Handling   fee), ", [custom]) == ("Agent Fee", True)
    assert normalize_charge_item(" (Handling   fee), ") == ("Handling Charge", True)
    assert normalize_charge_item("\t— FUEL  ADJUSTMENT —") == ("Fuel Adjustment", False)
    assert normalize_charge_item("  --...  ") == (None, False)
    assert normalize_charge_item(None) == (None, False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("P", PaymentTerm.PP),
        ("PP", PaymentTerm.PP),
        ("Prepaid", PaymentTerm.PP),
        (" Freight\tprepaid  ", PaymentTerm.PP),
        ("C", PaymentTerm.CC),
        ("CC", PaymentTerm.CC),
        ("Collect", PaymentTerm.CC),
        ("FREIGHT COLLECT", PaymentTerm.CC),
        ("PP or CC", None),
        ("PREPAID / COLLECT", None),
        ("PREPAID?", None),
        ("NET 30", None),
        (None, None),
    ],
)
def test_payment_term_uses_only_unambiguous_tokens(
    raw: str | None, expected: PaymentTerm | None
) -> None:
    assert normalize_payment_term(raw) is expected


@pytest.mark.parametrize("raw", ["2026-10-24", "10/24/2026", "10-24-2026", " 2026/10/24 "])
def test_date_formats_normalize_to_iso(raw: str) -> None:
    assert normalize_document_date(raw) == ("2026-10-24", None)


@pytest.mark.parametrize("raw", ["2026-02-30", "2026-13-01", "02/29/2026", "2026-10/24", ""])
def test_explicit_invalid_dates_produce_a_reason(raw: str) -> None:
    date, reason = normalize_document_date(raw)
    assert date is None and reason
    assert normalize_document_date(None) == (None, None)
    assert normalize_document_date("02/29/2024") == ("2024-02-29", None)


def test_alert_codes_have_fixed_severity_until_resolved() -> None:
    assert DECIMAL_TOLERANCE == Decimal("0.02")
    assert set(ALERT_SEVERITY_MAP) == set(AlertCode)
    assert len(AlertCode) == 17
    assert {
        code for code, severity in ALERT_SEVERITY_MAP.items() if severity == AlertSeverity.WARNING
    } == {
        AlertCode.CONTAINER_ISO6346_MISMATCH,
        AlertCode.OCR_SUSPICIOUS_FLAG,
        AlertCode.MULTI_SOURCE_BL_MISMATCH,
        AlertCode.CONTEXT_INFERRED_FIELD,
    }
    assert list(ALERT_SEVERITY_MAP.values()).count(AlertSeverity.BLOCKER) == 13
    with pytest.raises(ValidationError, match="告警严重度"):
        ValidationAlert(
            code=AlertCode.ENTITY_NOT_MATCHED,
            severity=AlertSeverity.WARNING,
            message="主体未匹配",
        )
    resolved = ValidationAlert(
        code=AlertCode.CONTEXT_INFERRED_FIELD, severity=AlertSeverity.RESOLVED, message="已确认"
    )
    assert resolved.severity is AlertSeverity.RESOLVED


def test_raw_json_keeps_invalid_fields_and_surviving_charges() -> None:
    result = parse_llm_raw_extraction(
        """{
            "reference_no": "DC-108478", "issuer": null, "document_date": "2026-02-30",
            "total_amount": 0.00, "total_currency": "ZZZ",
            "charges": [
                {"raw_charge_name": "H/C", "amount": "1.310.40", "unit_price": "1O0.00",
                 "quantity": null, "currency": "ZZZ", "payment_term": "C"},
                {"raw_charge_name": "Trucking", "currency": null, "payment_term": "collect"}
            ]
        }"""
    )

    assert result.extraction.document_date is None
    assert result.extraction.total_currency is None
    assert result.raw_invalid_fields == {"document_date": "2026-02-30", "total_currency": "ZZZ"}
    assert result.explicit_fields == {
        "reference_no",
        "document_date",
        "total_amount",
        "total_currency",
        "charges",
    }
    assert {alert.field: alert.code for alert in result.raw_parse_issues} == {
        "document_date": AlertCode.INVALID_DOCUMENT_DATE,
        "total_currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
    }
    assert (result.total_amount, result.has_explicit_total) == (Decimal("0.00"), True)

    invalid, valid = result.charges
    assert result.extraction.charges == [invalid.charge, valid.charge]
    assert invalid.charge.amount is invalid.charge.unit_price is invalid.charge.quantity is None
    assert invalid.charge.currency is None
    assert invalid.charge.payment_term is PaymentTerm.CC
    assert invalid.explicit_fields == {
        "raw_charge_name",
        "amount",
        "unit_price",
        "currency",
        "payment_term",
    }
    assert invalid.raw_invalid_fields == {
        "amount": "1.310.40",
        "unit_price": "1O0.00",
        "currency": "ZZZ",
    }
    assert invalid.raw_amount_text == "1.310.40"
    assert invalid.raw_unit_price_text == "1O0.00"
    assert invalid.raw_quantity_text is None
    assert {alert.field: alert.code for alert in invalid.raw_parse_issues} == {
        "amount": AlertCode.OCR_NUMERIC_CONFUSION,
        "unit_price": AlertCode.OCR_NUMERIC_CONFUSION,
        "currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
    }
    assert all(
        alert.severity is AlertSeverity.BLOCKER
        for alert in result.raw_parse_issues + invalid.raw_parse_issues
    )
    assert valid.explicit_fields == {"raw_charge_name", "payment_term"}
    assert valid.charge.amount is valid.charge.currency is None
    assert valid.charge.payment_term is PaymentTerm.CC
    assert valid.raw_parse_issues == []


def test_source_json_overrides_lossy_typed_fallback_and_keeps_decimal_totals() -> None:
    fallback = RawDocumentExtraction(total_amount="9007199254740991", document_date="2026-10-24")
    result = parse_llm_raw_extraction(
        """{
            "subtotal_amount": 9007199254740991.0100, "tax_amount": 0.0025,
            "total_amount": 9007199254740991.0125, "total_currency": "USD",
            "subtotals_by_currency": {"USD": 9007199254740991.0100},
            "taxes_by_currency": {"USD": 0.0025},
            "totals_by_currency": {"USD": 9007199254740991.0125}, "document_date": "10-24-2026",
            "charges": [{"raw_charge_name": "Wharfage", "amount": 9007199254740991.01,
                         "unit_price": 0.0025, "currency": " $ ",
                         "raw_amount_text": "9,007,199,254,740,991.01"}]
        }""",
        fallback,
    )
    assert result.extraction.document_date == "2026-10-24"
    assert result.subtotal_amount.as_tuple() == Decimal("9007199254740991.0100").as_tuple()
    assert result.tax_amount == Decimal("0.0025")
    assert result.total_amount == Decimal("9007199254740991.0125")
    assert result.subtotals_by_currency["USD"].as_tuple() == result.subtotal_amount.as_tuple()
    assert result.taxes_by_currency == {"USD": Decimal("0.0025")}
    assert result.totals_by_currency == {"USD": Decimal("9007199254740991.0125")}
    charge = result.charges[0]
    assert charge.charge.amount == Decimal("9007199254740991.01")
    assert charge.charge.unit_price == Decimal("0.0025")
    assert charge.charge.currency == "USD"
    assert charge.raw_amount_text == "9,007,199,254,740,991.01"
    assert charge.raw_unit_price_text == "0.0025"
    assert charge.raw_parse_issues[0].code is AlertCode.CONTEXT_INFERRED_FIELD


def test_bare_dollar_with_multiple_currency_candidates_stays_invalid() -> None:
    result = parse_llm_raw_extraction(
        '{"totals_by_currency":{"USD":50,"CAD":30},"charges":[{"raw_charge_name":"H/C","currency":"$"}]}'
    )
    assert result.has_explicit_total
    assert result.charges[0].charge.currency is None
    assert result.charges[0].raw_invalid_fields == {"currency": "$"}


def test_null_totals_are_omitted_and_metadata_only_is_explicit() -> None:
    result = parse_llm_raw_extraction(
        '{"total_amount":null,"totals_by_currency":{"USD":null},"charges":null,"metadata_only":true}'
    )
    assert result.metadata_only
    assert not result.has_explicit_total
    assert "total_amount" not in result.explicit_fields
    assert "charges" not in result.explicit_fields
    assert result.totals_by_currency == {}
    assert result.extraction.charges == []


def test_typed_fallback_keeps_explicit_source_values_and_preserves_missing_fields() -> None:
    parsed = RawDocumentExtraction.model_validate(
        {"reference_no": "DN-202609-001", "charges": [{"raw_charge_name": "H/C", "amount": None}]}
    )
    result = parse_llm_raw_extraction(None, parsed)
    assert result.extraction.reference_no == "DN-202609-001"
    assert result.explicit_fields == {"reference_no", "charges"}
    assert result.charges[0].explicit_fields == {"raw_charge_name"}
    assert result.charges[0].charge.amount is None


def test_valid_typed_value_cannot_override_malformed_raw_numeric_evidence() -> None:
    fallback = RawDocumentExtraction.model_validate(
        {"charges": [{"raw_charge_name": "H/C", "amount": "100.00"}]}
    )
    result = parse_llm_raw_extraction(
        '{"charges":[{"raw_charge_name":"H/C","amount":100.00,"raw_amount_text":"1O0.00"}]}',
        fallback,
    )
    (charge,) = result.charges
    assert charge.charge.amount is None
    assert "amount" in charge.explicit_fields
    assert charge.raw_amount_text == "1O0.00"
    assert charge.raw_invalid_fields == {"amount": "1O0.00"}
    assert [alert.code for alert in charge.raw_parse_issues] == [AlertCode.OCR_NUMERIC_CONFUSION]


@pytest.mark.parametrize("raw", [None, "", "{", "[]", '"text"', '{"total_amount": Infinity}'])
def test_unusable_raw_json_returns_a_failure_alert(raw: str | None) -> None:
    result = parse_llm_raw_extraction(raw)
    assert result.charges == []
    assert [alert.code for alert in result.raw_parse_issues] == [AlertCode.LLM_EXTRACTION_FAILED]


def test_wrong_field_types_are_audited_without_losing_other_fields() -> None:
    result = parse_llm_raw_extraction(
        '{"reference_no":[],"subtotal_amount":true,"charges":[null,'
        '{"raw_charge_name":"Handling","payment_term":"PP or CC","page":"unknown","amount":-0.01}]}'
    )
    assert result.raw_invalid_fields == {"reference_no": "[]", "subtotal_amount": "true"}
    assert {alert.field for alert in result.raw_parse_issues} == {
        "reference_no",
        "subtotal_amount",
        "charges.0",
    }
    assert result.extraction.reference_no is None
    assert result.subtotal_amount is None
    charge = result.charges[0]
    assert charge.charge.amount == Decimal("-0.01")
    assert charge.charge.payment_term is charge.charge.page is None
    assert charge.raw_invalid_fields == {"payment_term": "PP or CC", "page": "unknown"}


def test_shipment_refs_unpacks_value_objects_in_container_nos_and_keeps_invalid_checks() -> None:
    raw = """{
        "reference_no": "CMDUCHN4053601",
        "shipment_refs": {
            "mbl_nos": [{"value": "CMDUCHN4053601", "source": "page 1"}],
            "container_nos": [
                {"value": "SAPU2040689", "source": "page 1"},
                {"value": "JXLU4504463"},
                "MSCU6639870"
            ]
        },
        "charges": [
            {"raw_charge_name": "On Carriage Additional", "amount": "245.00", "currency": "USD"}
        ]
    }"""
    parsed = parse_llm_raw_extraction(raw)
    assert parsed.raw_invalid_fields == {}
    assert parsed.raw_parse_issues == []
    assert parsed.extraction.shipment_refs.container_nos == [
        "SAPU2040689",
        "JXLU4504463",
        "MSCU6639870",
    ]
    typed = RawDocumentExtraction.model_validate_json(raw)
    assert typed.shipment_refs.container_nos == ["SAPU2040689", "JXLU4504463", "MSCU6639870"]

    for bad_container in ({"source": "page 1"}, {"value": 123}, 123):
        invalid = parse_llm_raw_extraction(
            f'{{"shipment_refs": {{"container_nos": [{dumps_decimal_json(bad_container)}]}}, '
            '"charges": [{"raw_charge_name": "H/C", "amount": "10", "currency": "USD"}]}'
        )
        assert "shipment_refs" in invalid.raw_invalid_fields
        assert [alert.code for alert in invalid.raw_parse_issues] == [
            AlertCode.LLM_EXTRACTION_FAILED
        ]


def test_decimal_json_numbers_round_trip_byte_for_byte() -> None:
    raw = '{"amount":9007199254740991.01,"prices":[0.0025,1,-0.00],"note":"9007199254740991.01"}'
    loaded = loads_decimal_json(raw)
    assert loaded["amount"] == Decimal("9007199254740991.01")
    assert all(isinstance(price, Decimal) for price in loaded["prices"])
    assert loaded["prices"][0] == Decimal("0.0025")
    with localcontext() as context:
        context.prec = 6
        assert dumps_decimal_json(loaded).encode("utf-8") == raw.encode("utf-8")
        assert dumps_decimal_json(loads_decimal_json("2.5e-3")) == "0.0025"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_decimal_json_rejects_non_finite_numbers(token: str) -> None:
    with pytest.raises(ValueError):
        loads_decimal_json(f'{{"amount":{token}}}')
    with pytest.raises(ValueError):
        dumps_decimal_json({"amount": Decimal(token)})
    with pytest.raises(ValueError):
        dumps_decimal_json({"amount": float(token)})


ZIM = "ZIM AMERICAN INTEGRATED SHIPPING"
ALL_AMERICA = "ALL AMERICA FORWARDING"


@pytest.mark.parametrize(
    ("entity", "direction"), [(ZIM, PostingType.AR), (ALL_AMERICA, PostingType.AP)]
)
def test_zim_sample_resolves_both_accounting_perspectives(
    entity: str, direction: PostingType
) -> None:
    profiles = [
        AccountingEntityProfile(canonical_name=ZIM, aliases=[f"{ZIM} SERVICES"]),
        AccountingEntityProfile(canonical_name=ALL_AMERICA, aliases=[f"{ALL_AMERICA}, INC."]),
    ]
    result = resolve_entity_perspective(
        entity,
        issuer=f"{ZIM} Services",
        recipient=f"{ALL_AMERICA}, INC.",
        raw_amount=Decimal("26.90"),
        profiles=profiles,
    )
    assert result.posting_type is direction
    assert result.amount == Decimal("26.90")
    assert result.validation_alerts == []


def test_party_matching_does_not_use_unconfigured_short_names_or_ambiguous_aliases() -> None:
    neptune = "NEPTUNE SHIPPING LIMITED"
    assert normalize_party_name("NEPTUNE Shipping Limited, (LAX).") == (
        "neptune shipping limited (lax)"
    )
    assert entity_matches("Neptune Shipping Limited (LAX)", neptune)
    assert not entity_matches("NEPTUNE", neptune)
    assert not entity_matches("NEPTUNE SHIPPING HOLDINGS LIMITED", neptune)
    assert not entity_matches(None, neptune)
    assert not entity_matches("", "")
    profile = AccountingEntityProfile(canonical_name=neptune, aliases=["NEPTUNE"])
    other = AccountingEntityProfile(canonical_name="NEPTUNE GROUP", aliases=["NEPTUNE"])
    assert entity_matches("Neptune", neptune, [profile])
    assert entity_matches(neptune, "Neptune", [profile])
    assert not entity_matches("NEPTUNE", neptune, [profile, other])


def test_branch_fallback_requires_a_unique_compatible_configured_profile() -> None:
    uk = AccountingEntityProfile(canonical_name="Atlas Shipping (UK)", aliases=["Atlas London"])
    hk = AccountingEntityProfile(canonical_name="Atlas Shipping (HK)", aliases=["Atlas Hong Kong"])
    for profile in (uk, hk):
        assert entity_matches(profile.aliases[0], profile.canonical_name, [uk, hk])
        assert entity_matches("Atlas Shipping", profile.canonical_name, [profile])
        assert not entity_matches("Atlas Shipping", profile.canonical_name, [uk, hk])
    assert not entity_matches(uk.canonical_name, hk.canonical_name, [uk, hk])
    assert entity_matches("ATLAS Shipping (EU)", "Atlas Shipping (EU)", [uk, hk])
    assert not entity_matches("Atlas Shipping (EU)", uk.canonical_name, [uk, hk])
    assert not entity_matches(uk.canonical_name, hk.canonical_name, [uk])
    assert not entity_matches(uk.canonical_name, hk.canonical_name)
    single = AccountingEntityProfile(canonical_name="Atlas Shipping")
    assert entity_matches(hk.canonical_name, single.canonical_name, [single])
    assert not entity_matches(uk.canonical_name, hk.canonical_name, [single])
    registered_alias = uk.model_copy(update={"aliases": [hk.canonical_name]})
    assert entity_matches(hk.canonical_name, uk.canonical_name, [registered_alias])
    assert not entity_matches(hk.canonical_name, uk.canonical_name, [registered_alias, hk])


def test_explicit_payer_and_payee_take_precedence_over_issuer_and_recipient() -> None:
    profiles = [
        AccountingEntityProfile(canonical_name="SUN TRACK EXPRESS", aliases=["SHA"]),
        AccountingEntityProfile(canonical_name="TARGETING A INC", aliases=["TAR"]),
    ]
    parties = dict(
        issuer="SUN TRACK EXPRESS", recipient="TARGETING A INC", payer_hint="SHA", payee_hint="TAR"
    )
    assert (
        resolve_entity_perspective("SUN TRACK EXPRESS", profiles=profiles, **parties).posting_type
        is PostingType.AP
    )
    assert (
        resolve_entity_perspective("TARGETING A INC", profiles=profiles, **parties).posting_type
        is PostingType.AR
    )


def test_dc_108478_one_sided_payee_does_not_assume_the_consignee_pays() -> None:
    extraction = parse_llm_raw_extraction(
        '{"reference_no":"DC-108478","payee_hint":"NEPTUNE SHIPPING LIMITED (LAX)",'
        '"issuer":"COSCO","recipient":"ALL AMERICA FORWARDING"}'
    ).extraction
    parties = dict(
        issuer=extraction.issuer, recipient=extraction.recipient, payee_hint=extraction.payee_hint
    )
    neptune = resolve_entity_perspective("NEPTUNE SHIPPING LIMITED", **parties)
    assert neptune.posting_type is PostingType.AR
    assert neptune.validation_alerts == []
    for unrelated_entity in (ALL_AMERICA, "COSCO"):
        other = resolve_entity_perspective(unrelated_entity, **parties)
        assert other.posting_type is None
        assert other.operation is OperationType.PENDING_REVIEW
        assert [(alert.code, alert.severity) for alert in other.validation_alerts] == [
            (AlertCode.ENTITY_NOT_MATCHED, AlertSeverity.BLOCKER)
        ]


@pytest.mark.parametrize(
    ("document_type", "debit_credit", "raw_amount"),
    [
        ("Credit Note", "UNKNOWN", "40.00"),
        ("C/N", "UNKNOWN", "40.00"),
        ("贷项通知", "UNKNOWN", "-40.00"),
        ("Invoice", "CREDIT", "40.00"),
    ],
)
def test_credit_keeps_ledger_direction_and_uses_exact_negative_net_amount(
    document_type: str, debit_credit: str, raw_amount: str
) -> None:
    supplier_credit = resolve_entity_perspective(
        ALL_AMERICA,
        issuer=ZIM,
        recipient=ALL_AMERICA,
        document_type=document_type,
        debit_credit=debit_credit,
        raw_amount=Decimal(raw_amount),
    )
    assert supplier_credit.posting_type is PostingType.AP
    assert supplier_credit.is_credit
    assert supplier_credit.raw_amount_abs.as_tuple() == Decimal("40.00").as_tuple()
    assert supplier_credit.amount.as_tuple() == Decimal("-40.00").as_tuple()
    assert "贷项冲减：对应台账余额减少 40.00" in supplier_credit.ai_reasoning
    assert supplier_credit.validation_alerts == []


def test_credit_from_issuer_view_preserves_precision_even_with_a_low_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 6
        customer_credit = resolve_entity_perspective(
            ZIM,
            issuer=ZIM,
            recipient=ALL_AMERICA,
            document_type="Credit Note",
            raw_amount=Decimal("9007199254740991.01"),
        )
    assert customer_credit.posting_type is PostingType.AR
    assert customer_credit.amount == Decimal("-9007199254740991.01")


def test_negative_non_credit_amount_keeps_its_sign_and_blocks_the_record() -> None:
    result = resolve_entity_perspective(
        ALL_AMERICA,
        issuer=ZIM,
        recipient=ALL_AMERICA,
        document_type="Invoice",
        raw_amount=Decimal("-40.00"),
    )
    assert result.posting_type is PostingType.AP
    assert not result.is_credit
    assert result.amount == Decimal("-40.00")
    assert result.operation is OperationType.PENDING_REVIEW
    assert [(alert.code, alert.severity) for alert in result.validation_alerts] == [
        (AlertCode.NEGATIVE_NON_CREDIT_AMOUNT, AlertSeverity.BLOCKER)
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Credit Note",
        "Credit Memo",
        "Credit Invoice",
        "Credit Advice",
        "Credit Voucher",
        "Credit Adjustment",
        "C/N",
        "CN",
        "贷项通知",
        "贷项凭证",
        "贷项",
        "红字",
    ],
)
def test_credit_document_vocabulary_is_shared_with_title_extraction(title: str) -> None:
    assert is_credit_document_type(title)
    match = CREDIT_DOCUMENT_PATTERN.match(f"{title} REF-123")
    assert match is not None and match[0] == title
    result = resolve_entity_perspective(
        ZIM, issuer=ZIM, document_type=title, raw_amount=Decimal("30")
    )
    assert result.amount == Decimal("-30")


@pytest.mark.parametrize("document_type", ["Credit Card Invoice", "Letter of Credit"])
def test_credit_in_non_credit_titles_never_negates_a_charge(document_type: str) -> None:
    assert not is_credit_document_type(document_type)
    assert CREDIT_DOCUMENT_PATTERN.search(document_type) is None
    result = resolve_entity_perspective(
        ZIM, issuer=ZIM, document_type=document_type, raw_amount=Decimal("30")
    )
    assert result.posting_type is PostingType.AR
    assert not result.is_credit
    assert result.amount == Decimal("30")
    assert result.validation_alerts == []


def test_switching_entity_resets_approvals_links_and_inherited_values_before_rematching() -> None:
    old_bill = ExistingBillRecord(
        id="old-1",
        accounting_entity=ZIM,
        charge_item="Handling Charge",
        amount="30.00",
        currency="USD",
        posting_type=PostingType.AR,
    )
    new_bill = ExistingBillRecord(
        id="new-1",
        accounting_entity=ALL_AMERICA,
        charge_item="Handling Charge",
        amount="35.00",
        currency="USD",
        posting_type=PostingType.AP,
    )
    invalid_date = ValidationAlert.from_code(
        AlertCode.INVALID_DOCUMENT_DATE, "非法日期", field="document_date", actual="2026-02-30"
    )
    approved = ProcessedBillingRecord(
        id="record-1",
        source_doc_id="notice-1",
        accounting_entity=ZIM,
        issuer=ZIM,
        recipient=ALL_AMERICA,
        charge_item="Handling Charge",
        amount="30.00",
        currency="USD",
        document_date="2026-02-20",
        posting_type=PostingType.AR,
        operation=OperationType.UPDATE,
        review_status=ReviewStatus.APPROVED,
        matched_existing_bill_id="old-1",
        matched_bill_ref="previous-ref",
        match_level=MatchLevel.L1_DOC_CHARGE,
        candidate_bills=[
            CandidateBillSummary(
                id="old-1",
                label="旧账单 30.00 USD",
                charge_item="Handling Charge",
                amount="30.00",
                currency="USD",
            )
        ],
        raw_invalid_fields={"document_date": "2026-02-30"},
        raw_parse_issues=[invalid_date],
        validation_alerts=[invalid_date],
        inherited_fields={"amount": None, "currency": None, "document_date": None},
        human_overrides={
            "raw_charge_name": "Handling adjustment",
            "amount": "0.0025",
            "hbl_no": "HBL-REVISED",
            "posting_type": "AR",
            "matched_existing_bill_id": "old-1",
            "force_create_new": True,
        },
        human_review_history=[
            {"action": "APPROVED", "operator": "reviewer", "timestamp": "2026-11-19T08:00:00Z"}
        ],
    )
    rejected = approved.model_copy(
        update={
            "id": "record-2",
            "review_status": ReviewStatus.REJECTED,
            "reject_reason": "旧主体驳回",
            "human_overrides": {},
        }
    )
    calls = []

    def matcher(
        records: list[ProcessedBillingRecord], bills: list[ExistingBillRecord]
    ) -> list[ProcessedBillingRecord]:
        calls.append("matcher")
        assert bills == [new_bill]
        assert records[0].amount == Decimal("0.0025")
        assert records[0].raw_charge_name == "Handling adjustment"
        assert records[0].shipment_refs.hbl_no == "HBL-REVISED"
        assert records[0].human_overrides == {
            "raw_charge_name": "Handling adjustment",
            "amount": "0.0025",
            "hbl_no": "HBL-REVISED",
        }
        assert records[1].amount is None
        for record in records:
            assert record.accounting_entity == ALL_AMERICA
            assert record.review_status is ReviewStatus.NEEDS_REVIEW
            assert record.posting_type is PostingType.AP
            assert record.matched_existing_bill_id is record.matched_bill_ref is None
            assert record.match_level is MatchLevel.NONE
            assert record.candidate_bills == []
            assert record.inherited_fields == {}
            assert record.currency is record.document_date is record.reject_reason is None
            assert record.validation_alerts == [invalid_date]
        return [records[0], records[1].model_copy(update={"matched_existing_bill_id": bills[0].id})]

    def validator(records: list[ProcessedBillingRecord]) -> list[ProcessedBillingRecord]:
        calls.append("validator")
        assert records[1].matched_existing_bill_id == "new-1"
        assert records[0].validation_alerts == [invalid_date]
        return records

    updated, reset_count = recalculate_for_entity(
        [approved, rejected],
        ALL_AMERICA,
        existing_bills=[old_bill, new_bill],
        matcher_fn=matcher,
        validator_fn=validator,
    )
    assert calls == ["matcher", "validator"]
    assert reset_count == 2
    assert updated[0].matched_existing_bill_id is None
    assert updated[1].matched_existing_bill_id == "new-1"
    assert updated[0].human_review_history == approved.human_review_history
    assert approved.review_status is ReviewStatus.APPROVED
    assert approved.matched_existing_bill_id == "old-1"
    assert approved.amount == Decimal("30.00")
    assert rejected.reject_reason == "旧主体驳回"


def test_recalculation_without_callbacks_validates_credit_under_both_entities() -> None:
    previous = ProcessedBillingRecord(
        id="cn-1",
        source_doc_id="credit-note",
        accounting_entity=ZIM,
        issuer=ZIM,
        recipient=ALL_AMERICA,
        document_type="Credit Note",
        charge_item="Handling Charge",
        amount="-40.00",
        currency="USD",
        raw_explicit_amount="40.00",
        review_status=ReviewStatus.APPROVED,
        posting_type=PostingType.AR,
        matched_existing_bill_id="old-1",
    )
    for entity, direction in [(ZIM, PostingType.AR), (ALL_AMERICA, PostingType.AP)]:
        (current,), reset_count = recalculate_for_entity([previous], entity)
        assert reset_count == 1
        assert current.operation is OperationType.CREATE
        assert current.review_status is (
            ReviewStatus.NEEDS_REVIEW if entity == ZIM else ReviewStatus.AUTO_READY
        )
        assert current.requires_reapproval == (entity == ZIM)
        assert current.posting_type is direction
        assert current.is_credit
        assert current.amount == Decimal("-40.00")
        assert current.raw_explicit_amount == current.raw_amount_abs == Decimal("40.00")
        assert current.matched_existing_bill_id is None


def test_recalculating_same_entity_after_alias_removal_drops_old_posting_and_approval() -> None:
    profile = AccountingEntityProfile(canonical_name=ZIM, aliases=["ZIM US"])
    source = ProcessedBillingRecord(
        id="invoice",
        source_doc_id="invoice",
        accounting_entity=ZIM,
        issuer="ZIM US",
        recipient=ALL_AMERICA,
        charge_item="Handling Charge",
        amount="30",
        currency="USD",
    )
    (ready,), _ = recalculate_for_entity([source], ZIM, [profile])
    assert (ready.posting_type, ready.review_status) == (PostingType.AR, ReviewStatus.AUTO_READY)
    approved = ready.model_copy(update={"review_status": ReviewStatus.APPROVED})

    (blocked,), reset = recalculate_for_entity(
        [approved], ZIM, [profile.model_copy(update={"aliases": []})]
    )

    assert reset == 1
    assert blocked.posting_type is None
    assert blocked.operation is OperationType.PENDING_REVIEW
    assert [alert.code for alert in blocked.validation_alerts] == [AlertCode.ENTITY_NOT_MATCHED]


def test_document_bundle_and_failure_summary_keep_contract_fields() -> None:
    document = ParsedDocument(
        doc_id="doc-1",
        original_filename="test.pdf",
        sanitized_filename="test.pdf",
        source_kind="PDF",
        extraction_channel="FAILED",
        status="FAILED",
        content_sha256="a" * 64,
    )
    failure = FailedDocumentAlert(doc_id="doc-1", filename="test.pdf", reason="解析失败")
    result = BatchPipelineResult(
        accounting_entity=ZIM,
        documents=[DocumentExtractionBundle(document=document)],
        failed_document_alerts=[failure],
        failed_doc_ids=["doc-1"],
    )
    assert result.documents[0].document.doc_id == "doc-1"
    assert result.failed_document_alerts[0].reason == "解析失败"
    assert result.model_dump()["failed_document_alerts"] == [
        {
            "doc_id": "doc-1",
            "filename": "test.pdf",
            "error_message": "解析失败",
            "affected_record_ids": [],
        }
    ]


def test_domain_does_not_import_io_or_use_file_primitives() -> None:
    domain = Path(__file__).resolve().parents[2] / "src/shipping_billing_agent/domain"
    forbidden_modules = {
        "builtins",
        "ftplib",
        "http",
        "io",
        "os",
        "pathlib",
        "shutil",
        "socket",
        "sqlite3",
        "ssl",
        "subprocess",
        "tempfile",
        "urllib",
    }
    allowed_libraries = {"iso4217", "jsonschema", "pydantic", "shipping_billing_agent"}
    forbidden_calls = {"__import__", "open", "read_text", "read_bytes", "write_text", "write_bytes"}
    violations = []
    for source in sorted(domain.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "urllib.parse" and {alias.name for alias in node.names} <= {
                    "urlsplit",
                    "urlunsplit",
                }:
                    continue
                imports = [node.module]
            for imported in imports:
                root = imported.partition(".")[0]
                if (
                    root in forbidden_modules
                    or root not in sys.stdlib_module_names | allowed_libraries
                ):
                    violations.append((source.name, node.lineno, imported))
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", None)
                )
                if name in forbidden_calls:
                    violations.append((source.name, node.lineno, name))
    assert violations == []
