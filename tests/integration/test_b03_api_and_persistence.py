"""Run the billing API, SQLite, and CLI together with local files and no network."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openpyxl import Workbook
from starlette.concurrency import run_in_threadpool
from typer.testing import CliRunner

from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.cli.app import app as cli_app
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain.models import BatchStage, ParsedDocument
from shipping_billing_agent.gateways.local_ocr import RapidOcrFallback
from shipping_billing_agent.services import BatchService
from shipping_billing_agent.storage import BillingRepository, DatabaseManager

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs/workflow/contracts"
API = "/api/v1"
BATCHES = f"{API}/batches"
BILLS = f"{API}/existing-bills"
NEPTUNE = "NEPTUNE SHIPPING LIMITED"
TARGETING = "TARGETING A INC"
ZIM = "ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO. LLC"
FORWARDER = "ALL AMERICA FORWARDING, INC."
FALLBACK = "REMOTE_WITH_LOCAL_FALLBACK"
LARGE_AMOUNT = Decimal("9007199254740991.01")
FRACTIONAL_QUANTITY = Decimal("0.0025")
WAIT_SECONDS = 30
OCR_COMPLETION_SECONDS = 120


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("B03 regression must use local parsers and the in-process HTTP client")

    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "PUBLIC_RELAY_BASE_URL"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "[]")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        app_data_dir=tmp_path / "workbench",
        sqlite_db_path=tmp_path / "workbench" / "billing.sqlite",
    )


@pytest.fixture
def client(settings: AppSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def billing_schema() -> Draft202012Validator:
    schema = json.loads((CONTRACTS / "billing-output.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


@pytest.fixture
def real_samples() -> tuple[Path, Path, Path, Path]:
    directory = Path(
        os.environ.get("SHIPPING_BILLING_REAL_SAMPLES_DIR", str(ROOT / "笔试题AIAGENT"))
    )
    email_matches = sorted(directory.glob("深圳容宇 PRE-ALERT*.eml"))
    assert len(email_matches) == 1, f"Expected one original PRE-ALERT email in {directory}"
    image = directory / "图片示例.png"
    first_pdf = directory / "PDF示例账单1.pdf"
    second_pdf = directory / "PDF 示例账单2.pdf"
    assert all(path.is_file() for path in (image, first_pdf, second_pdf, *email_matches)), directory
    return image, first_pdf, second_pdf, email_matches[0]


def _body(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, f"{response.request.url}: {response.text}"
    return json.loads(response.content, parse_float=Decimal)


def _open_repository(path: Path) -> BillingRepository:
    return BillingRepository(DatabaseManager(path))


def _invoice(
    reference: str,
    amount: str = "100.00",
    *,
    total: str | None = None,
    quantity: str | None = None,
) -> tuple[str, bytes]:
    workbook = Workbook()
    try:
        sheet = workbook.active
        for row in (
            [f"Invoice No. {reference}"],
            ["Date: 2026-10-01"],
            [f"Issuer: {NEPTUNE}"],
            [f"Bill To: {TARGETING}"],
            ["Description", "Currency", "Quantity", "Amount"],
            ["Handling Charge", "USD", quantity, amount],
            ["Total", "USD", None, amount if total is None else total],
        ):
            sheet.append(row)
        with BytesIO() as output:
            workbook.save(output)
            return f"{reference}.xlsx", output.getvalue()
    finally:
        workbook.close()


def _upload(
    client: TestClient,
    *files: tuple[str, bytes],
    entity: str = NEPTUNE,
    wait: bool = True,
) -> httpx.Response:
    return client.post(
        BATCHES,
        params={"wait": "true" if wait else "false"},
        data={"accounting_entity": entity, "ocr_mode": FALLBACK},
        files=[("files", file) for file in files],
    )


def _record_url(batch_path: str, record: dict) -> str:
    return f"{batch_path}/records/{record['id']}"


def _records_by_reference(batch: dict) -> dict[str, dict]:
    return {record["document_info"]["reference_no"]: record for record in batch["records"]}


def _source_records(batch: dict, doc_id: str) -> list[dict]:
    return [row for row in batch["records"] if row["source_doc_id"] == doc_id]


def _assert_decimal_fields(charges: dict) -> None:
    assert charges["amount"].as_tuple() == LARGE_AMOUNT.as_tuple()
    assert charges["quantity"].as_tuple() == FRACTIONAL_QUANTITY.as_tuple()


def _assert_decimal_tokens(payload: bytes) -> None:
    assert re.search(rb'"amount"\s*:\s*9007199254740991\.01(?=\s*[,}])', payload)
    assert re.search(rb'"quantity"\s*:\s*0\.0025(?=\s*[,}])', payload)


def _http_routes(routes: list) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (included := getattr(route, "original_router", None)) is not None:
            yield from _http_routes(included.routes)


def test_all_contract_operations_are_mounted_and_browser_writes_are_guarded(
    client: TestClient,
) -> None:
    contract = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    expected = {
        (method.upper(), path): operation["operationId"]
        for path, operations in contract["paths"].items()
        for method, operation in operations.items()
        if method.upper() in write_methods | {"GET"}
    }
    registered = {
        (method, route.path): route.operation_id
        for route in _http_routes(client.app.routes)
        for method in route.methods
        if route.path.startswith(f"{API}/")
    }
    assert len(contract["paths"]) == 24 and len(expected) == 29
    assert {key: registered[key] for key in expected} == expected
    assert _body(client.get("/openapi.json")) == contract

    origin = {"Origin": "http://127.0.0.1:9999"}
    for method, route in sorted(registered):
        if method in write_methods:
            path = re.sub(r"\{[^}]+\}", "origin-probe", route)
            error = _body(client.request(method, path, headers=origin), 403)
            assert error["code"] == "FORBIDDEN_ORIGIN", (method, route)

    assert _body(client.get(f"{API}/health", headers=origin))["status"] == "ok"
    assert _body(client.get(BATCHES)) == []


class _GatedLocalOcr:
    """Pause the image recognizer while the real worker has a live extraction stage."""

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.local = RapidOcrFallback()

    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]:
        self.entered.set()
        if not self.release.wait(WAIT_SECONDS):
            raise TimeoutError("The test did not release the OCR worker")
        return self.local.recognize_image_bytes(content, filename)


def _wait_for_terminal_batch(client: TestClient, path: str) -> dict:
    deadline = monotonic() + OCR_COMPLETION_SECONDS
    while True:
        batch = _body(client.get(path))
        if batch["status"] != "PROCESSING":
            return batch
        assert monotonic() < deadline, f"{path} remained at {batch['stage']}"
        sleep(0.05)


def _assert_sample_batch(
    batch: dict, repository: BillingRepository, samples: tuple[Path, Path, Path, Path]
) -> dict[str, str]:
    assert (batch["status"], batch["stage"], batch["total_files"]) == ("COMPLETED", "COMPLETED", 4)
    assert batch["failed_documents"] == []
    stored = repository.get_batch(batch["id"])
    assert stored is not None
    documents = {doc["id"]: doc for doc in batch["documents"]}
    roots = {
        doc["original_filename"]: doc["id"]
        for doc in documents.values()
        if doc["parent_doc_id"] is None
    }
    assert roots.keys() == {path.name for path in samples}
    stored_documents = {doc["id"]: doc for doc in stored["documents"]}
    assert documents.keys() == stored_documents.keys()
    for sample in samples:
        assert (
            stored_documents[roots[sample.name]]["content_sha256"]
            == sha256(sample.read_bytes()).hexdigest()
        )

    image, first_pdf, second_pdf, email = samples
    children = [doc for doc in documents.values() if doc["parent_doc_id"] == roots[email.name]]
    assert len(children) == 4 and len(documents) == 8
    (body,) = [doc for doc in children if doc["source_kind"] == "EML_BODY"]
    assert {doc["sanitized_filename"] for doc in children if doc is not body} == {
        "InsertPic_C25B.jpg",
        "4055331281_MBL.pdf",
        "4055331281_HBL.pdf",
    }
    (signature,) = [doc for doc in children if doc["sanitized_filename"] == "InsertPic_C25B.jpg"]
    assert (signature["status"], signature["extraction_channel"]) == (
        "SKIPPED_NOISE",
        "SKIPPED_SIGNATURE",
    )

    expected = [
        (
            roots[image.name],
            "26.90",
            [("Wharfage Administration Fee", "5.00"), ("Wharfage", "21.90")],
        ),
        (roots[first_pdf.name], "245.00", [("On Carriage Additional", "245.00")]),
        (
            roots[second_pdf.name],
            "1410.40",
            [
                ("Handling Charge", "65.00"),
                ("Detention Free Time Charge", "1310.40"),
                ("Handling Charge", "35.00"),
            ],
        ),
        (body["id"], None, [("Handling Charge", "50.00")]),
    ]
    assert len(batch["records"]) == len(stored["records"]) == 7
    for doc_id, total, expected_lines in expected:
        assert Counter(
            (row["charge_details"]["charge_item"], row["charge_details"]["amount"])
            for row in _source_records(batch, doc_id)
        ) == Counter((name, Decimal(amount)) for name, amount in expected_lines), doc_id
        snapshot = stored_documents[doc_id]["extraction_snapshot"]
        assert snapshot is not None
        assert Counter(line.charge.amount for line in snapshot.charges) == Counter(
            Decimal(amount) for _, amount in expected_lines
        ), doc_id
        if total is not None:
            assert snapshot.has_explicit_total and snapshot.total_amount == Decimal(total)

    assert {row.id: (row.source_doc_id, row.amount) for row in stored["records"]} == {
        row["id"]: (row["source_doc_id"], row["charge_details"]["amount"])
        for row in batch["records"]
    }
    return roots


def test_four_real_samples_resume_from_extraction_and_survive_review_and_reopen(
    settings: AppSettings, real_samples: tuple[Path, Path, Path, Path]
) -> None:
    ocr = _GatedLocalOcr()
    with (
        BatchService(settings, local_ocr=ocr, extractor_mode="rule_only") as service,
        TestClient(create_app(batch_service=service)) as client,
    ):
        try:
            accepted = _body(
                _upload(
                    client,
                    *((sample.name, sample.read_bytes()) for sample in real_samples),
                    entity=ZIM,
                    wait=False,
                ),
                202,
            )
            batch_path = f"{BATCHES}/{accepted['id']}"
            assert accepted["stage"] == "QUEUED" and accepted["status"] == "PROCESSING"
            assert ocr.entered.wait(WAIT_SECONDS), "The batch never reached local OCR"
            extracting = _body(client.get(batch_path))
            assert (extracting["status"], extracting["stage"]) == ("PROCESSING", "EXTRACTING")
            assert extracting["records"] == []
        finally:
            ocr.release.set()
        uploaded = _wait_for_terminal_batch(client, batch_path)
        demo = _body(
            client.post(f"{BATCHES}/demo", json={"accounting_entity": ZIM, "ocr_mode": FALLBACK}),
            201,
        )

    repository = _open_repository(settings.sqlite_db_path)
    roots = _assert_sample_batch(uploaded, repository, real_samples)
    demo_roots = _assert_sample_batch(demo, repository, real_samples)
    assert set(roots.values()).isdisjoint(demo_roots.values())
    image_doc, first_pdf = (roots[sample.name] for sample in real_samples[:2])
    (administration, wharfage) = sorted(
        _source_records(uploaded, image_doc), key=lambda row: row["charge_details"]["amount"]
    )
    assert [row["financial_attributes"]["posting_type"] for row in (administration, wharfage)] == [
        "AR",
        "AR",
    ]

    approve_reason, reject_reason = "已核", "退" * 499 + "🧾"
    edited_charge = "复核后转运附加费"
    with TestClient(create_app(settings=settings)) as reopened:
        demo_path = f"{BATCHES}/{demo['id']}"
        assert _body(reopened.get(batch_path)) == uploaded
        assert _body(reopened.get(demo_path)) == demo

        cross_batch = reopened.patch(_record_url(demo_path, wharfage), json={"amount": 1})
        assert _body(cross_batch, 404)["code"] == "NOT_FOUND"
        cross_document = reopened.patch(
            f"{demo_path}/documents/{image_doc}", json={"total_amount": 1}
        )
        assert _body(cross_document, 404)["code"] == "NOT_FOUND"

        _body(
            reopened.post(
                f"{API}/dictionary",
                json={
                    "raw_alias": "On Carriage Additional - US Em",
                    "standard_name": edited_charge,
                },
            ),
            201,
        )
        applied = _body(reopened.post(f"{batch_path}/reapply-dictionary"))
        (renamed,) = _source_records(applied, first_pdf)
        assert renamed["charge_details"]["charge_item"] == edited_charge
        assert _body(reopened.get(demo_path)) == demo

        wharfage_path = _record_url(batch_path, wharfage)
        approved = _body(reopened.post(f"{wharfage_path}/approve", json={"reason": approve_reason}))
        switched = _body(
            reopened.post(
                f"{batch_path}/switch-entity",
                json={"accounting_entity": FORWARDER, "expected_version": approved["version"]},
            )
        )
        assert switched["reset_approval_count"] == 1
        image_rows = _source_records(switched["batch"], image_doc)
        assert {row["id"] for row in image_rows} == {administration["id"], wharfage["id"]}
        assert {row["financial_attributes"]["posting_type"] for row in image_rows} == {"AP"}

        _body(reopened.patch(wharfage_path, content=b'{"amount":22.90}'))
        blocker = _body(
            reopened.post(f"{wharfage_path}/approve", json={"reason": approve_reason}), 422
        )
        assert blocker["code"] == "HARD_BLOCKER_CANNOT_APPROVE"
        assert "AMOUNT_TOTAL_MISMATCH" in blocker["details"]["blocker_codes"]
        repaired = _body(
            reopened.patch(
                f"{batch_path}/documents/{image_doc}",
                content=b'{"total_amount":27.90,"total_currency":"USD"}',
            )
        )
        corrected_doc = next(doc for doc in repaired["documents"] if doc["id"] == image_doc)
        assert corrected_doc["total_amount"] == Decimal("27.90")

        for action in ("approve", "reject"):
            for invalid in ("核", reject_reason + "退"):
                response = reopened.post(f"{wharfage_path}/{action}", json={"reason": invalid})
                assert _body(response, 422)["code"] == "INVALID_REVIEW_REASON"
        assert _body(reopened.get(batch_path)) == repaired
        _body(reopened.post(f"{wharfage_path}/approve", json={"reason": approve_reason}))
        final = _body(
            reopened.post(
                f"{_record_url(batch_path, administration)}/reject",
                json={"reason": reject_reason},
            )
        )
        assert _body(reopened.get(demo_path)) == demo

    repository = _open_repository(settings.sqlite_db_path)
    stored_image = repository.get_document(uploaded["id"], image_doc)
    snapshot = stored_image["extraction_snapshot"]
    assert (snapshot.total_amount, snapshot.total_currency, snapshot.has_explicit_total) == (
        Decimal("27.90"),
        "USD",
        True,
    )
    assert stored_image["metadata"]["document_totals_review"]["original"]["total_amount"] == (
        Decimal("26.90")
    )
    assert [line.charge.amount for line in snapshot.charges] == [Decimal("5.00"), Decimal("21.90")]
    stored_rows = {row.id: row for row in repository.list_billing_records(uploaded["id"])}
    assert stored_rows[wharfage["id"]].review_status == "APPROVED"
    assert stored_rows[wharfage["id"]].human_review_history[-1].reason == approve_reason
    assert stored_rows[administration["id"]].review_status == "REJECTED"
    assert stored_rows[administration["id"]].reject_reason == reject_reason
    assert stored_rows[administration["id"]].human_review_history[-1].reason == reject_reason
    with TestClient(create_app(settings=settings)) as final_client:
        assert _body(final_client.get(batch_path)) == final


def test_restart_recovers_an_extraction_orphan_without_changing_completed_batch(
    settings: AppSettings,
) -> None:
    with TestClient(create_app(settings=settings)) as client:
        healthy = _body(_upload(client, _invoice("INV-HEALTHY-1")), 201)

    repository = _open_repository(settings.sqlite_db_path)
    orphan = repository.create_batch(NEPTUNE, stage=BatchStage.EXTRACTING, total_files=1)
    doc_id = uuid4().hex
    filename, original = _invoice("INV-INTERRUPTED-1")
    repository.upsert_source_documents(
        orphan["id"],
        [
            ParsedDocument(
                doc_id=doc_id,
                original_filename=filename,
                sanitized_filename=filename,
                source_kind="EXCEL",
                content_sha256=sha256(original).hexdigest(),
                extraction_channel="EXCEL_PARSER",
                status="PARSED",
            )
        ],
    )
    assert (orphan["status"], orphan["stage"]) == ("PROCESSING", "EXTRACTING")
    path = f"{BATCHES}/{orphan['id']}"
    with TestClient(create_app(settings=settings)) as restarted:
        recovered = _body(restarted.get(path))
        assert (recovered["status"], recovered["stage"]) == ("PARTIAL_ERROR", "FAILED")
        assert "服务重启" in recovered["stage_message"]
        assert recovered["version"] == orphan["version"] + 1 and recovered["failed_files"] == 1
        (failure,) = recovered["failed_documents"]
        assert (
            failure["doc_id"] == doc_id and failure["error_message"] == recovered["stage_message"]
        )
        assert _body(restarted.get(f"{BATCHES}/{healthy['id']}")) == healthy

    with TestClient(create_app(settings=settings)) as restarted_again:
        assert _body(restarted_again.get(path)) == recovered
        repaired = _body(
            restarted_again.post(
                f"{path}/documents/{doc_id}/retry", files={"file": (filename, original)}
            )
        )
        assert (repaired["status"], repaired["stage"], repaired["failed_documents"]) == (
            "COMPLETED",
            "COMPLETED",
            [],
        )
        assert set(_records_by_reference(repaired)) == {"INV-INTERRUPTED-1"}
    with repository.db.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_six_mixed_records_export_exactly_three_five_and_six_with_decimal_bytes(
    settings: AppSettings, client: TestClient, billing_schema: Draft202012Validator
) -> None:
    batch = _body(
        _upload(
            client,
            _invoice("INV-EXACT-1"),
            _invoice("INV-READY-2"),
            _invoice("INV-READY-3"),
            _invoice("INV-PENDING-4", total="99.00"),
            _invoice("INV-PENDING-5", total="98.00"),
            _invoice("INV-REJECTED-6"),
        ),
        201,
    )
    batch_path = f"{BATCHES}/{batch['id']}"
    records = _records_by_reference(batch)
    exact = records["INV-EXACT-1"]
    exact_path = _record_url(batch_path, exact)
    _body(client.patch(exact_path, content=b'{"amount":9007199254740991.01,"quantity":0.0025}'))
    _body(
        client.patch(
            f"{batch_path}/documents/{exact['source_doc_id']}",
            content=b'{"total_amount":9007199254740991.01,"total_currency":"USD"}',
        )
    )
    _body(client.post(f"{exact_path}/approve", json={"reason": "精度核对"}))
    reject_reason = "作废单据"
    final = _body(
        client.post(
            f"{_record_url(batch_path, records['INV-REJECTED-6'])}/reject",
            json={"reason": reject_reason},
        )
    )
    assert Counter(row["review_status"] for row in final["records"]) == {
        "AUTO_READY": 2,
        "APPROVED": 1,
        "NEEDS_REVIEW": 2,
        "REJECTED": 1,
    }
    repository = _open_repository(settings.sqlite_db_path)
    stored = repository.get_billing_record(batch["id"], exact["id"])
    _assert_decimal_fields({"amount": stored.amount, "quantity": stored.quantity})

    official_refs = {"INV-EXACT-1", "INV-READY-2", "INV-READY-3"}
    full_refs = official_refs | {"INV-PENDING-4", "INV-PENDING-5"}
    audit_refs = full_refs | {"INV-REJECTED-6"}
    downloads = {}
    for mode, expected_counts, references in (
        ("official", (3, 2, 1), official_refs),
        ("full_standard", (5, 0, 1), full_refs),
        ("audit", (6, 0, 0), audit_refs),
    ):
        preview = _body(client.get(f"{batch_path}/export", params={"mode": mode}))
        assert preview["mode"] == mode
        assert (
            preview["included_count"],
            preview["excluded_pending_count"],
            preview["excluded_rejected_count"],
        ) == expected_counts, mode
        download = client.get(f"{batch_path}/export/download", params={"mode": mode})
        data = _body(download)
        assert download.headers["content-disposition"].startswith("attachment;")
        assert data == preview["records"] and len(data) == expected_counts[0], mode
        billing_schema.validate(data)
        _assert_decimal_tokens(download.content)
        by_reference = _records_by_reference({"records": data})
        assert by_reference.keys() == references, mode
        _assert_decimal_fields(by_reference["INV-EXACT-1"]["charge_details"])
        downloads[mode] = by_reference

    rejected = downloads["audit"]["INV-REJECTED-6"]["audit_trail"]
    assert rejected["review_status"] == "REJECTED"
    assert rejected["human_review_history"][-1]["action"] == "REJECT"
    assert rejected["human_review_history"][-1]["reason"] == reject_reason


def _sync_receipts(repository: BillingRepository, batch_id: str) -> list[tuple]:
    with repository.db.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                """SELECT id, billing_record_id, record_updated_at, existing_bill_id
                   FROM baseline_sync_items WHERE batch_id = ? ORDER BY record_updated_at""",
                (batch_id,),
            )
        ]


def test_third_baseline_sync_updates_the_original_bill_and_preserves_both_receipts(
    settings: AppSettings, client: TestClient
) -> None:
    batch = _body(_upload(client, _invoice("INV-SYNC-1")), 201)
    path = f"{BATCHES}/{batch['id']}"
    (row,) = batch["records"]
    sync_path = f"{path}/sync-baseline"
    repository = _open_repository(settings.sqlite_db_path)

    assert _body(client.post(sync_path)) == {
        "created_count": 1,
        "updated_count": 0,
        "skipped_count": 0,
    }
    (first_bill,) = _body(client.get(BILLS))
    assert (first_bill["version"], first_bill["amount"]) == (1, Decimal("100.00"))
    (first_receipt,) = _sync_receipts(repository, batch["id"])
    assert first_receipt[1:] == (row["id"], row["updated_at"], first_bill["id"])

    assert _body(client.post(sync_path)) == {
        "created_count": 0,
        "updated_count": 0,
        "skipped_count": 1,
    }
    assert _body(client.get(BILLS)) == [first_bill]
    assert _sync_receipts(repository, batch["id"]) == [first_receipt]

    record_path = _record_url(path, row)
    _body(client.patch(record_path, content=b'{"amount":101.50}'))
    _body(
        client.patch(
            f"{path}/documents/{row['source_doc_id']}",
            content=b'{"total_amount":101.50,"total_currency":"USD"}',
        )
    )
    approved = _body(client.post(f"{record_path}/approve", json={"reason": "确认调整"}))
    (edited,) = approved["records"]
    assert (edited["id"], edited["review_status"]) == (row["id"], "APPROVED")
    assert _sync_receipts(repository, batch["id"]) == [first_receipt]

    assert _body(client.post(sync_path)) == {
        "created_count": 0,
        "updated_count": 1,
        "skipped_count": 0,
    }
    (updated_bill,) = _body(client.get(BILLS))
    assert (updated_bill["id"], updated_bill["version"], updated_bill["amount"]) == (
        first_bill["id"],
        2,
        Decimal("101.50"),
    )
    original_receipt, updated_receipt = _sync_receipts(repository, batch["id"])
    assert original_receipt == first_receipt
    assert updated_receipt[1:] == (row["id"], edited["updated_at"], first_bill["id"])


def test_overlapping_switch_with_stale_version_cannot_overwrite_a_committed_patch(
    settings: AppSettings,
) -> None:
    switch_started, patch_finished = Event(), Event()
    app = create_app(settings=settings)

    @app.middleware("http")
    async def let_patch_commit_first(request: Request, call_next):
        if request.url.path.endswith("/switch-entity"):
            switch_started.set()
            if not await run_in_threadpool(patch_finished.wait, WAIT_SECONDS):
                raise TimeoutError("The concurrent PATCH did not finish")
        return await call_next(request)

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as worker:
        batch = _body(_upload(client, _invoice("INV-RACE-1")), 201)
        path = f"{BATCHES}/{batch['id']}"
        (row,) = batch["records"]
        version = batch["version"]
        switch = worker.submit(
            client.post,
            f"{path}/switch-entity",
            json={"accounting_entity": TARGETING, "expected_version": version},
        )
        try:
            assert switch_started.wait(WAIT_SECONDS), "The switch request never reached the app"
            committed = _body(
                client.patch(
                    _record_url(path, row),
                    headers={"Origin": "http://127.0.0.1:5173"},
                    json={"charge_item": "核定手续费", "expected_version": version},
                )
            )
        finally:
            patch_finished.set()
        conflict = _body(switch.result(timeout=WAIT_SECONDS), 409)
        assert conflict["code"] == "BATCH_CONFLICT"
        assert conflict["details"] == {
            "resource_id": batch["id"],
            "expected": version,
            "actual": version + 1,
        }
        assert _body(client.get(path)) == committed

    repository = _open_repository(settings.sqlite_db_path)
    persisted = repository.get_batch(batch["id"])
    assert persisted["accounting_entity"] == NEPTUNE and persisted["version"] == version + 1
    (updated,) = persisted["records"]
    assert (updated.id, updated.charge_item, updated.posting_type) == (
        row["id"],
        "核定手续费",
        "AR",
    )
    assert [entry["action"] for entry in repository.list_review_audit_logs(batch["id"])] == [
        "PATCH_RECORD"
    ]


def test_cli_bundle_import_preserves_decimal_values_and_keeps_both_baselines_isolated(
    tmp_path: Path, settings: AppSettings, billing_schema: Draft202012Validator
) -> None:
    cli_db = tmp_path / "cli" / "billing.sqlite"
    bundle_file = tmp_path / "review-bundle.json"
    reference = "INV-CLI-EXACT-1"
    source_name, source_bytes = _invoice(
        reference, str(LARGE_AMOUNT), quantity=str(FRACTIONAL_QUANTITY)
    )
    invoice = tmp_path / source_name
    invoice.write_bytes(source_bytes)
    bill_id = uuid4().hex
    shared_fields = {
        "id": bill_id,
        "accounting_entity": NEPTUNE,
        "reference_no": reference,
        "charge_item": "Handling Charge",
        "currency": "USD",
        "posting_type": "AR",
    }
    baseline = tmp_path / "cli-baseline.json"
    baseline.write_text(
        json.dumps([shared_fields | {"amount": "30.00", "payment_term": "PP"}]), encoding="utf-8"
    )
    with TestClient(create_app(settings=settings)) as client:
        _body(
            client.post(
                f"{BILLS}/import",
                json={"bills": [shared_fields | {"amount": "40.00", "payment_term": "CC"}]},
            )
        )
        workbench_ledger = _body(client.get(BILLS))
        result = CliRunner().invoke(
            cli_app,
            [
                "parse",
                str(invoice),
                "--entity",
                NEPTUNE,
                "--bills",
                str(baseline),
                "--db-path",
                str(cli_db),
                "--bundle-out",
                str(bundle_file),
                "--output-mode",
                "audit",
            ],
        )
        assert result.exit_code == 0, (result.stderr, result.exception)
        (cli_export,) = json.loads(result.stdout, parse_float=Decimal)
        billing_schema.validate([cli_export])
        _assert_decimal_fields(cli_export["charge_details"])

        bundle_bytes = bundle_file.read_bytes()
        _assert_decimal_tokens(bundle_bytes)
        bundle = json.loads(bundle_bytes, parse_float=Decimal)
        assert bundle["bundle_version"] == "1.0"
        (source_bill,) = bundle["existing_bills_snapshot"]
        assert (source_bill["id"], source_bill["amount"], source_bill["payment_term"]) == (
            bill_id,
            Decimal("30.00"),
            "PP",
        )

        imported = _body(
            client.post(
                f"{BATCHES}/import-review-bundle",
                content=bundle_bytes,
                headers={"Content-Type": "application/json"},
            ),
            201,
        )
        (row,) = imported["records"]
        _assert_decimal_fields(row["charge_details"])
        assert row["matched_existing_bill_id"] == bill_id
        assert row["charge_details"]["payment_term"] == "PP"
        assert _body(client.get(BILLS)) == workbench_ledger

    source = _open_repository(cli_db)
    target = _open_repository(settings.sqlite_db_path)
    (cli_batch,) = source.list_batches()
    assert source.list_existing_bills() == []
    assert imported["id"] != cli_batch["id"]
    (target_bill,) = target.list_existing_bills()
    assert (target_bill.id, target_bill.amount, target_bill.payment_term) == (
        bill_id,
        Decimal("40.00"),
        "CC",
    )
    stored = target.get_batch(imported["id"])
    (scoped_bill,) = stored["batch_existing_bills"]
    assert (scoped_bill.id, scoped_bill.amount, scoped_bill.payment_term) == (
        bill_id,
        Decimal("30.00"),
        "PP",
    )
    (saved,) = stored["records"]
    assert (saved.matched_existing_bill_id, saved.payment_term) == (bill_id, "PP")
    assert saved.source_doc_id not in {
        doc["id"] for doc in source.get_batch(cli_batch["id"])["documents"]
    }
    _assert_decimal_fields({"amount": saved.amount, "quantity": saved.quantity})

    with TestClient(create_app(settings=settings)) as reopened:
        path = f"{BATCHES}/{imported['id']}"
        assert _body(reopened.get(path)) == imported
        rechecked = _body(reopened.post(f"{path}/reapply-dictionary"))
        (rechecked_row,) = rechecked["records"]
        assert rechecked_row["charge_details"]["payment_term"] == "PP"
        assert _body(reopened.get(BILLS)) == workbench_ledger
        downloaded = reopened.get(f"{path}/export/download", params={"mode": "audit"})
        items = _body(downloaded)
        billing_schema.validate(items)
        _assert_decimal_tokens(downloaded.content)
        _assert_decimal_fields(items[0]["charge_details"])
