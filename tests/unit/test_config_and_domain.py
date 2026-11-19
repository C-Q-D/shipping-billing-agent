"""Contract tests for settings, resource limits, and pure domain models."""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture
def settings_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from shipping_billing_agent.config import settings

    monkeypatch.chdir(tmp_path)
    for field_name in settings.AppSettings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
        monkeypatch.delenv(field_name, raising=False)
    return settings


@pytest.fixture
def currencies():
    from shipping_billing_agent.domain import currency

    return currency


@pytest.fixture
def domain_models():
    from shipping_billing_agent.domain import models

    return models


@pytest.mark.parametrize(
    ("base_url", "effective_url", "uses_deepseek_key"),
    [
        ("  ", "https://api.deepseek.com/v1", True),
        ("HTTPS://API.DEEPSEEK.COM/v1", "HTTPS://API.DEEPSEEK.COM/v1", True),
        ("http://api.deepseek.com/v1", "http://api.deepseek.com/v1", False),
        ("https://llm.example.com/v1", "https://llm.example.com/v1", False),
        ("https://api.deepseek.com.example.com", "https://api.deepseek.com.example.com", False),
        ("//api.deepseek.com/v1", "//api.deepseek.com/v1", False),
        ("https://[invalid/v1", "https://[invalid/v1", False),
    ],
)
def test_deepseek_key_fallback_is_limited_to_approved_endpoint(
    settings_api, base_url: str, effective_url: str, uses_deepseek_key: bool
) -> None:
    settings = settings_api.AppSettings(llm_base_url=base_url, deepseek_api_key=" test-deepseek ")

    expected_key = "test-deepseek" if uses_deepseek_key else None
    assert settings.resolve_llm_credentials() == (effective_url, expected_key)
    assert settings.is_llm_configured() is uses_deepseek_key


def test_explicit_llm_key_is_used_for_a_custom_endpoint(settings_api) -> None:
    settings = settings_api.AppSettings(
        llm_base_url=" https://llm.example.com/v1 ",
        llm_api_key=" test-llm ",
        deepseek_api_key="test-deepseek",
    )

    assert settings.resolve_llm_credentials() == ("https://llm.example.com/v1", "test-llm")
    assert settings.is_llm_configured() is True


@pytest.mark.parametrize(
    ("ocr_proxy_url", "ocr_trust_env", "expected_direct_mode"),
    [
        ("", "false", True),
        ("http://ocr-proxy.example.com:8080", "false", False),
        ("", " yes ", False),
    ],
)
def test_ocr_direct_mode_depends_on_ocr_routing_only(
    settings_api, ocr_proxy_url: str, ocr_trust_env: str, expected_direct_mode: bool
) -> None:
    settings = settings_api.AppSettings(
        llm_trust_env=True,
        llm_proxy_url="http://llm-proxy.example.com:8080",
        ocr_trust_env=ocr_trust_env,
        ocr_proxy_url=ocr_proxy_url,
    )

    assert settings.ocr_proxy_url == ocr_proxy_url
    assert settings.llm_proxy_url == "http://llm-proxy.example.com:8080"
    assert settings.is_ocr_direct_mode() is expected_direct_mode


def test_public_relay_requires_base_url_and_publisher(settings_api) -> None:
    publisher = object()
    unconfigured = settings_api.AppSettings(public_relay_base_url=" ")
    configured = settings_api.AppSettings(public_relay_base_url="https://relay.example.com")

    assert unconfigured.is_public_relay_ready(publisher) is False
    assert configured.is_public_relay_ready() is False
    assert configured.is_public_relay_ready(publisher) is True


@pytest.mark.parametrize("ttl_seconds", [1, 300])
def test_relay_presign_ttl_accepts_contract_endpoints(settings_api, ttl_seconds: int) -> None:
    assert settings_api.AppSettings(
        relay_presign_ttl_seconds=ttl_seconds
    ).relay_presign_ttl_seconds == (ttl_seconds)


@pytest.mark.parametrize("ttl_seconds", [0, 301])
def test_relay_presign_ttl_rejects_out_of_range_values(settings_api, ttl_seconds: int) -> None:
    with pytest.raises(ValidationError, match="relay_presign_ttl_seconds"):
        settings_api.AppSettings(relay_presign_ttl_seconds=ttl_seconds)


def test_load_settings_is_fresh_and_get_settings_cache_can_be_cleared(
    settings_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_api.get_settings.cache_clear()
    try:
        monkeypatch.setenv("LLM_MODEL", "initial-model")
        assert settings_api.get_settings().llm_model == "initial-model"

        monkeypatch.setenv("LLM_MODEL", "updated-model")
        assert settings_api.load_settings().llm_model == "updated-model"
        assert settings_api.get_settings().llm_model == "initial-model"

        settings_api.get_settings.cache_clear()
        assert settings_api.get_settings().llm_model == "updated-model"
    finally:
        settings_api.get_settings.cache_clear()


def test_resource_limits_and_allowed_extensions_match_plan() -> None:
    from shipping_billing_agent.config.limits import ALLOWED_UPLOAD_EXTENSIONS, ResourceLimits

    limits = ResourceLimits()

    assert limits.max_batch_files == 50
    assert limits.max_expanded_documents == 120
    assert limits.max_file_bytes == 50 * 1024 * 1024
    assert limits.max_eml_recursion_depth == 3
    assert limits.max_doc_self_correction_rounds == 1
    assert limits.max_concurrency == 4
    assert limits.max_llm_calls_per_batch == 100
    assert ALLOWED_UPLOAD_EXTENSIONS == {
        ".eml",
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".xls",
        ".xlsx",
        ".doc",
        ".docx",
    }


def test_currency_registry_contains_only_active_uppercase_codes(currencies) -> None:
    codes = currencies.ACTIVE_ISO4217_CODES
    assert len(codes) == 176
    assert all(
        len(code) == 3 and code.isascii() and code.isalpha() and code.isupper() for code in codes
    )
    assert currencies.is_valid_iso4217("USD") is True
    assert currencies.is_valid_iso4217("usd") is False
    assert currencies.is_valid_iso4217("XXX") is False
    assert currencies.is_valid_iso4217("XTS") is False


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [(" rmb ", "CNY"), ("€", "EUR"), ("£", "GBP"), ("us$", "USD"), ("jpy", "JPY")],
)
def test_explicit_currency_tokens_are_normalized(currencies, raw: str, canonical: str) -> None:
    assert currencies.normalize_currency(raw) == (canonical, None)


@pytest.mark.parametrize(
    ("context_currencies", "us_shipping_context", "expected", "reason_text"),
    [
        (("USD", "US$"), False, "USD", "同单"),
        ((), True, "USD", "美线"),
        ((), False, None, "币种符号 $ 存在多币种歧义，待复核"),
        (("USD", "CAD"), False, None, "币种符号 $ 存在多币种歧义，待复核"),
        (("CAD",), True, None, "币种符号 $ 存在多币种歧义，待复核"),
    ],
)
def test_dollar_symbol_needs_unambiguous_us_context(
    currencies,
    context_currencies: tuple[str, ...],
    us_shipping_context: bool,
    expected: str | None,
    reason_text: str,
) -> None:
    code, reason = currencies.normalize_currency(
        "$", context_currencies=context_currencies, us_shipping_context=us_shipping_context
    )

    assert code == expected
    assert reason and reason_text in reason


@pytest.mark.parametrize(
    ("raw", "expected_text"),
    [(" 1,310.40 ", "1310.40"), ("-0.00", "0.00"), ("1E+3", "1000"), ("245.0000", "245.0000")],
)
def test_decimal_preserves_precision_in_database_text(
    domain_models, raw: str, expected_text: str
) -> None:
    value = domain_models.parse_decimal_strict(raw)

    assert isinstance(value, Decimal)
    assert domain_models.decimal_to_db_text(value) == expected_text


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", Decimal("sNaN"), True, "1,23"])
def test_decimal_parse_rejects_invalid_money(domain_models, raw: object) -> None:
    with pytest.raises(ValueError):
        domain_models.parse_decimal_strict(raw)


def test_decimal_database_text_refuses_non_finite_decimal(domain_models) -> None:
    with pytest.raises(ValueError):
        domain_models.decimal_to_db_text(Decimal("Infinity"))


@pytest.mark.parametrize(
    ("enum_name", "values"),
    [
        ("OcrMode", "REMOTE_ONLY REMOTE_WITH_LOCAL_FALLBACK"),
        ("SourceKind", "EML EML_BODY EML_ATTACHMENT PDF IMAGE EXCEL WORD URL"),
        (
            "ExtractionChannel",
            "PLTPLT_OCR LOCAL_PDF_TEXT_FALLBACK LOCAL_RAPIDOCR_FALLBACK EXCEL_PARSER WORD_PARSER "
            "EML_TEXT_PARSER SKIPPED_SIGNATURE FAILED",
        ),
        ("DocumentStatus", "PARSED SKIPPED_NOISE METADATA_ONLY FAILED"),
        ("OperationType", "CREATE UPDATE NO_CHANGE PENDING_REVIEW"),
        ("ReviewStatus", "AUTO_READY NEEDS_REVIEW APPROVED REJECTED"),
        (
            "MatchLevel",
            "NONE L1_DOC_CHARGE L2_BL_CHARGE_CURR L3_BOOKING_CHARGE_CURR L4_CNTR_PARTY_DATE_AMT "
            "AMBIGUOUS_MULTI",
        ),
        ("PaymentTerm", "PP CC"),
        ("PostingType", "AR AP"),
        ("AlertSeverity", "WARNING BLOCKER RESOLVED"),
        ("BatchStatus", "PROCESSING COMPLETED PARTIAL_ERROR"),
        ("BatchStage", "QUEUED UNPACKING EXTRACTING MATCHING VALIDATING COMPLETED FAILED"),
    ],
)
def test_domain_enum_values_match_contract(domain_models, enum_name: str, values: str) -> None:
    enum = getattr(domain_models, enum_name)
    assert {member.value for member in enum} == set(values.split())


def test_input_validation_issue_keeps_the_rejected_input(domain_models) -> None:
    issue = domain_models.InputValidationIssue(
        code="UNSUPPORTED_EXTENSION", filename_or_url="notes.txt", message="不支持该扩展名"
    )

    assert issue.model_dump() == {
        "code": "UNSUPPORTED_EXTENSION",
        "filename_or_url": "notes.txt",
        "message": "不支持该扩展名",
    }


def test_parsed_attachment_preserves_parent_and_names_without_dumping_bytes(domain_models) -> None:
    content_sha256 = "a" * 64
    document = domain_models.ParsedDocument(
        doc_id="attachment-1",
        parent_doc_id="email-1",
        original_filename="fee\xa0sheet.xls",
        sanitized_filename="fee sheet.xls",
        source_kind="EML_ATTACHMENT",
        content_sha256=content_sha256,
        extraction_channel="EXCEL_PARSER",
        status="PARSED",
        tables=[{"rows": [["Handling", "", "1310.40"]]}],
        raw_bytes=b"\xff\xfe",
    )

    payload = document.model_dump(mode="json")
    assert payload["doc_id"] == "attachment-1"
    assert payload["parent_doc_id"] == "email-1"
    assert payload["original_filename"] == "fee\xa0sheet.xls"
    assert payload["sanitized_filename"] == "fee sheet.xls"
    assert payload["content_sha256"] == content_sha256
    assert payload["tables"][0]["rows"] == [["Handling", "", "1310.40"]]
    assert document.raw_bytes == b"\xff\xfe"
    assert "raw_bytes" not in payload


def test_public_url_document_supports_null_content_hash(domain_models) -> None:
    document = domain_models.ParsedDocument(
        doc_id="remote-1",
        original_filename="invoice.pdf",
        sanitized_filename="invoice.pdf",
        source_kind="URL",
        content_sha256=None,
        public_url="https://files.example.com/invoice.pdf",
        extraction_channel="PLTPLT_OCR",
        status="PARSED",
    )

    assert document.model_dump(mode="json")["content_sha256"] is None


@pytest.mark.parametrize(
    ("source_kind", "content_sha256"), [("PDF", None), ("PDF", "A" * 64), ("URL", "a" * 64)]
)
def test_document_hash_matches_source_kind(
    domain_models, source_kind: str, content_sha256: str | None
) -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        domain_models.ParsedDocument(
            doc_id="document-1",
            original_filename="invoice.pdf",
            sanitized_filename="invoice.pdf",
            source_kind=source_kind,
            content_sha256=content_sha256,
            extraction_channel="PLTPLT_OCR",
            status="PARSED",
        )


def test_structured_ocr_payload_keeps_fields_and_charge_rows(domain_models) -> None:
    payload = domain_models.OcrStructuredPayload.model_validate(
        {
            "items": [
                {
                    "invoiceNumber": "INV-42",
                    "mblNumber": "MBL-18",
                    "chargeList": [{"name": "Handling", "amount": "1310.40"}],
                }
            ],
            "has_charges": True,
            "has_metadata": True,
        }
    )

    item = payload.items[0]
    assert item.invoice_number == "INV-42"
    assert item.mbl_number == "MBL-18"
    assert item.charge_list == [{"name": "Handling", "amount": "1310.40"}]


def test_raw_extraction_validates_money_and_keeps_source_references(domain_models) -> None:
    mbl = {"value": "MBL-42", "source": "invoice page 1"}
    clause = {"page": 1, "text": "Pay within 30 days", "reason": "payment instruction"}
    extracted = domain_models.RawDocumentExtraction.model_validate_json(
        json.dumps(
            {
                "shipment_refs": {"mbl_nos": [mbl]},
                "excluded_clauses": [clause],
                "total_amount": "1,310.40",
                "total_currency": "USD",
                "charges": [
                    {
                        "raw_charge_name": "Handling",
                        "quantity": "3",
                        "unit_price": None,
                        "amount": "1,310.40",
                        "currency": "$",
                    }
                ],
            }
        )
    )

    charge = extracted.charges[0]
    assert charge.raw_charge_name == "Handling"
    assert charge.quantity == Decimal("3")
    assert charge.unit_price is None
    assert charge.amount == Decimal("1310.40")
    assert charge.currency == "$"
    assert extracted.total_amount == Decimal("1310.40")
    assert extracted.shipment_refs.mbl_nos[0].model_dump() == mbl
    assert extracted.excluded_clauses[0].model_dump() == clause


def test_raw_extraction_rejects_non_finite_charge_amount(domain_models) -> None:
    with pytest.raises(ValidationError) as error:
        domain_models.RawDocumentExtraction.model_validate(
            {"charges": [{"raw_charge_name": "Handling", "amount": "NaN"}]}
        )

    assert error.value.errors()[0]["loc"] == ("charges", 0, "amount")


def test_domain_imports_no_io_or_agent_frameworks() -> None:
    domain_dir = Path(__file__).resolve().parents[2] / "src/shipping_billing_agent/domain"
    nodes = [
        node
        for path in domain_dir.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]
    import_roots = {
        alias.name.partition(".")[0]
        for node in nodes
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in nodes
        if isinstance(node, ast.ImportFrom) and node.module and not node.level
    }
    forbidden_modules = {
        "httpx",
        "sqlite3",
        "fastapi",
        "langchain",
        "langchain_core",
        "langchain_openai",
    }

    assert not import_roots & forbidden_modules
