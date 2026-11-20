from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep
from typing import Any

import pytest

from shipping_billing_agent.domain.entity_resolver import recalculate_for_entity
from shipping_billing_agent.domain.models import (
    AccountingEntityProfile,
    AlertCode,
    BatchStage,
    BatchStatus,
    CandidateBillSummary,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    DocumentStatus,
    ExcludedClause,
    ExistingBillRecord,
    ExtractionChannel,
    FailedDocumentAlert,
    FieldDiff,
    HumanReviewAction,
    MatchLevel,
    OcrStructuredPayload,
    OperationType,
    PackageShipmentRefs,
    ParsedDocument,
    ParsedTable,
    PaymentTerm,
    PostingType,
    ProcessedBillingRecord,
    RawChargeEnvelope,
    RawChargeItem,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ReviewStatus,
    ShipmentRefs,
    SourceDocTotalsSnapshot,
    SourceEvidence,
    SourceKind,
    SourceRefValue,
    ValidationAlert,
    dumps_decimal_json,
    loads_decimal_json,
)
from shipping_billing_agent.storage import (
    BillingRepository,
    DatabaseManager,
    OptimisticLockError,
    ResourceNotFoundError,
)

_ENTITY = "NEPTUNE SHIPPING LIMITED"
_ENTITY_ALIAS = "NEPTUNE SHIPPING LIMITED (LAX)"
_OTHER_ENTITY = "TARGETING A INC"
_LARGE_AMOUNT = Decimal("9007199254740991.01")
_SMALL_AMOUNT = Decimal("0.0025")
_CONTRACT = Path(__file__).resolve().parents[2] / "docs/workflow/contracts/schema.sql"
_EVAL_METRICS = (
    "schema_valid_rate",
    "field_exact_match",
    "ar_ap_accuracy",
    "operation_accuracy",
    "conflict_recall",
    "pass_at_k",
    "pass_pow_k",
)


@pytest.fixture
def repository(tmp_path: Path) -> BillingRepository:
    return BillingRepository(DatabaseManager(tmp_path / "nested" / "billing.db"))


def _document(doc_id: str, **overrides: Any) -> ParsedDocument:
    return ParsedDocument.model_validate(
        {
            "doc_id": doc_id,
            "original_filename": f"{doc_id}.pdf",
            "sanitized_filename": f"{doc_id}.pdf",
            "source_kind": SourceKind.PDF,
            "content_sha256": "a" * 64,
            "extraction_channel": ExtractionChannel.PLTPLT_OCR,
            "status": DocumentStatus.PARSED,
            **overrides,
        }
    )


def _record(record_id: str = "record", **overrides: Any) -> ProcessedBillingRecord:
    return ProcessedBillingRecord.model_validate(
        {
            "id": record_id,
            "source_doc_id": "doc",
            "accounting_entity": _ENTITY,
            "operation": OperationType.CREATE,
            "review_status": ReviewStatus.AUTO_READY,
            "charge_item": "Handling Charge",
            "raw_charge_name": "H/D Fee",
            "amount": _LARGE_AMOUNT,
            "currency": "USD",
            "posting_type": PostingType.AR,
            **overrides,
        }
    )


def _bill(bill_id: str, **overrides: Any) -> ExistingBillRecord:
    return ExistingBillRecord.model_validate(
        {
            "id": bill_id,
            "accounting_entity": _ENTITY,
            "charge_item": "Handling Charge",
            "amount": Decimal("12.30"),
            "currency": "USD",
            "posting_type": PostingType.AR,
            **overrides,
        }
    )


def _completed_batch(repository: BillingRepository, **options: Any) -> dict[str, Any]:
    return repository.create_batch(
        _ENTITY, stage=BatchStage.COMPLETED, status=BatchStatus.COMPLETED, **options
    )


def _schema_objects(connection: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return [
        (kind, name, re.sub(r"\s+", "", sql) if sql else None)
        for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]


def _sync_items(repository: BillingRepository, batch_id: str) -> list[dict[str, Any]]:
    with repository.db.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM baseline_sync_items WHERE batch_id = ? ORDER BY rowid", (batch_id,)
            )
        ]


def test_initialization_matches_eight_table_contract(repository: BillingRepository) -> None:
    with closing(sqlite3.connect(":memory:")) as contract:
        contract.executescript(_CONTRACT.read_text(encoding="utf-8"))
        expected = _schema_objects(contract)

    with repository.db.connect() as connection:
        assert _schema_objects(connection) == expected
        assert {name for kind, name, _ in expected if kind == "table"} == {
            "accounting_entities",
            "charge_dictionary",
            "existing_bills",
            "batches",
            "source_documents",
            "billing_records",
            "baseline_sync_items",
            "eval_runs",
        }
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_unversioned_contract_upgrades_without_losing_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.db"
    legacy = _CONTRACT.read_text(encoding="utf-8").replace(
        "    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),\n"
        "    batch_existing_bills_json TEXT,\n"
        "    failed_documents_json TEXT NOT NULL DEFAULT '[]',\n",
        "",
    )
    legacy = legacy.replace("    extraction_snapshot_json TEXT,\n", "").replace(
        "    record_state_json TEXT NOT NULL DEFAULT '{}',\n", ""
    )
    timestamp = "2026-01-01T00:00:00Z"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(legacy)
        connection.execute(
            """INSERT INTO batches
               (id, short_code, accounting_entity, status, stage, created_at, updated_at)
               VALUES ('batch', 'B-1', ?, 'COMPLETED', 'COMPLETED', ?, ?)""",
            (_ENTITY, timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO source_documents
               (id, batch_id, original_filename, sanitized_filename, source_kind,
                content_sha256, extraction_channel, status, created_at)
               VALUES ('doc', 'batch', 'doc.pdf', 'doc.pdf', 'PDF', ?,
                       'PLTPLT_OCR', 'PARSED', ?)""",
            ("a" * 64, timestamp),
        )
        connection.execute(
            """INSERT INTO billing_records
               (id, batch_id, source_doc_id, operation, review_status, accounting_entity,
                charge_item, amount, unit_price, currency, posting_type, created_at, updated_at)
               VALUES ('record', 'batch', 'doc', 'CREATE', 'AUTO_READY', ?,
                       'Handling Charge', ?, ?, 'USD', 'AR', ?, ?)""",
            (_ENTITY, str(_LARGE_AMOUNT), str(_SMALL_AMOUNT), timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO charge_dictionary (id, raw_alias, standard_name, created_at)
               VALUES ('custom', 'handling', 'Custom Handling', ?)""",
            (timestamp,),
        )
        connection.commit()

    repository = BillingRepository(DatabaseManager(path))
    batch = repository.get_batch("batch")
    assert batch["version"] == 1
    assert batch["batch_existing_bills"] is None
    assert batch["failed_document_alerts"] == []
    assert batch["documents"][0]["extraction_snapshot"] is None
    (record,) = batch["records"]
    assert record.amount.as_tuple() == _LARGE_AMOUNT.as_tuple()
    assert record.unit_price.as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert [row["id"] for row in repository.list_charge_dictionary()] == ["custom"]
    assert repository.sync_baseline("batch") == {"created": 1, "updated": 0, "skipped": 0}
    reopened = BillingRepository(DatabaseManager(path))
    assert reopened.get_batch("batch") == batch
    assert reopened.sync_baseline("batch") == {"created": 0, "updated": 0, "skipped": 1}


def test_seed_rows_respect_edits_and_deletions(repository: BillingRepository) -> None:
    profiles = repository.list_accounting_entities()
    assert len(profiles) == 5
    neptune = next(row for row in profiles if row["canonical_name"] == _ENTITY)
    assert neptune["is_default"]
    assert neptune["aliases"] == ["NEPTUNE SHIPPING LIMITED (LAX)", "NEPTUNE"]
    edited = repository.update_accounting_entity(
        neptune["id"],
        AccountingEntityProfile(canonical_name=_ENTITY, aliases=["Neptune USA"], is_default=True),
    )
    added = repository.create_accounting_entity(AccountingEntityProfile(canonical_name="Partner"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        repository.create_accounting_entity(AccountingEntityProfile(canonical_name=_ENTITY))

    entries = repository.list_charge_dictionary()
    assert all(row["is_builtin"] for row in entries)
    handling = next(row for row in entries if row["raw_alias"] == "h/d fee")
    assert handling["standard_name"] == "Handling Charge"
    edited_entry = repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="h/d fee", standard_name="Customer Handling")
    )
    assert edited_entry["id"] == handling["id"]
    assert edited_entry["is_builtin"]

    candidate = repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="admin x", standard_name="Custom Administration")
    )
    changed = repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="admin z", standard_name="Custom Fee"),
        entry_id=candidate["id"],
    )
    assert changed["id"] == candidate["id"]

    reopened = BillingRepository(DatabaseManager(repository.db.path))
    assert edited in reopened.list_accounting_entities()
    assert added in reopened.list_accounting_entities()
    assert len(reopened.list_accounting_entities()) == 6
    assert edited_entry in reopened.list_charge_dictionary()
    for entry in reopened.list_charge_dictionary():
        assert reopened.delete_charge_dictionary_entry(entry["id"])
    assert BillingRepository(DatabaseManager(repository.db.path)).list_charge_dictionary() == []


@pytest.mark.parametrize(
    "stage",
    [
        BatchStage.QUEUED,
        BatchStage.UNPACKING,
        BatchStage.EXTRACTING,
        BatchStage.MATCHING,
        BatchStage.VALIDATING,
    ],
)
def test_startup_recovers_interrupted_batches_and_documents(
    repository: BillingRepository, stage: BatchStage
) -> None:
    prior_alert = FailedDocumentAlert(doc_id="no-rows", filename="no-rows.pdf", reason="没有费用行")
    batch = repository.create_batch(
        _ENTITY, total_files=3, failed_document_alerts=[prior_alert], failed_files=1
    )
    batch_id = batch["id"]
    repository.upsert_source_documents(
        batch_id,
        [
            _document("unfinished"),
            _document("no-rows"),
            DocumentExtractionBundle(
                document=_document("finished"),
                extraction=RawExtractionEnvelope(extraction=RawDocumentExtraction()),
            ),
        ],
    )
    active = repository.update_batch_stage(batch_id, stage, stage_message="正在处理")
    completed = _completed_batch(repository)
    worker = BillingRepository(DatabaseManager(repository.db.path, recover_orphans=False))
    assert worker.get_batch(batch_id)["stage"] == stage
    assert active["status"] == "PROCESSING"
    assert active["version"] == batch["version"] + 1

    reopened = BillingRepository(DatabaseManager(repository.db.path))
    failed = reopened.get_batch(batch_id)
    assert failed["status"] == "PARTIAL_ERROR"
    assert failed["stage"] == "FAILED"
    assert "可重试" in failed["stage_message"]
    assert failed["version"] == active["version"] + 1
    assert failed["failed_files"] == 2
    assert failed["failed_document_alerts"][0] == prior_alert
    interrupted = reopened.get_document(batch_id, "unfinished")
    assert interrupted["status"] == "FAILED"
    assert interrupted["extraction_channel"] == "FAILED"
    assert interrupted["error_message"] == failed["stage_message"]
    assert failed["failed_document_alerts"][1].error_message == interrupted["error_message"]
    assert reopened.get_document(batch_id, "finished")["status"] == "PARSED"
    assert reopened.get_document(batch_id, "no-rows")["status"] == "PARSED"
    assert reopened.get_batch(completed["id"])["stage"] == "COMPLETED"
    assert BillingRepository(DatabaseManager(repository.db.path)).get_batch(batch_id) == failed


def test_worker_process_exit_allows_recovery_and_counts_one_failed_mail(
    repository: BillingRepository, tmp_path: Path
) -> None:
    prior = FailedDocumentAlert(
        doc_id="body", filename="body.pdf", reason="正文已有告警", affected_record_ids=["line"]
    )
    batch_id = repository.create_batch(
        _ENTITY, stage=BatchStage.EXTRACTING, total_files=1, failed_document_alerts=[prior]
    )["id"]
    repository.upsert_source_documents(
        batch_id,
        [
            _document("mail", source_kind=SourceKind.EML),
            _document("body", source_kind=SourceKind.EML_BODY, parent_doc_id="mail"),
            _document("nested", source_kind=SourceKind.EML, parent_doc_id="mail"),
            _document("attachment", status=DocumentStatus.FAILED, parent_doc_id="nested"),
        ],
    )
    repository.upsert_billing_record(batch_id, _record("line", source_doc_id="attachment"))
    before = repository.get_batch(batch_id)
    ready = tmp_path / "worker-holds-lease"
    script = """
import sys
from pathlib import Path
from shipping_billing_agent.storage import DatabaseManager

db = DatabaseManager(sys.argv[1], recover_orphans=False)
lease = db.try_acquire_worker_lease(sys.argv[2])
assert lease is not None and lease.active
with lease:
    Path(sys.argv[3]).touch()
    sys.stdin.buffer.read(1)
"""
    command = [sys.executable, "-c", script, str(repository.db.path), batch_id, str(ready)]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
        try:
            deadline = monotonic() + 15
            while not ready.is_file():
                if worker.poll() is not None or monotonic() >= deadline:
                    pytest.fail("Worker process did not acquire the file lease")
                sleep(0.02)
            reopened = BillingRepository(DatabaseManager(repository.db.path))
            assert reopened.get_batch(batch_id) == before
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)

    recovered = BillingRepository(DatabaseManager(repository.db.path)).get_batch(batch_id)
    assert (recovered["status"], recovered["stage"], recovered["failed_files"]) == (
        "PARTIAL_ERROR",
        "FAILED",
        1,
    )
    assert recovered["total_files"] == 1 and recovered["version"] == before["version"] + 1
    alerts = {alert.doc_id: alert for alert in recovered["failed_document_alerts"]}
    assert alerts.keys() == {"mail", "body", "nested", "attachment"}
    assert alerts["body"] == prior
    assert alerts["attachment"].affected_record_ids == ["line"]
    assert recovered["records"] == before["records"]


def test_document_and_record_snapshots_reopen_with_exact_values(
    repository: BillingRepository,
) -> None:
    refs = ShipmentRefs(
        mbl_nos=[SourceRefValue(value="MBL-1", source="邮件附件")],
        hbl_no="HBL-1",
        booking_no="BKG-1",
        container_nos=["MSCU1234567"],
    )
    issue = ValidationAlert.from_code(
        AlertCode.OCR_NUMERIC_CONFUSION, "数量有疑似 OCR 字符", field="quantity", actual="O"
    )
    charge = RawChargeItem(
        raw_charge_name="H/D Fee", unit_price=_SMALL_AMOUNT, amount=_LARGE_AMOUNT, currency="USD"
    )
    clause = ExcludedClause(text="滞箱费另计", reason="非本次实际费用", page=2)
    snapshot = RawExtractionEnvelope(
        extraction=RawDocumentExtraction(
            document_type="Invoice",
            issuer=_ENTITY,
            recipient="Client",
            shipment_refs=refs,
            subtotal_amount=_LARGE_AMOUNT,
            tax_amount=_SMALL_AMOUNT,
            total_amount=_LARGE_AMOUNT,
            total_currency="USD",
            charges=[charge],
            excluded_clauses=[clause],
        ),
        charges=[
            RawChargeEnvelope(
                charge=charge,
                explicit_fields={"amount", "unit_price"},
                raw_quantity_text="O",
                raw_invalid_fields={"quantity": "O"},
                raw_parse_issues=[issue],
            )
        ],
        explicit_fields={"total_amount", "shipment_refs"},
        raw_invalid_fields={"document_date": "202O-01-10"},
        raw_parse_issues=[issue],
        subtotals_by_currency={"USD": _LARGE_AMOUNT},
        taxes_by_currency={"USD": _SMALL_AMOUNT},
        totals_by_currency={"USD": _LARGE_AMOUNT, "JPY": Decimal("0.00")},
        has_explicit_total=True,
    )
    shared = repository.create_existing_bill(_bill("matched"))
    isolated = _bill(shared.id, amount=Decimal("30.0000"))
    alert = FailedDocumentAlert(
        doc_id="doc",
        filename="doc.pdf",
        reason="表格的一部分没有可用费用行",
        affected_record_ids=["record"],
    )
    batch = _completed_batch(
        repository, batch_existing_bills=[isolated], failed_document_alerts=[alert]
    )
    batch_id = batch["id"]
    doc = _document(
        "doc",
        parent_doc_id="root",
        raw_text="OCR 识别文本",
        tables=[ParsedTable(rows=[["Amount", str(_LARGE_AMOUNT)]], source="page 1")],
        metadata={"reference": "RN-1", "page": 2},
        ocr_payload=OcrStructuredPayload.model_validate(
            {
                "items": [{"fileType": "Invoice", "chargeList": [{"amount": _LARGE_AMOUNT}]}],
                "has_charges": True,
                "has_metadata": True,
            }
        ),
    )
    repository.upsert_source_documents(
        batch_id,
        [
            DocumentExtractionBundle(document=doc, extraction=snapshot, retry_count=1),
            _document("root", source_kind=SourceKind.EML),
        ],
        stored_paths={"doc": "original/doc.pdf"},
    )
    repository.upsert_source_documents(batch_id, [doc])

    delta = FieldDiff(
        field="amount", old_value=isolated.amount, new_value=_LARGE_AMOUNT, changed=True
    )
    record = _record(
        batch_id=batch_id,
        operation=OperationType.UPDATE,
        review_status=ReviewStatus.APPROVED,
        match_level=MatchLevel.L1_DOC_CHARGE,
        matched_existing_bill_id=isolated.id,
        matched_bill_ref="历史样本 30.0000",
        matched_from_dictionary=True,
        candidate_bills=[
            CandidateBillSummary(
                id=isolated.id,
                label="候选",
                charge_item=isolated.charge_item,
                amount=isolated.amount,
                currency="USD",
            )
        ],
        quantity=Decimal("1.0000"),
        unit_price=_SMALL_AMOUNT,
        issuer=_ENTITY,
        recipient="Client",
        payer_hint="Client",
        payee_hint=_ENTITY,
        document_type="Credit Note",
        document_date="2026-01-01",
        reference_no="R-1",
        debit_credit="CREDIT",
        is_credit=True,
        payment_term=PaymentTerm.CC,
        field_diffs=[delta],
        validation_alerts=[issue],
        ai_reasoning=["按费用名称匹配", "沿用历史账单字段"],
        excluded_clauses=[clause],
        explicit_fields={"amount", "posting_type", "issuer", "recipient", "raw_charge_name"},
        inherited_fields={"unit_price": _SMALL_AMOUNT, "document_date": None},
        source_values_before_overrides={"amount": Decimal("10.0100")},
        human_overrides={"amount": _LARGE_AMOUNT, "payment_term": "CC"},
        human_review_history=[
            HumanReviewAction(
                action="PATCH",
                operator="财务",
                reason="核对原始账单",
                timestamp="2026-01-01T00:00:00Z",
                overrides={"amount": _LARGE_AMOUNT, "unit_price": _SMALL_AMOUNT},
            )
        ],
        raw_invalid_fields={"quantity": "O"},
        raw_parse_issues=[issue],
        raw_explicit_amount=_LARGE_AMOUNT,
        raw_amount_abs=_LARGE_AMOUNT,
        raw_amount_text=str(_LARGE_AMOUNT),
        raw_unit_price_text=str(_SMALL_AMOUNT),
        raw_quantity_text="O",
        shipment_refs=refs,
        package_sibling_refs=[PackageShipmentRefs(source_doc_id="root", hbl_nos=["HBL-1"])],
        source_evidence=SourceEvidence(source_doc_id="doc", source_file="doc.pdf", page=1),
        source_doc_totals=SourceDocTotalsSnapshot.from_extraction("doc", snapshot),
        confidence=0.94,
        retry_count=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )
    repository.upsert_billing_record(batch_id, record)

    reopened = BillingRepository(DatabaseManager(repository.db.path))
    detail = reopened.get_batch(batch_id)
    assert detail["batch_existing_bills"] == [isolated]
    assert detail["failed_document_alerts"] == [alert]
    assert detail["counts"] == {
        "create": 0,
        "update": 1,
        "no_change": 0,
        "pending_review": 0,
        "rejected": 0,
    }
    assert reopened.list_existing_bills() == [shared]
    restored = detail["records"][0]
    assert restored.model_dump(mode="python") == record.model_dump(mode="python")
    assert restored.amount.as_tuple() == _LARGE_AMOUNT.as_tuple()
    assert restored.unit_price.as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert restored.quantity.as_tuple() == record.quantity.as_tuple()
    assert restored.field_diffs[0].old_value.as_tuple() == isolated.amount.as_tuple()
    assert (
        restored.human_review_history[0].overrides["amount"].as_tuple() == _LARGE_AMOUNT.as_tuple()
    )
    assert restored.inherited_fields["unit_price"].as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert (
        restored.source_values_before_overrides["amount"].as_tuple()
        == Decimal("10.0100").as_tuple()
    )
    restored_doc = reopened.get_document(batch_id, "doc")
    assert restored_doc["extraction_snapshot"] == snapshot
    assert restored_doc["extraction_snapshot"].taxes_by_currency["USD"].as_tuple() == (
        _SMALL_AMOUNT.as_tuple()
    )
    assert restored_doc["stored_path"] == "original/doc.pdf"
    assert restored_doc["retry_count"] == 1
    assert restored_doc["affected_record_ids"] == ["record"]
    assert ParsedDocument.model_validate({"doc_id": "doc", **restored_doc}) == doc
    with reopened.db.connect() as connection:
        assert tuple(
            connection.execute(
                "SELECT matched_existing_bill_id, amount, unit_price FROM billing_records"
            ).fetchone()
        ) == (None, str(_LARGE_AMOUNT), str(_SMALL_AMOUNT))

    patched = snapshot.model_copy(
        update={
            "extraction": snapshot.extraction.model_copy(update={"total_amount": _SMALL_AMOUNT}),
            "totals_by_currency": {"USD": _SMALL_AMOUNT},
        }
    )
    reopened.update_document_status_and_snapshot(batch_id, "doc", extraction_snapshot=patched)
    reopened = BillingRepository(DatabaseManager(repository.db.path))
    corrected = reopened.get_document(batch_id, "doc")
    assert corrected["total_amount"].as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert corrected["extraction_snapshot"] == patched


def test_reopened_record_can_restore_inherited_and_pre_override_values(
    repository: BillingRepository,
) -> None:
    batch = _completed_batch(repository)
    repository.upsert_source_documents(batch["id"], [_document("doc")])
    repository.upsert_billing_record(
        batch["id"],
        _record(
            issuer=_OTHER_ENTITY,
            recipient="Client",
            document_date="2026-01-20",
            unit_price=Decimal("40.00"),
            amount=Decimal("50.00"),
            inherited_fields={"unit_price": _SMALL_AMOUNT},
            source_values_before_overrides={"amount": Decimal("30.00")},
            human_overrides={"amount": Decimal("50.00")},
            explicit_fields={
                "amount",
                "unit_price",
                "currency",
                "issuer",
                "recipient",
                "raw_charge_name",
            },
        ),
    )
    reopened = BillingRepository(DatabaseManager(repository.db.path))
    stored = reopened.get_billing_record(batch["id"], "record")
    without_override = stored.model_copy(update={"human_overrides": {}})
    recalculated, _ = recalculate_for_entity([without_override], _OTHER_ENTITY)
    assert recalculated[0].amount.as_tuple() == Decimal("30.00").as_tuple()
    assert recalculated[0].unit_price.as_tuple() == _SMALL_AMOUNT.as_tuple()


def test_optimistic_locks_and_batch_scope(repository: BillingRepository) -> None:
    first = _completed_batch(repository)
    second = _completed_batch(repository)
    first_id, second_id = first["id"], second["id"]
    repository.upsert_source_documents(first_id, [_document("doc")])
    repository.upsert_source_documents(second_id, [_document("other-doc")])
    record = repository.upsert_billing_record(first_id, _record())
    updated = repository.update_batch_with_version_check(
        first_id, first["version"], stage_message="已复核", batch_existing_bills=[]
    )
    assert updated["version"] == first["version"] + 1
    assert repository.get_batch(first_id)["batch_existing_bills"] == []
    assert repository.get_batch(second_id)["batch_existing_bills"] is None
    with pytest.raises(OptimisticLockError, match="BATCH_CONFLICT") as batch_conflict:
        repository.update_batch_with_version_check(
            first_id, first["version"], stage_message="过时修改"
        )
    assert batch_conflict.value.actual == updated["version"]
    with pytest.raises(OptimisticLockError, match="BATCH_CONFLICT"):
        repository.sync_batch_to_baseline(first_id, expected_version=first["version"])
    assert repository.list_existing_bills() == []

    changed = repository.upsert_billing_record(
        first_id,
        record.model_copy(update={"amount": _SMALL_AMOUNT}),
        expected_updated_at=record.updated_at,
    )
    assert changed.updated_at != record.updated_at
    with pytest.raises(OptimisticLockError, match="RECORD_CONFLICT") as record_conflict:
        repository.upsert_billing_record(first_id, record, expected_updated_at=record.updated_at)
    assert record_conflict.value.actual == changed.updated_at

    assert repository.get_document(second_id, "doc") is None
    assert repository.get_billing_record(second_id, "record") is None
    with pytest.raises(ResourceNotFoundError, match="not found"):
        repository.update_document_status_and_snapshot(
            second_id, "doc", status=DocumentStatus.FAILED
        )
    with pytest.raises(ResourceNotFoundError, match="Parent document"):
        repository.upsert_source_documents(second_id, [_document("child", parent_doc_id="doc")])
    with pytest.raises(ValueError, match="another batch"):
        repository.upsert_source_documents(second_id, [_document("doc")])
    with pytest.raises(ValueError, match="another batch"):
        repository.upsert_billing_record(second_id, _record(source_doc_id="other-doc"))
    with pytest.raises(ResourceNotFoundError, match="Document"):
        repository.upsert_billing_record(second_id, _record("new-id", source_doc_id="doc"))
    assert repository.get_billing_record(first_id, "record") == changed
    assert repository.get_document(first_id, "doc")["status"] == "PARSED"

    with (
        pytest.raises(OptimisticLockError, match="RECORD_CONFLICT"),
        repository.transaction() as transaction,
    ):
        transaction.update_batch_with_version_check(
            first_id, updated["version"], stage_message="暂存的修改"
        )
        transaction.upsert_billing_record(first_id, record, expected_updated_at=record.updated_at)
    assert repository.get_batch(first_id)["version"] == updated["version"]
    assert repository.get_batch(first_id)["stage_message"] == "已复核"


@pytest.mark.parametrize("force_create", [False, True], ids=["automatic", "forced"])
def test_baseline_sync_is_idempotent_across_in_place_rewrites(
    repository: BillingRepository, force_create: bool
) -> None:
    batch = _completed_batch(repository)
    batch_id = batch["id"]
    doc = _document("doc")
    repository.upsert_source_documents(batch_id, [doc])
    record = repository.upsert_billing_records(
        batch_id,
        [
            _record("ready", human_overrides={"force_create_new": True} if force_create else {}),
            _record("pending", operation=OperationType.PENDING_REVIEW),
            _record("no-change", operation=OperationType.NO_CHANGE),
            _record("rejected", review_status=ReviewStatus.REJECTED),
            _record("needs-review", review_status=ReviewStatus.NEEDS_REVIEW),
        ],
    )[0]
    assert repository.sync_baseline(batch_id) == {"created": 1, "updated": 0, "skipped": 0}
    first_items = _sync_items(repository, batch_id)
    assert [item["billing_record_id"] for item in first_items] == ["ready"]
    shared_id = first_items[0]["existing_bill_id"]
    first_bill = repository.get_existing_bill(shared_id)
    assert first_bill.version == 1
    assert first_bill.amount.as_tuple() == _LARGE_AMOUNT.as_tuple()

    reopened = BillingRepository(DatabaseManager(repository.db.path))
    assert reopened.sync_baseline(batch_id) == {"created": 0, "updated": 0, "skipped": 1}
    reopened.upsert_source_documents(
        batch_id, [doc.model_copy(update={"raw_text": "纠正后的文本"})]
    )
    edited = reopened.upsert_billing_record(
        batch_id,
        record.model_copy(update={"amount": _SMALL_AMOUNT, "review_status": ReviewStatus.APPROVED}),
        expected_updated_at=record.updated_at,
    )
    assert _sync_items(reopened, batch_id) == first_items
    assert edited.created_at == record.created_at
    assert edited.updated_at != record.updated_at
    assert reopened.sync_baseline(batch_id) == {"created": 0, "updated": 1, "skipped": 0}
    items = _sync_items(reopened, batch_id)
    assert items[0] == first_items[0]
    assert [item["record_updated_at"] for item in items] == [record.updated_at, edited.updated_at]
    assert {item["existing_bill_id"] for item in items} == {shared_id}
    (final_bill,) = reopened.list_existing_bills()
    assert final_bill.version == 2
    assert final_bill.created_at == first_bill.created_at
    assert final_bill.amount.as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert reopened.upsert_billing_record(batch_id, edited).updated_at == edited.updated_at
    assert reopened.sync_baseline(batch_id) == {"created": 0, "updated": 0, "skipped": 1}


def test_baseline_sync_updates_shared_match_and_isolates_snapshot_ids(
    repository: BillingRepository,
) -> None:
    shared = repository.create_existing_bill(
        _bill(
            "shared-match", external_bill_key="LEDGER-1", extra_fields={"import_note": "保留库字段"}
        )
    )
    batch = _completed_batch(repository)
    batch_id = batch["id"]
    repository.upsert_source_documents(batch_id, [_document("doc-shared")])
    repository.upsert_billing_record(
        batch_id,
        _record(
            "shared-update",
            source_doc_id="doc-shared",
            operation=OperationType.UPDATE,
            matched_existing_bill_id=shared.id,
        ),
    )
    assert repository.sync_baseline(batch_id) == {"created": 0, "updated": 1, "skipped": 0}
    updated = repository.get_existing_bill(shared.id)
    assert updated.version == 2
    assert updated.amount.as_tuple() == _LARGE_AMOUNT.as_tuple()
    assert updated.extra_fields == shared.extra_fields
    assert updated.external_bill_key == shared.external_bill_key
    assert _sync_items(repository, batch_id)[0]["existing_bill_id"] == shared.id

    isolated = _completed_batch(repository, batch_existing_bills=[shared])
    scoped_id = isolated["id"]
    repository.upsert_source_documents(scoped_id, [_document("doc-isolated")])
    repository.upsert_billing_record(
        scoped_id,
        _record(
            "snapshot-update",
            source_doc_id="doc-isolated",
            amount=_SMALL_AMOUNT,
            operation=OperationType.UPDATE,
            matched_existing_bill_id=shared.id,
        ),
    )
    assert repository.sync_baseline(scoped_id) == {"created": 1, "updated": 0, "skipped": 0}
    assert repository.get_existing_bill(shared.id) == updated
    (receipt,) = _sync_items(repository, scoped_id)
    snapshot_record = repository.get_billing_record(scoped_id, "snapshot-update")
    repository.upsert_billing_record(
        scoped_id, snapshot_record.model_copy(update={"amount": Decimal("25")})
    )
    assert repository.sync_baseline(scoped_id) == {"created": 0, "updated": 1, "skipped": 0}
    assert repository.get_existing_bill(receipt["existing_bill_id"]).amount == Decimal("25")
    assert repository.get_existing_bill(shared.id) == updated

    assert repository.clear_existing_bills(_ENTITY) == 2
    assert _sync_items(repository, batch_id) == []
    assert repository.get_billing_record(batch_id, "shared-update").matched_existing_bill_id is None
    assert repository.get_batch(scoped_id)["batch_existing_bills"] == [shared]
    assert (
        repository.get_billing_record(scoped_id, "snapshot-update").matched_existing_bill_id
        == shared.id
    )


def test_baseline_sync_rolls_back_bills_and_receipts_on_late_failure(
    repository: BillingRepository,
) -> None:
    batch_id = _completed_batch(repository)["id"]
    repository.upsert_source_documents(batch_id, [_document("doc")])
    repository.upsert_billing_record(batch_id, _record("prior"))
    assert repository.sync_baseline(batch_id)["created"] == 1
    original_bills = repository.list_existing_bills()
    original_items = _sync_items(repository, batch_id)

    repository.upsert_billing_records(
        batch_id,
        [
            _record("prior", amount=Decimal("12.0000")),
            _record("created-before-error", source_line_index=1),
            _record("blocked", source_line_index=2),
        ],
    )
    with repository.db.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_last_sync_item BEFORE INSERT ON baseline_sync_items
               WHEN NEW.billing_record_id = 'blocked'
               BEGIN
                   SELECT RAISE(ABORT, 'simulated receipt write failure');
               END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated receipt write failure"):
        repository.sync_baseline(batch_id)
    with repository.transaction() as transaction:
        with pytest.raises(sqlite3.IntegrityError, match="simulated receipt write failure"):
            transaction.sync_baseline(batch_id)
        assert transaction.list_existing_bills() == original_bills
    assert repository.list_existing_bills() == original_bills
    assert _sync_items(repository, batch_id) == original_items

    with repository.db.connect() as connection:
        connection.execute("DROP TRIGGER reject_last_sync_item")
    assert repository.sync_baseline(batch_id) == {"created": 2, "updated": 1, "skipped": 0}
    assert len(_sync_items(repository, batch_id)) == 4


def test_concurrent_sync_writers_commit_the_record_once(repository: BillingRepository) -> None:
    batch = _completed_batch(repository)
    batch_id = batch["id"]
    repository.upsert_source_documents(batch_id, [_document("doc")])
    repository.upsert_billing_record(batch_id, _record())
    barrier = Barrier(2)

    def sync() -> dict[str, int]:
        barrier.wait(timeout=5)
        return repository.sync_batch_to_baseline(batch_id, expected_version=batch["version"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sync) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results, key=lambda result: result["created"]) == [
        {"created": 0, "updated": 0, "skipped": 1},
        {"created": 1, "updated": 0, "skipped": 0},
    ]
    assert len(repository.list_existing_bills()) == len(_sync_items(repository, batch_id)) == 1


def test_review_audit_survives_record_and_dictionary_edits(repository: BillingRepository) -> None:
    batch_id = _completed_batch(repository)["id"]
    repository.upsert_source_documents(batch_id, [_document("doc")])
    prior = repository.upsert_billing_record(batch_id, _record())
    diff = FieldDiff(field="amount", old_value=_LARGE_AMOUNT, new_value=_SMALL_AMOUNT, changed=True)
    log = repository.insert_review_audit_log(
        batch_id,
        prior.id,
        action="PATCH",
        operator="财务人员",
        reason="核对后修改金额",
        before={"amount": _LARGE_AMOUNT},
        after={"amount": _SMALL_AMOUNT},
        diff=[diff],
    )
    edited = repository.upsert_billing_record(
        batch_id,
        prior.model_copy(update={"amount": _SMALL_AMOUNT}),
        expected_updated_at=prior.updated_at,
    )
    assert edited.updated_at != prior.updated_at
    entry = repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="H/D Fee", standard_name="客户自定义")
    )
    assert repository.delete_charge_dictionary_entry(entry["id"])

    reopened = BillingRepository(DatabaseManager(repository.db.path))
    assert reopened.get_billing_record(batch_id, prior.id) == edited
    assert reopened.list_review_audit_logs(batch_id) == [log]
    assert reopened.list_review_audit_logs(batch_id, prior.id) == [log]
    assert reopened.list_review_audit_logs(batch_id, "other-record") == []
    assert reopened.list_review_audit_logs("other-batch", prior.id) == []
    assert reopened.list_review_audit_logs(batch_id)[0]["before"]["amount"].as_tuple() == (
        _LARGE_AMOUNT.as_tuple()
    )


def test_shared_bill_import_and_clear_do_not_touch_batch_snapshot(
    repository: BillingRepository,
) -> None:
    isolated = _bill("isolated", amount=_LARGE_AMOUNT)
    batch_id = _completed_batch(repository, batch_existing_bills=[isolated])["id"]
    bills = [_bill("shared"), _bill("stale"), _bill("other", accounting_entity=_OTHER_ENTITY)]
    assert repository.import_existing_bills(bills) == {"imported_count": 3, "total_count": 3}
    assert repository.get_existing_bill("isolated") is None
    shared = repository.get_existing_bill("shared")
    edited = repository.update_existing_bill(shared.model_copy(update={"amount": _LARGE_AMOUNT}))
    assert edited.amount.as_tuple() == _LARGE_AMOUNT.as_tuple()
    assert edited.version == 2

    counts = repository.import_existing_bills(
        [_bill("shared", amount=_SMALL_AMOUNT)], replace_existing=True, accounting_entity=_ENTITY
    )
    assert counts == {"imported_count": 1, "total_count": 2}
    assert repository.get_existing_bill("stale") is None
    assert [bill.id for bill in repository.list_existing_bills(_OTHER_ENTITY)] == ["other"]
    assert repository.get_existing_bill("shared").version == 3
    assert repository.get_existing_bill("shared").amount.as_tuple() == _SMALL_AMOUNT.as_tuple()
    assert not repository.delete_existing_bill("other", accounting_entity=_ENTITY)
    assert repository.delete_existing_bill("other", accounting_entity=_OTHER_ENTITY)
    assert repository.clear_existing_bills() == 1
    assert repository.get_batch(batch_id)["batch_existing_bills"] == [isolated]


def test_shared_bill_scopes_treat_aliases_as_equivalent(repository: BillingRepository) -> None:
    opposite_branch = _bill("other-branch", accounting_entity=f"{_ENTITY} (SFO)")
    unconfigured = _bill("unconfigured", accounting_entity="Example Co. (HQ)")
    repository.import_existing_bills(
        [
            _bill("canonical"),
            _bill("alias", accounting_entity=_ENTITY_ALIAS),
            _bill("obsolete"),
            opposite_branch,
            unconfigured,
        ]
    )
    expected = {"canonical", "alias", "obsolete"}
    for name in (_ENTITY, _ENTITY_ALIAS, "NEPTUNE"):
        assert {bill.id for bill in repository.list_existing_bills(name)} == expected
    assert repository.get_existing_bill("alias", accounting_entity=_ENTITY) is not None
    assert repository.get_existing_bill("canonical", accounting_entity=_ENTITY_ALIAS) is not None
    assert repository.get_existing_bill("other-branch", accounting_entity=_ENTITY) is None
    assert repository.get_existing_bill("unconfigured", accounting_entity="example co") is None

    assert repository.delete_existing_bill("canonical", accounting_entity=_ENTITY_ALIAS)
    assert not repository.delete_existing_bill("other-branch", accounting_entity=_ENTITY_ALIAS)
    assert repository.import_existing_bills(
        [_bill("alias", amount=_SMALL_AMOUNT)],
        replace_existing=True,
        accounting_entity=_ENTITY_ALIAS,
    ) == {"imported_count": 1, "total_count": 3}
    (updated,) = repository.list_existing_bills(_ENTITY_ALIAS)
    assert (updated.id, updated.version, updated.amount) == ("alias", 2, _SMALL_AMOUNT)
    with pytest.raises(ValueError, match="requested accounting entity"):
        repository.import_existing_bills(
            [opposite_branch], replace_existing=True, accounting_entity=_ENTITY
        )
    assert repository.clear_existing_bills(_ENTITY_ALIAS) == 1
    remaining = repository.list_existing_bills()
    assert {bill.id for bill in remaining} == {"other-branch", "unconfigured"}


@pytest.mark.parametrize(
    ("entity", "other_entity"),
    [
        ("Harbor Orchid Logistics", "Harbor Orchid Logistics (LAX)"),
        ("Harbor Orchid Logistics (LAX)", "Harbor Orchid Logistics"),
    ],
)
def test_unconfigured_entities_are_isolated_in_shared_bill_operations(
    repository: BillingRepository, entity: str, other_entity: str
) -> None:
    bill = repository.create_existing_bill(_bill("scoped", accounting_entity=entity))
    other = repository.create_existing_bill(_bill("other", accounting_entity=other_entity))

    assert repository.list_existing_bills(f" {entity} ") == [bill]
    assert repository.get_existing_bill(other.id, accounting_entity=entity) is None
    assert not repository.delete_existing_bill(other.id, accounting_entity=entity)
    with pytest.raises(ValueError, match="requested accounting entity"):
        repository.import_existing_bills([other], replace_existing=True, accounting_entity=entity)
    assert repository.import_existing_bills(
        [bill], replace_existing=True, accounting_entity=entity
    ) == {"imported_count": 1, "total_count": 2}
    assert repository.clear_existing_bills(entity) == 1
    assert repository.list_existing_bills() == [other]
    assert repository.clear_existing_bills(other_entity) == 1


def test_sync_accepts_alias_scope_for_legacy_matches_and_prior_receipts(
    repository: BillingRepository,
) -> None:
    repository.create_existing_bill(_bill("ledger", accounting_entity=_ENTITY_ALIAS))
    batch_id = _completed_batch(repository)["id"]
    repository.upsert_source_documents(batch_id, [_document("doc")])
    (record,) = repository.upsert_billing_records(
        batch_id,
        [
            _record(
                operation=OperationType.UPDATE,
                accounting_entity="NEPTUNE",
                matched_existing_bill_id="ledger",
            )
        ],
    )
    with repository.db.connect() as connection:
        match_id, payload = connection.execute(
            "SELECT matched_existing_bill_id, record_state_json FROM billing_records WHERE id = ?",
            (record.id,),
        ).fetchone()
        assert match_id == "ledger"
        # Simulate the preceding version's representation of a shared alias match.
        state = loads_decimal_json(payload) | {"snapshot_matched_bill_id": match_id}
        connection.execute(
            "UPDATE billing_records SET matched_existing_bill_id = NULL, record_state_json = ? "
            "WHERE id = ?",
            (dumps_decimal_json(state), record.id),
        )

    assert repository.sync_baseline(batch_id) == {"created": 0, "updated": 1, "skipped": 0}
    updated = repository.get_existing_bill("ledger")
    assert (updated.version, updated.amount) == (2, _LARGE_AMOUNT)
    aliased = updated.model_copy(update={"accounting_entity": _ENTITY_ALIAS})
    repository.import_existing_bills([aliased])
    repository.upsert_billing_record(batch_id, _record(amount=_SMALL_AMOUNT))
    assert repository.sync_baseline(batch_id) == {"created": 0, "updated": 1, "skipped": 0}
    (bill,) = repository.list_existing_bills()
    assert (bill.id, bill.version, bill.amount) == ("ledger", 4, _SMALL_AMOUNT)
    assert {item["existing_bill_id"] for item in _sync_items(repository, batch_id)} == {"ledger"}


@pytest.mark.parametrize("target", ["matched-bill", "force-create"])
def test_baseline_sync_target_conflict_rolls_back_earlier_records(
    repository: BillingRepository, target: str
) -> None:
    batch_id = _completed_batch(repository)["id"]
    repository.upsert_source_documents(batch_id, [_document("doc")])
    repository.import_existing_bills([_bill("first-ledger"), _bill("second-ledger")])
    record = repository.upsert_billing_record(
        batch_id,
        _record(
            "reviewed",
            source_line_index=1,
            operation=OperationType.UPDATE,
            matched_existing_bill_id="first-ledger",
        ),
    )
    assert repository.sync_baseline(batch_id) == {"created": 0, "updated": 1, "skipped": 0}
    old_bills = repository.list_existing_bills()
    old_receipts = _sync_items(repository, batch_id)
    force_create = target == "force-create"
    repository.upsert_billing_records(
        batch_id,
        [
            _record("eligible-before-conflict"),
            record.model_copy(
                update={
                    "operation": OperationType.CREATE if force_create else OperationType.UPDATE,
                    "amount": Decimal("120"),
                    "matched_existing_bill_id": None if force_create else "second-ledger",
                    "human_overrides": {"force_create_new": True} if force_create else {},
                    "review_status": ReviewStatus.APPROVED,
                }
            ),
        ],
    )

    with pytest.raises(OptimisticLockError) as error:
        repository.sync_baseline(batch_id)

    assert error.value.code == "BASELINE_SYNC_TARGET_CONFLICT"
    assert error.value.resource_id == record.id
    assert error.value.expected == "first-ledger"
    assert error.value.actual == ("force_create_new" if force_create else "second-ledger")
    assert repository.list_existing_bills() == old_bills
    assert _sync_items(repository, batch_id) == old_receipts


def test_eval_runs_restore_null_metrics_and_full_reports(repository: BillingRepository) -> None:
    batch = _completed_batch(repository)
    unverified = repository.insert_eval_run(
        run_id="unverified",
        run_mode="LIVE_E2E",
        status="UNVERIFIED",
        error_message="OCR gateway unreachable",
        model_name="deepseek-flash",
        k_trials=3,
        actual_trials=0,
        total_cases=10,
        report={"cases": [{"id": "sample", "reason": "upstream timeout", "amount": _LARGE_AMOUNT}]},
        created_at="2026-01-02T00:00:00Z",
    )
    completed = repository.insert_eval_run(
        run_id="completed",
        run_mode="OFFLINE_DETERMINISTIC",
        status="COMPLETED",
        model_name="offline",
        k_trials=2,
        actual_trials=2,
        total_cases=10,
        schema_valid_rate=1.0,
        field_exact_match=0.75,
        ar_ap_accuracy=0.5,
        operation_accuracy=0.25,
        conflict_recall=0.0,
        pass_at_k=0.5,
        pass_pow_k=0.25,
        report={"sample": {"values": [_SMALL_AMOUNT, None, True]}},
        created_at="2026-01-01T00:00:00Z",
    )
    reopened = BillingRepository(DatabaseManager(repository.db.path))
    assert reopened.get_eval_run("unverified") == unverified
    assert reopened.get_eval_run("completed") == completed
    assert reopened.get_eval_run("missing") is None
    assert reopened.list_eval_runs() == [unverified, completed]
    assert reopened.list_batches() == [batch]
    assert all(unverified[name] is None for name in _EVAL_METRICS)
    assert [completed[name] for name in _EVAL_METRICS] == [1.0, 0.75, 0.5, 0.25, 0.0, 0.5, 0.25]
    assert unverified["report"]["cases"][0]["amount"].as_tuple() == _LARGE_AMOUNT.as_tuple()
    assert completed["report"]["sample"]["values"][0].as_tuple() == _SMALL_AMOUNT.as_tuple()
