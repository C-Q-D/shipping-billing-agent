"""Exercise the default Harness through the API, CLI, and SQLite history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.cli import app as cli
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.harness.dataset import SAMPLES_ENV, EvalCase, EvalContext
from shipping_billing_agent.harness.runner import METRICS, OCR_SAMPLE_URL_ENV, run_eval_suite

EVAL_RUNS = "/api/v1/eval-runs"
OFFLINE = "OFFLINE_DETERMINISTIC"
SUMMARY_METRICS = (
    "schema_valid_rate",
    "field_exact_match",
    "ar_ap_accuracy",
    "operation_accuracy",
    "conflict_recall",
    "pass_at_k",
    "pass_pow_k",
)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("PUBLIC_RELAY_BASE_URL", "")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.delenv(OCR_SAMPLE_URL_ENV, raising=False)
    monkeypatch.delenv(SAMPLES_ENV, raising=False)

    def reject_outbound_http(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Offline evaluation and preflight checks must not call external gateways")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_outbound_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_outbound_http)
    return AppSettings(_env_file=None, sqlite_db_path=tmp_path / "api.sqlite")


def _history(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(EVAL_RUNS)
    assert response.status_code == 200
    return json.loads(response.text, parse_float=Decimal)


def _only_stored_run(path: Path) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM eval_runs").fetchall()
    assert len(rows) == 1
    return rows[0]


def _assert_offline_suite(summary: dict[str, Any]) -> None:
    assert (summary["run_mode"], summary["status"]) == (OFFLINE, "COMPLETED")
    assert summary["k_trials"] == summary["actual_trials"] == 1
    assert summary["total_cases"] == 16
    assert all(summary[metric] == Decimal("1") for metric in SUMMARY_METRICS)

    report = summary["report"]
    assert report["executed_cases"] == 16
    assert report["failing_cases"] == []
    assert len({case["case_id"] for case in report["case_results"]}) == 16
    assert len({case["group_id"] for case in report["case_results"]}) == 13
    assert {case["status"] for case in report["case_results"]} == {"PASSED"}
    for metric in METRICS:
        counts = report[metric]
        assert counts["denominator"] > 0
        rate = 0 if metric == "false_positive_rate" else 1
        assert counts["rate"] == rate
        assert counts["numerator"] == rate * counts["denominator"]
    for metric in ("pass_at_k", "pass_pow_k"):
        assert report[metric]["denominator"] == 16


def _assert_unverified(summary: dict[str, Any], stored: sqlite3.Row, mode: str) -> None:
    assert summary["id"] == stored["id"]
    for row in (summary, stored):
        assert (row["run_mode"], row["status"], row["actual_trials"]) == (mode, "UNVERIFIED", 0)
        assert all(row[metric] is None for metric in SUMMARY_METRICS)
    report = summary["report"]
    assert summary["total_cases"] == 16
    assert report["executed_cases"] == report["llm_calls"] == report["ocr_calls"] == 0
    assert report["avg_latency_ms"] is None
    assert report["case_results"] == report["failing_cases"] == []
    for metric in METRICS:
        assert report[metric] == {"rate": None, "numerator": 0, "denominator": 0}


def test_default_api_offline_suite_matches_history(settings: AppSettings) -> None:
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(EVAL_RUNS, json={"run_mode": OFFLINE, "k_trials": 1})
        assert response.status_code == 201, response.text
        summary = json.loads(response.text, parse_float=Decimal)
        _assert_offline_suite(summary)
        assert _history(client) == [summary]
    assert _only_stored_run(settings.sqlite_db_path)["id"] == summary["id"]


def test_explicit_none_keeps_api_eval_disabled(settings: AppSettings) -> None:
    with TestClient(create_app(settings=settings, eval_runner=None)) as client:
        response = client.post(EVAL_RUNS, json={"run_mode": OFFLINE, "k_trials": 1})
        assert response.status_code == 201
        summary = response.json()
        assert (summary["status"], summary["actual_trials"], summary["total_cases"]) == (
            "UNVERIFIED",
            0,
            0,
        )
        assert _history(client) == [summary]


@pytest.mark.parametrize("mode", ["LIVE_LLM", "LIVE_E2E"])
def test_live_api_and_cli_use_isolated_settings_and_store_null_metrics(
    settings: AppSettings, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = settings.model_copy(update={"llm_base_url": None, "llm_api_key": None})
    monkeypatch.setenv("LLM_API_KEY", "host-credential-must-not-be-used")

    with TestClient(create_app(settings=isolated)) as client:
        response = client.post(EVAL_RUNS, json={"run_mode": mode, "k_trials": 1})
        assert response.status_code == 201, response.text
        api_summary = json.loads(response.text, parse_float=Decimal)
        assert _history(client) == [api_summary]
    _assert_unverified(api_summary, _only_stored_run(isolated.sqlite_db_path), mode)
    assert "LLM_API_KEY" in api_summary["error_message"]

    monkeypatch.setattr(cli, "load_settings", lambda: isolated)
    db_path = tmp_path / "cli-live.sqlite"
    result = CliRunner().invoke(
        cli.app, ["eval", "--mode", mode, "--k-trials", "1", "--db-path", str(db_path)]
    )
    assert result.exit_code == 1, result.exception
    assert "UNVERIFIED" in result.stderr and "LLM_API_KEY" in result.stderr
    cli_summary = json.loads(result.stdout, parse_float=Decimal)
    _assert_unverified(cli_summary, _only_stored_run(db_path), mode)
    assert cli_summary["report"] == api_summary["report"]


def test_default_cli_writes_one_offline_run_and_outputs_its_json_report(
    settings: AppSettings, tmp_path: Path
) -> None:
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "--mode",
            OFFLINE,
            "--k-trials",
            "1",
            "--db-path",
            str(settings.sqlite_db_path),
            "--out",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""
    summary = json.loads(output.read_text(encoding="utf-8"), parse_float=Decimal)
    _assert_offline_suite(summary)
    assert _only_stored_run(settings.sqlite_db_path)["id"] == summary["id"]
    with TestClient(create_app(settings=settings)) as client:
        assert _history(client) == [summary]


def test_live_e2e_cli_reports_missing_ocr_input_when_llm_is_configured(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "configured-test-key")
    result = CliRunner().invoke(
        cli.app, ["eval", "--mode", "LIVE_E2E", "--db-path", str(settings.sqlite_db_path)]
    )
    assert result.exit_code == 1, result.exception
    assert "UNVERIFIED" in result.stderr and OCR_SAMPLE_URL_ENV in result.stderr
    summary = json.loads(result.stdout, parse_float=Decimal)
    _assert_unverified(summary, _only_stored_run(settings.sqlite_db_path), "LIVE_E2E")


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_cli_prints_case_differences_to_stderr_and_exits_nonzero(
    settings: AppSettings, tmp_path: Path, status: str
) -> None:
    case_id = "test-charge-amount"

    def exercise(context: EvalContext) -> None:
        if status == "FAILED":
            raise RuntimeError("模拟 OCR 超时")
        context.check("费用金额", Decimal("30.00"), Decimal("31.00"))

    runner = partial(run_eval_suite, cases=[EvalCase(case_id, exercise)])
    args = ["eval", "--k-trials", "1", "--db-path", str(settings.sqlite_db_path)]
    output = tmp_path / "failed-report.json"
    if status == "FAILED":
        args += ["--out", str(output)]
    result = CliRunner().invoke(cli.app, args, obj=runner)
    assert result.exit_code == 1, result.exception

    report_json = result.stdout
    if status == "FAILED":
        assert result.stdout == ""
        report_json = output.read_text(encoding="utf-8")
    summary = json.loads(report_json, parse_float=Decimal)
    assert summary["status"] == status
    (failure,) = summary["report"]["failing_cases"]
    expected, actual = (
        ("完成当前用例", "RuntimeError: 模拟 OCR 超时")
        if status == "FAILED"
        else ("30.00", "31.00")
    )
    assert failure["case_id"] == case_id
    assert (failure["expected"], failure["actual"]) == (expected, actual)
    assert f'expected "{expected}" | actual "{actual}"' in next(
        line for line in result.stderr.splitlines() if case_id in line and "expected" in line
    )
    with TestClient(create_app(settings=settings)) as client:
        assert _history(client) == [summary]


def test_cli_flags_low_pass_pow_k_without_case_differences(settings: AppSettings) -> None:
    rate = Decimal("0.123456789012345678901")

    def runner(**options: Any) -> dict[str, Any]:
        assert (options["run_mode"], options["k_trials"]) == (OFFLINE, 1)
        assert options["settings"].sqlite_db_path == settings.sqlite_db_path
        return {
            "status": "COMPLETED",
            "model_name": "test-runner",
            "pass_pow_k": rate,
            "report": {"pass_pow_k": {"rate": rate}, "failing_cases": []},
        }

    result = CliRunner().invoke(
        cli.app, ["eval", "--k-trials", "1", "--db-path", str(settings.sqlite_db_path)], obj=runner
    )
    assert result.exit_code == 1, result.exception
    assert f"pass^k expected 1 | actual {rate}" in result.stderr
    summary = json.loads(result.stdout, parse_float=Decimal)
    assert summary["pass_pow_k"] == summary["report"]["pass_pow_k"]["rate"] == rate
    with TestClient(create_app(settings=settings)) as client:
        assert _history(client) == [summary]
