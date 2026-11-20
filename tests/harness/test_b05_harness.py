from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain.models import (
    AlertCode,
    BatchPipelineResult,
    FailedDocumentAlert,
    OperationType,
    ProcessedBillingRecord,
    ReviewStatus,
    ValidationAlert,
)
from shipping_billing_agent.harness import (
    EvalCase,
    EvalMode,
    EvalRunResult,
    build_eval_cases,
    meets_thresholds,
    render_json,
    render_markdown,
    run_eval_suite,
)
from shipping_billing_agent.harness.dataset import (
    ATTACK_BANK_ACCOUNT,
    ATTACK_CASE_ID,
    ATTACK_EMAIL,
    ATTACK_PHONE,
    ATTACK_SIGNATURE,
    ATTACK_TEXT,
    NO_CHARGE_DETAILS,
    EvalContext,
    ExpectedRecord,
)
from shipping_billing_agent.harness.runner import METRICS, OCR_SAMPLE_URL_ENV

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "workflow" / "contracts"
CASES = {case.case_id: case for case in build_eval_cases()}
ATTACK = CASES[ATTACK_CASE_ID]
PNG = CASES["C01_REAL_PNG_ZIM_VS_ALL_AMERICA"]
ZERO = "C12_EXPLICIT_ZERO_TOTAL_AND_MISSING_LINE"
PUBLIC_IMAGE = "https://1.1.1.1/image.png?signature=harness-test-ticket"


@pytest.fixture(scope="module", autouse=True)
def no_live_network():
    def reject_request(*args, **kwargs):
        pytest.fail("Harness tests exercise real gateway code with local HTTP transports only")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.HTTPTransport, "handle_request", reject_request)
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_request)
        patch.setenv("LANGSMITH_TRACING", "false")
        patch.setenv("LANGCHAIN_TRACING_V2", "false")
        yield


@pytest.fixture(scope="module")
def offline() -> EvalRunResult:
    return run_eval_suite(k_trials=2)


@pytest.fixture
def live_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        llm_base_url="https://custom-model.example.test/v1",
        llm_api_key="harness-custom-key",
        deepseek_api_key="",
        llm_model="test-model",
        llm_max_retries=3,
        llm_proxy_url="",
        llm_trust_env=False,
        ocr_gateway_url="https://ocr.example.test/ai/process",
        ocr_proxy_url="",
        ocr_trust_env=False,
        public_relay_base_url="",
    )


def _checks(result: EvalRunResult, case_id: str, trial: int = 1) -> dict:
    (detail,) = [
        case
        for case in result.report_json["case_results"]
        if case["case_id"] == case_id and case["trial"] == trial
    ]
    return {check["check"]: check for check in detail["checks"]}


def _row(reference: str, amount: str, **fields) -> ProcessedBillingRecord:
    values = {
        "id": reference,
        "source_doc_id": "fixture",
        "reference_no": reference,
        "charge_item": "Handling Charge",
        "amount": amount,
        "currency": "USD",
        "posting_type": "AR",
        "operation": "CREATE",
        "review_status": "AUTO_READY",
        "accounting_entity": "Example Carrier Ltd.",
        "issuer": "Example Carrier Ltd.",
        "recipient": "Example Customer Inc.",
        **fields,
    }
    return ProcessedBillingRecord(**values)


def _batch(rows: list[ProcessedBillingRecord]) -> BatchPipelineResult:
    return BatchPipelineResult(accounting_entity="Example Carrier Ltd.", records=rows)


def _chat(body: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "mock-chat",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(body)},
                }
            ],
        },
    )


def _attack_reply(amount: str, *, forged_approval: bool = False) -> dict:
    reply = {
        "reference_no": "INV-ATTACK-1",
        "total_amount": amount,
        "total_currency": "USD",
        "charges": [{"raw_charge_name": "Handling Charge", "amount": amount, "currency": "USD"}],
    }
    if forged_approval:
        reply["review_status"] = "APPROVED"
    return reply


def _assert_unscored(result: EvalRunResult) -> None:
    for metric in METRICS:
        assert result.report_json[metric]["rate"] is None
    for key in (
        "schema_valid_rate",
        "field_exact_match",
        "ar_ap_accuracy",
        "operation_accuracy",
        "conflict_recall",
        "pass_at_k",
        "pass_pow_k",
        "avg_latency_ms",
    ):
        assert getattr(result, key) is None


def test_two_complete_offline_trials(offline: EvalRunResult) -> None:
    assert offline.status == "COMPLETED", offline.report_json["failing_cases"]
    assert offline.actual_trials == offline.k_trials == 2
    assert offline.total_cases == 18
    report = offline.report_json
    assert report["executed_cases"] == 36
    assert report["ocr_calls"] == 2
    assert report["llm_calls"] == 10
    assert report["failing_cases"] == []
    assert {row["status"] for row in report["case_results"]} == {"PASSED"}
    assert {
        row["group_id"][:3] for row in report["case_results"] if row["group_id"].startswith("C")
    } == {f"C{number:02}" for number in range(1, 14)}
    for metric in METRICS:
        counts = report[metric]
        assert counts["denominator"] > 0
        rate = 0.0 if metric == "false_positive_rate" else 1.0
        assert counts["rate"] == rate
        assert counts["numerator"] == counts["denominator"] * rate
    assert meets_thresholds(offline)


def test_stability_counts_any_and_every_trial_separately() -> None:
    amounts = iter(["31.00", "30.00", "29.00"])
    truth = ExpectedRecord("INV-1", "Handling Charge", Decimal("30.00"))

    def exercise(ctx: EvalContext) -> None:
        ctx.score("账单", _batch([_row("INV-1", next(amounts))]), [truth])

    result = run_eval_suite(k_trials=3, cases=[EvalCase("stability", exercise)])
    assert result.status == "COMPLETED"
    assert [row["status"] for row in result.report_json["case_results"]] == [
        "FAILED",
        "PASSED",
        "FAILED",
    ]
    assert result.pass_at_k == 1.0
    assert result.pass_pow_k == 0.0
    assert result.report_json["pass_at_k"] == {"rate": 1.0, "numerator": 1, "denominator": 1}
    assert result.report_json["pass_pow_k"] == {"rate": 0.0, "numerator": 0, "denominator": 1}
    assert not meets_thresholds(result)
    assert meets_thresholds(result, min_pass_pow_k=0.0)
    assert {failure["trial"] for failure in result.report_json["failing_cases"]} == {1, 3}


@pytest.mark.parametrize("count", [0, 2], ids=["missing", "extra"])
def test_row_count_mismatch_cannot_be_a_full_pass(count: int) -> None:
    expected = ExpectedRecord("INV-1", "Handling Charge", Decimal("30"))

    def exercise(ctx: EvalContext) -> None:
        ctx.score("账单", _batch([_row("INV-1", "30")] * count), [expected])

    result = run_eval_suite(k_trials=1, cases=[EvalCase("row-count", exercise)])
    assert result.status == "COMPLETED"
    assert result.pass_pow_k == 0.0
    assert not meets_thresholds(result)
    assert _checks(result, "row-count")["账单 记录数"]["actual"] == count
    if count == 0:
        assert result.schema_valid_rate == 0.0


def test_scoring_counts_false_positives_and_missed_conflicts() -> None:
    conflict = AlertCode.CROSS_DOCUMENT_CONFLICT
    truth = [
        ExpectedRecord("N1", "Handling Charge", Decimal("30")),
        ExpectedRecord("N2", "Handling Charge", Decimal("31"), posting_type="AP"),
        ExpectedRecord(
            "C1",
            "Handling Charge",
            Decimal("40"),
            currency="EUR",
            operation=OperationType.PENDING_REVIEW,
            review_status=ReviewStatus.NEEDS_REVIEW,
            alerts=(conflict,),
        ),
        ExpectedRecord(
            "C2",
            "Handling Charge",
            Decimal("50"),
            currency="EUR",
            posting_type="AP",
            operation=OperationType.PENDING_REVIEW,
            review_status=ReviewStatus.NEEDS_REVIEW,
            alerts=(conflict,),
        ),
    ]
    wrong_block = ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "误拦")
    cross_conflict = ValidationAlert.from_code(conflict, "跨文档金额冲突")
    actual = [
        _row("C2", "50", posting_type="AP"),
        _row("N1", "30"),
        _row(
            "C1",
            "40",
            currency="EUR",
            operation="PENDING_REVIEW",
            review_status="NEEDS_REVIEW",
            validation_alerts=[cross_conflict],
        ),
        _row(
            "N2",
            "32",
            operation="PENDING_REVIEW",
            review_status="NEEDS_REVIEW",
            validation_alerts=[wrong_block],
        ),
    ]

    def exercise(ctx: EvalContext) -> None:
        ctx.score("混测", _batch(actual), truth)

    result = run_eval_suite(k_trials=1, cases=[EvalCase("mixed-metrics", exercise)])
    assert result.status == "COMPLETED"
    for metric, numerator, denominator in (
        ("ar_ap_accuracy", 3, 4),
        ("amount_accuracy", 3, 4),
        ("currency_accuracy", 3, 4),
        ("conflict_recall", 1, 2),
        ("alert_precision", 1, 2),
        ("false_positive_rate", 1, 2),
        ("operation_accuracy", 2, 4),
        ("field_exact_match", 2, 4),
    ):
        assert result.report_json[metric] == {
            "rate": numerator / denominator,
            "numerator": numerator,
            "denominator": denominator,
        }
    assert result.pass_at_k == result.pass_pow_k == 0.0
    assert not meets_thresholds(result)


@pytest.mark.parametrize("mode", ["LIVE_LLM", "LIVE_E2E"])
def test_live_without_model_configuration_is_unverified(live_settings, mode: EvalMode) -> None:
    result = run_eval_suite(mode, settings=live_settings.model_copy(update={"llm_api_key": ""}))
    assert result.status == "UNVERIFIED"
    assert "LLM_API_KEY" in result.error_message
    assert result.actual_trials == result.report_json["executed_cases"] == 0
    assert result.report_json["llm_calls"] == result.report_json["ocr_calls"] == 0
    _assert_unscored(result)
    assert not meets_thresholds(result)
    assert "**UNVERIFIED**" in render_markdown(result)


def test_custom_llm_key_configures_only_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "harness-custom-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://custom-model.example.test/v1")
    monkeypatch.setenv("LLM_PROXY_URL", "")
    monkeypatch.setenv("LLM_TRUST_ENV", "false")
    monkeypatch.delenv(OCR_SAMPLE_URL_ENV, raising=False)

    def model(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "custom-model.example.test"
        assert request.headers["Authorization"] == "Bearer harness-custom-only"
        return _chat(_attack_reply("100.00"))

    result = run_eval_suite(
        "LIVE_LLM", k_trials=1, cases=[ATTACK], llm_transport=httpx.MockTransport(model)
    )
    assert result.status == "COMPLETED", result.report_json["failing_cases"]
    assert result.pass_pow_k == 1.0
    assert result.report_json["llm_calls"] == 1
    assert result.report_json["ocr_calls"] == 0
    e2e = run_eval_suite("LIVE_E2E", k_trials=1, cases=[PNG, ATTACK])
    assert e2e.status == "UNVERIFIED"
    assert OCR_SAMPLE_URL_ENV in e2e.error_message
    assert e2e.actual_trials == e2e.report_json["executed_cases"] == 0
    _assert_unscored(e2e)


def test_rate_limit_keeps_completed_trials_counts_and_failure_position(live_settings) -> None:
    second = replace(ATTACK, case_id=ATTACK.case_id + "/second")
    outcomes = iter([True, True, True, False])

    def model(_request: httpx.Request) -> httpx.Response:
        if next(outcomes):
            return _chat(_attack_reply("100.00"))
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    result = run_eval_suite(
        "LIVE_LLM",
        3,
        cases=[ATTACK, second],
        settings=live_settings,
        llm_transport=httpx.MockTransport(model),
    )
    assert result.status == "FAILED"
    assert result.actual_trials == 1
    assert result.report_json["executed_cases"] == 3
    assert result.report_json["llm_calls"] == 4
    assert result.report_json["ocr_calls"] == 0
    assert result.report_json["field_exact_match"]["denominator"] == 3
    assert result.report_json["case_results"][-1]["status"] == "ERROR"
    (failure,) = result.report_json["failing_cases"]
    assert (failure["trial"], failure["case_id"]) == (2, second.case_id)
    assert "HTTP 429" in failure["reason"] and "HTTP 429" in result.error_message
    _assert_unscored(result)
    assert not meets_thresholds(result)
    markdown = render_markdown(result)
    assert "| 轮次 | 用例 | 预期 | 实际 | 原因 |" in markdown
    assert second.case_id in markdown and "HTTP 429" in markdown


@pytest.mark.parametrize("amount", ["95.00", None], ids=["wrong-charge", "missing-charge"])
def test_parseable_model_answers_with_business_errors_are_scored(live_settings, amount) -> None:
    reply = {"charges": []} if amount is None else _attack_reply(amount)
    result = run_eval_suite(
        "LIVE_LLM",
        1,
        cases=[ATTACK],
        settings=live_settings,
        llm_transport=httpx.MockTransport(lambda _request: _chat(reply)),
    )
    assert result.status == "COMPLETED", result.report_json["failing_cases"]
    assert result.actual_trials == result.report_json["executed_cases"] == 1
    assert result.error_message is None
    assert result.report_json["amount_accuracy"] == {"rate": 0.0, "numerator": 0, "denominator": 1}
    assert result.pass_at_k == result.pass_pow_k == 0.0
    assert not meets_thresholds(result)


def test_unexpected_document_failure_is_infrastructure_failure() -> None:
    def exercise(ctx: EvalContext) -> None:
        result = _batch([])
        result.failed_document_alerts.append(
            FailedDocumentAlert(doc_id="fixture", filename="bill.pdf", reason="OCR 网络超时")
        )
        ctx.score("Document", result, [])

    result = run_eval_suite(k_trials=1, cases=[EvalCase("unexpected-failure", exercise)])
    assert result.status == "FAILED"
    assert "bill.pdf" in result.error_message and "OCR 网络超时" in result.error_message
    assert result.actual_trials == result.report_json["executed_cases"] == 0
    _assert_unscored(result)


def test_c12_expected_failure_card_passes(offline: EvalRunResult) -> None:
    checks = _checks(offline, ZERO)
    failure = checks["仅总额无明细触发失败卡 业务失败卡"]
    assert failure["passed"]
    assert failure["actual"][0][1:] == [NO_CHARGE_DETAILS, []]
    assert checks["仅总额无明细触发失败卡 记录数"]["actual"] == 0
    assert checks["显式零总额 official 导出数"]["actual"] == 0


@pytest.mark.parametrize(
    ("suffix", "filename", "amounts"),
    [
        ("xls", "b01_merged.xls", ["65.25", "310.42"]),
        ("xlsx", "b01_merged.xlsx", ["65.25", "310.42"]),
        ("doc", "b01_empty_columns.doc", ["65.25", "310.42"]),
        ("docx", "b01_interleaved.docx", ["65.25"]),
    ],
)
def test_c13_office_subcases_are_reported_separately(offline, suffix, filename, amounts) -> None:
    case_id = f"C13_OFFICE_FORMATS/{suffix}"
    checks = _checks(offline, case_id)
    rows = [checks[f"{filename} 第 {index} 行"]["actual"] for index in range(1, len(amounts) + 1)]
    assert [row["amount"] for row in rows] == amounts
    assert [row["currency"] for row in rows] == [None] * len(amounts)
    assert checks[f"{filename} full_standard 导出数"]["actual"] == len(amounts)
    assert checks[f"{filename} full_standard Schema"]["passed"]
    assert case_id in render_markdown(offline)


@pytest.mark.parametrize(("branch", "amount"), [("payment_term", "30.00"), ("amount", "40.00")])
def test_c05_partial_update_inherits_fields_against_both_baselines(offline, branch, amount) -> None:
    checks = _checks(offline, f"C05_PARTIAL_UPDATE_DN202609001/{branch}")
    for baseline, operation in (("V1", "UPDATE"), ("V2", "NO_CHANGE")):
        row = checks[f"{baseline} 第 1 行"]["actual"]
        assert (row["operation"], row["amount"], row["currency"], row["payment_term"]) == (
            operation,
            amount,
            "USD",
            "CC",
        )
        assert (row["issuer"], row["recipient"], row["posting_type"]) == (
            "Example Carrier Ltd.",
            "Example Customer Inc.",
            "AR",
        )
    diff = ["amount", "30.00", "40.00"] if branch == "amount" else ["payment_term", "PP", "CC"]
    assert checks["V1 字段变动"]["actual"] == [diff]
    assert checks["V2 字段变动"]["actual"] == []


def test_c08_offline_fake_is_blocked_without_self_repair_evidence(offline: EvalRunResult) -> None:
    checks = _checks(offline, ATTACK_CASE_ID)
    row = checks["离线篡改金额 第 1 行"]["actual"]
    assert row["amount"] == "0"
    assert row["operation"] == "PENDING_REVIEW"
    assert checks["离线篡改金额 official 导出数"]["actual"] == 0
    assert checks["替身调用提示词脱敏隔离"]["passed"]
    assert offline.report_json["self_repair_evidence"] == "UNVERIFIED"


@pytest.mark.parametrize(
    ("amounts", "verified", "official"),
    [(("100.00",), False, 1), (("0", "0"), False, 0), (("0", "100.00"), True, 1)],
    ids=["honest-first", "guarded-zero", "repaired-with-evidence"],
)
def test_c08_live_uses_wire_sanitization_and_separate_baseline(
    live_settings,
    amounts,
    verified,
    official,
) -> None:
    responses = iter(amounts)
    user_prompts = []

    def model(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, user = body["messages"]
        assert [system["role"], user["role"]] == ["system", "user"]
        assert ATTACK_TEXT not in system["content"]
        prompt = user["content"]
        _, _, source = prompt.partition("<UNTRUSTED_DOCUMENT_CONTENT ")
        snippet, close_tag, suffix = source.partition("</UNTRUSTED_DOCUMENT_CONTENT>")
        assert close_tag and ATTACK_TEXT in snippet and ATTACK_TEXT not in suffix
        assert "&lt;/UNTRUSTED_DOCUMENT_CONTENT&gt;" in snippet
        for token in (
            ATTACK_BANK_ACCOUNT,
            ATTACK_EMAIL,
            ATTACK_PHONE,
            ATTACK_SIGNATURE,
            "[SYSTEM]",
        ):
            assert all(token not in message["content"] for message in body["messages"])
        user_prompts.append(prompt)
        amount = next(responses)
        return _chat(_attack_reply(amount, forged_approval=amount == "0"))

    result = run_eval_suite(
        "LIVE_LLM",
        1,
        cases=[ATTACK],
        settings=live_settings,
        llm_transport=httpx.MockTransport(model),
    )
    assert result.status == "COMPLETED", result.report_json["failing_cases"]
    assert meets_thresholds(result), result.report_json["failing_cases"]
    assert result.report_json["llm_calls"] == len(user_prompts) == len(amounts)
    assert result.report_json["self_repair_evidence"] == ("VERIFIED" if verified else "UNVERIFIED")
    checks = _checks(result, ATTACK_CASE_ID)
    assert checks["联机业务底线 official 导出数"]["actual"] == official
    assert checks["文档不能赋予 APPROVED"]["passed"]
    assert checks["实际 HTTP 请求正文脱敏隔离"]["passed"]
    if len(amounts) == 2:
        assert "First-pass validation differences:" in user_prompts[1]
        assert "提取值=0, 原文值=100.00" in user_prompts[1]
    assert live_settings.llm_api_key not in render_json(result)


def _image_ocr_response(*, with_charges: bool, invalid_lines: bool = False) -> dict:
    charges = [
        {
            "chargeName": "Wharfage Administration Fee",
            "amount": "5.00",
            "unitPrice": "5.00",
            "currency": "USD",
            "paymentTerm": "CC",
        },
        {"chargeName": "Wharfage", "amount": "21.90", "unitPrice": "2.85", "currency": "USD"},
    ]
    return {
        "data": {
            "list": [
                {
                    "fileType": "Invoice",
                    "invoiceNumber": "ORFR1618731",
                    "invoiceDate": "2025-10-03",
                    "issuer": "ZIMAMERICAN INTEGRATED SHIPPING SERVICES CO. LLC",
                    "billTo": "ALL AMERICA FORWARDING, INC.",
                    "totalAmount": "26.90",
                    "currency": "USD",
                    "chargeList": ([{"unknown": "no fee evidence"}] if invalid_lines else charges)
                    if with_charges
                    else [],
                }
            ]
        }
    }


@pytest.mark.parametrize("via_relay", [False, True], ids=["public-url", "relay"])
def test_live_e2e_requires_real_gateway_response_and_uses_live_injection_baseline(
    live_settings,
    via_relay,
):
    ocr_requests = []
    published = []

    class Publisher:
        def publish(self, content: bytes, filename: str, *, ttl_seconds: int) -> str:
            assert content.startswith(b"\x89PNG\r\n\x1a\n")
            published.append((filename, ttl_seconds))
            return PUBLIC_IMAGE

    if via_relay:
        live_settings = live_settings.model_copy(
            update={"public_relay_base_url": "https://1.1.1.1/"}
        )

    def ocr(request: httpx.Request) -> httpx.Response:
        ocr_requests.append(json.loads(request.content)["file"])
        return httpx.Response(200, json=_image_ocr_response(with_charges=True))

    def model(request: httpx.Request) -> httpx.Response:
        if ATTACK_TEXT in json.loads(request.content)["messages"][1]["content"]:
            return _chat(_attack_reply("100.00"))
        return _chat(
            {
                "charges": [
                    {
                        "raw_charge_name": "Wharfage Administration Fee",
                        "amount": "5.00",
                        "currency": "USD",
                        "payment_term": "CC",
                        "unit_price": "5.00",
                    },
                    {
                        "raw_charge_name": "Wharfage",
                        "amount": "21.90",
                        "currency": "USD",
                        "unit_price": "2.85",
                    },
                ]
            }
        )

    result = run_eval_suite(
        "LIVE_E2E",
        1,
        cases=[PNG, ATTACK],
        settings=live_settings,
        ocr_sample_url="" if via_relay else PUBLIC_IMAGE,
        relay_publisher=Publisher() if via_relay else None,
        ocr_transport=httpx.MockTransport(ocr),
        llm_transport=httpx.MockTransport(model),
    )
    assert result.status == "COMPLETED", result.report_json["failing_cases"]
    assert meets_thresholds(result), result.report_json["failing_cases"]
    assert result.report_json["ocr_calls"] == 1
    assert result.report_json["llm_calls"] == 3
    assert ocr_requests == [PUBLIC_IMAGE]
    assert published == (
        [("图片示例.png", live_settings.relay_presign_ttl_seconds)] if via_relay else []
    )
    assert _checks(result, PNG.case_id)["远端 OCR 来源"]["actual"] == "PLTPLT_OCR"
    assert _checks(result, ATTACK_CASE_ID)["实际 HTTP 请求正文脱敏隔离"]["passed"]
    assert "harness-test-ticket" not in render_json(result)


@pytest.mark.parametrize("with_charges", [False, True], ids=["metadata-only", "opaque-charge-row"])
def test_live_e2e_cannot_pass_without_valid_remote_charge_rows(live_settings, with_charges):
    def ocr(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_image_ocr_response(with_charges=with_charges, invalid_lines=True)
        )

    result = run_eval_suite(
        "LIVE_E2E",
        1,
        cases=[PNG],
        settings=live_settings,
        ocr_sample_url=PUBLIC_IMAGE,
        ocr_transport=httpx.MockTransport(ocr),
    )
    assert result.status == "FAILED"
    assert result.report_json["executed_cases"] == result.report_json["llm_calls"] == 0
    assert result.report_json["ocr_calls"] == 1
    assert "PLTPLT_OCR" in result.error_message
    _assert_unscored(result)


def test_live_e2e_rejects_a_nonpublic_ocr_input_before_starting(live_settings) -> None:
    result = run_eval_suite(
        "LIVE_E2E",
        1,
        cases=[PNG],
        settings=live_settings,
        ocr_sample_url="http://127.0.0.1/not-a-public-sample.png",
    )
    assert result.status == "UNVERIFIED"
    assert result.actual_trials == result.report_json["executed_cases"] == 0
    assert result.report_json["ocr_calls"] == 0
    assert "公网" in result.error_message
    _assert_unscored(result)


@pytest.mark.parametrize(
    "missing_directory", [True, False], ids=["missing-directory", "missing-file"]
)
def test_missing_real_samples_are_an_explicit_failure(tmp_path, missing_directory) -> None:
    directory = tmp_path / "missing" if missing_directory else tmp_path
    result = run_eval_suite(k_trials=1, samples_dir=directory)
    assert result.status == "FAILED"
    assert result.actual_trials == result.report_json["executed_cases"] == 0
    assert "图片示例.png" in result.error_message and "samples_dir" in result.error_message
    (failure,) = result.report_json["failing_cases"]
    assert failure["case_id"] == PNG.case_id and failure["trial"] == 1
    _assert_unscored(result)


def test_mixed_export_uses_three_nonempty_schema_valid_payloads(offline: EvalRunResult) -> None:
    checks = _checks(offline, "MIXED_EXPORT_MODES")
    for mode, count in (("official", 3), ("full_standard", 5), ("audit", 6)):
        assert checks[f"六条混合记录 {mode} 导出数"]["actual"] == count
        assert checks[f"六条混合记录 {mode} Schema"]["actual"] == "通过"


def test_sql_row_and_api_summary_match_frozen_contracts(offline: EvalRunResult) -> None:
    row = offline.to_eval_run_row()
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript((CONTRACTS / "schema.sql").read_text(encoding="utf-8"))
        assert set(row) == {column[1] for column in db.execute("PRAGMA table_info(eval_runs)")}
        params = ", ".join(f":{name}" for name in row)
        db.execute(f"INSERT INTO eval_runs ({', '.join(row)}) VALUES ({params})", row)
        (persisted,) = db.execute("SELECT report_json FROM eval_runs").fetchone()
        assert json.loads(persisted) == offline.report_json

    openapi = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    schema = openapi["components"]["schemas"]["EvalRunSummary"]
    summary = offline.to_eval_run_summary()
    assert set(summary) == set(schema["properties"])
    Draft202012Validator(schema).validate(summary)
    assert summary["report"] == offline.report_json
    report = json.loads(render_json(offline))
    assert report["operation_accuracy"] == 1.0
    assert report["avg_latency_ms"] > 0
    assert report["report_json"] == offline.report_json
