"""Exercise the published contract and review round trip through the HTTP entry points."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from sqlite3 import IntegrityError
from types import SimpleNamespace

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain import ExistingBillRecord, dumps_decimal_json, loads_decimal_json
from shipping_billing_agent.domain.models import OcrStructuredPayload
from shipping_billing_agent.services import BatchService, export_review_bundle
from shipping_billing_agent.storage import BillingRepository

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs/workflow/contracts"
API = "/api/v1"
ENTITY = "NEPTUNE SHIPPING LIMITED"
OTHER_ENTITY = "TARGETING A INC"
FALLBACK = "REMOTE_WITH_LOCAL_FALLBACK"
LARGE = Decimal("9007199254740991.01")
SMALL = Decimal("0.0025")


def _invoice(reference: str = "INV-123", amount: str = "100.00") -> bytes:
    return (
        f"Invoice No. {reference}\nDate: 2026-10-01\nIssuer: {ENTITY}\nBill To: {OTHER_ENTITY}\n"
        f"Handling Charge {amount} USD\nTotal USD {amount}"
    ).encode()


class _OfflineOcr:
    def recognize_image_bytes(self, content: bytes, _filename: str) -> tuple[str, float]:
        return content.decode(), 0.99

    def recognize_url(self, _url: str) -> SimpleNamespace:
        return SimpleNamespace(
            payload=OcrStructuredPayload(items=[], has_charges=False, has_metadata=True),
            formatted_text=_invoice("INV-321").decode(),
        )


def _settings(path: Path) -> AppSettings:
    return AppSettings(
        app_data_dir=path,
        sqlite_db_path=path / "test.sqlite",
        public_relay_base_url="",
        llm_api_key="",
        deepseek_api_key="",
    )


@pytest.fixture
def make_service(tmp_path: Path) -> Iterator:
    services = []

    def make() -> BatchService:
        ocr = _OfflineOcr()
        service = BatchService(
            _settings(tmp_path / f"app-{len(services)}"),
            remote_ocr=ocr,
            local_ocr=ocr,
            extractor_mode="rule_only",
        )
        services.append(service)
        return service

    yield make
    for service in services:
        service.close()


@pytest.fixture
def api(make_service) -> Iterator[tuple[TestClient, BatchService]]:
    service = make_service()
    with TestClient(create_app(batch_service=service)) as client:
        yield client, service


@pytest.fixture(scope="module")
def contract() -> dict:
    return yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))


def _assert_schema(contract: dict, schema: str, value: object) -> None:
    Draft202012Validator(
        {"$ref": f"#/components/schemas/{schema}", "components": contract["components"]}
    ).validate(value)


def _http_routes(routes) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (included := getattr(route, "original_router", None)) is not None:
            yield from _http_routes(included.routes)


def _create(client: TestClient, reference: str = "INV-123") -> dict:
    response = client.post(
        f"{API}/batches",
        files={"files": ("invoice.png", _invoice(reference), "image/png")},
        data={"accounting_entity": ENTITY, "ocr_mode": FALLBACK},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _record_url(batch: dict) -> str:
    return f"{API}/batches/{batch['id']}/records/{batch['records'][0]['id']}"


def test_registered_operations_and_served_openapi_match_contract(api, contract) -> None:
    client, _ = api
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    expected = {
        (method.upper(), path): operation["operationId"]
        for path, operations in contract["paths"].items()
        for method, operation in operations.items()
        if method in methods
    }
    assert len(contract["paths"]) == 24
    assert len(expected) == 29
    actual = {
        (method, route.path): route.operation_id
        for route in _http_routes(client.app.routes)
        for method in route.methods
        if route.path.startswith(API)
    }
    assert actual == expected
    assert client.get("/openapi.json").json() == contract


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unlisted_loopback_port_cannot_write(api, contract, method: str) -> None:
    client, service = api
    response = client.request(
        method,
        f"{API}/entities",
        headers={"Origin": "http://127.0.0.1:9999"},
        json={"canonical_name": "Origin Probe"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_ORIGIN"
    _assert_schema(contract, "ErrorResponse", response.json())
    assert "Origin Probe" not in {
        row["canonical_name"] for row in service.repository.list_accounting_entities()
    }


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 201),
        ({"Origin": "http://127.0.0.1:5174"}, 201),
        ({"Origin": "http://localhost:5173"}, 201),
        ({"Referer": "http://localhost:8000/workbench?x=1"}, 201),
        ({"Origin": "https://workbench.example.test:443"}, 201),
        ({"Referer": "https://workbench.example.test/workbench"}, 201),
        ({"Origin": "http://workbench.example.test:443"}, 403),
        ({"Origin": "https://workbench.example.test:444"}, 403),
        ({"Origin": "https://example.test"}, 403),
        ({"Origin": "https://localhost:5173"}, 403),
        ({"Origin": "null", "Referer": "http://localhost:8000/workbench"}, 403),
        ({"Origin": "https://workbench.example.test/path"}, 403),
        ({"Referer": "http://127.0.0.1:9999/workbench"}, 403),
    ],
)
def test_origin_and_referer_compare_full_origin(make_service, headers, status) -> None:
    app = create_app(batch_service=make_service())
    with TestClient(app, base_url="https://workbench.example.test") as client:
        response = client.post(f"{API}/entities", headers=headers, json={"canonical_name": "Probe"})
        assert response.status_code == status


def test_origin_configuration_is_honored(make_service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", '["https://extension.example.test"]')
    with TestClient(create_app(batch_service=make_service())) as client:
        allowed = client.post(
            f"{API}/entities",
            headers={"Origin": "https://extension.example.test:443"},
            json={"canonical_name": "Configured"},
        )
        denied = client.delete(
            f"{API}/existing-bills", headers={"Origin": "http://extension.example.test"}
        )
    assert allowed.status_code == 201
    assert denied.status_code == 403


def test_decimal_patch_persistence_preview_and_file_download(api, contract, make_service) -> None:
    client, service = api
    batch = _create(client)
    doc_id = batch["documents"][0]["id"]
    record_id = batch["records"][0]["id"]
    batch_path = f"{API}/batches/{batch['id']}"

    patch = client.patch(
        _record_url(batch),
        content=b'{"amount":9007199254740991.01,"quantity":0.0025,"unit_price":null}',
    )
    assert patch.status_code == 200, patch.text
    totals = client.patch(
        f"{batch_path}/documents/{doc_id}",
        content=b'{"total_amount":9007199254740991.01,"total_currency":"USD"}',
    )
    assert totals.status_code == 200, totals.text
    _assert_schema(contract, "BatchDetail", totals.json())
    assert (
        loads_decimal_json(totals.text)["documents"][0]["total_amount"].as_tuple()
        == LARGE.as_tuple()
    )

    with service.repository.db.connect() as connection:
        stored = connection.execute(
            "SELECT amount, quantity, typeof(amount), typeof(quantity) "
            "FROM billing_records WHERE id = ?",
            (record_id,),
        ).fetchone()
    assert tuple(stored) == (str(LARGE), str(SMALL), "text", "text")

    preview = client.get(f"{batch_path}/export")
    download = client.get(f"{batch_path}/export/download?mode=official")
    assert preview.status_code == download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert (
        b'"amount":9007199254740991.01' in preview.content
        and b'"quantity":0.0025' in preview.content
    )
    assert (
        b'"amount":9007199254740991.01' in download.content
        and b'"quantity":0.0025' in download.content
    )
    _assert_schema(contract, "ExportResponse", preview.json())
    summary = loads_decimal_json(preview.text)
    assert (
        summary["included_count"],
        summary["excluded_pending_count"],
        summary["excluded_rejected_count"],
    ) == (1, 0, 0)
    downloaded = loads_decimal_json(download.text)
    assert downloaded == summary["records"]
    charges = downloaded[0]["charge_details"]
    assert charges["amount"].as_tuple() == LARGE.as_tuple()
    assert charges["quantity"].as_tuple() == SMALL.as_tuple()
    schema = json.loads((CONTRACTS / "billing-output.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(downloaded)
    assert len(downloaded[0]) == 6

    bundle = dumps_decimal_json(export_review_bundle(service.repository, batch["id"])).encode()
    assert b'"amount":9007199254740991.01' in bundle
    assert b'"quantity":0.0025' in bundle
    target = make_service()
    with TestClient(create_app(batch_service=target)) as importer:
        imported = importer.post(f"{API}/batches/import-review-bundle", content=bundle)
        assert imported.status_code == 201, imported.text
        reloaded = importer.get(f"{API}/batches/{imported.json()['id']}/export/download")
        imported_charges = loads_decimal_json(reloaded.text)[0]["charge_details"]
        assert imported_charges["amount"].as_tuple() == LARGE.as_tuple()
        assert imported_charges["quantity"].as_tuple() == SMALL.as_tuple()


def test_multipart_baseline_wait_false_and_manual_record(api, contract) -> None:
    client, service = api
    baseline = (
        '[{"id":"purchase","accounting_entity":"'
        + ENTITY
        + '","reference_no":"INV-123","charge_item":"Handling Charge",'
        '"amount":9007199254740991.01,"currency":"USD","posting_type":"AR"}]'
    )
    response = client.post(
        f"{API}/batches?wait=false",
        files={"files": ("invoice.png", _invoice())},
        data={"accounting_entity": ENTITY, "ocr_mode": FALLBACK, "existing_bills_json": baseline},
    )
    assert response.status_code == 202, response.text
    queued = response.json()
    _assert_schema(contract, "BatchSummary", queued)
    assert queued["stage"] == "QUEUED" and "records" not in queued
    assert client.get(f"{API}/batches?limit=1").status_code == 200
    processed = service.wait_for_batch(queued["id"], timeout=10)
    snapshot = service.repository.get_batch(queued["id"])["batch_existing_bills"]
    assert snapshot[0].amount.as_tuple() == LARGE.as_tuple()
    assert service.repository.list_existing_bills() == []
    doc_id = processed["documents"][0]["id"]
    manual = client.post(
        f"{API}/batches/{queued['id']}/records",
        content=f'{{"source_doc_id":"{doc_id}","charge_item":"Other","amount":0.0025,"currency":"USD"}}',
    )
    assert manual.status_code == 201, manual.text
    assert b'"amount":0.0025' in manual.content


def test_cross_batch_document_and_record_ids_are_rejected(api, contract) -> None:
    client, _ = api
    left, right = _create(client, "INV-LEFT"), _create(client, "INV-RIGHT")
    path = f"{API}/batches/{left['id']}"
    record_id, doc_id = right["records"][0]["id"], right["documents"][0]["id"]
    results = [
        client.patch(f"{path}/records/{record_id}", json={"amount": 2}),
        client.post(f"{path}/records/{record_id}/approve", json={"reason": "审核"}),
        client.post(f"{path}/records/{record_id}/reject", json={"reason": "审核"}),
        client.patch(f"{path}/documents/{doc_id}", json={"total_amount": 2}),
        client.post(f"{path}/documents/{doc_id}/retry"),
        client.post(f"{path}/documents/{doc_id}/force-parse"),
        client.post(
            f"{path}/records",
            json={"source_doc_id": doc_id, "charge_item": "H/D", "amount": 2, "currency": "USD"},
        ),
    ]
    for response in results:
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
        _assert_schema(contract, "ErrorResponse", response.json())
    assert client.get(path).json() == left
    assert client.get(f"{API}/batches/{right['id']}").json() == right


def test_review_blockers_reason_lengths_and_optimistic_locks(api, contract) -> None:
    client, _ = api
    batch = _create(client)
    path = _record_url(batch)
    schemas = contract["components"]["schemas"]
    summary = schemas["BatchSummary"]
    assert "version" not in summary["required"]
    assert summary["properties"]["version"] == {"type": "integer", "minimum": 1}
    template = f"{API}/batches/{{batch_id}}/records/{{record_id}}"
    operations = contract["paths"]
    patch_contract = operations[template]["patch"]
    assert patch_contract["responses"]["409"]["$ref"] == "#/components/responses/Conflict"
    assert {"expected_version", "expected_updated_at"} <= schemas["RecordPatchRequest"][
        "properties"
    ].keys()
    body = {
        "amount": 5,
        "expected_version": batch["version"],
        "expected_updated_at": batch["records"][0]["updated_at"],
    }
    _assert_schema(contract, "RecordPatchRequest", body)
    patch = client.patch(path, json=body)
    assert patch.status_code == 200
    assert patch.json()["version"] == batch["version"] + 1
    for action in ("approve", "reject"):
        assert operations[f"{template}/{action}"]["post"]["responses"]["422"]["$ref"] == (
            "#/components/responses/ValidationFailed"
        )
        for reason in ("😀", " A", "核" * 501):
            invalid = client.post(f"{path}/{action}", json={"reason": reason})
            assert invalid.status_code == 422
            assert invalid.json()["code"] == "INVALID_REVIEW_REASON"
            _assert_schema(contract, "ErrorResponse", invalid.json())
    blocked = client.post(f"{path}/approve", json={"reason": "核对"})
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "HARD_BLOCKER_CANNOT_APPROVE"
    assert "AMOUNT_TOTAL_MISMATCH" in blocked.json()["details"]["blocker_codes"]
    _assert_schema(contract, "ErrorResponse", blocked.json())
    stale_batch = client.patch(path, json={"amount": 100, "expected_version": batch["version"]})
    stale_record = client.patch(
        path,
        json={
            "amount": 100,
            "expected_updated_at": batch["records"][0]["updated_at"],
        },
    )
    assert (stale_batch.status_code, stale_batch.json()["code"]) == (409, "BATCH_CONFLICT")
    assert (stale_record.status_code, stale_record.json()["code"]) == (409, "RECORD_CONFLICT")
    for conflict in (stale_batch, stale_record):
        _assert_schema(contract, "ErrorResponse", conflict.json())
    max_reason = "核" * 500
    rejected = client.post(f"{path}/reject", json={"reason": f" {max_reason} "})
    assert rejected.status_code == 200
    assert rejected.json()["records"][0]["reject_reason"] == max_reason


def test_master_data_status_codes_and_existing_bill_precision(api, contract) -> None:
    client, service = api
    created = client.post(f"{API}/entities", json={"canonical_name": "New Entity"})
    assert created.status_code == 201
    conflict = client.post(f"{API}/entities", json={"canonical_name": "New Entity"})
    assert (conflict.status_code, conflict.json()["code"]) == (409, "DATA_CONFLICT")
    entity_path = f"{API}/entities/{created.json()['id']}"
    profile = {"canonical_name": "Renamed", "aliases": ["Alias"], "is_default": True}
    updated = client.put(entity_path, json=profile)
    assert updated.status_code == 200
    assert {field: updated.json()[field] for field in profile} == profile
    name_conflict = client.put(entity_path, json={"canonical_name": ENTITY})
    assert (name_conflict.status_code, name_conflict.json()["code"]) == (409, "DATA_CONFLICT")
    assert all(
        "409" in contract["paths"][path][method]["responses"]
        for path, method in ((f"{API}/entities", "post"), (f"{API}/entities/{{entity_id}}", "put"))
    )
    assert client.delete(entity_path).status_code == 405
    _assert_schema(contract, "AccountingEntity", client.get(f"{API}/entities").json()[0])

    item = {"raw_alias": "T03", "standard_name": "Handling Charge"}
    entry = client.post(f"{API}/dictionary", json=item)
    assert entry.status_code == 201
    entry_path = f"{API}/dictionary/{entry.json()['id']}"
    updated = client.post(f"{API}/dictionary", json=item | {"category": "local"})
    assert updated.status_code == 201
    assert updated.json()["id"] == entry.json()["id"] and updated.json()["category"] == "local"
    _assert_schema(contract, "ChargeDictionaryEntry", client.get(f"{API}/dictionary").json()[0])
    removed = client.delete(entry_path)
    assert removed.status_code == 204 and removed.content == b""
    assert client.delete(entry_path).status_code == 404

    response = client.post(
        f"{API}/existing-bills/import",
        content=(
            '{"default_accounting_entity":"' + ENTITY + '","bills":[{"id":"history",'
            '"charge_item":"Handling Charge","amount":9007199254740991.01,"quantity":0.0025,'
            '"currency":"USD","posting_type":"AR"}]}'
        ),
    )
    assert response.status_code == 200
    assert response.json() == {"imported_count": 1, "total_count": 1}
    bills = client.get(f"{API}/existing-bills", params={"accounting_entity": ENTITY})
    _assert_schema(contract, "ExistingBill", bills.json()[0])
    assert (
        b'"amount":9007199254740991.01' in bills.content and b'"quantity":0.0025' in bills.content
    )
    assert service.repository.get_existing_bill("history").amount.as_tuple() == LARGE.as_tuple()
    cleared = client.delete(f"{API}/existing-bills", params={"accounting_entity": ENTITY})
    assert cleared.status_code == 204 and client.get(f"{API}/existing-bills").json() == []


def test_retry_accepts_json_url_and_multipart_replacement(api) -> None:
    client, _ = api
    batch = _create(client)
    doc_id = batch["documents"][0]["id"]
    path = f"{API}/batches/{batch['id']}/documents/{doc_id}/retry"
    retry_url = client.post(path, json={"public_url": "https://8.8.8.8/invoice.pdf?secret=hidden"})
    assert retry_url.status_code == 200, retry_url.text
    assert "secret=hidden" not in retry_url.text
    assert retry_url.json()["documents"][0]["source_kind"] == "URL"
    replacement = client.post(path, files={"file": ("new.png", _invoice("INV-987"))})
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["documents"][0]["id"] == doc_id
    assert replacement.json()["records"][0]["document_info"]["reference_no"] == "INV-987"


def test_export_modes_count_actual_rows_and_match_downloads(api, contract) -> None:
    client, _ = api
    created = client.post(
        f"{API}/batches",
        files=[("files", (f"{index}.png", _invoice(f"INV-{index}"))) for index in range(3)],
        data={"accounting_entity": ENTITY, "ocr_mode": FALLBACK},
    )
    assert created.status_code == 201
    batch = created.json()
    path = f"{API}/batches/{batch['id']}"
    ready, pending, rejected = [item["id"] for item in batch["records"]]
    assert (
        client.post(f"{path}/records/{ready}/approve", json={"reason": "核对"}).status_code == 200
    )
    assert client.patch(f"{path}/records/{pending}", json={"amount": 5}).status_code == 200
    assert (
        client.post(f"{path}/records/{rejected}/reject", json={"reason": "驳回"}).status_code == 200
    )

    for mode, counts in (
        ("official", (1, 1, 1)),
        ("full_standard", (2, 0, 1)),
        ("audit", (3, 0, 0)),
    ):
        response = client.get(f"{path}/export", params={"mode": mode})
        assert response.status_code == 200
        preview = response.json()
        _assert_schema(contract, "ExportResponse", preview)
        assert (
            preview["included_count"],
            preview["excluded_pending_count"],
            preview["excluded_rejected_count"],
        ) == counts
        assert len(preview["records"]) == counts[0]
        download = client.get(f"{path}/export/download", params={"mode": mode})
        assert download.status_code == 200 and download.json() == preview["records"]
    assert client.get(f"{path}/export", params={"mode": "all"}).status_code == 400


def test_switch_reapply_and_sync_are_scoped_and_idempotent(api, contract) -> None:
    client, _ = api
    batch = _create(client)
    path = f"{API}/batches/{batch['id']}"
    assert client.post(f"{path}/reapply-dictionary").status_code == 200
    switched = client.post(f"{path}/switch-entity", json={"accounting_entity": OTHER_ENTITY})
    assert switched.status_code == 200, switched.text
    _assert_schema(contract, "SwitchEntityResponse", switched.json())
    assert switched.json()["batch"]["accounting_entity"] == OTHER_ENTITY
    assert switched.json()["batch"]["records"][0]["financial_attributes"]["posting_type"] == "AP"
    first = client.post(f"{path}/sync-baseline", json={"idempotency_key": "same"})
    second = client.post(f"{path}/sync-baseline", json={"idempotency_key": "same"})
    assert first.json() == {"created_count": 1, "updated_count": 0, "skipped_count": 0}
    assert second.json() == {"created_count": 0, "updated_count": 0, "skipped_count": 1}


def test_bundle_round_trip_preserves_decisions_history_and_isolated_baseline(
    make_service,
    contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = make_service(), make_service()
    bill = ExistingBillRecord(
        id="ledger",
        accounting_entity=ENTITY,
        charge_item="Handling Charge",
        amount=Decimal("30.00"),
        currency="USD",
        posting_type="AR",
        reference_no="INV-123",
        payment_term="PP",
    )
    target.repository.create_existing_bill(bill.model_copy(update={"amount": Decimal("75.00")}))
    baseline = (
        '[{"id":"ledger","accounting_entity":"' + ENTITY + '","charge_item":"Handling Charge",'
    )
    baseline += (
        '"reference_no":"INV-123","amount":30.00,"currency":"USD",'
        '"posting_type":"AR","payment_term":"PP"}]'
    )
    batch = source.create_batch(
        accounting_entity=ENTITY,
        ocr_mode=FALLBACK,
        source_channel="CLI",
        existing_bills_json=baseline,
        files=[
            ("C:/bundle-private/first.png", _invoice(amount="40.00")),
            ("second.png", _invoice("INV-456")),
        ],
    )
    assert batch["source_channel"] == "CLI"
    path = f"{API}/batches/{batch['id']}"
    with TestClient(create_app(batch_service=source)) as client:
        for record in batch["records"]:
            is_update = record["document_info"]["reference_no"] == "INV-123"
            action = "approve" if is_update else "reject"
            decision = client.post(
                f"{path}/records/{record['id']}/{action}", json={"reason": "核对"}
            )
            assert decision.status_code == 200, decision.text
    exported = export_review_bundle(source.repository, batch["id"])
    _assert_schema(contract, "ReviewBundleV1", exported)
    content = dumps_decimal_json(exported).encode()
    assert b"bundle-private" not in content
    assert b"stored_path" not in content and b"raw_bytes" not in content
    assert exported["bundle_version"] == "1.0" and exported["exported_at"].endswith("Z")

    with TestClient(create_app(batch_service=target)) as client:
        import_path = f"{API}/batches/import-review-bundle"
        response = client.post(import_path, content=content)
        assert response.status_code == 201, response.text
        imported = response.json()
        new_id = imported["id"]
        assert new_id != batch["id"]
        assert {item["id"] for item in batch["documents"]}.isdisjoint(
            item["id"] for item in imported["documents"]
        )
        assert {record["review_status"] for record in imported["records"]} == {
            "APPROVED",
            "REJECTED",
        }
        assert {record["source_doc_id"] for record in imported["records"]} == {
            item["id"] for item in imported["documents"]
        }
        stored = target.repository.get_batch(new_id)
        assert stored["batch_existing_bills"] == [bill]
        assert target.repository.get_existing_bill("ledger").amount == Decimal("75.00")
        approved = next(
            record for record in stored["records"] if record.review_status == "APPROVED"
        )
        assert approved.matched_existing_bill_id == "ledger"
        assert {"payment_term"} <= approved.inherited_fields.keys()
        assert approved.human_review_history[-1].action == "APPROVE"
        assert {entry["action"] for entry in target.repository.list_review_audit_logs(new_id)} == {
            "APPROVE",
            "REJECT",
        }
        original = client.post(f"{API}/batches/{new_id}/documents/{approved.source_doc_id}/retry")
        assert (original.status_code, original.json()["code"]) == (400, "ORIGINAL_FILE_UNAVAILABLE")

        repeated = client.post(import_path, content=content)
        assert repeated.status_code == 201 and repeated.json()["id"] != new_id
        before = _row_counts(target.repository)
        insert_audit = BillingRepository.insert_review_audit_log
        attempts = []

        def fail_second_audit(repository, *args, **kwargs):
            attempts.append(args)
            if len(attempts) == 2:
                raise IntegrityError("injected late failure")
            return insert_audit(repository, *args, **kwargs)

        monkeypatch.setattr(BillingRepository, "insert_review_audit_log", fail_second_audit)
        failed = client.post(import_path, content=content)
        assert len(attempts) == 2
        assert (failed.status_code, failed.json()["code"]) == (400, "INVALID_REVIEW_BUNDLE")
        assert _row_counts(target.repository) == before


def _row_counts(repository: BillingRepository) -> tuple[int, int, int, int]:
    with repository.db.connect() as connection:
        row = connection.execute("""SELECT
            (SELECT count(*) FROM batches), (SELECT count(*) FROM source_documents),
            (SELECT count(*) FROM billing_records),
            (SELECT count(*) FROM existing_bills)""").fetchone()
    return tuple(row)


def test_bundle_rechecks_blockers_and_rejects_broken_references(api) -> None:
    client, service = api
    batch = _create(client)
    exported = json.loads(dumps_decimal_json(export_review_bundle(service.repository, batch["id"])))
    path = f"{API}/batches/import-review-bundle"
    before = _row_counts(service.repository)
    wrong_version = client.post(path, json=exported | {"bundle_version": 1.0})
    assert (wrong_version.status_code, wrong_version.json()["code"]) == (
        400,
        "INVALID_BUNDLE_VERSION",
    )
    for field, value in (
        ("parent_doc_id", "missing"),
        ("parent_doc_id", exported["documents"][0]["id"]),
    ):
        malformed = deepcopy(exported)
        malformed["documents"][0][field] = value
        assert client.post(path, json=malformed).status_code == 400
    wrong_entity = deepcopy(exported)
    wrong_entity["records"][0]["financial_attributes"]["accounting_entity"] = OTHER_ENTITY
    assert client.post(path, json=wrong_entity).status_code == 400
    unscoped = deepcopy(exported)
    unscoped["records"][0]["source_doc_id"] = "other-batch-source"
    assert client.post(path, json=unscoped).status_code == 400
    assert _row_counts(service.repository) == before

    record = exported["records"][0]
    record["review_status"] = "APPROVED"
    record["human_overrides"]["currency"] = None
    validated = client.post(path, json=exported)
    assert validated.status_code == 201, validated.text
    result = validated.json()["records"][0]
    assert result["review_status"] == "NEEDS_REVIEW"
    assert any(alert["severity"] == "BLOCKER" for alert in result["validation_alerts"])


def test_eval_runs_persist_unverified_and_exact_injected_results(tmp_path: Path, contract) -> None:
    settings = _settings(tmp_path)
    path = f"{API}/eval-runs"
    with TestClient(create_app(settings=settings)) as client:
        unverified = client.post(path, json={"run_mode": "LIVE_LLM"})
        assert unverified.status_code == 201
        assert unverified.json()["status"] == "UNVERIFIED"
        assert (
            unverified.json()["actual_trials"] == 0
            and unverified.json()["schema_valid_rate"] is None
        )
        _assert_schema(contract, "EvalRunSummary", unverified.json())
        assert client.post(path, json={"run_mode": "LIVE_LLM", "k_trials": 11}).status_code == 422

    rate = Decimal("0.123456789012345678901")

    async def runner(run_mode: str, k_trials: int) -> dict:
        assert (run_mode, k_trials) == ("OFFLINE_DETERMINISTIC", 2)
        return {
            "id": "evaluated",
            "status": "COMPLETED",
            "model_name": "fixture",
            "total_cases": 1,
            "actual_trials": 2,
            "schema_valid_rate": rate,
            "report": {"amount": LARGE},
        }

    with TestClient(create_app(settings=settings, eval_runner=runner)) as client:
        finished = client.post(path, json={"run_mode": "OFFLINE_DETERMINISTIC", "k_trials": 2})
        assert finished.status_code == 201, finished.text
        assert loads_decimal_json(finished.text)["schema_valid_rate"].as_tuple() == rate.as_tuple()
        _assert_schema(contract, "EvalRunSummary", finished.json())
    with TestClient(create_app(settings=settings)) as client:
        history = client.get(path)
        assert [row["status"] for row in history.json()] == ["COMPLETED", "UNVERIFIED"]
        exact = loads_decimal_json(history.text)[0]
        assert exact["schema_valid_rate"].as_tuple() == rate.as_tuple()
        assert exact["report"]["amount"].as_tuple() == LARGE.as_tuple()


def test_static_workbench_keeps_api_docs_and_errors_reachable(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>Billing workspace</title>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets/app.js").write_text("console.log('test')", encoding="utf-8")
    with TestClient(create_app(settings=_settings(tmp_path), frontend_dir=dist)) as client:
        assert "Billing workspace" in client.get("/").text
        assert "Billing workspace" in client.get("/workbench/review").text
        assert "console.log" in client.get("/assets/app.js").text
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get(f"{API}/health").json()["status"] == "ok"
        assert (
            client.get("/docs").status_code == 200
            and client.get("/openapi.json").status_code == 200
        )
        missing = client.get(f"{API}/missing")
        assert (missing.status_code, missing.json()["code"]) == (404, "NOT_FOUND")


def test_bundle_service_does_not_import_fastapi_or_harness() -> None:
    services = ROOT / "src/shipping_billing_agent/services"
    for path in services.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or "", *(alias.name for alias in node.names)]
            else:
                continue
            assert all(not {"fastapi", "harness"}.intersection(name.split(".")) for name in imports)
