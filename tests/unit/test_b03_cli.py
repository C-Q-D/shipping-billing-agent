"""Test CLI commands and portable review against independently persisted SQLite data."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from shipping_billing_agent import __version__
from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.cli.app import app
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain import ExistingBillRecord, dumps_decimal_json, loads_decimal_json
from shipping_billing_agent.domain.models import OcrStructuredPayload
from shipping_billing_agent.gateways.local_ocr import LocalOcrError, RapidOcrFallback
from shipping_billing_agent.gateways.ocr_client import OcrRecognitionResult, PltPltOcrClient
from shipping_billing_agent.services import export_review_bundle
from shipping_billing_agent.storage import BillingRepository, DatabaseManager

CONTRACTS = Path(__file__).resolve().parents[2] / "docs/workflow/contracts"
ENTITY = "NEPTUNE SHIPPING LIMITED"
CUSTOMER = "TARGETING A INC"
FALLBACK = "REMOTE_WITH_LOCAL_FALLBACK"
LARGE = Decimal("9007199254740991.01")
SMALL = Decimal("0.0025")
HARNESS_RUNNER_MODULE = "shipping_billing_agent.harness.runner"
METRICS = (
    "schema_valid_rate",
    "field_exact_match",
    "ar_ap_accuracy",
    "operation_accuracy",
    "conflict_recall",
    "pass_at_k",
    "pass_pow_k",
)


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "workbench"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "workbench" / "billing.sqlite"))
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("PUBLIC_RELAY_BASE_URL", "")
    monkeypatch.delenv("ALLOW_REMOTE_BIND", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    return CliRunner()


@pytest.fixture
def offline_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    def recognize(_client: RapidOcrFallback, content: bytes, filename: str) -> tuple[str, float]:
        print(f"OCR 诊断 {filename}")
        if content == b"damaged image":
            raise LocalOcrError("测试图片无法读取")
        return content.decode("utf-8"), 0.99

    monkeypatch.setattr(RapidOcrFallback, "recognize_image_bytes", recognize)


@pytest.fixture(scope="module")
def output_schema() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((CONTRACTS / "billing-output.schema.json").read_text(encoding="utf-8"))
    )


def _invoice(reference: str, amount: str, *, total: str | None = None) -> str:
    return (
        f"Invoice No. {reference}\nDate: 2026-10-01\nIssuer: {ENTITY}\nBill To: {CUSTOMER}\n"
        f"Handling Charge {amount} USD\nTotal USD {amount if total is None else total}"
    )


def _parse_args(*paths: Path) -> list[str]:
    return ["parse", *map(str, paths), "--entity", ENTITY, "--ocr-mode", FALLBACK]


def _repository(db_path: Path) -> BillingRepository:
    return BillingRepository(DatabaseManager(db_path, recover_orphans=False))


def test_version_keeps_the_existing_entrypoint(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n" and not result.stderr


@pytest.mark.parametrize(("pending", "failed"), [(True, False), (False, True), (True, True)])
def test_parse_mixed_batch_stdout_and_strict_exit(
    runner: CliRunner,
    tmp_path: Path,
    offline_ocr: None,
    output_schema: Draft202012Validator,
    pending: bool,
    failed: bool,
) -> None:
    ready = tmp_path / "ready.PNG"
    ready.write_text(_invoice("INV-101", "40.00"), encoding="utf-8")
    folder = tmp_path / "input"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    hidden = folder / ".hidden"
    hidden.mkdir()
    for junk in (hidden / "hidden.png", folder / "~$temp.png", folder / "notes.txt"):
        junk.write_bytes(b"damaged image")
    if pending:
        (nested / "pending.png").write_text(_invoice("INV-102", "50", total="5"), "utf-8")
    if failed:
        # A parsed summary without charge lines must still be reported as a failed document.
        summary = _invoice("INV-103", "20").replace("Handling Charge 20 USD\n", "")
        (nested / "summary.png").write_text(summary, encoding="utf-8")

    args = _parse_args(ready, folder)
    result = runner.invoke(app, [*args, "--output-mode", "full_standard"])
    assert result.exit_code == 0, result.stderr
    records = loads_decimal_json(result.stdout)
    output_schema.validate(records)
    assert {row["operation"] for row in records} == {"CREATE"} | (
        {"PENDING_REVIEW"} if pending else set()
    )
    assert "ready.PNG [LOCAL_RAPIDOCR_FALLBACK] 1 条" in result.stderr
    assert f"pending_review_count={int(pending)}" in result.stderr
    assert f"failed_files={int(failed)}" in result.stderr
    assert ("失败单据 summary.png" in result.stderr) is failed
    assert "OCR 诊断 ready.PNG" in result.stderr
    assert all(junk not in result.stderr for junk in ("hidden.png", "temp.png", "notes.txt"))

    strict = runner.invoke(app, [*args, "--strict"])
    assert strict.exit_code == 1, strict.stderr
    standard = loads_decimal_json(strict.stdout)
    output_schema.validate(standard)
    assert [record["operation"] for record in standard] == ["CREATE"]
    assert not (tmp_path / "workbench" / "billing.sqlite").exists()


def test_parse_writes_utf8_file_and_strict_succeeds_for_clean_batch(
    runner: CliRunner, tmp_path: Path, offline_ocr: None, output_schema: Draft202012Validator
) -> None:
    source = tmp_path / "账单.png"
    source.write_text(_invoice("INV-101", "40.00"), encoding="utf-8")
    output = tmp_path / "标准账单.json"
    result = runner.invoke(
        app, [*_parse_args(source), "--strict", "--output-mode", "audit", "--out", str(output)]
    )
    assert result.exit_code == 0, result.stderr
    assert not result.stdout and "账单.png" in result.stderr
    (record,) = loads_decimal_json(output.read_text(encoding="utf-8"))
    output_schema.validate([record])
    assert record["audit_trail"]["source_file"] == source.name
    assert record["audit_trail"]["review_status"] == "AUTO_READY"


def test_parse_all_documents_failed_returns_nonzero(
    runner: CliRunner, tmp_path: Path, offline_ocr: None
) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"damaged image")
    result = runner.invoke(app, _parse_args(broken))
    assert result.exit_code == 1
    assert loads_decimal_json(result.stdout) == []
    assert "失败单据 broken.png" in result.stderr and "failed_files=1" in result.stderr


def test_parse_accepts_repeated_public_urls_and_local_files(
    runner: CliRunner, tmp_path: Path, offline_ocr: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "local.png"
    source.write_text(_invoice("INV-100", "40.00"), encoding="utf-8")
    urls = ["https://93.184.216.34/INV-101.pdf", "https://93.184.216.34/INV-102.pdf"]
    called = []

    def recognize(_client: PltPltOcrClient, url: str) -> OcrRecognitionResult:
        called.append(url)
        return OcrRecognitionResult(
            payload=OcrStructuredPayload(items=[], has_charges=True, has_metadata=True),
            formatted_text=_invoice(Path(urlsplit(url).path).stem, "40.00"),
            pages_split=None,
            raw_payload={},
        )

    monkeypatch.setattr(PltPltOcrClient, "recognize_url", recognize)
    result = runner.invoke(app, [*_parse_args(source), "--url", urls[0], "--url", urls[1]])
    assert result.exit_code == 0, result.stderr
    assert sorted(called) == urls
    records = loads_decimal_json(result.stdout)
    assert {record["document_info"]["reference_no"] for record in records} == {
        "INV-100",
        "INV-101",
        "INV-102",
    }
    assert "INV-101.pdf [PLTPLT_OCR]" in result.stderr
    assert "INV-102.pdf [PLTPLT_OCR]" in result.stderr


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ([], "EMPTY_INPUT"),
        (["missing.png"], "INVALID_PATH"),
        (["--url", "http://127.0.0.1/file.pdf?token=private"], "SSRF_BLOCKED"),
        (["--url", "https://93.184.216.34/file.pdf"] * 51, "TOO_MANY_FILES"),
    ],
)
def test_parse_input_validation_exits_without_stdout(
    runner: CliRunner, options: list[str], error: str
) -> None:
    result = runner.invoke(app, ["parse", "--entity", ENTITY, *options])
    assert result.exit_code == 2
    assert not result.stdout and error in result.stderr
    assert "token=private" not in result.stderr


def test_parse_output_cannot_overwrite_the_configured_workbench_database(
    runner: CliRunner, tmp_path: Path
) -> None:
    workbench_db = tmp_path / "workbench" / "billing.sqlite"
    repository = _repository(workbench_db)
    existing_batch = repository.create_batch(ENTITY)
    source = tmp_path / "invoice.png"
    source.write_text(_invoice("INV-101", "40"), encoding="utf-8")
    result = runner.invoke(app, [*_parse_args(source), "--out", str(workbench_db)])
    assert result.exit_code == 1 and not result.stdout
    assert "输出文件不能覆盖" in result.stderr
    assert repository.list_batches() == [existing_batch]


def test_parse_review_bundle_transfers_isolated_baseline_and_exact_review_history(
    runner: CliRunner, tmp_path: Path, offline_ocr: None, output_schema: Draft202012Validator
) -> None:
    cli_db = tmp_path / "cli" / "billing.sqlite"
    web_db = tmp_path / "web" / "billing.sqlite"
    cli_repository, web_repository = _repository(cli_db), _repository(web_db)
    baseline = ExistingBillRecord(
        id="ledger",
        accounting_entity=ENTITY,
        charge_item="Handling Charge",
        amount=Decimal("30.00"),
        currency="USD",
        posting_type="AR",
        reference_no="INV-123",
        quantity=SMALL,
        payment_term="PP",
    )
    web_bill = web_repository.create_existing_bill(
        baseline.model_copy(update={"amount": Decimal("40.00"), "quantity": Decimal("1")})
    )
    bills = tmp_path / "bills.json"
    bills.write_text(dumps_decimal_json([baseline.model_dump()]), encoding="utf-8")
    source = tmp_path / "precision.png"
    source.write_text(_invoice("INV-123", str(LARGE)), encoding="utf-8")
    bundle = tmp_path / "bundle.json"
    result = runner.invoke(
        app,
        [
            *_parse_args(source),
            "--db-path",
            str(cli_db),
            "--bills",
            str(bills),
            "--bundle-out",
            str(bundle),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert "导入复核包" in result.stderr and "/api/v1/batches/import-review-bundle" in result.stderr
    standard = loads_decimal_json(result.stdout)
    output_schema.validate(standard)
    (record,) = standard
    assert record["operation"] == "UPDATE"
    assert record["charge_details"]["amount"] == LARGE
    assert record["charge_details"]["quantity"] == SMALL

    raw_bundle = bundle.read_bytes()
    contract = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    Draft202012Validator(
        {"$ref": "#/components/schemas/ReviewBundleV1", "components": contract["components"]}
    ).validate(json.loads(raw_bundle))
    portable = loads_decimal_json(raw_bundle.decode("utf-8"))
    assert portable["bundle_version"] == "1.0"
    assert portable["existing_bills_snapshot"] == [baseline.model_dump()]
    assert b'"amount":9007199254740991.01' in raw_bundle and b'"quantity":0.0025' in raw_bundle
    assert cli_repository.list_existing_bills() == []
    assert web_repository.list_existing_bills() == [web_bill]
    assert web_repository.list_batches() == []
    assert not cli_db.samefile(web_db)
    (cli_batch,) = cli_repository.list_batches()
    assert cli_batch["source_channel"] == "CLI"

    settings = AppSettings(sqlite_db_path=web_db, app_data_dir=web_db.parent)
    import_path = "/api/v1/batches/import-review-bundle"
    with TestClient(create_app(settings=settings)) as client:
        received = client.post(import_path, content=raw_bundle)
        assert received.status_code == 201, received.text
        imported = loads_decimal_json(received.text)
        assert imported["id"] != cli_batch["id"]
        (record,) = imported["records"]
        assert record["id"] != portable["records"][0]["id"]
        assert record["matched_existing_bill_id"] == baseline.id
        assert record["charge_details"]["quantity"] == SMALL
        (amount_diff,) = [diff for diff in record["field_diffs"] if diff["field"] == "amount"]
        assert Decimal(amount_diff["old_value"]) == baseline.amount
        assert Decimal(amount_diff["new_value"]) == LARGE

        path = f"/api/v1/batches/{imported['id']}"
        record_path = f"{path}/records/{record['id']}"
        edited = client.patch(record_path, json={"payment_term": "CC", "review_note": "核对条款"})
        assert edited.status_code == 200, edited.text
        approved = client.post(f"{record_path}/approve", json={"reason": "核对通过"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["records"][0]["review_status"] == "APPROVED"

        download = client.get(f"{path}/export/download", params={"mode": "audit"})
        assert download.status_code == 200, download.text
        exported = loads_decimal_json(download.text)
        output_schema.validate(exported)
        (approved_line,) = exported
        assert approved_line["charge_details"]["amount"] == LARGE
        assert approved_line["charge_details"]["quantity"] == SMALL
        assert approved_line["charge_details"]["payment_term"] == "CC"
        history = approved_line["audit_trail"]["human_review_history"]
        assert [action["action"] for action in history] == ["PATCH_RECORD", "APPROVE"]
    reviewed_bundle = export_review_bundle(web_repository, imported["id"])
    stored = web_repository.get_batch(imported["id"])
    assert stored["batch_existing_bills"] == [baseline]
    assert web_repository.list_existing_bills() == [web_bill]
    logs = web_repository.list_review_audit_logs(imported["id"])
    assert [(row["action"], row["reason"]) for row in logs] == [
        ("PATCH_RECORD", "核对条款"),
        ("APPROVE", "核对通过"),
    ]

    with TestClient(create_app(settings=AppSettings(sqlite_db_path=cli_db))) as client:
        restored = client.post(import_path, content=dumps_decimal_json(reviewed_bundle))
        assert restored.status_code == 201, restored.text
        (restored_record,) = loads_decimal_json(restored.text)["records"]
        assert restored_record["review_status"] == "APPROVED"
        assert restored_record["charge_details"]["amount"] == LARGE
        assert restored_record["charge_details"]["quantity"] == SMALL
    assert cli_repository.list_existing_bills() == []


@pytest.mark.parametrize(
    ("host", "allow_remote", "allowed"),
    [
        ("0.0.0.0", None, False),
        ("0.0.0.0", "true", True),
        ("127.0.0.1", "false", True),
        ("localhost", None, True),
        ("::1", None, True),
    ],
)
def test_serve_checks_remote_bind_before_starting_uvicorn(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    allow_remote: str | None,
    allowed: bool,
) -> None:
    if allow_remote is not None:
        monkeypatch.setenv("ALLOW_REMOTE_BIND", allow_remote)
    launches = []
    db_path = tmp_path / "server.sqlite"

    def run(application, **options) -> None:
        launches.append(options)
        with TestClient(application) as client:
            assert client.get("/api/v1/health").status_code == 200

    monkeypatch.setattr(uvicorn, "run", run)
    result = runner.invoke(
        app, ["serve", "--host", host, "--port", "8123", "--db-path", str(db_path)]
    )
    if allowed:
        assert result.exit_code == 0, result.stderr
        assert launches == [{"host": host, "port": 8123, "workers": 1}]
        assert db_path.is_file()
    else:
        assert result.exit_code == 2 and not launches and not db_path.exists()
        assert "拒绝监听 0.0.0.0" in result.stderr and "ALLOW_REMOTE_BIND=true" in result.stderr


def test_serve_ocr_mode_applies_to_new_batches(
    runner: CliRunner, offline_ocr: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(application, **_options) -> None:
        with TestClient(application) as client:
            created = client.post(
                "/api/v1/batches",
                files={"files": ("invoice.png", _invoice("INV-101", "40").encode("utf-8"))},
                data={"accounting_entity": ENTITY},
            )
            assert created.status_code == 201, created.text
            assert created.json()["ocr_mode"] == FALLBACK
            assert created.json()["records"][0]["review_status"] == "AUTO_READY"

    monkeypatch.setattr(uvicorn, "run", run)
    result = runner.invoke(app, ["serve", "--ocr-mode", FALLBACK])
    assert result.exit_code == 0, result.exception


def test_eval_persists_injected_runner_report_for_the_workbench(
    runner: CliRunner, tmp_path: Path
) -> None:
    db_path, output = tmp_path / "eval.sqlite", tmp_path / "report.json"
    repository = _repository(db_path)
    processing = repository.create_batch(ENTITY)

    async def run_eval(*, run_mode: str, k_trials: int) -> dict:
        assert (run_mode, k_trials) == ("OFFLINE_DETERMINISTIC", 2)
        return {
            "status": "COMPLETED",
            "model_name": "fixture",
            "total_cases": 1,
            "actual_trials": 2,
            "pass_at_k": Decimal("1.0"),
            "pass_pow_k": Decimal("1.0"),
            "report": {"amount": LARGE, "quantity": SMALL},
        }

    result = runner.invoke(
        app,
        ["eval", "--k-trials", "2", "--db-path", str(db_path), "--out", str(output)],
        obj=run_eval,
    )
    assert result.exit_code == 0, result.stderr
    assert not result.stdout
    report = loads_decimal_json(output.read_text(encoding="utf-8"))
    assert report["report"] == {"amount": LARGE, "quantity": SMALL}
    (stored,) = repository.list_eval_runs()
    assert stored["id"] == report["id"]
    assert (stored["status"], stored["actual_trials"]) == ("COMPLETED", 2)
    assert repository.list_batches() == [processing]
    with TestClient(create_app(settings=AppSettings(sqlite_db_path=db_path))) as client:
        history = client.get("/api/v1/eval-runs")
        assert history.status_code == 200
        assert loads_decimal_json(history.text) == [report]


@pytest.mark.parametrize("mode", ["OFFLINE_DETERMINISTIC", "LIVE_LLM", "LIVE_E2E"])
def test_eval_unverified_has_null_metrics_and_nonzero_exit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    db_path = tmp_path / "eval.sqlite"
    monkeypatch.setitem(sys.modules, HARNESS_RUNNER_MODULE, None)

    def must_not_run(**_options) -> None:
        pytest.fail("LIVE runner was called with missing credentials")

    result = runner.invoke(
        app,
        ["eval", "--db-path", str(db_path), "--mode", mode],
        obj=None if mode == "OFFLINE_DETERMINISTIC" else must_not_run,
    )
    assert result.exit_code == 1 and "UNVERIFIED" in result.stderr
    report = loads_decimal_json(result.stdout)
    (stored,) = _repository(db_path).list_eval_runs()
    assert report["id"] == stored["id"]
    for row in (report, stored):
        assert (row["run_mode"], row["status"], row["actual_trials"]) == (mode, "UNVERIFIED", 0)
        assert all(row[metric] is None for metric in METRICS)


def test_eval_discovers_runner_and_reports_a_completed_but_failing_run(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "eval.sqlite"

    def run_eval(*, run_mode: str, k_trials: int) -> dict:
        assert (run_mode, k_trials) == ("OFFLINE_DETERMINISTIC", 3)
        return {
            "status": "COMPLETED",
            "model_name": "fixture",
            "total_cases": 1,
            "actual_trials": 3,
            "pass_at_k": 1,
            "pass_pow_k": 0,
            "report": {"failing_cases": ["amount"]},
        }

    harness = ModuleType(HARNESS_RUNNER_MODULE)
    harness.run_eval_suite = run_eval
    monkeypatch.setitem(sys.modules, HARNESS_RUNNER_MODULE, harness)
    result = runner.invoke(app, ["eval", "--db-path", str(db_path)])
    assert result.exit_code == 1 and "评测未达标" in result.stderr
    report = loads_decimal_json(result.stdout)
    assert (report["status"], report["pass_at_k"], report["pass_pow_k"]) == ("COMPLETED", 1, 0)
    assert _repository(db_path).get_eval_run(report["id"])["status"] == "COMPLETED"
