"""Persist billing state and coordinate SQLite transactions for the application services."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ..domain.models import (
    AccountingEntityProfile,
    BatchStage,
    BatchStatus,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    DocumentStatus,
    ExistingBillRecord,
    ExtractionChannel,
    FailedDocumentAlert,
    FieldDiff,
    OcrMode,
    ParsedDocument,
    ProcessedBillingRecord,
    RawExtractionEnvelope,
    decimal_to_db_text,
    dumps_decimal_json,
    loads_decimal_json,
    parse_decimal_strict,
)
from ..domain.serializer import _json_values
from .db import DatabaseManager, utc_timestamp


class OptimisticLockError(RuntimeError):
    """The version read by the caller no longer describes the stored resource."""

    def __init__(self, code: str, resource_id: str, expected: int | str, actual: int | str) -> None:
        self.code = code
        self.resource_id = resource_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"{code} for {resource_id}: expected {expected!r}, found {actual!r}")


class ResourceNotFoundError(LookupError):
    """A required resource is absent within the requested scope."""


class _Unchanged(Enum):
    VALUE = 0


_UNCHANGED = _Unchanged.VALUE
_BATCH_UPDATE_FIELDS = {
    "accounting_entity",
    "status",
    "stage",
    "stage_message",
    "ocr_mode",
    "total_files",
    "succeeded_files",
    "failed_files",
    "batch_existing_bills",
    "failed_document_alerts",
}
_BATCH_COUNTS = ("create", "update", "no_change", "pending_review", "rejected")
_BATCH_SUMMARY_SELECT = """
    SELECT b.*,
           COUNT(*) FILTER (WHERE r.operation = 'CREATE' AND r.review_status != 'REJECTED')
               AS create_count,
           COUNT(*) FILTER (WHERE r.operation = 'UPDATE' AND r.review_status != 'REJECTED')
               AS update_count,
           COUNT(*) FILTER (WHERE r.operation = 'NO_CHANGE' AND r.review_status != 'REJECTED')
               AS no_change_count,
           COUNT(*) FILTER (WHERE r.operation = 'PENDING_REVIEW' AND r.review_status != 'REJECTED')
               AS pending_review_count,
           COUNT(*) FILTER (WHERE r.review_status = 'REJECTED') AS rejected_count
    FROM batches AS b LEFT JOIN billing_records AS r ON r.batch_id = b.id
"""
_DOCUMENT_FIELDS = (
    "parent_doc_id",
    "original_filename",
    "sanitized_filename",
    "media_type",
    "source_kind",
    "content_sha256",
    "public_url",
    "extraction_channel",
    "status",
    "error_message",
    "raw_text",
)
_DOCUMENT_AMOUNT_FIELDS = ("subtotal_amount", "tax_amount", "total_amount")
_RECORD_FIELDS = (
    "id",
    "batch_id",
    "source_doc_id",
    "source_line_index",
    "operation",
    "review_status",
    "match_level",
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "charge_item",
    "raw_charge_name",
    "currency",
    "payment_term",
    "accounting_entity",
    "posting_type",
    "reject_reason",
    "confidence",
    "retry_count",
    "created_at",
    "updated_at",
)
_RECORD_JSON_FIELDS = (
    "shipment_refs",
    "source_evidence",
    "field_diffs",
    "validation_alerts",
    "ai_reasoning",
    "human_overrides",
)
_BILL_AMOUNT_FIELDS = ("quantity", "unit_price", "amount")
_BILL_FIELDS_FROM_RECORD = (
    "accounting_entity",
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "charge_item",
    "raw_charge_name",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "payment_term",
    "posting_type",
)


class BillingRepository:
    """Use domain models for billing snapshots and dictionaries for SQL resource metadata."""

    def __init__(
        self, db: DatabaseManager, *, _connection: sqlite3.Connection | None = None
    ) -> None:
        self.db = db
        self._connection = _connection

    @contextmanager
    def transaction(self) -> Iterator[BillingRepository]:
        """Group a batch version check and subsequent writes in the same transaction."""
        with self._transaction(write=True) as connection:
            yield BillingRepository(self.db, _connection=connection)

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        if connection is None:
            with self.db.transaction(write=write) as connection:
                yield connection
        elif not write:
            yield connection
        else:
            connection.execute("SAVEPOINT repository_operation")
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK TO repository_operation")
                    connection.execute("RELEASE repository_operation")
                raise
            else:
                connection.execute("RELEASE repository_operation")

    # Batches

    def create_batch(
        self,
        accounting_entity: str,
        *,
        batch_id: str | None = None,
        short_code: str | None = None,
        source_channel: Literal["WEB", "CLI"] = "WEB",
        status: BatchStatus = BatchStatus.PROCESSING,
        stage: BatchStage = BatchStage.QUEUED,
        stage_message: str | None = None,
        ocr_mode: OcrMode = OcrMode.REMOTE_ONLY,
        total_files: int = 0,
        succeeded_files: int = 0,
        failed_files: int = 0,
        batch_existing_bills: Sequence[ExistingBillRecord] | None = None,
        failed_document_alerts: Sequence[FailedDocumentAlert] = (),
    ) -> dict[str, Any]:
        batch_id = uuid4().hex if batch_id is None else batch_id
        timestamp = utc_timestamp()
        values = {
            "id": batch_id,
            "short_code": f"B-{uuid4().hex[:12].upper()}" if short_code is None else short_code,
            "accounting_entity": accounting_entity,
            "source_channel": source_channel,
            "status": status,
            "stage": stage,
            "stage_message": stage_message,
            "ocr_mode": ocr_mode,
            "total_files": total_files,
            "succeeded_files": succeeded_files,
            "failed_files": failed_files,
            "batch_existing_bills_json": _dump_batch_bills(batch_existing_bills),
            "failed_documents_json": _dump_alerts(failed_document_alerts),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self._transaction(write=True) as connection:
            _insert(connection, "batches", values)
            return _batch_summary(_require_batch(connection, batch_id))

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = _select_batch(connection, batch_id)
            if row is None:
                return None
            records = _list_billing_records(connection, batch_id)
            alerts = [
                FailedDocumentAlert.model_validate(alert)
                for alert in loads_decimal_json(row["failed_documents_json"])
            ]
            affected: dict[str, list[str]] = {}
            for record in records:
                affected.setdefault(record.source_doc_id, []).append(record.id)
            for alert in alerts:
                record_ids = affected.setdefault(alert.doc_id, [])
                record_ids.extend(
                    record_id
                    for record_id in alert.affected_record_ids
                    if record_id not in record_ids
                )
            documents = [
                _document_detail(doc, affected.get(doc["id"], []))
                for doc in connection.execute(
                    "SELECT * FROM source_documents WHERE batch_id = ? ORDER BY created_at, rowid",
                    (batch_id,),
                )
            ]
            batch_bills = row["batch_existing_bills_json"]
            return {
                **_batch_summary(row),
                "documents": documents,
                "records": records,
                "failed_document_alerts": alerts,
                "batch_existing_bills": (
                    [
                        ExistingBillRecord.model_validate(bill)
                        for bill in loads_decimal_json(batch_bills)
                    ]
                    if batch_bills is not None
                    else None
                ),
            }

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        with self._transaction() as connection:
            rows = connection.execute(
                _BATCH_SUMMARY_SELECT + " GROUP BY b.id ORDER BY b.created_at DESC, b.id LIMIT ?",
                (limit,),
            )
            return [_batch_summary(row) for row in rows]

    def update_batch_stage(
        self,
        batch_id: str,
        stage: BatchStage,
        *,
        stage_message: str | None = None,
        status: BatchStatus | None = None,
    ) -> dict[str, Any]:
        if status is None:
            status = {
                BatchStage.COMPLETED: BatchStatus.COMPLETED,
                BatchStage.FAILED: BatchStatus.PARTIAL_ERROR,
            }.get(stage, BatchStatus.PROCESSING)
        return self._update_batch(
            batch_id, None, {"stage": stage, "stage_message": stage_message, "status": status}
        )

    def update_batch_with_version_check(
        self, batch_id: str, expected_version: int, **changes: Any
    ) -> dict[str, Any]:
        return self._update_batch(batch_id, expected_version, changes)

    def _update_batch(
        self, batch_id: str, expected_version: int | None, changes: dict[str, Any]
    ) -> dict[str, Any]:
        unknown_fields = changes.keys() - _BATCH_UPDATE_FIELDS
        if unknown_fields:
            raise TypeError(f"Unsupported batch fields: {', '.join(sorted(unknown_fields))}")
        with self._transaction(write=True) as connection:
            row = _require_batch(connection, batch_id)
            _check_batch_version(row, expected_version)
            if not changes:
                return _batch_summary(row)
            if "batch_existing_bills" in changes:
                changes["batch_existing_bills_json"] = _dump_batch_bills(
                    changes.pop("batch_existing_bills")
                )
            if "failed_document_alerts" in changes:
                changes["failed_documents_json"] = _dump_alerts(
                    changes.pop("failed_document_alerts")
                )
            _update(
                connection,
                "batches",
                {**changes, "version": row["version"] + 1, "updated_at": utc_timestamp()},
                "id = ?",
                (batch_id,),
            )
            return _batch_summary(_require_batch(connection, batch_id))

    # Source documents

    def upsert_source_documents(
        self,
        batch_id: str,
        documents: Iterable[DocumentExtractionBundle | ParsedDocument],
        *,
        stored_paths: Mapping[str, Path | str | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Update source rows in place. An omitted extraction leaves the stored snapshot intact."""
        bundles: dict[str, DocumentExtractionBundle] = {}
        for item in documents:
            bundle = (
                DocumentExtractionBundle(document=item)
                if isinstance(item, ParsedDocument)
                else item
            )
            doc_id = bundle.document.doc_id
            if doc_id in bundles:
                raise ValueError(f"Duplicate source document: {doc_id}")
            bundles[doc_id] = bundle
        paths = stored_paths or {}

        with self._transaction(write=True) as connection:
            _require_batch(connection, batch_id)
            existing = {
                row["id"]: row
                for row in connection.execute(
                    """SELECT id, parent_doc_id, stored_path, raw_ocr_json
                       FROM source_documents WHERE batch_id = ?""",
                    (batch_id,),
                )
            }
            parents = {doc_id: row["parent_doc_id"] for doc_id, row in existing.items()}
            parents.update(
                (doc_id, bundle.document.parent_doc_id) for doc_id, bundle in bundles.items()
            )
            graph = TopologicalSorter(
                {doc_id: [] if parent is None else [parent] for doc_id, parent in parents.items()}
            )
            timestamp = utc_timestamp()
            for doc_id in graph.static_order():
                if doc_id not in parents:
                    raise ResourceNotFoundError(
                        f"Parent document {doc_id!r} not found in {batch_id}"
                    )
                if doc_id not in bundles:
                    continue

                bundle = bundles[doc_id]
                previous = existing.get(doc_id)
                stored_path = paths.get(doc_id, previous["stored_path"] if previous else None)
                retry_count = bundle.retry_count
                if previous and "retry_count" not in bundle.model_fields_set:
                    _, parsed = _source_ocr_fields(previous["raw_ocr_json"])
                    retry_count = int(parsed.get("retry_count", 0))
                values = {
                    **{field: getattr(bundle.document, field) for field in _DOCUMENT_FIELDS},
                    "id": doc_id,
                    "batch_id": batch_id,
                    "stored_path": str(stored_path) if stored_path is not None else None,
                    "raw_ocr_json": _source_ocr_json(bundle.document, retry_count),
                    "created_at": timestamp,
                }
                if "extraction" in bundle.model_fields_set:
                    values.update(_extraction_columns(bundle.extraction))
                _upsert_by_id(connection, "source_documents", values)
            return [_require_document(connection, batch_id, doc_id) for doc_id in bundles]

    def get_document(self, batch_id: str, doc_id: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            return _get_document(connection, batch_id, doc_id)

    def update_document_status_and_snapshot(
        self,
        batch_id: str,
        doc_id: str,
        *,
        status: DocumentStatus | None = None,
        extraction_snapshot: RawExtractionEnvelope | None | _Unchanged = _UNCHANGED,
        error_message: str | None | _Unchanged = _UNCHANGED,
        extraction_channel: ExtractionChannel | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if extraction_snapshot is not _UNCHANGED:
            changes.update(_extraction_columns(extraction_snapshot))
        if status is not None:
            changes["status"] = status
        if error_message is not _UNCHANGED:
            changes["error_message"] = error_message
        if extraction_channel is not None:
            changes["extraction_channel"] = extraction_channel
        with self._transaction(write=True) as connection:
            if changes:
                _update(
                    connection,
                    "source_documents",
                    changes,
                    "batch_id = ? AND id = ?",
                    (
                        batch_id,
                        doc_id,
                    ),
                )
            return _require_document(connection, batch_id, doc_id)

    # Billing records and their review history

    def upsert_billing_records(
        self, batch_id: str, records: Iterable[ProcessedBillingRecord]
    ) -> list[ProcessedBillingRecord]:
        with self._transaction(write=True) as connection:
            batch = _require_batch(connection, batch_id)
            return [_store_billing_record(connection, batch, record) for record in records]

    def upsert_billing_record(
        self,
        batch_id: str,
        record: ProcessedBillingRecord,
        *,
        expected_updated_at: str | None = None,
    ) -> ProcessedBillingRecord:
        with self._transaction(write=True) as connection:
            batch = _require_batch(connection, batch_id)
            return _store_billing_record(connection, batch, record, expected_updated_at)

    def get_billing_record(self, batch_id: str, record_id: str) -> ProcessedBillingRecord | None:
        with self._transaction() as connection:
            row = _select_record(connection, batch_id, record_id)
            return _billing_record(row) if row is not None else None

    def list_billing_records(self, batch_id: str) -> list[ProcessedBillingRecord]:
        with self._transaction() as connection:
            return _list_billing_records(connection, batch_id)

    def delete_billing_records(self, batch_id: str, record_ids: Iterable[str]) -> int:
        """Delete rows that a source replacement actually removed."""
        with self._transaction(write=True) as connection:
            cursor = connection.executemany(
                "DELETE FROM billing_records WHERE batch_id = ? AND id = ?",
                ((batch_id, record_id) for record_id in record_ids),
            )
            return cursor.rowcount

    def insert_review_audit_log(
        self,
        batch_id: str,
        record_id: str,
        *,
        action: str,
        operator: str,
        reason: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        diff: Sequence[FieldDiff] = (),
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        log = {
            "id": uuid4().hex,
            "batch_id": batch_id,
            "record_id": record_id,
            "action": action,
            "operator": operator,
            "reason": reason,
            "before": dict(before) if before is not None else None,
            "after": dict(after) if after is not None else None,
            "diff": [change.model_dump(mode="python") for change in diff],
            "timestamp": timestamp or utc_timestamp(),
        }
        with self._transaction(write=True) as connection:
            row = _select_record(connection, batch_id, record_id)
            if row is None:
                raise ResourceNotFoundError(f"Record {record_id!r} not found in batch {batch_id!r}")
            state = loads_decimal_json(row["record_state_json"])
            state.setdefault("review_audit_logs", []).append(log)
            connection.execute(
                "UPDATE billing_records SET record_state_json = ? WHERE batch_id = ? AND id = ?",
                (dumps_decimal_json(_json_values(state)), batch_id, record_id),
            )
        return log

    def list_review_audit_logs(
        self, batch_id: str, record_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT record_state_json FROM billing_records WHERE batch_id = ?"
        params = (batch_id,)
        if record_id is not None:
            sql += " AND id = ?"
            params += (record_id,)
        with self._transaction() as connection:
            logs = [
                log
                for row in connection.execute(sql, params)
                for log in loads_decimal_json(row["record_state_json"]).get("review_audit_logs", [])
            ]
        return sorted(logs, key=lambda log: log["timestamp"])

    # Master data

    def list_accounting_entities(self) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            return [
                _entity_detail(row)
                for row in connection.execute(
                    "SELECT * FROM accounting_entities ORDER BY is_default DESC, canonical_name"
                )
            ]

    def create_accounting_entity(self, profile: AccountingEntityProfile) -> dict[str, Any]:
        entity_id = uuid4().hex
        timestamp = utc_timestamp()
        with self._transaction(write=True) as connection:
            _insert(
                connection,
                "accounting_entities",
                {
                    "id": entity_id,
                    **_entity_columns(profile),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            return _entity_detail(
                connection.execute(
                    "SELECT * FROM accounting_entities WHERE id = ?", (entity_id,)
                ).fetchone()
            )

    def update_accounting_entity(
        self, entity_id: str, profile: AccountingEntityProfile
    ) -> dict[str, Any]:
        with self._transaction(write=True) as connection:
            updated = _update(
                connection,
                "accounting_entities",
                {**_entity_columns(profile), "updated_at": utc_timestamp()},
                "id = ?",
                (entity_id,),
            )
            if not updated:
                raise ResourceNotFoundError(f"Accounting entity {entity_id!r} not found")
            return _entity_detail(
                connection.execute(
                    "SELECT * FROM accounting_entities WHERE id = ?", (entity_id,)
                ).fetchone()
            )

    def list_charge_dictionary(self) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            return [
                _dictionary_detail(row)
                for row in connection.execute("SELECT * FROM charge_dictionary ORDER BY raw_alias")
            ]

    def upsert_charge_dictionary_entry(
        self, item: ChargeDictionaryItem, *, entry_id: str | None = None
    ) -> dict[str, Any]:
        with self._transaction(write=True) as connection:
            if entry_id is None:
                connection.execute(
                    """INSERT INTO charge_dictionary
                       (id, raw_alias, standard_name, category, is_builtin, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(raw_alias) DO UPDATE SET
                           standard_name = excluded.standard_name, category = excluded.category""",
                    (
                        uuid4().hex,
                        item.raw_alias,
                        item.standard_name,
                        item.category,
                        item.is_builtin,
                        utc_timestamp(),
                    ),
                )
            else:
                updated = _update(
                    connection,
                    "charge_dictionary",
                    {
                        "raw_alias": item.raw_alias,
                        "standard_name": item.standard_name,
                        "category": item.category,
                    },
                    "id = ?",
                    (entry_id,),
                )
                if not updated:
                    raise ResourceNotFoundError(f"Dictionary entry {entry_id!r} not found")
            row = connection.execute(
                "SELECT * FROM charge_dictionary WHERE raw_alias = ?", (item.raw_alias,)
            ).fetchone()
            return _dictionary_detail(row)

    def delete_charge_dictionary_entry(self, entry_id: str) -> bool:
        with self._transaction(write=True) as connection:
            return bool(
                connection.execute(
                    "DELETE FROM charge_dictionary WHERE id = ?", (entry_id,)
                ).rowcount
            )

    # Shared existing bills

    def list_existing_bills(self, accounting_entity: str | None = None) -> list[ExistingBillRecord]:
        where, params = _entity_filter(accounting_entity)
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM existing_bills {where} ORDER BY created_at, id", params
            )
            return [_existing_bill(row) for row in rows]

    def get_existing_bill(
        self, bill_id: str, *, accounting_entity: str | None = None
    ) -> ExistingBillRecord | None:
        with self._transaction() as connection:
            row = _select_existing_bill(connection, bill_id, accounting_entity)
            return _existing_bill(row) if row is not None else None

    def create_existing_bill(self, bill: ExistingBillRecord) -> ExistingBillRecord:
        with self._transaction(write=True) as connection:
            return _store_existing_bill(connection, bill)

    def update_existing_bill(self, bill: ExistingBillRecord) -> ExistingBillRecord:
        with self._transaction(write=True) as connection:
            previous = _select_existing_bill(connection, bill.id)
            if previous is None:
                raise ResourceNotFoundError(f"Existing bill {bill.id!r} not found")
            return _store_existing_bill(connection, bill, previous=previous)

    def delete_existing_bill(self, bill_id: str, *, accounting_entity: str | None = None) -> bool:
        with self._transaction(write=True) as connection:
            sql = "DELETE FROM existing_bills WHERE id = ?"
            params = (bill_id,)
            if accounting_entity is not None:
                sql += " AND accounting_entity = ?"
                params += (accounting_entity,)
            return bool(connection.execute(sql, params).rowcount)

    def clear_existing_bills(self, accounting_entity: str | None = None) -> int:
        where, params = _entity_filter(accounting_entity)
        with self._transaction(write=True) as connection:
            return connection.execute(f"DELETE FROM existing_bills {where}", params).rowcount

    def import_existing_bills(
        self,
        bills: Iterable[ExistingBillRecord],
        *,
        replace_existing: bool = False,
        accounting_entity: str | None = None,
    ) -> dict[str, int]:
        """Import shared bills atomically, optionally replacing bills in the specified scope."""
        bills = tuple(bills)
        new_ids = {bill.id for bill in bills}
        if len(new_ids) != len(bills):
            raise ValueError("Existing bill import contains duplicate IDs")
        if accounting_entity is not None and any(
            bill.accounting_entity != accounting_entity for bill in bills
        ):
            raise ValueError("Every imported bill must belong to the requested accounting entity")

        with self._transaction(write=True) as connection:
            for bill in bills:
                previous = _select_existing_bill(connection, bill.id)
                if previous is not None and previous["accounting_entity"] != bill.accounting_entity:
                    raise ValueError(f"Existing bill {bill.id!r} belongs to another entity")
                _store_existing_bill(connection, bill, previous=previous)
            if replace_existing:
                where, params = _entity_filter(accounting_entity)
                old_bills = connection.execute(
                    f"SELECT id FROM existing_bills {where}", params
                ).fetchall()
                connection.executemany(
                    "DELETE FROM existing_bills WHERE id = ?",
                    ((row["id"],) for row in old_bills if row["id"] not in new_ids),
                )
            total = connection.execute("SELECT COUNT(*) FROM existing_bills").fetchone()[0]
            return {"imported_count": len(bills), "total_count": total}

    def sync_batch_to_baseline(
        self, batch_id: str, expected_version: int | None = None
    ) -> dict[str, int]:
        """Apply approved versions to the shared baseline and record their receipts atomically."""
        counts = {"created": 0, "updated": 0, "skipped": 0}
        with self._transaction(write=True) as connection:
            batch = _require_batch(connection, batch_id)
            _check_batch_version(batch, expected_version)
            records = connection.execute(
                """SELECT * FROM billing_records
                   WHERE batch_id = ? AND operation IN ('CREATE', 'UPDATE')
                       AND review_status IN ('AUTO_READY', 'APPROVED')
                   ORDER BY source_doc_id, source_line_index, id""",
                (batch_id,),
            ).fetchall()
            for row in records:
                sync_key = (batch_id, row["id"], row["updated_at"])
                if connection.execute(
                    """SELECT 1 FROM baseline_sync_items
                       WHERE batch_id = ? AND billing_record_id = ? AND record_updated_at = ?""",
                    sync_key,
                ).fetchone():
                    counts["skipped"] += 1
                    continue

                if row["accounting_entity"] != batch["accounting_entity"]:
                    raise ValueError(f"Record {row['id']!r} belongs to another accounting entity")
                previous = _baseline_target(connection, row)
                bill = _store_existing_bill(
                    connection, _bill_from_record(_billing_record(row), previous), previous=previous
                )
                connection.execute(
                    """INSERT INTO baseline_sync_items
                       (id, batch_id, billing_record_id, record_updated_at,
                        accounting_entity, existing_bill_id, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (uuid4().hex, *sync_key, bill.accounting_entity, bill.id, utc_timestamp()),
                )
                counts["updated" if previous is not None else "created"] += 1
        return counts

    sync_baseline = sync_batch_to_baseline

    # Harness evaluation runs

    def insert_eval_run(
        self,
        *,
        run_mode: Literal["OFFLINE_DETERMINISTIC", "LIVE_LLM", "LIVE_E2E"],
        status: Literal["COMPLETED", "FAILED", "UNVERIFIED"],
        model_name: str,
        total_cases: int,
        report: Mapping[str, Any],
        run_id: str | None = None,
        error_message: str | None = None,
        k_trials: int = 1,
        actual_trials: int = 0,
        schema_valid_rate: float | None = None,
        field_exact_match: float | None = None,
        ar_ap_accuracy: float | None = None,
        operation_accuracy: float | None = None,
        conflict_recall: float | None = None,
        pass_at_k: float | None = None,
        pass_pow_k: float | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        run_id = uuid4().hex if run_id is None else run_id
        values = {
            "id": run_id,
            "run_mode": run_mode,
            "status": status,
            "model_name": model_name,
            "total_cases": total_cases,
            "error_message": error_message,
            "k_trials": k_trials,
            "actual_trials": actual_trials,
            "schema_valid_rate": schema_valid_rate,
            "field_exact_match": field_exact_match,
            "ar_ap_accuracy": ar_ap_accuracy,
            "operation_accuracy": operation_accuracy,
            "conflict_recall": conflict_recall,
            "pass_at_k": pass_at_k,
            "pass_pow_k": pass_pow_k,
            "report_json": dumps_decimal_json(dict(report)),
            "created_at": created_at or utc_timestamp(),
        }
        with self._transaction(write=True) as connection:
            _insert(connection, "eval_runs", values)
            row = connection.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            return _eval_detail(row)

    def get_eval_run(self, run_id: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            return _eval_detail(row) if row is not None else None

    def list_eval_runs(self) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            return [
                _eval_detail(row)
                for row in connection.execute(
                    "SELECT * FROM eval_runs ORDER BY created_at DESC, id"
                )
            ]


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


def _update(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
    where: str,
    params: tuple[Any, ...],
) -> int:
    assignments = ", ".join(f"{column} = ?" for column in values)
    return connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {where}", (*values.values(), *params)
    ).rowcount


def _upsert_by_id(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in values
        if column not in {"id", "batch_id", "created_at"}
    )
    scope = f"WHERE {table}.batch_id = excluded.batch_id"
    cursor = connection.execute(
        f"""INSERT INTO {table} ({columns}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates} {scope}""",
        tuple(values.values()),
    )
    if not cursor.rowcount:
        raise ValueError(f"ID {values['id']!r} already belongs to another batch")


def _select_batch(connection: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return connection.execute(
        _BATCH_SUMMARY_SELECT + " WHERE b.id = ? GROUP BY b.id", (batch_id,)
    ).fetchone()


def _require_batch(connection: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = _select_batch(connection, batch_id)
    if row is None:
        raise ResourceNotFoundError(f"Batch {batch_id!r} not found")
    return row


def _batch_summary(row: sqlite3.Row) -> dict[str, Any]:
    summary = dict(row)
    summary.pop("batch_existing_bills_json")
    summary.pop("failed_documents_json")
    summary["counts"] = {name: summary.pop(f"{name}_count") for name in _BATCH_COUNTS}
    return summary


def _check_batch_version(batch: sqlite3.Row, expected: int | None) -> None:
    if expected is not None and batch["version"] != expected:
        raise OptimisticLockError("BATCH_CONFLICT", batch["id"], expected, batch["version"])


def _dump_batch_bills(bills: Sequence[ExistingBillRecord] | None) -> str | None:
    if bills is None:
        return None
    return dumps_decimal_json(_json_values([bill.model_dump(mode="python") for bill in bills]))


def _dump_alerts(alerts: Sequence[FailedDocumentAlert]) -> str:
    return dumps_decimal_json([alert.model_dump(mode="python") for alert in alerts])


def _source_ocr_json(document: ParsedDocument, retry_count: int) -> str | None:
    parsed = document.model_dump(mode="python", include={"ocr_payload", "tables", "metadata"})
    ocr = parsed.pop("ocr_payload") or {}
    # Keep the parsed document context needed to retry it after a restart.
    if parsed["tables"] or parsed["metadata"] or retry_count:
        ocr["_parsed_document"] = {**parsed, "retry_count": retry_count}
    return dumps_decimal_json(_json_values(ocr)) if ocr else None


def _source_ocr_fields(ocr_json: str | None) -> tuple[Any, dict[str, Any]]:
    ocr = loads_decimal_json(ocr_json) if ocr_json is not None else None
    parsed = ocr.pop("_parsed_document", {}) if isinstance(ocr, dict) else {}
    return ocr, parsed


def _extraction_columns(extraction: RawExtractionEnvelope | None) -> dict[str, Any]:
    return {
        "extraction_snapshot_json": (
            dumps_decimal_json(_json_values(extraction.model_dump(mode="python")))
            if extraction is not None
            else None
        ),
        "excluded_clauses_json": dumps_decimal_json(
            [clause.model_dump(mode="python") for clause in extraction.extraction.excluded_clauses]
            if extraction is not None
            else []
        ),
        "shipment_refs_json": dumps_decimal_json(
            extraction.extraction.shipment_refs.model_dump(mode="python")
            if extraction is not None
            else {}
        ),
        "total_currency": extraction.total_currency if extraction is not None else None,
        **{
            name: decimal_to_db_text(getattr(extraction, name)) if extraction is not None else None
            for name in _DOCUMENT_AMOUNT_FIELDS
        },
    }


def _document_detail(row: sqlite3.Row, record_ids: list[str]) -> dict[str, Any]:
    document = dict(row)
    document["raw_text"] = document["raw_text"] or ""
    snapshot = document.pop("extraction_snapshot_json")
    document["extraction_snapshot"] = (
        RawExtractionEnvelope.model_validate(loads_decimal_json(snapshot))
        if snapshot is not None
        else None
    )
    document["affected_record_ids"] = record_ids
    document["excluded_clauses"] = loads_decimal_json(document.pop("excluded_clauses_json"))
    document["shipment_refs"] = loads_decimal_json(document.pop("shipment_refs_json"))
    for name in _DOCUMENT_AMOUNT_FIELDS:
        document[name] = (
            parse_decimal_strict(document[name]) if document[name] is not None else None
        )

    ocr, parsed = _source_ocr_fields(document.pop("raw_ocr_json"))
    document["ocr_payload"] = ocr or None
    document["tables"] = parsed.get("tables", [])
    document["metadata"] = parsed.get("metadata", {})
    document["retry_count"] = int(parsed.get("retry_count", 0))
    return document


def _get_document(
    connection: sqlite3.Connection, batch_id: str, doc_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM source_documents WHERE batch_id = ? AND id = ?", (batch_id, doc_id)
    ).fetchone()
    if row is None:
        return None
    record_ids = [
        record["id"]
        for record in connection.execute(
            """SELECT id FROM billing_records WHERE batch_id = ? AND source_doc_id = ?
               ORDER BY source_line_index, id""",
            (batch_id, doc_id),
        )
    ]
    return _document_detail(row, record_ids)


def _require_document(connection: sqlite3.Connection, batch_id: str, doc_id: str) -> dict[str, Any]:
    document = _get_document(connection, batch_id, doc_id)
    if document is None:
        raise ResourceNotFoundError(f"Document {doc_id!r} not found in batch {batch_id!r}")
    return document


def _select_record(
    connection: sqlite3.Connection, batch_id: str, record_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM billing_records WHERE batch_id = ? AND id = ?", (batch_id, record_id)
    ).fetchone()


def _list_billing_records(
    connection: sqlite3.Connection, batch_id: str
) -> list[ProcessedBillingRecord]:
    rows = connection.execute(
        """SELECT * FROM billing_records WHERE batch_id = ?
           ORDER BY source_doc_id, source_line_index, id""",
        (batch_id,),
    )
    return [_billing_record(row) for row in rows]


def _billing_record(row: sqlite3.Row) -> ProcessedBillingRecord:
    state = loads_decimal_json(row["record_state_json"])
    state.update({name: row[name] for name in _RECORD_FIELDS})
    for name in _BILL_AMOUNT_FIELDS:
        state.setdefault(name, row[name])
    for name in _RECORD_JSON_FIELDS:
        if name not in state:
            state[name] = loads_decimal_json(row[f"{name}_json"])
    # The shared FK can be nulled by clearing the baseline after the snapshot was written.
    state["matched_existing_bill_id"] = state.get(
        "snapshot_matched_bill_id", row["matched_existing_bill_id"]
    )
    return ProcessedBillingRecord.model_validate(state)


def _store_billing_record(
    connection: sqlite3.Connection,
    batch: sqlite3.Row,
    record: ProcessedBillingRecord,
    expected_updated_at: str | None = None,
) -> ProcessedBillingRecord:
    batch_id = batch["id"]
    if record.batch_id is not None and record.batch_id != batch_id:
        raise ValueError(f"Record {record.id!r} belongs to a different batch")
    previous = _select_record(connection, batch_id, record.id)
    if expected_updated_at is not None:
        if previous is None:
            raise ResourceNotFoundError(f"Record {record.id!r} not found in batch {batch_id!r}")
        if previous["updated_at"] != expected_updated_at:
            raise OptimisticLockError(
                "RECORD_CONFLICT", record.id, expected_updated_at, previous["updated_at"]
            )
    if not connection.execute(
        "SELECT 1 FROM source_documents WHERE batch_id = ? AND id = ?",
        (batch_id, record.source_doc_id),
    ).fetchone():
        raise ResourceNotFoundError(
            f"Document {record.source_doc_id!r} not found in batch {batch_id!r}"
        )

    sql_match_id = record.matched_existing_bill_id
    if sql_match_id is not None and (
        batch["batch_existing_bills_json"] is not None
        or _select_existing_bill(connection, sql_match_id, record.accounting_entity) is None
    ):
        sql_match_id = None

    state = record.model_dump(mode="python")
    state["batch_id"] = batch_id
    if previous is None:
        state["created_at"] = record.created_at or utc_timestamp()
        state["updated_at"] = record.updated_at or state["created_at"]
    else:
        state["created_at"] = previous["created_at"]
        state["updated_at"] = previous["updated_at"]
        previous_state = _billing_record(previous).model_dump(mode="python")
        changed = previous["matched_existing_bill_id"] != sql_match_id or dumps_decimal_json(
            _json_values(state)
        ) != dumps_decimal_json(_json_values(previous_state))
        state["updated_at"] = _record_timestamp(
            previous["updated_at"], record.updated_at, changed=changed
        )
        stored_state = loads_decimal_json(previous["record_state_json"])
        if "review_audit_logs" in stored_state:
            state["review_audit_logs"] = stored_state["review_audit_logs"]

    if sql_match_id is None and record.matched_existing_bill_id is not None:
        state["snapshot_matched_bill_id"] = record.matched_existing_bill_id
    values = {
        **{name: state[name] for name in _RECORD_FIELDS},
        **{name: decimal_to_db_text(getattr(record, name)) for name in _BILL_AMOUNT_FIELDS},
        **{
            f"{name}_json": dumps_decimal_json(_json_values(state[name]))
            for name in _RECORD_JSON_FIELDS
        },
        "matched_existing_bill_id": sql_match_id,
        "candidate_bill_ids_json": dumps_decimal_json([bill.id for bill in record.candidate_bills]),
        "record_state_json": dumps_decimal_json(_json_values(state)),
    }
    _upsert_by_id(connection, "billing_records", values)
    return record.model_copy(
        update={
            "batch_id": batch_id,
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
        }
    )


def _as_datetime(timestamp: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _record_timestamp(current: str, requested: str | None, *, changed: bool) -> str:
    previous_time = _as_datetime(current)
    if requested is not None:
        requested_time = _as_datetime(requested)
        if requested_time is not None and (previous_time is None or requested_time > previous_time):
            return requested
    if not changed:
        return current
    now = datetime.now(UTC)
    if previous_time is not None and now <= previous_time:
        now = previous_time + timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _entity_columns(profile: AccountingEntityProfile) -> dict[str, Any]:
    return {
        "canonical_name": profile.canonical_name,
        "aliases_json": dumps_decimal_json(profile.aliases),
        "is_default": profile.is_default,
    }


def _entity_detail(row: sqlite3.Row) -> dict[str, Any]:
    entity = dict(row)
    entity["aliases"] = loads_decimal_json(entity.pop("aliases_json"))
    entity["is_default"] = bool(entity["is_default"])
    return entity


def _dictionary_detail(row: sqlite3.Row) -> dict[str, Any]:
    entry = dict(row)
    entry["is_builtin"] = bool(entry["is_builtin"])
    return entry


def _entity_filter(accounting_entity: str | None) -> tuple[str, tuple[str, ...]]:
    return (
        ("", ())
        if accounting_entity is None
        else ("WHERE accounting_entity = ?", (accounting_entity,))
    )


def _select_existing_bill(
    connection: sqlite3.Connection, bill_id: str, accounting_entity: str | None = None
) -> sqlite3.Row | None:
    sql = "SELECT * FROM existing_bills WHERE id = ?"
    params = (bill_id,)
    if accounting_entity is not None:
        sql += " AND accounting_entity = ?"
        params += (accounting_entity,)
    return connection.execute(sql, params).fetchone()


def _existing_bill(row: sqlite3.Row) -> ExistingBillRecord:
    bill = dict(row)
    bill["container_nos"] = loads_decimal_json(bill.pop("container_nos_json"))
    bill["extra_fields"] = loads_decimal_json(bill.pop("extra_fields_json"))
    return ExistingBillRecord.model_validate(bill)


def _store_existing_bill(
    connection: sqlite3.Connection, bill: ExistingBillRecord, *, previous: sqlite3.Row | None = None
) -> ExistingBillRecord:
    if previous is None:
        created_at = bill.created_at or utc_timestamp()
        bill = bill.model_copy(
            update={
                "created_at": created_at,
                "updated_at": bill.updated_at or created_at,
            }
        )
    else:
        bill = bill.model_copy(
            update={
                "created_at": previous["created_at"],
                "updated_at": utc_timestamp(),
                "version": previous["version"] + 1,
            }
        )
    values = bill.model_dump(mode="python", exclude={"container_nos", "extra_fields"})
    values.update({name: decimal_to_db_text(getattr(bill, name)) for name in _BILL_AMOUNT_FIELDS})
    values["container_nos_json"] = dumps_decimal_json(bill.container_nos)
    values["extra_fields_json"] = dumps_decimal_json(_json_values(bill.extra_fields))
    if previous is None:
        _insert(connection, "existing_bills", values)
    else:
        values.pop("id")
        _update(connection, "existing_bills", values, "id = ?", (bill.id,))
    return bill


def _baseline_target(connection: sqlite3.Connection, record: sqlite3.Row) -> sqlite3.Row | None:
    previous = connection.execute(
        """SELECT bill.* FROM baseline_sync_items AS sync
           JOIN existing_bills AS bill ON bill.id = sync.existing_bill_id
           WHERE sync.batch_id = ? AND sync.billing_record_id = ?
               AND sync.accounting_entity = ? AND bill.accounting_entity = sync.accounting_entity
           ORDER BY sync.synced_at DESC, sync.rowid DESC LIMIT 1""",
        (record["batch_id"], record["id"], record["accounting_entity"]),
    ).fetchone()
    if previous is not None:
        return previous
    matched_id = record["matched_existing_bill_id"]
    if record["operation"] == "UPDATE" and matched_id is not None:
        return _select_existing_bill(connection, matched_id, record["accounting_entity"])
    return None


def _bill_from_record(
    record: ProcessedBillingRecord, previous: sqlite3.Row | None
) -> ExistingBillRecord:
    refs = record.shipment_refs
    return ExistingBillRecord(
        id=previous["id"] if previous is not None else uuid4().hex,
        external_bill_key=previous["external_bill_key"] if previous is not None else None,
        extra_fields=loads_decimal_json(previous["extra_fields_json"])
        if previous is not None
        else {},
        mbl_no=refs.mbl_nos[0].value if refs.mbl_nos else None,
        hbl_no=refs.hbl_no,
        booking_no=refs.booking_no,
        container_nos=refs.container_nos,
        **{field: getattr(record, field) for field in _BILL_FIELDS_FROM_RECORD},
    )


def _eval_detail(row: sqlite3.Row) -> dict[str, Any]:
    run = dict(row)
    run["report"] = loads_decimal_json(run.pop("report_json"))
    return run
