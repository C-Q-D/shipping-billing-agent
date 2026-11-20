"""Open short-lived SQLite connections and bootstrap the billing database."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from errno import EACCES, EAGAIN
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Self
from uuid import uuid4

from ..domain.charge_normalizer import _CHARGE_NAMES
from ..domain.models import AccountingEntityProfile, dumps_decimal_json, loads_decimal_json

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_RESTART_MESSAGE = "服务重启中断处理，可重试或切换样本恢复"
_SNAPSHOT_COLUMNS = {
    "batches": {
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)",
        "batch_existing_bills_json": "TEXT",
        "failed_documents_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "source_documents": {"extraction_snapshot_json": "TEXT"},
    "billing_records": {"record_state_json": "TEXT NOT NULL DEFAULT '{}'"},
}
_WORKER_LEASE_LOCK = Lock()
_ACTIVE_WORKERS: dict[Path, BatchWorkerLease] = {}


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def document_root_ids(parents: Mapping[str, str | None]) -> dict[str, str]:
    """Map each source document to the submitted file at the root of its tree."""
    roots = {}
    for doc_id in parents:
        root = doc_id
        seen = {root}
        while (parent := parents[root]) is not None:
            if parent in seen or parent not in parents:
                raise ValueError("Invalid source document tree")
            if parent in roots:
                root = roots[parent]
                break
            root = parent
            seen.add(root)
        roots[doc_id] = root
    return roots


def _lock_worker_file(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class BatchWorkerLease:
    """Hold a worker's file lock across queueing, execution, and the final commit."""

    def __init__(self, batch_id: str, path: Path, stream: BinaryIO) -> None:
        self.batch_id = batch_id
        self._path = path
        self._stream = stream

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def active(self) -> bool:
        with _WORKER_LEASE_LOCK:
            return _ACTIVE_WORKERS.get(self._path) is self and not self._stream.closed

    def close(self) -> None:
        # Keep the lock file in place so concurrent processes always lock the same file.
        with _WORKER_LEASE_LOCK:
            if _ACTIVE_WORKERS.get(self._path) is not self:
                return
            try:
                if sys.platform == "win32":
                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                _ACTIVE_WORKERS.pop(self._path)
                self._stream.close()


class DatabaseManager:
    """Own the startup of a file-backed database and create connections in the caller's thread."""

    def __init__(self, db_path: Path | str, *, recover_orphans: bool = True) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("WAL and independent connections require a file-backed database")
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(recover_orphans=recover_orphans)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection

    def try_acquire_worker_lease(self, batch_id: str) -> BatchWorkerLease | None:
        digest = sha256(batch_id.encode("utf-8")).hexdigest()
        path = self.path.with_name(f".{self.path.name}.{digest}.worker.lock")
        with _WORKER_LEASE_LOCK:
            if path in _ACTIVE_WORKERS:
                return None
            stream = path.open("a+b")
            try:
                _lock_worker_file(stream)
            except OSError as exc:
                stream.close()
                if exc.errno in {EACCES, EAGAIN}:
                    return None
                raise
            except BaseException:
                stream.close()
                raise
            lease = BatchWorkerLease(batch_id, path, stream)
            _ACTIVE_WORKERS[path] = lease
            return lease

    def _initialize(self, *, recover_orphans: bool) -> None:
        with self.connect() as connection, connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise RuntimeError(f"Could not enable SQLite WAL mode: {mode}")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise RuntimeError(f"Unsupported SQLite schema version: {version}")
            if version == 0:
                for statement in filter(str.strip, _SCHEMA_V1.split(";")):
                    connection.execute(statement)
                _add_snapshot_columns(connection)
                _seed_master_data(connection)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            if recover_orphans:
                _recover_interrupted_batches(connection, self)


def _add_snapshot_columns(connection: sqlite3.Connection) -> None:
    """Upgrade databases created from the earlier contract, which had no snapshot columns."""
    for table, definitions in _SNAPSHOT_COLUMNS.items():
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in definitions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _seed_master_data(connection: sqlite3.Connection) -> None:
    timestamp = utc_timestamp()
    if not connection.execute("SELECT 1 FROM accounting_entities LIMIT 1").fetchone():
        profiles = (
            AccountingEntityProfile(
                canonical_name="NEPTUNE SHIPPING LIMITED",
                aliases=["NEPTUNE SHIPPING LIMITED (LAX)", "NEPTUNE"],
                is_default=True,
            ),
            AccountingEntityProfile(canonical_name="SUN TRACK EXPRESS (CHINA) CO., LTD."),
            AccountingEntityProfile(canonical_name="TARGETING A INC"),
            AccountingEntityProfile(
                canonical_name="ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO. LLC",
                aliases=["ZIMAMERICAN INTEGRATED SHIPPING SERVICES CO. LLC", "ZIM"],
            ),
            AccountingEntityProfile(canonical_name="ALL AMERICA FORWARDING, INC."),
        )
        connection.executemany(
            """INSERT INTO accounting_entities
               (id, canonical_name, aliases_json, is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                (
                    uuid4().hex,
                    profile.canonical_name,
                    dumps_decimal_json(profile.aliases),
                    profile.is_default,
                    timestamp,
                    timestamp,
                )
                for profile in profiles
            ),
        )
    if not connection.execute("SELECT 1 FROM charge_dictionary LIMIT 1").fetchone():
        connection.executemany(
            """INSERT INTO charge_dictionary (id, raw_alias, standard_name, is_builtin, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (
                (uuid4().hex, alias, standard_name, timestamp)
                for alias, standard_name in _CHARGE_NAMES.items()
            ),
        )


def _recover_interrupted_batches(connection: sqlite3.Connection, db: DatabaseManager) -> None:
    batches = connection.execute(
        """SELECT id, failed_documents_json, failed_files FROM batches
           WHERE status = 'PROCESSING' OR stage IN (
               'QUEUED', 'UNPACKING', 'PARSING', 'EXTRACTING', 'MATCHING',
               'VALIDATING', 'PROCESSING'
           )"""
    ).fetchall()
    for batch in batches:
        if lease := db.try_acquire_worker_lease(batch["id"]):
            with lease:
                _recover_interrupted_batch(connection, batch)


def _recover_interrupted_batch(connection: sqlite3.Connection, batch: sqlite3.Row) -> None:
    batch_id = batch["id"]
    alerts = loads_decimal_json(batch["failed_documents_json"])
    alerted_documents = {alert["doc_id"] for alert in alerts}
    records_by_document: dict[str, list[str]] = {}
    for record in connection.execute(
        """SELECT id, source_doc_id FROM billing_records WHERE batch_id = ?
           ORDER BY source_line_index, id""",
        (batch_id,),
    ):
        records_by_document.setdefault(record["source_doc_id"], []).append(record["id"])

    documents = connection.execute(
        """SELECT id, parent_doc_id, original_filename, status, error_message,
                  extraction_snapshot_json
           FROM source_documents WHERE batch_id = ?""",
        (batch_id,),
    ).fetchall()
    roots = document_root_ids({doc["id"]: doc["parent_doc_id"] for doc in documents})
    for document in documents:
        doc_id = document["id"]
        record_ids = records_by_document.get(doc_id, [])
        awaiting_extraction = (
            document["status"] == "PARSED"
            and document["extraction_snapshot_json"] is None
            and not record_ids
            and doc_id not in alerted_documents
        )
        interrupted = awaiting_extraction or document["status"] not in (
            "PARSED",
            "SKIPPED_NOISE",
            "METADATA_ONLY",
            "FAILED",
        )
        if not interrupted and document["status"] != "FAILED":
            continue

        message = document["error_message"] if not interrupted else None
        if not message:
            message = _RESTART_MESSAGE
            connection.execute(
                """UPDATE source_documents SET status = 'FAILED', extraction_channel = 'FAILED',
                   error_message = ? WHERE batch_id = ? AND id = ?""",
                (message, batch_id, doc_id),
            )
        if doc_id not in alerted_documents:
            alerts.append(
                {
                    "doc_id": doc_id,
                    "filename": document["original_filename"],
                    "error_message": message,
                    "affected_record_ids": record_ids,
                }
            )
            alerted_documents.add(doc_id)

    failed_root_ids = {roots[doc_id] for doc_id in alerted_documents if doc_id in roots}
    connection.execute(
        """UPDATE batches
           SET status = 'PARTIAL_ERROR', stage = 'FAILED', stage_message = ?,
               failed_documents_json = ?, failed_files = ?,
               version = version + 1, updated_at = ?
           WHERE id = ?""",
        (
            _RESTART_MESSAGE,
            dumps_decimal_json(alerts),
            max(batch["failed_files"], len(failed_root_ids)),
            utc_timestamp(),
            batch_id,
        ),
    )


# Packaged with the application so initialization also works from an installed wheel.
# The storage contract test checks this DDL against docs/workflow/contracts/schema.sql.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS accounting_entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE CHECK (length(trim(canonical_name)) BETWEEN 1 AND 120),
    aliases_json TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS charge_dictionary (
    id TEXT PRIMARY KEY,
    raw_alias TEXT NOT NULL UNIQUE CHECK (length(trim(raw_alias)) > 0),
    standard_name TEXT NOT NULL CHECK (length(trim(standard_name)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS existing_bills (
    id TEXT PRIMARY KEY,
    external_bill_key TEXT,
    accounting_entity TEXT NOT NULL CHECK (length(trim(accounting_entity)) BETWEEN 1 AND 120),
    document_type TEXT,
    reference_no TEXT,
    document_date TEXT,
    issuer TEXT,
    recipient TEXT,
    mbl_no TEXT,
    hbl_no TEXT,
    booking_no TEXT,
    container_nos_json TEXT NOT NULL DEFAULT '[]',
    charge_item TEXT NOT NULL CHECK (length(trim(charge_item)) > 0),
    raw_charge_name TEXT,
    quantity TEXT CHECK (
        quantity IS NULL OR (quantity NOT GLOB '*[^0-9.-]*' AND quantity GLOB '*[0-9]*')
    ),
    unit_price TEXT CHECK (
        unit_price IS NULL OR (unit_price NOT GLOB '*[^0-9.-]*' AND unit_price GLOB '*[0-9]*')
    ),
    amount TEXT NOT NULL CHECK (amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*'),
    currency TEXT NOT NULL CHECK (
        currency GLOB '[A-Z][A-Z][A-Z]' AND currency NOT IN ('XXX', 'XTS')
    ),
    payment_term TEXT CHECK (payment_term IN ('PP', 'CC') OR payment_term IS NULL),
    posting_type TEXT NOT NULL CHECK (posting_type IN ('AR', 'AP')),
    extra_fields_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_existing_bills_l1
    ON existing_bills (accounting_entity, reference_no, charge_item);

CREATE INDEX IF NOT EXISTS idx_existing_bills_l2_mbl
    ON existing_bills (accounting_entity, mbl_no, charge_item, currency);

CREATE INDEX IF NOT EXISTS idx_existing_bills_l2_hbl
    ON existing_bills (accounting_entity, hbl_no, charge_item, currency);

CREATE INDEX IF NOT EXISTS idx_existing_bills_l3_booking
    ON existing_bills (accounting_entity, booking_no, charge_item, currency);

CREATE INDEX IF NOT EXISTS idx_existing_bills_entity_date
    ON existing_bills (accounting_entity, document_date);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    short_code TEXT NOT NULL UNIQUE CHECK (length(trim(short_code)) > 0),
    accounting_entity TEXT NOT NULL CHECK (length(trim(accounting_entity)) BETWEEN 1 AND 120),
    source_channel TEXT NOT NULL DEFAULT 'WEB' CHECK (source_channel IN ('WEB', 'CLI')),
    status TEXT NOT NULL DEFAULT 'PROCESSING' CHECK (
        status IN ('PROCESSING', 'COMPLETED', 'PARTIAL_ERROR')
    ),
    stage TEXT NOT NULL DEFAULT 'QUEUED' CHECK (
        stage IN ('QUEUED', 'UNPACKING', 'EXTRACTING', 'MATCHING', 'VALIDATING', 'COMPLETED',
        'FAILED')
    ),
    stage_message TEXT,
    ocr_mode TEXT NOT NULL DEFAULT 'REMOTE_ONLY' CHECK (
        ocr_mode IN ('REMOTE_ONLY', 'REMOTE_WITH_LOCAL_FALLBACK')
    ),
    total_files INTEGER NOT NULL DEFAULT 0 CHECK (total_files >= 0),
    succeeded_files INTEGER NOT NULL DEFAULT 0 CHECK (succeeded_files >= 0),
    failed_files INTEGER NOT NULL DEFAULT 0 CHECK (failed_files >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    batch_existing_bills_json TEXT,
    failed_documents_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_created_at
    ON batches (created_at DESC);

CREATE TABLE IF NOT EXISTS source_documents (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    parent_doc_id TEXT REFERENCES source_documents(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    sanitized_filename TEXT NOT NULL,
    media_type TEXT,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('EML', 'EML_BODY', 'EML_ATTACHMENT', 'PDF', 'IMAGE', 'EXCEL', 'WORD', 'URL')
    ),
    content_sha256 TEXT CHECK (
        (source_kind = 'URL' AND content_sha256 IS NULL)
        OR (content_sha256 IS NOT NULL AND length(trim(content_sha256)) > 0)
    ),
    stored_path TEXT,
    public_url TEXT,
    extraction_channel TEXT NOT NULL CHECK (
        extraction_channel IN (
            'PLTPLT_OCR',
            'LOCAL_PDF_TEXT_FALLBACK',
            'LOCAL_RAPIDOCR_FALLBACK',
            'EXCEL_PARSER',
            'WORD_PARSER',
            'EML_TEXT_PARSER',
            'SKIPPED_SIGNATURE',
            'FAILED'
        )
    ),
    status TEXT NOT NULL CHECK (status IN ('PARSED', 'SKIPPED_NOISE', 'METADATA_ONLY', 'FAILED')),
    error_message TEXT,
    raw_text TEXT,
    raw_ocr_json TEXT,
    extraction_snapshot_json TEXT,
    excluded_clauses_json TEXT NOT NULL DEFAULT '[]',
    shipment_refs_json TEXT NOT NULL DEFAULT '{}',
    subtotal_amount TEXT CHECK (
        subtotal_amount IS NULL OR (subtotal_amount NOT GLOB '*[^0-9.-]*' AND subtotal_amount GLOB
        '*[0-9]*')
    ),
    tax_amount TEXT CHECK (
        tax_amount IS NULL OR (tax_amount NOT GLOB '*[^0-9.-]*' AND tax_amount GLOB '*[0-9]*')
    ),
    total_amount TEXT CHECK (
        total_amount IS NULL OR (total_amount NOT GLOB '*[^0-9.-]*' AND total_amount GLOB '*[0-9]*')
    ),
    total_currency TEXT CHECK (
        total_currency IS NULL OR (total_currency GLOB '[A-Z][A-Z][A-Z]' AND total_currency NOT IN
        ('XXX', 'XTS'))
    ),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_documents_batch
    ON source_documents (batch_id, parent_doc_id);

CREATE INDEX IF NOT EXISTS idx_source_documents_sha
    ON source_documents (batch_id, content_sha256);

CREATE TABLE IF NOT EXISTS billing_records (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    source_doc_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    source_line_index INTEGER NOT NULL DEFAULT 0 CHECK (source_line_index >= 0),
    operation TEXT NOT NULL CHECK (
        operation IN ('CREATE', 'UPDATE', 'NO_CHANGE', 'PENDING_REVIEW')
    ),
    review_status TEXT NOT NULL CHECK (
        review_status IN ('AUTO_READY', 'NEEDS_REVIEW', 'APPROVED', 'REJECTED')
    ),
    match_level TEXT NOT NULL DEFAULT 'NONE' CHECK (
        match_level IN (
            'NONE',
            'L1_DOC_CHARGE',
            'L2_BL_CHARGE_CURR',
            'L3_BOOKING_CHARGE_CURR',
            'L4_CNTR_PARTY_DATE_AMT',
            'AMBIGUOUS_MULTI'
        )
    ),
    matched_existing_bill_id TEXT REFERENCES existing_bills(id) ON DELETE SET NULL,
    candidate_bill_ids_json TEXT NOT NULL DEFAULT '[]',
    document_type TEXT,
    reference_no TEXT,
    document_date TEXT,
    issuer TEXT,
    recipient TEXT,
    charge_item TEXT,
    raw_charge_name TEXT,
    quantity TEXT CHECK (
        quantity IS NULL OR (quantity NOT GLOB '*[^0-9.-]*' AND quantity GLOB '*[0-9]*')
    ),
    unit_price TEXT CHECK (
        unit_price IS NULL OR (unit_price NOT GLOB '*[^0-9.-]*' AND unit_price GLOB '*[0-9]*')
    ),
    amount TEXT CHECK (amount IS NULL OR (amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*')),
    currency TEXT CHECK (
        currency IS NULL OR (currency GLOB '[A-Z][A-Z][A-Z]' AND currency NOT IN ('XXX', 'XTS'))
    ),
    payment_term TEXT CHECK (payment_term IN ('PP', 'CC') OR payment_term IS NULL),
    accounting_entity TEXT NOT NULL CHECK (length(trim(accounting_entity)) BETWEEN 1 AND 120),
    posting_type TEXT CHECK (posting_type IN ('AR', 'AP') OR posting_type IS NULL),
    shipment_refs_json TEXT NOT NULL DEFAULT '{}',
    source_evidence_json TEXT NOT NULL DEFAULT '{}',
    field_diffs_json TEXT NOT NULL DEFAULT '[]',
    validation_alerts_json TEXT NOT NULL DEFAULT '[]',
    ai_reasoning_json TEXT NOT NULL DEFAULT '[]',
    human_overrides_json TEXT NOT NULL DEFAULT '{}',
    record_state_json TEXT NOT NULL DEFAULT '{}',
    reject_reason TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_billing_records_batch_op
    ON billing_records (batch_id, operation, review_status);

CREATE INDEX IF NOT EXISTS idx_billing_records_source_doc
    ON billing_records (source_doc_id, source_line_index);

CREATE TABLE IF NOT EXISTS baseline_sync_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    billing_record_id TEXT NOT NULL REFERENCES billing_records(id) ON DELETE CASCADE,
    accounting_entity TEXT NOT NULL CHECK (length(trim(accounting_entity)) BETWEEN 1 AND 120),
    record_updated_at TEXT NOT NULL,
    existing_bill_id TEXT NOT NULL REFERENCES existing_bills(id) ON DELETE CASCADE,
    synced_at TEXT NOT NULL,
    UNIQUE (batch_id, billing_record_id, record_updated_at)
);

CREATE INDEX IF NOT EXISTS idx_baseline_sync_batch
    ON baseline_sync_items (batch_id, billing_record_id);

CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('OFFLINE_DETERMINISTIC', 'LIVE_LLM', 'LIVE_E2E')),
    status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED', 'UNVERIFIED')),
    error_message TEXT,
    model_name TEXT NOT NULL,
    k_trials INTEGER NOT NULL DEFAULT 1 CHECK (k_trials >= 1),
    actual_trials INTEGER NOT NULL DEFAULT 0 CHECK (actual_trials >= 0),
    total_cases INTEGER NOT NULL CHECK (total_cases >= 0),
    schema_valid_rate REAL CHECK (
        schema_valid_rate IS NULL OR (schema_valid_rate >= 0.0 AND schema_valid_rate <= 1.0)
    ),
    field_exact_match REAL CHECK (
        field_exact_match IS NULL OR (field_exact_match >= 0.0 AND field_exact_match <= 1.0)
    ),
    ar_ap_accuracy REAL CHECK (
        ar_ap_accuracy IS NULL OR (ar_ap_accuracy >= 0.0 AND ar_ap_accuracy <= 1.0)
    ),
    operation_accuracy REAL CHECK (
        operation_accuracy IS NULL OR (operation_accuracy >= 0.0 AND operation_accuracy <= 1.0)
    ),
    conflict_recall REAL CHECK (
        conflict_recall IS NULL OR (conflict_recall >= 0.0 AND conflict_recall <= 1.0)
    ),
    pass_at_k REAL CHECK (pass_at_k IS NULL OR (pass_at_k >= 0.0 AND pass_at_k <= 1.0)),
    pass_pow_k REAL CHECK (pass_pow_k IS NULL OR (pass_pow_k >= 0.0 AND pass_pow_k <= 1.0)),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_created_at
    ON eval_runs (created_at DESC);
"""
