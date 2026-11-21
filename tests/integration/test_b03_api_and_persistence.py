"""Run the billing API, SQLite, and CLI together with local files and no network."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openpyxl import Workbook
from PIL import Image
from starlette.concurrency import run_in_threadpool
from typer.testing import CliRunner

from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.cli.app import app as cli_app
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain import validate_batch
from shipping_billing_agent.domain.entity_resolver import (
    LLM_CHARGE_MISMATCH_MESSAGE,
    LLM_FIELD_CONFLICT_MESSAGE,
    LLM_PARTY_CONFLICT_MESSAGE,
)
from shipping_billing_agent.domain.models import (
    AlertCode,
    BatchStage,
    ParsedDocument,
    ValidationAlert,
    dumps_decimal_json,
    loads_decimal_json,
)
from shipping_billing_agent.gateways.local_ocr import RapidOcrFallback
from shipping_billing_agent.services import (
    BatchService,
    ServiceValidationError,
    export_review_bundle,
)
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


def _import_bundle(client: TestClient, bundle: dict[str, Any]) -> dict[str, Any]:
    return _body(
        client.post(f"{BATCHES}/import-review-bundle", content=dumps_decimal_json(bundle)), 201
    )


def _open_repository(path: Path) -> BillingRepository:
    return BillingRepository(DatabaseManager(path))


def _invoice(
    reference: str,
    amount: str = "100.00",
    *,
    total: str | None = None,
    quantity: str | None = None,
    issuer: str = NEPTUNE,
) -> tuple[str, bytes]:
    workbook = Workbook()
    try:
        sheet = workbook.active
        for row in (
            [f"Invoice No. {reference}"],
            ["Date: 2026-10-01"],
            [f"Issuer: {issuer}"],
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
    existing_bills_json: str | None = None,
) -> httpx.Response:
    data = {"accounting_entity": entity, "ocr_mode": FALLBACK}
    if existing_bills_json is not None:
        data["existing_bills_json"] = existing_bills_json
    return client.post(
        BATCHES,
        params={"wait": "true" if wait else "false"},
        data=data,
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


@pytest.mark.parametrize(
    ("source_total", "peer_amount", "remaining_blocker"),
    [
        ("100.00", None, None),
        ("97.00", None, ("AMOUNT_TOTAL_MISMATCH", "total_amount")),
        ("100.00", "95.00", ("CROSS_DOCUMENT_CONFLICT", "amount")),
    ],
    ids=["confirmed-fields", "printed-total-still-disagrees", "other-document-still-disagrees"],
)
def test_review_confirmation_clears_only_the_reported_extraction_conflicts(
    client: TestClient,
    settings: AppSettings,
    source_total: str,
    peer_amount: str | None,
    remaining_blocker: tuple[str, str] | None,
) -> None:
    reference = "INV-REVIEW-13"
    files = [_invoice(reference, total=source_total)]
    if peer_amount is not None:
        _, peer = _invoice(reference, amount=peer_amount)
        files.append(("conflicting-invoice.xlsx", peer))
    batch = _body(_upload(client, *files), 201)
    batch_id = batch["id"]
    batch_path = f"{BATCHES}/{batch_id}"
    repository = _open_repository(settings.sqlite_db_path)
    rows = repository.list_billing_records(batch_id)
    original = next(row for row in rows if row.amount == Decimal("100.00"))
    assert original.reference_no == reference
    parse_issues = [
        ValidationAlert.from_code(
            AlertCode.CROSS_DOCUMENT_CONFLICT,
            LLM_FIELD_CONFLICT_MESSAGE,
            field=field,
            expected=expected,
            actual=actual,
        )
        for field, expected, actual in (
            ("issuer", NEPTUNE, "CMA CGM"),
            ("reference_no", reference, "MODEL-REF"),
        )
    ]
    parse_issues.extend([
        ValidationAlert.from_code(
            AlertCode.CROSS_DOCUMENT_CONFLICT,
            LLM_PARTY_CONFLICT_MESSAGE,
            field="payee_hint",
            expected=NEPTUNE,
            actual="CMA CGM",
        ),
        ValidationAlert.from_code(
            AlertCode.AMOUNT_TOTAL_MISMATCH,
            LLM_CHARGE_MISMATCH_MESSAGE.format(name="Handling Charge"),
            field="reported_charge_amount",
            expected="100.00",
            actual="90.00",
        ),
    ])
    seeded_rows = [
        row.model_copy(update={"raw_parse_issues": parse_issues}) if row.id == original.id else row
        for row in rows
    ]
    repository.upsert_billing_records(batch_id, validate_batch(seeded_rows))
    record_url = f"{batch_path}/records/{original.id}"
    export_url = f"{batch_path}/export?mode=official"
    approval = {"reason": "已核对原始单据"}
    totals_or_peer = {remaining_blocker} if remaining_blocker else set()

    def blocker_fields(batch_detail: dict) -> set[tuple[str, str]]:
        record = next(row for row in batch_detail["records"] if row["id"] == original.id)
        return {
            (alert["code"], alert["field"])
            for alert in record["validation_alerts"]
            if alert["severity"] == "BLOCKER"
        }

    blocked = _body(client.post(f"{record_url}/approve", json=approval), 422)
    assert blocked["code"] == "HARD_BLOCKER_CANNOT_APPROVE"
    assert set(blocked["details"]["blocker_codes"]) == {
        "CROSS_DOCUMENT_CONFLICT", "AMOUNT_TOTAL_MISMATCH",
    }
    assert blocker_fields(_body(client.get(batch_path))) == totals_or_peer | {
        (issue.code.value, issue.field) for issue in parse_issues
    }

    partially_confirmed = _body(client.patch(record_url, json={"issuer": NEPTUNE}))
    assert blocker_fields(partially_confirmed) == totals_or_peer | {
        ("CROSS_DOCUMENT_CONFLICT", "reference_no"),
        ("AMOUNT_TOTAL_MISMATCH", "reported_charge_amount"),
    }
    rechecked = _body(client.patch(record_url, json={"reference_no": reference, "amount": 100}))
    assert blocker_fields(rechecked) == totals_or_peer
    saved = repository.get_billing_record(batch_id, original.id)
    assert saved is not None
    assert saved.human_overrides.keys() == {"issuer", "reference_no", "amount"}
    assert saved.raw_parse_issues == parse_issues
    assert _body(client.get(batch_path)) == rechecked

    if remaining_blocker:
        assert saved.review_status == "NEEDS_REVIEW"
        blocked = _body(client.post(f"{record_url}/approve", json=approval), 422)
        assert blocked["details"]["blocker_codes"] == [remaining_blocker[0]]
        assert _body(client.get(export_url))["records"] == []
    else:
        assert (saved.review_status, saved.validation_alerts) == ("AUTO_READY", [])
        approved = _body(client.post(f"{record_url}/approve", json=approval))
        assert approved["records"][0]["review_status"] == "APPROVED"
        (exported,) = _body(client.get(export_url))["records"]
        assert exported["document_info"]["reference_no"] == reference
        assert (exported["charge_details"]["amount"], exported["charge_details"]["currency"]) == (
            Decimal("100.00"), "USD",
        )


@pytest.mark.parametrize(
    ("conflicts", "confirmation", "remaining_fields"),
    [
        pytest.param(
            [("document_date", LLM_FIELD_CONFLICT_MESSAGE, "2026-10-01", "2026-10-02")],
            {"document_date": "2026-10-01"},
            set(),
            id="confirms-model-date",
        ),
        pytest.param(
            [("reference_no", "同一文件提供了不同的源单字段值", "INV-A", "INV-B")],
            {"reference_no": "INV-A"},
            {"reference_no"},
            id="preserves-rule-ocr-conflict",
        ),
        pytest.param(
            [
                ("payer_hint", LLM_PARTY_CONFLICT_MESSAGE, TARGETING, "Model Payer"),
                ("payee_hint", LLM_PARTY_CONFLICT_MESSAGE, NEPTUNE, "Model Payee"),
            ],
            {"recipient": "Targeting A, Inc."},
            {"payee_hint"},
            id="recipient-confirms-payer-only",
        ),
    ],
)
def test_review_confirmation_respects_extraction_evidence(
    client: TestClient,
    settings: AppSettings,
    conflicts: list[tuple[str, str, str, str]],
    confirmation: dict[str, str],
    remaining_fields: set[str],
) -> None:
    batch = _body(_upload(client, _invoice("INV-A")), 201)
    batch_id = batch["id"]
    repository = _open_repository(settings.sqlite_db_path)
    (original,) = repository.list_billing_records(batch_id)
    issues = [
        ValidationAlert.from_code(
            AlertCode.CROSS_DOCUMENT_CONFLICT,
            message,
            field=field,
            expected=expected,
            actual=actual,
        )
        for field, message, expected, actual in conflicts
    ]
    seeded = original.model_copy(update={"raw_parse_issues": issues})
    repository.upsert_billing_records(batch_id, validate_batch([seeded]))
    record_url = f"{BATCHES}/{batch_id}/records/{original.id}"
    approval = {"reason": "已核对原始单据"}

    assert _body(client.post(f"{record_url}/approve", json=approval), 422)["code"] == (
        "HARD_BLOCKER_CANNOT_APPROVE"
    )
    (reviewed,) = _body(client.patch(record_url, json=confirmation))["records"]
    assert {
        alert["field"] for alert in reviewed["validation_alerts"] if alert["severity"] == "BLOCKER"
    } == remaining_fields
    saved = repository.get_billing_record(batch_id, original.id)
    assert saved is not None
    assert saved.human_overrides == confirmation
    assert saved.raw_parse_issues == issues

    response = client.post(f"{record_url}/approve", json=approval)
    if remaining_fields:
        assert _body(response, 422)["code"] == "HARD_BLOCKER_CANNOT_APPROVE"
    else:
        (approved,) = _body(response)["records"]
        assert approved["review_status"] == "APPROVED"
        assert approved["document_info"]["document_date"] == "2026-10-01"


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
    assert len(contract["paths"]) == 25 and len(expected) == 30
    assert registered == expected
    assert _body(client.get("/openapi.json")) == contract
    for path in (
        f"{BATCHES}/{{batch_id}}/records",
        f"{BATCHES}/{{batch_id}}/records/{{record_id}}/approve",
    ):
        assert contract["paths"][path]["post"]["responses"]["404"] == {
            "$ref": "#/components/responses/NotFound"
        }

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
            with TestClient(create_app(settings=settings)) as second:
                assert _body(second.get(batch_path)) == extracting
                switch = second.post(
                    f"{batch_path}/switch-entity", json={"accounting_entity": TARGETING}
                )
                assert _body(switch, 409)["code"] == "BATCH_PROCESSING"
                assert _body(second.post(f"{batch_path}/sync-baseline"), 409)["code"] == (
                    "BATCH_PROCESSING"
                )
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


@pytest.mark.parametrize("packaged", [False, True], ids=["loose-copies", "mail-attachments"])
def test_replacing_deduplicated_invoice_keeps_surviving_copy_in_official_download(
    client: TestClient, packaged: bool
) -> None:
    invoice = _invoice("INV-101")
    if packaged:
        copies = []
        for name in ("first.eml", "second.eml", "third.eml"):
            mail = EmailMessage()
            mail.set_content(f"Original invoice enclosed in {name}.")
            mail.add_attachment(
                invoice[1], maintype="application", subtype="octet-stream", filename=invoice[0]
            )
            copies.append((name, mail.as_bytes()))
    else:
        copies = [("first.xlsx", invoice[1]), ("second.xlsx", invoice[1])]

    batch = _body(_upload(client, *copies, _invoice("INV-303", "42.00")), 201)
    path = f"{BATCHES}/{batch['id']}"
    rows = _records_by_reference(batch)
    assert len(batch["records"]) == 2 and rows.keys() == {"INV-101", "INV-303"}
    row_path = _record_url(path, rows["INV-303"])
    reviewed = _body(client.post(f"{row_path}/approve", json={"reason": "已核对"}))
    documents = {doc["id"]: doc for doc in batch["documents"]}
    owner = documents[rows["INV-101"]["source_doc_id"]]
    target = documents[owner["parent_doc_id"]] if packaged else owner
    copy_names = {invoice[0]} if packaged else {"first.xlsx", "second.xlsx"}
    survivors = [
        doc
        for doc in documents.values()
        if doc["id"] != owner["id"] and doc["original_filename"] in copy_names
    ]
    replacement = _body(
        client.post(
            f"{path}/documents/{target['id']}/retry",
            files={"file": _invoice("INV-202", "180.00")},
        )
    )
    assert replacement["status"] == "COMPLETED" and replacement["failed_documents"] == []
    rows = _records_by_reference(replacement)
    assert len(replacement["records"]) == 3
    assert rows["INV-101"]["source_doc_id"] in {doc["id"] for doc in survivors}
    if packaged:
        mail_names = {documents[doc["parent_doc_id"]]["original_filename"] for doc in survivors}
        assert len(mail_names) == 2
        assert set(rows["INV-101"]["source_evidence"]["source_paths"]) == {
            f"{email}/{invoice[0]}" for email in mail_names
        }
    assert rows["INV-202"]["source_doc_id"] == target["id"]
    assert rows["INV-303"] == _records_by_reference(reviewed)["INV-303"]

    reapplied = _body(client.post(f"{path}/reapply-dictionary"))
    assert len(reapplied["records"]) == 3
    downloaded = _body(client.get(f"{path}/export/download"))
    assert len(downloaded) == 3
    assert {
        row["document_info"]["reference_no"]: row["charge_details"]["amount"] for row in downloaded
    } == {
        "INV-101": Decimal("100.00"),
        "INV-202": Decimal("180.00"),
        "INV-303": Decimal("42.00"),
    }


@pytest.mark.parametrize("duplicate_replacement", [False, True], ids=["new-file", "reviewed-copy"])
def test_replacing_invoice_keeps_the_rejected_record_and_audit(
    client: TestClient, settings: AppSettings, duplicate_replacement: bool
) -> None:
    first = _invoice("INV-801")
    second = _invoice("INV-802", "160.00")
    files = [first, second] if duplicate_replacement else [first, ("copy.xlsx", first[1]), second]
    batch = _body(_upload(client, *files), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    rows = _records_by_reference(batch)
    rejected_ref = "INV-802" if duplicate_replacement else "INV-801"
    reason = "该单据已作废"
    reviewed = _body(
        client.post(f"{_record_url(path, rows[rejected_ref])}/reject", json={"reason": reason})
    )
    before = _records_by_reference(reviewed)[rejected_ref]
    repository = _open_repository(settings.sqlite_db_path)
    logs = repository.list_review_audit_logs(batch_id)
    assert [log["action"] for log in logs] == ["REJECT"]

    replaced = _body(
        client.post(
            f"{path}/documents/{rows['INV-801']['source_doc_id']}/retry",
            files={"file": second if duplicate_replacement else _invoice("INV-803")},
        )
    )

    updated = _records_by_reference(replaced)
    expected_refs = {"INV-802"} if duplicate_replacement else {"INV-801", "INV-802", "INV-803"}
    assert updated.keys() == expected_refs
    assert len(replaced["records"]) == len(expected_refs)
    if not duplicate_replacement:
        assert updated["INV-801"]["source_doc_id"] != rows["INV-801"]["source_doc_id"]
    assert (updated[rejected_ref]["id"], updated[rejected_ref]["created_at"]) == (
        before["id"],
        before["created_at"],
    )
    assert (updated[rejected_ref]["review_status"], updated[rejected_ref]["reject_reason"]) == (
        "REJECTED",
        reason,
    )
    assert repository.list_review_audit_logs(batch_id) == logs

    official = _body(client.get(f"{path}/export/download"))
    assert _records_by_reference({"records": official}).keys() == expected_refs - {rejected_ref}
    audit = _body(client.get(f"{path}/export/download", params={"mode": "audit"}))
    trail = _records_by_reference({"records": audit})[rejected_ref]["audit_trail"]
    assert trail["review_status"] == "REJECTED"
    assert trail["human_review_history"][-1]["action"] == "REJECT"
    assert trail["human_review_history"][-1]["reason"] == reason


@pytest.mark.parametrize("reupload", [False, True], ids=["retry-original", "same-byte-upload"])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_same_byte_ocr_change_keeps_audit_and_requires_review_before_official_download(
    client: TestClient,
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
    reupload: bool,
    decision: str,
) -> None:
    content = b"Original invoice image"
    ocr_amount = 80

    def recognize(_ocr: RapidOcrFallback, data: bytes, _filename: str) -> tuple[str, float]:
        assert data == content
        return (
            f"Invoice No. INV-801\nDate: 2026-10-01\nIssuer: {NEPTUNE}\nBill To: {TARGETING}\n"
            f"Handling Charge {ocr_amount} USD\nTotal USD {ocr_amount}",
            0.99,
        )

    monkeypatch.setattr(RapidOcrFallback, "recognize_image_bytes", recognize)
    batch = _body(_upload(client, ("original.png", content), ("copy.png", content)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    record_path = _record_url(path, row)
    assert row["charge_details"]["amount"] == 80
    _body(client.patch(record_path, json={"payment_term": "PP"}))
    reviewed = _body(client.post(f"{record_path}/{decision}", json={"reason": "已核对原件"}))
    assert reviewed["records"][0]["review_status"] == (
        "APPROVED" if decision == "approve" else "REJECTED"
    )
    download = f"{path}/export/download"
    expected_count = 1 if decision == "approve" else 0
    assert len(_body(client.get(download, params={"mode": "official"}))) == expected_count

    repository = _open_repository(settings.sqlite_db_path)
    before = repository.get_billing_record(batch_id, row["id"])
    logs = repository.list_review_audit_logs(batch_id)
    assert [log["action"] for log in logs] == ["PATCH_RECORD", decision.upper()]
    ocr_amount = 81
    retry_path = f"{path}/documents/{row['source_doc_id']}/retry"
    files = {"file": ("reuploaded.png", content)} if reupload else None
    retried = _body(client.post(retry_path, files=files))

    (updated,) = retried["records"]
    assert retried["failed_documents"] == []
    assert updated["id"] == row["id"] and updated["created_at"] == row["created_at"]
    assert updated["charge_details"]["amount"] == 81
    assert updated["review_status"] == "NEEDS_REVIEW"
    after = repository.get_billing_record(batch_id, row["id"])
    assert after.human_overrides == before.human_overrides
    assert after.human_review_history == before.human_review_history
    assert repository.list_review_audit_logs(batch_id) == logs
    assert _body(client.get(download, params={"mode": "official"})) == []
    reapplied = _body(client.post(f"{path}/reapply-dictionary"))
    assert reapplied["records"][0]["review_status"] == "NEEDS_REVIEW"
    assert _body(client.get(download, params={"mode": "official"})) == []
    reapproved = _body(client.post(f"{record_path}/approve", json={"reason": "确认新识别金额"}))
    assert reapproved["records"][0]["review_status"] == "APPROVED"
    (official,) = _body(client.get(download, params={"mode": "official"}))
    assert official["charge_details"]["amount"] == 81


@pytest.mark.parametrize("initial_fees", [(80, 5), (80,)], ids=["reorder", "insert-before"])
@pytest.mark.parametrize("reupload", [False, True], ids=["retry-original", "same-byte-upload"])
def test_same_byte_ocr_preserves_the_rejected_fee_when_same_charge_moves(
    client: TestClient,
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
    initial_fees: tuple[int, ...],
    reupload: bool,
) -> None:
    content = b"Original invoice image"
    fees = initial_fees

    def recognize(_ocr: RapidOcrFallback, data: bytes, _filename: str) -> tuple[str, float]:
        assert data == content
        lines = "\n".join(f"Handling Charge {amount} USD" for amount in fees)
        return (
            f"Invoice No. INV-801\nDate: 2026-10-01\nIssuer: {NEPTUNE}\nBill To: {TARGETING}\n"
            f"{lines}\nTotal USD {sum(fees)}",
            0.99,
        )

    monkeypatch.setattr(RapidOcrFallback, "recognize_image_bytes", recognize)
    batch = _body(_upload(client, ("original.png", content)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    rejected = batch["records"][0]
    assert rejected["charge_details"]["amount"] == 80
    record_path = _record_url(path, rejected)
    _body(client.patch(record_path, json={"payment_term": "PP"}))
    reason = "该笔费用作废"
    reviewed = _body(client.post(f"{record_path}/reject", json={"reason": reason}))
    assert reviewed["records"][0]["review_status"] == "REJECTED"
    repository = _open_repository(settings.sqlite_db_path)
    before = repository.get_billing_record(batch_id, rejected["id"])
    logs = repository.list_review_audit_logs(batch_id)
    assert [log["action"] for log in logs] == ["PATCH_RECORD", "REJECT"]

    fees = (5, 80)
    retry_path = f"{path}/documents/{rejected['source_doc_id']}/retry"
    files = {"file": ("reuploaded.png", content)} if reupload else None
    retried = _body(client.post(retry_path, files=files))

    assert retried["failed_documents"] == []
    allowed, blocked = retried["records"]
    assert (allowed["charge_details"]["amount"], allowed["review_status"]) == (5, "AUTO_READY")
    if len(initial_fees) == 2:
        assert allowed["id"] == batch["records"][1]["id"]
    assert (blocked["id"], blocked["created_at"], blocked["source_line_index"]) == (
        rejected["id"],
        rejected["created_at"],
        1,
    )
    assert (
        blocked["charge_details"]["amount"], blocked["review_status"], blocked["reject_reason"]
    ) == (80, "REJECTED", reason)
    after = repository.get_billing_record(batch_id, rejected["id"])
    assert after.human_review_history == before.human_review_history
    assert after.human_overrides == before.human_overrides
    assert repository.list_review_audit_logs(batch_id) == logs
    download = f"{path}/export/download"
    (official,) = _body(client.get(download))
    assert official["charge_details"]["amount"] == 5
    _body(client.post(f"{path}/reapply-dictionary"))
    assert _body(client.get(download)) == [official]


def test_same_byte_ocr_keeps_ambiguous_fees_out_of_the_synced_ledger(
    client: TestClient, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"Original invoice image"
    fees = (80, 5, 7)

    def recognize(_ocr: RapidOcrFallback, data: bytes, _filename: str) -> tuple[str, float]:
        assert data == content
        lines = "\n".join(f"Handling Charge {amount} USD" for amount in fees)
        return (
            f"Invoice No. INV-801\nDate: 2026-10-01\nIssuer: {NEPTUNE}\nBill To: {TARGETING}\n"
            f"{lines}\nTotal USD {sum(fees)}",
            0.99,
        )

    monkeypatch.setattr(RapidOcrFallback, "recognize_image_bytes", recognize)
    batch = _body(_upload(client, ("original.png", content)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    rejected, *automatic = batch["records"]
    assert [row["charge_details"]["amount"] for row in batch["records"]] == [80, 5, 7]
    assert {row["review_status"] for row in automatic} == {"AUTO_READY"}
    reason = "该笔费用作废"
    _body(client.post(f"{_record_url(path, rejected)}/reject", json={"reason": reason}))
    assert _body(client.post(f"{path}/sync-baseline")) == {
        "created_count": 2,
        "updated_count": 0,
        "skipped_count": 0,
    }
    repository = _open_repository(settings.sqlite_db_path)
    original_bills = _body(client.get(BILLS))
    assert sorted(row["amount"] for row in original_bills) == [5, 7]
    automatic_ids = {row["id"] for row in automatic}
    assert {receipt[1] for receipt in _sync_receipts(repository, batch_id)} == automatic_ids
    logs = repository.list_review_audit_logs(batch_id)

    fees = (6, 80, 8)
    retried = _body(client.post(f"{path}/documents/{rejected['source_doc_id']}/retry"))

    assert retried["failed_documents"] == []
    first, kept, last = retried["records"]
    assert [row["charge_details"]["amount"] for row in retried["records"]] == [6, 80, 8]
    assert (kept["id"], kept["review_status"], kept["reject_reason"]) == (
        rejected["id"],
        "REJECTED",
        reason,
    )
    assert repository.list_review_audit_logs(batch_id) == logs
    for ambiguous in (first, last):
        assert ambiguous["id"] not in automatic_ids
        assert (ambiguous["operation"], ambiguous["review_status"]) == (
            "PENDING_REVIEW",
            "NEEDS_REVIEW",
        )
        assert any(
            alert["code"] == "OCR_SUSPICIOUS_FLAG" and alert["field"] == "extraction_snapshot"
            for alert in ambiguous["validation_alerts"]
        )
    assert _body(client.get(f"{path}/export/download")) == []
    assert _body(client.post(f"{path}/sync-baseline")) == {
        "created_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
    }
    assert _body(client.get(BILLS)) == original_bills


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


def _ledger_bill(bill_id: str, reference: str, *, entity: str = NEPTUNE) -> dict[str, Any]:
    return {
        "id": bill_id,
        "reference_no": reference,
        "accounting_entity": entity,
        "charge_item": "Handling Charge",
        "amount": 80,
        "currency": "USD",
        "posting_type": "AR",
    }


@pytest.mark.parametrize("side", ["plus", "dash"])
def test_api_scopes_configured_punctuation_variants_to_each_entity(client: TestClient, side: str):
    names = {"plus": "Harbor+Orchid Logistics", "dash": "Harbor-Orchid Logistics"}
    for key, name in names.items():
        _body(
            client.post(
                f"{API}/entities", json={"canonical_name": name, "aliases": [f"Alias {key}"]}
            ),
            201,
        )
    bills = [_ledger_bill(key, f"INV-{key}", entity=name) for key, name in names.items()]
    _body(client.post(f"{BILLS}/import", json={"bills": bills}))

    for key, name in names.items():
        (found,) = _body(client.get(BILLS, params={"accounting_entity": name}))
        assert found["id"] == key
    ambiguous = _body(client.get(BILLS, params={"accounting_entity": "Harbor Orchid Logistics"}))
    assert ambiguous == []

    response = client.delete(
        BILLS,
        params={"accounting_entity": names[side]},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 204, response.text
    assert _body(client.get(BILLS, params={"accounting_entity": names[side]})) == []
    (remaining,) = _body(client.get(BILLS))
    assert remaining["id"] == next(key for key in names if key != side)


def test_api_scopes_shared_bills_to_explicitly_registered_branches(client: TestClient) -> None:
    headquarters = "Harbor Orchid Logistics"
    branch = f"{headquarters} (LAX)"
    profile = _body(client.post(f"{API}/entities", json={"canonical_name": headquarters}), 201)
    head_bill = _ledger_bill("hq", "INV-HQ", entity=headquarters)
    branch_bill = _ledger_bill("lax", "INV-LAX", entity=branch)
    _body(client.post(f"{BILLS}/import", json={"bills": [head_bill, branch_bill]}))

    def bill_ids(entity: str) -> set[str]:
        return {row["id"] for row in _body(client.get(BILLS, params={"accounting_entity": entity}))}

    def clear_headquarters() -> None:
        response = client.delete(
            BILLS,
            params={"accounting_entity": headquarters},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert response.status_code == 204, response.text

    assert bill_ids(headquarters) == {"hq"}
    assert bill_ids(branch) == {"lax"}
    replacement = _ledger_bill("new-hq", "INV-NEW-HQ", entity=headquarters)
    replace = {"replace_existing": True, "default_accounting_entity": headquarters}
    assert _body(client.post(f"{BILLS}/import", json=replace | {"bills": [replacement]})) == {
        "imported_count": 1,
        "total_count": 2,
    }
    assert bill_ids(headquarters) == {"new-hq"}
    assert bill_ids(branch) == {"lax"}
    clear_headquarters()
    assert bill_ids(headquarters) == set()
    assert bill_ids(branch) == {"lax"}

    _body(client.post(f"{BILLS}/import", json={"bills": [head_bill]}))
    _body(
        client.put(
            f"{API}/entities/{profile['id']}",
            json={"canonical_name": headquarters, "aliases": [branch]},
        )
    )
    assert bill_ids(headquarters) == bill_ids(f"{headquarters.lower()} （lax）") == {"hq", "lax"}
    assert _body(client.post(f"{BILLS}/import", json=replace | {"bills": [replacement]})) == {
        "imported_count": 1,
        "total_count": 1,
    }
    assert bill_ids(branch) == {"new-hq"}
    _body(client.post(f"{BILLS}/import", json={"bills": [branch_bill]}))
    clear_headquarters()
    assert _body(client.get(BILLS)) == []


@pytest.mark.parametrize(
    "issuer", ["Harbor-Orchid Logistics", "Harbor West", "harbor－orchid Logistics"]
)
def test_near_named_entities_cannot_share_approval_or_sync_each_others_invoices(
    client: TestClient, issuer: str
) -> None:
    plus, dash = "Harbor+Orchid Logistics", "Harbor-Orchid Logistics"
    for name, alias in ((plus, "Harbor East"), (dash, "Harbor West")):
        _body(
            client.post(f"{API}/entities", json={"canonical_name": name, "aliases": [alias]}), 201
        )

    batch = _body(_upload(client, _invoice("INV-PLUS", issuer=plus), entity=plus), 201)
    path = f"{BATCHES}/{batch['id']}"
    (record,) = batch["records"]
    approved = _body(
        client.post(f"{_record_url(path, record)}/approve", json={"reason": "确认加号主体账单"})
    )
    assert approved["records"][0]["review_status"] == "APPROVED"
    (official,) = _body(client.get(f"{path}/export/download"))
    assert official["financial_attributes"]["accounting_entity"] == plus

    switched = _body(client.post(f"{path}/switch-entity", json={"accounting_entity": dash}))
    assert switched["reset_approval_count"] == 1
    (updated,) = switched["batch"]["records"]
    assert updated["id"] == record["id"]
    assert updated["financial_attributes"] == {"accounting_entity": dash, "posting_type": None}
    assert (updated["review_status"], updated["operation"]) == ("NEEDS_REVIEW", "PENDING_REVIEW")
    assert {alert["code"] for alert in updated["validation_alerts"]} == {"ENTITY_NOT_MATCHED"}
    blocked_sync = {"created_count": 0, "updated_count": 0, "skipped_count": 0}
    assert _body(client.get(f"{path}/export/download")) == []
    assert _body(client.post(f"{path}/sync-baseline")) == blocked_sync

    reverse = _body(_upload(client, _invoice("INV-DASH", issuer=issuer), entity=plus), 201)
    reverse_path = f"{BATCHES}/{reverse['id']}"
    (other,) = reverse["records"]
    assert (other["review_status"], other["operation"]) == ("NEEDS_REVIEW", "PENDING_REVIEW")
    assert {alert["code"] for alert in other["validation_alerts"]} == {"ENTITY_NOT_MATCHED"}
    assert _body(client.get(f"{reverse_path}/export/download")) == []
    assert _body(client.post(f"{reverse_path}/sync-baseline")) == blocked_sync
    assert _body(client.get(BILLS)) == []


@pytest.mark.parametrize("scope", ["isolated", "imported-profile", "imported-null"])
@pytest.mark.parametrize(
    "sibling", ["Harbor-Orchid Logistics", "HARBOR+ORCHID LOGISTICS"], ids=["dash", "uppercase"]
)
def test_retry_keeps_registered_siblings_separate_in_isolated_batches(
    client: TestClient, settings: AppSettings, scope: str, sibling: str
) -> None:
    plus = "Harbor+Orchid Logistics"
    for name, aliases in ((plus, ["Harbor East", sibling]), (sibling, ["Harbor West"])):
        _body(
            client.post(f"{API}/entities", json={"canonical_name": name, "aliases": aliases}), 201
        )
    invoice = _invoice("INV-DASH", issuer=sibling)
    batch = _body(_upload(client, invoice, entity=plus, existing_bills_json="[]"), 201)
    assert batch["records"][0]["review_status"] == "NEEDS_REVIEW"
    repository = _open_repository(settings.sqlite_db_path)
    if scope != "isolated":
        bundle = export_review_bundle(repository, batch["id"])
        assert bundle["accounting_entity_profile"]["canonical_name"] == plus
        if scope == "imported-null":
            bundle["accounting_entity_profile"] = None
        batch = _import_bundle(client, bundle)
        assert batch["records"][0]["review_status"] == "NEEDS_REVIEW"

    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (document,) = batch["documents"]
    files = None if scope == "isolated" else {"file": invoice}
    retried = _body(client.post(f"{path}/documents/{document['id']}/retry", files=files))
    (row,) = retried["records"]
    assert retried["failed_documents"] == []
    assert row["charge_details"]["amount"] == 100
    assert (row["operation"], row["review_status"]) == ("PENDING_REVIEW", "NEEDS_REVIEW")
    assert {alert["code"] for alert in row["validation_alerts"]} == {"ENTITY_NOT_MATCHED"}
    assert _body(client.get(f"{path}/export/download")) == []
    assert _body(client.post(f"{path}/sync-baseline")) == {
        "created_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
    }
    assert _body(client.get(BILLS)) == []
    assert _sync_receipts(repository, batch_id) == []
    assert (export_review_bundle(repository, batch_id)["accounting_entity_profile"] is None) == (
        scope == "imported-null"
    )


@pytest.mark.parametrize(
    "resolution", ["none", "registered-sibling", "alias", "manual", "new-file"]
)
def test_cross_database_retry_requires_evidence_to_clear_the_entity_block(
    client: TestClient, settings: AppSettings, tmp_path: Path, resolution: str
) -> None:
    plus, dash = "Harbor+Orchid Logistics", "Harbor-Orchid Logistics"
    for name in (plus, dash):
        _body(client.post(f"{API}/entities", json={"canonical_name": name}), 201)
    invoice = _invoice("INV-PUNCTUATION", issuer=dash)
    batch = _body(_upload(client, invoice, entity=plus, existing_bills_json="[]"), 201)

    def blocked_record(detail: dict) -> dict:
        (row,) = detail["records"]
        assert (row["operation"], row["review_status"]) == ("PENDING_REVIEW", "NEEDS_REVIEW")
        assert row["charge_details"]["amount"] == 100
        assert row["financial_attributes"] == {"accounting_entity": plus, "posting_type": None}
        (alert,) = row["validation_alerts"]
        assert (alert["code"], alert["severity"]) == ("ENTITY_NOT_MATCHED", "BLOCKER")
        return row

    expected_alerts = blocked_record(batch)["validation_alerts"]
    source = _open_repository(settings.sqlite_db_path)
    bundle = export_review_bundle(source, batch["id"])
    (source_document,) = bundle["documents"]
    assert source_document["content_sha256"] == sha256(invoice[1]).hexdigest()

    target = tmp_path / "import-target"
    target_settings = AppSettings(
        _env_file=None, app_data_dir=target, sqlite_db_path=target / "billing.sqlite"
    )
    with TestClient(create_app(settings=target_settings)) as importer:
        entity_path = f"{API}/entities"
        current_names = {row["canonical_name"] for row in _body(importer.get(entity_path))}
        assert {plus, dash}.isdisjoint(current_names)
        if resolution == "registered-sibling":
            for name in (plus, dash):
                _body(importer.post(entity_path, json={"canonical_name": name}), 201)
        imported = _import_bundle(importer, bundle)
        record = blocked_record(imported)
        batch_id = imported["id"]
        path = f"{BATCHES}/{batch_id}"
        (document,) = imported["documents"]
        retry_path = f"{path}/documents/{document['id']}/retry"
        retried = _body(importer.post(retry_path, files={"file": invoice}))
        assert retried["failed_documents"] == []
        assert blocked_record(retried)["id"] == record["id"]
        assert _body(importer.get(f"{path}/export/download")) == []
        assert _body(importer.post(f"{path}/sync-baseline")) == {
            "created_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
        }
        assert _body(importer.get(BILLS)) == []
        target_repository = _open_repository(target_settings.sqlite_db_path)
        assert _sync_receipts(target_repository, batch_id) == []

        if resolution == "none":
            exported_again = export_review_bundle(target_repository, batch_id)
            assert blocked_record(exported_again)["validation_alerts"] == expected_alerts
            third = tmp_path / "second-import"
            third_settings = AppSettings(
                _env_file=None, app_data_dir=third, sqlite_db_path=third / "billing.sqlite"
            )
            with TestClient(create_app(settings=third_settings)) as second_importer:
                imported_again = _import_bundle(second_importer, exported_again)
                assert blocked_record(imported_again)["validation_alerts"] == expected_alerts
            return
        if resolution == "registered-sibling":
            return
        if resolution == "alias":
            profile = _body(importer.post(entity_path, json={"canonical_name": plus}), 201)
            _body(
                importer.put(
                    f"{entity_path}/{profile['id']}",
                    json={"canonical_name": plus, "aliases": [dash]},
                )
            )
        elif resolution == "manual":
            patched = _body(importer.patch(_record_url(path, record), json={"posting_type": "AR"}))
            assert patched["records"][0]["review_status"] == "AUTO_READY"
            (stored,) = target_repository.get_batch(batch_id)["records"]
            assert stored.human_overrides["posting_type"] == "AR"
        elif resolution == "new-file":
            corrected = _invoice("INV-PUNCTUATION", issuer=plus)
            assert corrected[1] != invoice[1]
            invoice = corrected

        (restored,) = _body(importer.post(retry_path, files={"file": invoice}))["records"]
        assert (restored["operation"], restored["review_status"]) == ("CREATE", "AUTO_READY")
        assert restored["financial_attributes"] == {"accounting_entity": plus, "posting_type": "AR"}
        assert restored["validation_alerts"] == []
        (official,) = _body(importer.get(f"{path}/export/download"))
        assert official["charge_details"]["amount"] == 100
        assert _body(importer.post(f"{path}/sync-baseline"))["created_count"] == 1
        (bill,) = _body(importer.get(BILLS))
        assert (bill["accounting_entity"], bill["amount"]) == (plus, 100)


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


def test_sync_conflicts_when_a_reviewed_record_is_rebound_to_another_bill(
    client: TestClient, settings: AppSettings
) -> None:
    reference = "INV-811"
    first = _ledger_bill("first-ledger", reference)
    second = _ledger_bill("second-ledger", "INV-812") | {"amount": 90}
    _body(client.post(f"{BILLS}/import", json={"bills": [first, second]}))
    batch = _body(_upload(client, _invoice(reference)), 201)
    path = f"{BATCHES}/{batch['id']}"
    (record,) = batch["records"]
    assert (record["operation"], record["matched_existing_bill_id"]) == ("UPDATE", first["id"])
    assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    before = _body(client.get(BILLS))
    assert {bill["id"]: (bill["amount"], bill["version"]) for bill in before} == {
        first["id"]: (100, 2),
        second["id"]: (90, 1),
    }
    repository = _open_repository(settings.sqlite_db_path)
    receipts = _sync_receipts(repository, batch["id"])
    assert len(receipts) == 1 and receipts[0][-1] == first["id"]

    record_path = _record_url(path, record)
    _body(client.patch(record_path, json={"matched_existing_bill_id": second["id"], "amount": 120}))
    _body(
        client.patch(
            f"{path}/documents/{record['source_doc_id']}",
            json={"total_amount": 120, "total_currency": "USD"},
        )
    )
    approved = _body(client.post(f"{record_path}/approve", json={"reason": "确认改绑"}))
    (updated,) = approved["records"]
    assert (
        updated["operation"],
        updated["matched_existing_bill_id"],
        updated["review_status"],
    ) == ("UPDATE", second["id"], "APPROVED")

    error = _body(client.post(f"{path}/sync-baseline"), 409)
    assert error["code"] == "BASELINE_SYNC_TARGET_CONFLICT"
    assert error["message"] == "该记录此前已同步到其他历史账单，请先核对共享历史基准后再处理改绑"
    assert error["details"] == {
        "resource_id": record["id"],
        "expected": first["id"],
        "actual": second["id"],
    }
    assert _body(client.get(BILLS)) == before
    assert _sync_receipts(repository, batch["id"]) == receipts


@pytest.mark.parametrize("shared_match", [False, True], ids=["empty-baseline", "alias-match"])
def test_shared_review_bundle_after_sync_keeps_the_evaluated_baseline(
    client: TestClient, settings: AppSettings, tmp_path: Path, shared_match: bool
) -> None:
    reference = "INV-501"
    alias = "NEPTUNE SHIPPING LIMITED (LAX)"
    if shared_match:
        bill = _ledger_bill("old-bill", reference, entity=alias)
        unrelated = bill | {
            "id": "other-bill",
            "accounting_entity": TARGETING,
            "posting_type": "AP",
        }
        _body(client.post(f"{BILLS}/import", json={"bills": [bill, unrelated]}))

    original_bills = _body(client.get(BILLS))
    batch = _body(_upload(client, _invoice(reference)), 201)
    path = f"{BATCHES}/{batch['id']}"
    (row,) = batch["records"]
    operation = "UPDATE" if shared_match else "CREATE"
    assert (row["operation"], row["review_status"]) == (operation, "AUTO_READY")
    assert row["matched_existing_bill_id"] == ("old-bill" if shared_match else None)
    metadata_fields = {
        "shared_baseline_snapshot",
        "shared_baseline_scoped",
        "entity_profile_snapshot",
        "synced_without_snapshot",
    }
    assert not metadata_fields & batch.keys()
    assert not metadata_fields & _body(client.get(BATCHES))[0].keys()
    repository = _open_repository(settings.sqlite_db_path)
    with repository.db.connect() as connection:
        (match_id,) = connection.execute(
            "SELECT matched_existing_bill_id FROM billing_records WHERE id = ?", (row["id"],)
        ).fetchone()
        assert match_id == row["matched_existing_bill_id"]
    approved = _body(client.post(f"{_record_url(path, row)}/approve", json={"reason": "核对通过"}))
    assert _body(client.post(f"{path}/sync-baseline")) == {
        "created_count": 0 if shared_match else 1,
        "updated_count": 1 if shared_match else 0,
        "skipped_count": 0,
    }
    (synced,) = _body(client.get(BILLS, params={"accounting_entity": NEPTUNE}))
    assert (synced["amount"], synced["version"]) == (100, 2 if shared_match else 1)
    if shared_match:
        assert synced["id"] == "old-bill"
        assert _body(client.get(BILLS, params={"accounting_entity": alias})) == [synced]
    assert _body(client.get(path)) == approved

    reopened = _open_repository(settings.sqlite_db_path)
    stored = reopened.get_batch(batch["id"])
    assert stored["batch_existing_bills"] is None
    assert stored["shared_baseline_scoped"] is True
    evaluated_bills = [bill for bill in original_bills if bill["accounting_entity"] == alias]
    assert [bill.model_dump() for bill in stored["shared_baseline_snapshot"]] == evaluated_bills
    bundle = export_review_bundle(reopened, batch["id"])
    assert bundle["existing_bills_snapshot"] == evaluated_bills

    target = tmp_path / "bundle-import"
    target_settings = AppSettings(app_data_dir=target, sqlite_db_path=target / "billing.sqlite")
    with TestClient(create_app(settings=target_settings)) as importer:
        response = importer.post(
            f"{BATCHES}/import-review-bundle", content=dumps_decimal_json(bundle)
        )
        imported = _body(response, 201)
        (restored,) = imported["records"]
        assert (restored["operation"], restored["review_status"]) == (operation, "APPROVED")
        assert _body(importer.get(BILLS)) == []
        assert _body(importer.post(f"{BATCHES}/{imported['id']}/sync-baseline")) == {
            "created_count": 1,
            "updated_count": 0,
            "skipped_count": 0,
        }
        (new_bill,) = _body(importer.get(BILLS))
        assert (new_bill["reference_no"], new_bill["amount"]) == (reference, 100)


@pytest.mark.parametrize(
    "profile_change", ["refreshed-baseline", "removed-alias", "reassigned-alias"]
)
def test_review_bundle_keeps_the_baseline_and_profile_used_for_approval(
    client: TestClient, settings: AppSettings, tmp_path: Path, profile_change: str
) -> None:
    custom_profile = profile_change != "refreshed-baseline"
    reassigned = profile_change == "reassigned-alias"
    reference = "INV-813"
    entity = ledger_entity = issuer = NEPTUNE
    profile = None
    if custom_profile:
        entity, ledger_entity = "Harbor Orchid Logistics", "North Coast Ledger"
        issuer = "Orchid USA"
        profile = _body(
            client.post(
                f"{API}/entities",
                json={"canonical_name": entity, "aliases": [issuer, ledger_entity]},
            ),
            201,
        )

    batch = _body(_upload(client, _invoice(reference, issuer=issuer), entity=entity), 201)
    path = f"{BATCHES}/{batch['id']}"
    (initial,) = batch["records"]
    assert initial["matched_existing_bill_id"] is None
    ledger = _ledger_bill("recent-ledger", reference, entity=ledger_entity)
    _body(client.post(f"{BILLS}/import", json={"bills": [ledger]}))
    recalculated = _body(client.post(f"{path}/reapply-dictionary"))
    (matched,) = recalculated["records"]
    assert (matched["operation"], matched["matched_existing_bill_id"]) == ("UPDATE", ledger["id"])
    approved = _body(
        client.post(f"{_record_url(path, matched)}/approve", json={"reason": "核对通过"})
    )
    assert approved["records"][0]["review_status"] == "APPROVED"
    assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    assert _body(client.get(BILLS))[0]["amount"] == 100

    source = _open_repository(settings.sqlite_db_path)
    batch_id = batch["id"]
    snapshot = source.get_batch(batch_id)["shared_baseline_snapshot"]
    assert [(bill.id, bill.amount) for bill in snapshot] == [(ledger["id"], 80)]
    if profile is not None:
        updated_profile = _body(
            client.put(
                f"{API}/entities/{profile['id']}", json={"canonical_name": entity, "aliases": []}
            )
        )
        assert updated_profile["aliases"] == []
    if reassigned:
        _body(client.post(f"{API}/entities", json={"canonical_name": ledger_entity}), 201)
        assert _body(client.get(path))["records"][0]["review_status"] == "APPROVED"
    bundle = export_review_bundle(source, batch_id)
    (frozen,) = bundle["existing_bills_snapshot"]
    assert (frozen["id"], frozen["amount"], frozen["accounting_entity"]) == (
        ledger["id"],
        80,
        ledger_entity,
    )
    if custom_profile:
        assert bundle["accounting_entity_profile"] == {
            "canonical_name": entity,
            "aliases": [issuer, ledger_entity],
        }

    target = tmp_path / "portable-target"
    target_settings = AppSettings(app_data_dir=target, sqlite_db_path=target / "billing.sqlite")
    with TestClient(create_app(settings=target_settings)) as importer:
        profiles = _body(importer.get(f"{API}/entities"))
        imported = _import_bundle(importer, bundle)
        (restored,) = imported["records"]
        assert (
            restored["operation"],
            restored["matched_existing_bill_id"],
            restored["review_status"],
        ) == ("UPDATE", ledger["id"], "APPROVED")
        assert _body(importer.get(BILLS)) == []
        assert _body(importer.get(f"{API}/entities")) == profiles
        if custom_profile:
            imported_id = imported["id"]
            target_repository = _open_repository(target_settings.sqlite_db_path)
            if reassigned:
                _body(importer.post(f"{API}/entities", json={"canonical_name": ledger_entity}), 201)
                profiles = _body(importer.get(f"{API}/entities"))
            immediate = export_review_bundle(target_repository, imported_id)
            assert immediate["accounting_entity_profile"] == bundle["accounting_entity_profile"]
            assert immediate["existing_bills_snapshot"] == bundle["existing_bills_snapshot"]
            reapplied = _body(importer.post(f"{BATCHES}/{imported_id}/reapply-dictionary"))
            (row,) = reapplied["records"]
            assert (row["operation"], row["matched_existing_bill_id"], row["review_status"]) == (
                "UPDATE",
                ledger["id"],
                "APPROVED",
            )
            second_bundle = export_review_bundle(target_repository, imported_id)
            assert second_bundle["accounting_entity_profile"] == bundle["accounting_entity_profile"]
            assert second_bundle["existing_bills_snapshot"] == bundle["existing_bills_snapshot"]
            assert _body(importer.get(f"{API}/entities")) == profiles

    if custom_profile:
        third = tmp_path / "portable-third"
        third_settings = AppSettings(app_data_dir=third, sqlite_db_path=third / "billing.sqlite")
        with TestClient(create_app(settings=third_settings)) as importer:
            if reassigned:
                _body(importer.post(f"{API}/entities", json={"canonical_name": ledger_entity}), 201)
            profiles = _body(importer.get(f"{API}/entities"))
            imported = _import_bundle(importer, second_bundle)
            (row,) = imported["records"]
            assert (row["operation"], row["matched_existing_bill_id"], row["review_status"]) == (
                "UPDATE",
                ledger["id"],
                "APPROVED",
            )
            assert _body(importer.get(f"{API}/entities")) == profiles
            assert _body(importer.get(BILLS)) == []

    _body(client.post(f"{path}/reapply-dictionary"))
    refreshed = source.get_batch(batch_id)["shared_baseline_snapshot"]
    assert [bill.model_dump() for bill in refreshed] == _body(client.get(BILLS))


def test_bundle_keeps_an_explicitly_unconfigured_entity_across_import_and_recheck(
    client: TestClient, settings: AppSettings, tmp_path: Path
) -> None:
    entity, reference = "Independent Harbor", "INV-816"
    ledger = _ledger_bill("independent-ledger", reference, entity=entity)
    _body(client.post(f"{BILLS}/import", json={"bills": [ledger]}))
    batch = _body(_upload(client, _invoice(reference, issuer=entity), entity=entity), 201)
    path = f"{BATCHES}/{batch['id']}"
    (row,) = batch["records"]
    approved = _body(client.post(f"{_record_url(path, row)}/approve", json={"reason": "已核对"}))
    (approved_row,) = approved["records"]
    assert (approved_row["operation"], approved_row["matched_existing_bill_id"]) == (
        "UPDATE", ledger["id"]
    )
    assert approved_row["review_status"] == "APPROVED"
    late_profile = {"canonical_name": "Local Harbor Ledger", "aliases": [entity]}
    _body(client.post(f"{API}/entities", json=late_profile), 201)
    source = _open_repository(settings.sqlite_db_path)
    bundle = export_review_bundle(source, batch["id"])
    assert bundle["accounting_entity_profile"] is None
    assert [bill["id"] for bill in bundle["existing_bills_snapshot"]] == [ledger["id"]]

    target_db = tmp_path / "unconfigured-target" / "billing.sqlite"
    target_settings = AppSettings(app_data_dir=target_db.parent, sqlite_db_path=target_db)
    with TestClient(create_app(settings=target_settings)) as importer:
        _body(importer.post(f"{API}/entities", json=late_profile), 201)
        profiles = _body(importer.get(f"{API}/entities"))
        imported = _import_bundle(importer, bundle)
        reapplied = _body(importer.post(f"{BATCHES}/{imported['id']}/reapply-dictionary"))
        for state in (imported, reapplied):
            (restored,) = state["records"]
            assert (
                restored["operation"],
                restored["matched_existing_bill_id"],
                restored["review_status"],
            ) == ("UPDATE", ledger["id"], "APPROVED")
        exported = export_review_bundle(_open_repository(target_db), imported["id"])
        assert exported["accounting_entity_profile"] is None
        assert exported["existing_bills_snapshot"] == bundle["existing_bills_snapshot"]
        assert _body(importer.get(f"{API}/entities")) == profiles


def test_bundle_after_sync_and_reapproval_keeps_the_new_evaluated_baseline(
    client: TestClient, settings: AppSettings, tmp_path: Path
) -> None:
    batch = _body(_upload(client, _invoice("INV-814")), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    repository = _open_repository(settings.sqlite_db_path)
    assert repository.get_batch(batch_id)["shared_baseline_snapshot"] == []

    assert _body(client.post(f"{path}/sync-baseline"))["created_count"] == 1
    (ledger,) = _body(client.get(BILLS))
    assert ledger["amount"] == 100
    assert repository.get_batch(batch_id)["shared_baseline_snapshot"] == []
    record_path = _record_url(path, row)
    _body(client.patch(record_path, json={"amount": 120}))
    _body(
        client.patch(
            f"{path}/documents/{row['source_doc_id']}",
            json={"total_amount": 120, "total_currency": "USD"},
        )
    )
    approved = _body(client.post(f"{record_path}/approve", json={"reason": "核对更正后的金额"}))
    (updated,) = approved["records"]
    assert (
        updated["operation"], updated["matched_existing_bill_id"], updated["review_status"]
    ) == ("UPDATE", ledger["id"], "APPROVED")
    assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    assert _body(client.get(BILLS))[0]["amount"] == 120

    bundle = export_review_bundle(repository, batch_id)
    assert bundle["existing_bills_snapshot"] == [ledger]
    target = tmp_path / "reevaluated-bundle"
    target_settings = AppSettings(app_data_dir=target, sqlite_db_path=target / "billing.sqlite")
    with TestClient(create_app(settings=target_settings)) as importer:
        imported = _import_bundle(importer, bundle)
        (restored,) = imported["records"]
        assert (
            restored["operation"], restored["matched_existing_bill_id"], restored["review_status"]
        ) == ("UPDATE", ledger["id"], "APPROVED")
        assert restored["charge_details"]["amount"] == 120
        assert _body(importer.get(BILLS)) == []


def _write_legacy_shared_snapshot(
    repository: BillingRepository, batch_id: str, bills: list[dict[str, Any]], *, keep_profile: bool
) -> None:
    # 7277ba4 saved the entire ledger in this envelope without a scope marker.
    metadata = {"shared_baseline_snapshot": bills}
    if keep_profile:
        profile = repository.get_batch(batch_id)["entity_profile_snapshot"]
        metadata["entity_profile_snapshot"] = profile.model_dump()
    with repository.db.connect() as connection:
        connection.execute(
            "UPDATE batches SET batch_existing_bills_json = ? WHERE id = ?",
            (dumps_decimal_json(metadata), batch_id),
        )


@pytest.mark.parametrize(
    ("entity", "ledger_entity", "other_entity"),
    [
        (NEPTUNE, f"{NEPTUNE} (LAX)", TARGETING),
        ("Harbor+Orchid Logistics", "Harbor+Orchid Logistics", "Harbor-Orchid Logistics"),
    ],
    ids=["saved-alias", "punctuation-sibling"],
)
def test_export_filters_a_legacy_full_ledger_using_the_saved_profile(
    client: TestClient, settings: AppSettings, entity: str, ledger_entity: str, other_entity: str
) -> None:
    if entity != NEPTUNE:
        for name in (entity, other_entity):
            _body(client.post(f"{API}/entities", json={"canonical_name": name}), 201)
    reference = "INV-958"
    bills = [
        _ledger_bill("recent-ledger", reference, entity=ledger_entity),
        _ledger_bill("other-ledger", "INV-948", entity=other_entity) | {"amount": 45},
    ]
    _body(client.post(f"{BILLS}/import", json={"bills": bills}))
    batch = _body(_upload(client, _invoice(reference, issuer=entity), entity=entity), 201)
    batch_id = batch["id"]
    (row,) = batch["records"]
    record_path = _record_url(f"{BATCHES}/{batch_id}", row)
    approved = _body(client.post(f"{record_path}/approve", json={"reason": "已核对"}))
    (row,) = approved["records"]
    assert (row["matched_existing_bill_id"], row["review_status"]) == ("recent-ledger", "APPROVED")
    repository = _open_repository(settings.sqlite_db_path)
    _write_legacy_shared_snapshot(repository, batch_id, _body(client.get(BILLS)), keep_profile=True)

    if ledger_entity != entity:
        _body(client.post(f"{API}/entities", json={"canonical_name": ledger_entity}), 201)
    reopened = _open_repository(settings.sqlite_db_path)
    stored = reopened.get_batch(batch_id)
    assert stored["shared_baseline_scoped"] is False
    assert {bill.id for bill in stored["shared_baseline_snapshot"]} == {
        "recent-ledger", "other-ledger",
    }
    bundle = export_review_bundle(reopened, batch_id)
    assert [(bill["id"], bill["amount"]) for bill in bundle["existing_bills_snapshot"]] == [
        ("recent-ledger", 80),
    ]


@pytest.mark.parametrize(
    "competition",
    [
        "canonical-before",
        "alias-before",
        "canonical-after",
        "canonical-during",
        "canonical-during-matched",
        "alias-edited",
    ],
)
def test_export_legacy_alias_scope_uses_the_batch_time_window(
    client: TestClient, settings: AppSettings, competition: str
) -> None:
    branch, reference = f"{NEPTUNE} (LAX)", "INV-961"
    competing = {"canonical_name": branch}
    if competition in {"alias-before", "alias-edited"}:
        competing = {"canonical_name": "Pacific Ledger", "aliases": [branch]}
    entity_path = f"{API}/entities"
    late_split = competition == "canonical-after"
    matched_during = competition == "canonical-during-matched"
    registered_during = competition == "canonical-during" or matched_during
    matched_alias = late_split or matched_during
    competitor = None
    if competition in {"canonical-before", "alias-before", "alias-edited"}:
        initial = competing if competition != "alias-edited" else competing | {"aliases": []}
        competitor = _body(client.post(entity_path, json=initial), 201)

    bills = [
        _ledger_bill("recent-ledger", reference, entity=branch if matched_alias else NEPTUNE),
        _ledger_bill("other-ledger", "INV-OTHER", entity=TARGETING if matched_alias else branch)
        | {"amount": 45},
    ]
    _body(client.post(f"{BILLS}/import", json={"bills": bills}))
    batch = _body(_upload(client, _invoice(reference)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    if registered_during:
        competitor = _body(client.post(entity_path, json=competing), 201)
    approved = _body(client.post(f"{_record_url(path, row)}/approve", json={"reason": "已核对"}))
    (row,) = approved["records"]
    assert (row["operation"], row["matched_existing_bill_id"], row["review_status"]) == (
        "UPDATE",
        "recent-ledger",
        "APPROVED",
    )
    pre_sync = _body(client.get(BILLS))
    if not matched_during:
        assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    if late_split:
        competitor = _body(client.post(entity_path, json=competing), 201)
    elif competition == "alias-edited":
        competitor = _body(client.put(f"{entity_path}/{competitor['id']}", json=competing))

    repository = _open_repository(settings.sqlite_db_path)
    _write_legacy_shared_snapshot(repository, batch_id, pre_sync, keep_profile=True)
    reopened = _open_repository(settings.sqlite_db_path)
    stored = reopened.get_batch(batch_id)
    assert reopened.has_baseline_sync_items(batch_id) is (not matched_during)
    assert stored["shared_baseline_scoped"] is False
    assert branch in stored["entity_profile_snapshot"].aliases
    assert {(bill.id, bill.amount) for bill in stored["shared_baseline_snapshot"]} == {
        ("recent-ledger", 80),
        ("other-ledger", 45),
    }
    created, updated = competitor["created_at"], competitor["updated_at"]
    if late_split:
        assert stored["updated_at"] < created
    elif registered_during:
        assert stored["created_at"] < created <= stored["updated_at"]
    elif competition == "alias-edited":
        assert created <= stored["created_at"] < updated
    else:
        assert created <= updated <= stored["created_at"]

    if registered_during or competition == "alias-edited":
        with pytest.raises(ServiceValidationError) as error:
            export_review_bundle(reopened, batch_id)
        assert (error.value.status_code, error.value.code) == (409, "MISSING_BASELINE_SNAPSHOT")
    else:
        bundle = export_review_bundle(reopened, batch_id)
        assert [(bill["id"], bill["amount"]) for bill in bundle["existing_bills_snapshot"]] == [
            ("recent-ledger", 80),
        ]
        (exported,) = bundle["records"]
        assert (
            exported["operation"], exported["matched_existing_bill_id"], exported["review_status"]
        ) == ("UPDATE", "recent-ledger", "APPROVED")


@pytest.mark.parametrize(
    "remove_before_approval", [True, False], ids=["during-batch", "after-sync"]
)
def test_export_refuses_a_synced_legacy_batch_after_competing_alias_removal(
    client: TestClient, settings: AppSettings, remove_before_approval: bool
) -> None:
    branch, reference = f"{NEPTUNE} (LAX)", "INV-962"
    entity_path = f"{API}/entities"
    competing = {"canonical_name": "Pacific Ledger", "aliases": [branch]}
    competitor = _body(client.post(entity_path, json=competing), 201)
    assert competitor["aliases"] == [branch]
    competitor_path = f"{entity_path}/{competitor['id']}"
    without_alias = competing | {"aliases": []}

    bills = [
        _ledger_bill("recent-ledger", reference),
        _ledger_bill("other-ledger", "INV-OTHER", entity=branch) | {"amount": 45},
    ]
    _body(client.post(f"{BILLS}/import", json={"bills": bills}))
    pre_sync = _body(client.get(BILLS))
    batch = _body(_upload(client, _invoice(reference)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    repository = _open_repository(settings.sqlite_db_path)
    _write_legacy_shared_snapshot(repository, batch_id, pre_sync, keep_profile=True)
    before = export_review_bundle(repository, batch_id)
    assert [(bill["id"], bill["amount"]) for bill in before["existing_bills_snapshot"]] == [
        ("recent-ledger", 80),
    ]

    if remove_before_approval:
        edited = _body(client.put(competitor_path, json=without_alias))
    approved = _body(client.post(f"{_record_url(path, row)}/approve", json={"reason": "已核对"}))
    (row,) = approved["records"]
    assert (row["operation"], row["matched_existing_bill_id"], row["review_status"]) == (
        "UPDATE",
        "recent-ledger",
        "APPROVED",
    )
    assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    if not remove_before_approval:
        edited = _body(client.put(competitor_path, json=without_alias))

    reopened = _open_repository(settings.sqlite_db_path)
    stored = reopened.get_batch(batch_id)
    assert reopened.has_baseline_sync_items(batch_id)
    assert stored["shared_baseline_scoped"] is False
    assert branch in stored["entity_profile_snapshot"].aliases
    assert {
        (bill.id, bill.accounting_entity, bill.amount)
        for bill in stored["shared_baseline_snapshot"]
    } == {("recent-ledger", NEPTUNE, 80), ("other-ledger", branch, 45)}
    assert edited["aliases"] == []
    assert competitor["created_at"] <= stored["created_at"] < edited["updated_at"]
    if remove_before_approval:
        assert edited["updated_at"] <= stored["updated_at"]
    else:
        assert stored["updated_at"] < edited["updated_at"]

    with pytest.raises(ServiceValidationError) as error:
        export_review_bundle(reopened, batch_id)
    assert (error.value.status_code, error.value.code) == (409, "MISSING_BASELINE_SNAPSHOT")


def test_export_refuses_a_synced_legacy_match_without_a_provable_entity_scope(
    client: TestClient, settings: AppSettings
) -> None:
    alias, reference = f"{NEPTUNE} (LAX)", "INV-959"
    ledger = _ledger_bill("former-alias-ledger", reference, entity=alias)
    other = _ledger_bill("other-ledger", "INV-OTHER", entity=TARGETING) | {"amount": 45}
    _body(client.post(f"{BILLS}/import", json={"bills": [ledger, other]}))
    batch = _body(_upload(client, _invoice(reference)), 201)
    batch_id = batch["id"]
    (row,) = batch["records"]
    assert row["matched_existing_bill_id"] == ledger["id"]
    pre_sync = _body(client.get(BILLS))
    assert _body(client.post(f"{BATCHES}/{batch_id}/sync-baseline"))["updated_count"] == 1
    _body(client.post(f"{API}/entities", json={"canonical_name": alias}), 201)
    repository = _open_repository(settings.sqlite_db_path)
    _write_legacy_shared_snapshot(repository, batch_id, pre_sync, keep_profile=False)
    assert repository.has_baseline_sync_items(batch_id)
    assert repository.get_batch(batch_id)["has_entity_profile_snapshot"] is False

    with pytest.raises(ServiceValidationError) as error:
        export_review_bundle(repository, batch_id)
    assert (error.value.status_code, error.value.code) == (409, "MISSING_BASELINE_SNAPSHOT")


def test_zero_row_sync_keeps_a_rejected_legacy_batch_exportable(
    client: TestClient, settings: AppSettings
) -> None:
    batch = _body(_upload(client, _invoice("INV-NOOP-SYNC")), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    _body(client.post(f"{_record_url(path, row)}/reject", json={"reason": "该笔费用作废"}))
    repository = _open_repository(settings.sqlite_db_path)
    with repository.db.connect() as connection:
        connection.execute(
            "UPDATE batches SET batch_existing_bills_json = NULL WHERE id = ?", (batch_id,)
        )
    before = export_review_bundle(repository, batch_id)
    assert before["records"][0]["review_status"] == "REJECTED"
    assert _body(client.post(f"{path}/sync-baseline")) == {
        "created_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
    }

    reopened = _open_repository(settings.sqlite_db_path)
    assert not reopened.has_baseline_sync_items(batch_id)
    assert reopened.get_batch(batch_id)["synced_without_snapshot"] is False
    after = export_review_bundle(reopened, batch_id)
    assert after["records"] == before["records"]
    assert after["existing_bills_snapshot"] == before["existing_bills_snapshot"] == []


def test_export_rejects_legacy_synced_batch_without_a_baseline_snapshot(
    client: TestClient, settings: AppSettings
) -> None:
    batch = _body(_upload(client, _invoice("INV-LEGACY")), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    assert _body(client.post(f"{path}/sync-baseline"))["created_count"] == 1
    repository = _open_repository(settings.sqlite_db_path)
    with repository.db.connect() as connection:
        connection.execute(
            "UPDATE batches SET batch_existing_bills_json = NULL WHERE id = ?", (batch_id,)
        )
    assert repository.get_batch(batch_id)["shared_baseline_snapshot"] is None
    assert len(_sync_receipts(repository, batch_id)) == 1

    with pytest.raises(ServiceValidationError) as error:
        export_review_bundle(repository, batch_id)

    assert (error.value.status_code, error.value.code) == (409, "MISSING_BASELINE_SNAPSHOT")
    assert error.value.message == (
        "该历史共享库批次缺少同步前基线快照，已同步基线后无法无损导出复核包"
    )


@pytest.mark.parametrize("has_profile", [False, True], ids=["legacy-null", "profile-only"])
@pytest.mark.parametrize(
    ("legacy_sync", "replace"),
    [(False, False), (True, False), (True, True)],
    ids=["current-clear", "legacy-clear", "legacy-replace"],
)
def test_legacy_sync_without_snapshot_stays_unexportable_after_ledger_replacement(
    client: TestClient,
    settings: AppSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_profile: bool,
    legacy_sync: bool,
    replace: bool,
) -> None:
    reference = "INV-815"
    _body(client.post(f"{BILLS}/import", json={"bills": [_ledger_bill("old-ledger", reference)]}))
    batch = _body(_upload(client, _invoice(reference)), 201)
    batch_id = batch["id"]
    path = f"{BATCHES}/{batch_id}"
    (row,) = batch["records"]
    approved = _body(client.post(f"{_record_url(path, row)}/approve", json={"reason": "已核对"}))
    (approved_row,) = approved["records"]
    assert (approved_row["matched_existing_bill_id"], approved_row["review_status"]) == (
        "old-ledger", "APPROVED"
    )
    repository = _open_repository(settings.sqlite_db_path)
    profile_snapshot = repository.get_batch(batch_id)["entity_profile_snapshot"]
    assert profile_snapshot.canonical_name == NEPTUNE
    payload = (
        dumps_decimal_json({"entity_profile_snapshot": profile_snapshot.model_dump()})
        if has_profile
        else None
    )
    with repository.db.connect() as connection:
        connection.execute(
            "UPDATE batches SET batch_existing_bills_json = ? WHERE id = ?", (payload, batch_id)
        )
    assert _body(client.post(f"{path}/sync-baseline"))["updated_count"] == 1
    assert len(_sync_receipts(repository, batch_id)) == 1
    if legacy_sync:
        # b88a79f stored the receipt without a batch marker or a record sync selection.
        with repository.db.connect() as connection:
            connection.execute(
                "UPDATE batches SET batch_existing_bills_json = ? WHERE id = ?", (payload, batch_id)
            )
            (record_state,) = connection.execute(
                "SELECT record_state_json FROM billing_records WHERE id = ?", (row["id"],)
            ).fetchone()
            state = loads_decimal_json(record_state)
            del state["baseline_sync_selection"]
            connection.execute(
                "UPDATE billing_records SET record_state_json = ? WHERE id = ?",
                (dumps_decimal_json(state), row["id"]),
            )
    assert repository.get_batch(batch_id)["synced_without_snapshot"] is (not legacy_sync)
    with pytest.raises(ServiceValidationError) as before_clear:
        export_review_bundle(repository, batch_id)
    assert (before_clear.value.status_code, before_clear.value.code) == (
        409, "MISSING_BASELINE_SNAPSHOT"
    )

    assert repository.get_batch(batch_id)["synced_without_snapshot"] is (not legacy_sync)
    if not replace:
        cleared = client.delete(
            BILLS,
            params={"accounting_entity": NEPTUNE},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert cleared.status_code == 204, cleared.text
    replacement = _ledger_bill("replacement-ledger", reference) | {"amount": 45}
    _body(
        client.post(
            f"{BILLS}/import",
            json={
                "bills": [replacement],
                "replace_existing": replace,
                "default_accounting_entity": NEPTUNE,
            },
        )
    )
    assert _sync_receipts(repository, batch_id) == []
    (ledger,) = _body(client.get(BILLS))
    assert (ledger["id"], ledger["amount"]) == ("replacement-ledger", 45)

    reopened = _open_repository(settings.sqlite_db_path)
    legacy = reopened.get_batch(batch_id)
    assert legacy["batch_existing_bills"] is None and legacy["shared_baseline_snapshot"] is None
    with pytest.raises(ServiceValidationError) as after_clear:
        export_review_bundle(reopened, batch_id)
    assert (after_clear.value.status_code, after_clear.value.code) == (
        409, "MISSING_BASELINE_SNAPSHOT"
    )
    assert legacy["synced_without_snapshot"] is True
    assert legacy["entity_profile_snapshot"] == (profile_snapshot if has_profile else None)

    # CLI parse accepts new inputs. Select the historical batch at the creation boundary.
    monkeypatch.setattr(BatchService, "create_batch", lambda *_args, **_options: {"id": batch_id})
    name, contents = _invoice(reference)
    invoice = tmp_path / name
    invoice.write_bytes(contents)
    bundle_file = tmp_path / "must-not-exist.json"
    result = CliRunner().invoke(
        cli_app,
        [
            "parse",
            str(invoice),
            "--entity",
            NEPTUNE,
            "--db-path",
            str(settings.sqlite_db_path),
            "--bundle-out",
            str(bundle_file),
        ],
    )
    assert result.exit_code == 2, (result.stderr, result.exception)
    assert "MISSING_BASELINE_SNAPSHOT" in result.stderr
    assert not bundle_file.exists()


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
        assert conflict["message"] == "资源已被更新，请刷新后重试"
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


def test_cli_bundle_import_distinguishes_an_explicit_null_profile_from_omission(
    client: TestClient, settings: AppSettings, tmp_path: Path
) -> None:
    name, contents = _invoice("INV-NULL-PROFILE")
    invoice = tmp_path / name
    invoice.write_bytes(contents)
    bundle_file = tmp_path / "cli-bundle.json"
    result = CliRunner().invoke(
        cli_app,
        [
            "parse",
            str(invoice),
            "--entity",
            NEPTUNE,
            "--db-path",
            str(tmp_path / "cli" / "billing.sqlite"),
            "--bundle-out",
            str(bundle_file),
        ],
    )
    assert result.exit_code == 0, (result.stderr, result.exception)
    bundle = loads_decimal_json(bundle_file.read_bytes())
    profile = bundle.pop("accounting_entity_profile")
    assert profile["canonical_name"] == NEPTUNE and "NEPTUNE" in profile["aliases"]
    profiles = _body(client.get(f"{API}/entities"))
    assert any(row["canonical_name"] == NEPTUNE and row["aliases"] for row in profiles)

    explicit_null = bundle | {"accounting_entity_profile": None}
    for payload, expected in ((explicit_null, None), (bundle, profile)):
        imported = _import_bundle(client, payload)
        batch_id = imported["id"]
        assert len(imported["documents"]) == len(imported["records"]) == 1
        _body(client.post(f"{BATCHES}/{batch_id}/reapply-dictionary"))
        reopened = _open_repository(settings.sqlite_db_path)
        stored = reopened.get_batch(batch_id)
        assert stored["has_entity_profile_snapshot"] is True
        with reopened.db.connect() as connection:
            (raw_metadata,) = connection.execute(
                "SELECT batch_existing_bills_json FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        raw_profile = loads_decimal_json(raw_metadata)["entity_profile_snapshot"]
        if expected is None:
            assert raw_profile is None
            assert stored["entity_profile_snapshot"] is None
        else:
            assert {key: raw_profile[key] for key in expected} == expected
        assert export_review_bundle(reopened, batch_id)["accounting_entity_profile"] == expected

    assert _body(client.get(f"{API}/entities")) == profiles


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


def test_document_content_endpoint_previews_real_samples_across_reopen(
    settings: AppSettings, real_samples: tuple[Path, Path, Path, Path]
) -> None:
    image, first_pdf, second_pdf, email = real_samples
    with (
        BatchService(settings, local_ocr=RapidOcrFallback(), extractor_mode="rule_only") as service,
        TestClient(create_app(batch_service=service)) as client,
    ):
        batch = _body(
            _upload(
                client,
                *((sample.name, sample.read_bytes()) for sample in real_samples),
                entity=ZIM,
            ),
            201,
        )

    batch_id = batch["id"]
    docs = {doc["sanitized_filename"]: doc for doc in batch["documents"]}
    docs_by_orig = {doc["original_filename"]: doc for doc in batch["documents"]}
    with TestClient(create_app(settings=settings)) as reopened:
        base = f"{BATCHES}/{batch_id}/documents"

        pdf1_id = docs_by_orig[first_pdf.name]["id"]
        pdf1_res = reopened.get(f"{base}/{pdf1_id}/content")
        assert pdf1_res.status_code == 200
        assert pdf1_res.content == first_pdf.read_bytes()
        assert pdf1_res.headers["content-type"] == "application/pdf"
        assert pdf1_res.headers["content-disposition"].startswith("inline; ")
        assert f"filename*=UTF-8''{quote(first_pdf.name, safe='')}" in (
            pdf1_res.headers["content-disposition"]
        )
        assert pdf1_res.headers["content-security-policy"] == (
            "frame-ancestors 'self'; object-src 'self'"
        )
        pdf1_text = reopened.get(f"{base}/{pdf1_id}/content", params={"view": "text"})
        assert pdf1_text.status_code == 200
        assert pdf1_text.headers["content-type"] == "text/plain; charset=utf-8"
        assert "245" in pdf1_text.text

        pdf2_id = docs_by_orig[second_pdf.name]["id"]
        pdf2_res = reopened.get(f"{base}/{pdf2_id}/content")
        assert pdf2_res.status_code == 200
        assert pdf2_res.content == second_pdf.read_bytes()

        img_id = docs_by_orig[image.name]["id"]
        img_res = reopened.get(f"{base}/{img_id}/content")
        assert img_res.status_code == 200
        assert img_res.headers["content-type"] == "image/png"
        assert img_res.content == image.read_bytes()
        assert f"filename*=UTF-8''{quote(image.name, safe='')}" in (
            img_res.headers["content-disposition"]
        )

        # EML PDF attachments are commonly labeled application/octet-stream in MIME parts.
        for att_name in ("4055331281_MBL.pdf", "4055331281_HBL.pdf"):
            att_id = docs[att_name]["id"]
            att_res = reopened.get(f"{base}/{att_id}/content")
            assert att_res.status_code == 200
            assert att_res.headers["content-type"] == "application/pdf"
            assert att_res.content.startswith(b"%PDF-")
            assert att_res.headers["content-disposition"].startswith("inline; ")
            assert att_res.headers["content-security-policy"] == (
                "frame-ancestors 'self'; object-src 'self'"
            )

        sig_id = docs["InsertPic_C25B.jpg"]["id"]
        sig_res = reopened.get(f"{base}/{sig_id}/content")
        assert sig_res.status_code == 200
        assert sig_res.headers["content-type"] == "image/jpeg"
        assert sig_res.headers["content-disposition"].startswith("inline; ")
        sig_text = reopened.get(f"{base}/{sig_id}/content", params={"view": "text"})
        assert _body(sig_text, 404)["code"] == "TEXT_PREVIEW_UNAVAILABLE"

        body_doc = next(doc for doc in batch["documents"] if doc["source_kind"] == "EML_BODY")
        body_res = reopened.get(f"{base}/{body_doc['id']}/content")
        assert body_res.status_code == 200
        assert body_res.headers["content-type"] == "text/plain; charset=utf-8"
        assert "MBL" in body_res.text

        eml_id = docs_by_orig[email.name]["id"]
        eml_inline = reopened.get(f"{base}/{eml_id}/content")
        assert eml_inline.status_code == 200
        assert eml_inline.headers["content-type"] == "text/plain; charset=utf-8"
        assert eml_inline.headers["content-disposition"].startswith("inline; ")
        eml_dl = reopened.get(f"{base}/{eml_id}/content", params={"download": "true"})
        assert eml_dl.status_code == 200
        assert eml_dl.headers["content-type"] == "message/rfc822"
        assert eml_dl.content == email.read_bytes()
        assert eml_dl.headers["content-disposition"].startswith("attachment; ")


def test_document_content_handles_eml_xss_tiff_excel_tamper_and_review_bundles(
    settings: AppSettings,
) -> None:
    tiff_buf = BytesIO()
    Image.new("CMYK", (8, 8), color=(0, 128, 255, 0)).save(tiff_buf, format="TIFF")
    tiff_bytes = tiff_buf.getvalue()

    msg = EmailMessage()
    msg["From"] = "billing@neptune.example"
    msg["To"] = "client@targeting.example"
    msg["Subject"] = "Invoice INV-SAFE-1"
    msg.set_content(
        "Invoice No. INV-SAFE-1\nDate: 2026-10-01\nIssuer: "
        f"{NEPTUNE}\nBill To: {TARGETING}\nHandling Charge 100.00 USD\nTotal USD 100.00"
    )
    msg.add_attachment(
        b"<html><script>alert(1)</script></html>",
        maintype="text",
        subtype="html",
        filename="xss.html",
    )
    msg.add_attachment(
        b"<svg onload='alert(1)'/>",
        maintype="image",
        subtype="svg+xml",
        filename="vector.svg",
    )
    msg.add_attachment(
        b"<svg onload='alert(2)'/>",
        maintype="image",
        subtype="png",
        filename="fake_image.png",
    )
    msg.add_attachment(
        tiff_bytes,
        maintype="image",
        subtype="tiff",
        filename="scan.tif",
    )

    xlsx_name, xlsx_bytes = _invoice("INV-XLSX-1")

    with TestClient(create_app(settings=settings)) as client:
        batch = _body(
            _upload(client, ("bundle.eml", msg.as_bytes()), (xlsx_name, xlsx_bytes)),
            201,
        )
        batch_id = batch["id"]
        base = f"{BATCHES}/{batch_id}/documents"
        by_name = {doc["sanitized_filename"]: doc for doc in batch["documents"]}

        xlsx_res = client.get(f"{base}/{by_name[xlsx_name]['id']}/content")
        assert xlsx_res.status_code == 200
        assert xlsx_res.headers["content-disposition"].startswith("attachment; ")
        assert xlsx_res.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert xlsx_res.content == xlsx_bytes
        xlsx_text = client.get(
            f"{base}/{by_name[xlsx_name]['id']}/content", params={"view": "text"}
        )
        assert xlsx_text.status_code == 200
        assert "INV-XLSX-1" in xlsx_text.text

        for unsafe_name in ("xss.html", "vector.svg", "fake_image.png"):
            unsafe_res = client.get(f"{base}/{by_name[unsafe_name]['id']}/content")
            assert unsafe_res.status_code == 200
            assert unsafe_res.headers["content-type"] == "application/octet-stream"
            assert unsafe_res.headers["content-disposition"].startswith("attachment; ")
            assert unsafe_res.headers["x-content-type-options"] == "nosniff"
            assert unsafe_res.headers["content-security-policy"] == "default-src 'none'; sandbox"

        tif_id = by_name["scan.tif"]["id"]
        tif_inline = client.get(f"{base}/{tif_id}/content")
        assert tif_inline.status_code == 200
        assert tif_inline.headers["content-type"] == "image/png"
        assert tif_inline.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert tif_inline.headers["content-disposition"].startswith("inline; ")
        tif_dl = client.get(f"{base}/{tif_id}/content", params={"download": "true"})
        assert tif_dl.status_code == 200
        assert tif_dl.headers["content-type"] == "image/tiff"
        assert tif_dl.content == tiff_bytes
        assert tif_dl.headers["content-disposition"].startswith("attachment; ")

        # Tamper stored Excel file on disk -> content returns 404, retry keeps 400 contract,
        # while extracted text view remains readable.
        repository = _open_repository(settings.sqlite_db_path)
        stored_xlsx = repository.get_document(batch_id, by_name[xlsx_name]["id"])
        Path(stored_xlsx["stored_path"]).write_bytes(b"corrupted")
        tampered_get = client.get(f"{base}/{by_name[xlsx_name]['id']}/content")
        assert _body(tampered_get, 404)["code"] == "ORIGINAL_FILE_UNAVAILABLE"
        assert "INV-XLSX-1" in client.get(
            f"{base}/{by_name[xlsx_name]['id']}/content", params={"view": "text"}
        ).text
        tampered_retry = client.post(f"{base}/{by_name[xlsx_name]['id']}/retry")
        assert _body(tampered_retry, 400)["code"] == "ORIGINAL_FILE_UNAVAILABLE"

        # Exported/imported review bundle omits stored_path and raw_text -> 404 on both.
        exported_bundle = export_review_bundle(repository, batch_id)
        imported = _import_bundle(client, exported_bundle)
        imp_xlsx = next(
            doc for doc in imported["documents"] if doc["sanitized_filename"] == xlsx_name
        )
        imp_base = f"{BATCHES}/{imported['id']}/documents/{imp_xlsx['id']}/content"
        assert _body(client.get(imp_base), 404)["code"] == "ORIGINAL_FILE_UNAVAILABLE"
        assert _body(client.get(imp_base, params={"view": "text"}), 404)["code"] == (
            "TEXT_PREVIEW_UNAVAILABLE"
        )

