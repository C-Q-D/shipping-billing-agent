-- =============================================================================
-- 多格式文档解析的航运智能账单助手智能体 · SQLite 3 数据库契约
-- 版本: 1.1.0
-- 说明: 单库本地持久化；金额/单价/数量统一采用规范十进制字符串存储防浮点误差
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. 记账主体档案表
CREATE TABLE IF NOT EXISTS accounting_entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE CHECK (length(trim(canonical_name)) BETWEEN 1 AND 120),
    aliases_json TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. 费用名称标准映射词典表
CREATE TABLE IF NOT EXISTS charge_dictionary (
    id TEXT PRIMARY KEY,
    raw_alias TEXT NOT NULL UNIQUE CHECK (length(trim(raw_alias)) > 0),
    standard_name TEXT NOT NULL CHECK (length(trim(standard_name)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 3. 已有历史账单基准表（按 accounting_entity 隔离检索）
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
    quantity TEXT CHECK (quantity IS NULL OR (quantity NOT GLOB '*[^0-9.-]*' AND quantity GLOB '*[0-9]*')),
    unit_price TEXT CHECK (unit_price IS NULL OR (unit_price NOT GLOB '*[^0-9.-]*' AND unit_price GLOB '*[0-9]*')),
    amount TEXT NOT NULL CHECK (amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*'),
    currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]' AND currency NOT IN ('XXX', 'XTS')),
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

-- 4. 解析批次主表（含实时处理阶段 stage 供轮询与刷新恢复）
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    short_code TEXT NOT NULL UNIQUE CHECK (length(trim(short_code)) > 0),
    accounting_entity TEXT NOT NULL CHECK (length(trim(accounting_entity)) BETWEEN 1 AND 120),
    source_channel TEXT NOT NULL DEFAULT 'WEB' CHECK (source_channel IN ('WEB', 'CLI')),
    status TEXT NOT NULL DEFAULT 'PROCESSING' CHECK (status IN ('PROCESSING', 'COMPLETED', 'PARTIAL_ERROR')),
    stage TEXT NOT NULL DEFAULT 'QUEUED' CHECK (
        stage IN ('QUEUED', 'UNPACKING', 'EXTRACTING', 'MATCHING', 'VALIDATING', 'COMPLETED', 'FAILED')
    ),
    stage_message TEXT,
    ocr_mode TEXT NOT NULL DEFAULT 'REMOTE_ONLY' CHECK (ocr_mode IN ('REMOTE_ONLY', 'REMOTE_WITH_LOCAL_FALLBACK')),
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

-- 5. 单据来源与递归附件表
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
    subtotal_amount TEXT CHECK (subtotal_amount IS NULL OR (subtotal_amount NOT GLOB '*[^0-9.-]*' AND subtotal_amount GLOB '*[0-9]*')),
    tax_amount TEXT CHECK (tax_amount IS NULL OR (tax_amount NOT GLOB '*[^0-9.-]*' AND tax_amount GLOB '*[0-9]*')),
    total_amount TEXT CHECK (total_amount IS NULL OR (total_amount NOT GLOB '*[^0-9.-]*' AND total_amount GLOB '*[0-9]*')),
    total_currency TEXT CHECK (total_currency IS NULL OR (total_currency GLOB '[A-Z][A-Z][A-Z]' AND total_currency NOT IN ('XXX', 'XTS'))),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_documents_batch
    ON source_documents (batch_id, parent_doc_id);

CREATE INDEX IF NOT EXISTS idx_source_documents_sha
    ON source_documents (batch_id, content_sha256);

-- 6. 费用账单记录表
CREATE TABLE IF NOT EXISTS billing_records (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    source_doc_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    source_line_index INTEGER NOT NULL DEFAULT 0 CHECK (source_line_index >= 0),
    operation TEXT NOT NULL CHECK (operation IN ('CREATE', 'UPDATE', 'NO_CHANGE', 'PENDING_REVIEW')),
    review_status TEXT NOT NULL CHECK (review_status IN ('AUTO_READY', 'NEEDS_REVIEW', 'APPROVED', 'REJECTED')),
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
    quantity TEXT CHECK (quantity IS NULL OR (quantity NOT GLOB '*[^0-9.-]*' AND quantity GLOB '*[0-9]*')),
    unit_price TEXT CHECK (unit_price IS NULL OR (unit_price NOT GLOB '*[^0-9.-]*' AND unit_price GLOB '*[0-9]*')),
    amount TEXT CHECK (amount IS NULL OR (amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*')),
    currency TEXT CHECK (currency IS NULL OR (currency GLOB '[A-Z][A-Z][A-Z]' AND currency NOT IN ('XXX', 'XTS'))),
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

-- 7. 基准库增量同步幂等明细表（支持局部补审再同步与并发双击保护）
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

-- 8. Harness 评测运行记录表（指标允许为 NULL 以如实表达 FAILED / UNVERIFIED）
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('OFFLINE_DETERMINISTIC', 'LIVE_LLM', 'LIVE_E2E')),
    status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED', 'UNVERIFIED')),
    error_message TEXT,
    model_name TEXT NOT NULL,
    k_trials INTEGER NOT NULL DEFAULT 1 CHECK (k_trials >= 1),
    actual_trials INTEGER NOT NULL DEFAULT 0 CHECK (actual_trials >= 0),
    total_cases INTEGER NOT NULL CHECK (total_cases >= 0),
    schema_valid_rate REAL CHECK (schema_valid_rate IS NULL OR (schema_valid_rate >= 0.0 AND schema_valid_rate <= 1.0)),
    field_exact_match REAL CHECK (field_exact_match IS NULL OR (field_exact_match >= 0.0 AND field_exact_match <= 1.0)),
    ar_ap_accuracy REAL CHECK (ar_ap_accuracy IS NULL OR (ar_ap_accuracy >= 0.0 AND ar_ap_accuracy <= 1.0)),
    operation_accuracy REAL CHECK (operation_accuracy IS NULL OR (operation_accuracy >= 0.0 AND operation_accuracy <= 1.0)),
    conflict_recall REAL CHECK (conflict_recall IS NULL OR (conflict_recall >= 0.0 AND conflict_recall <= 1.0)),
    pass_at_k REAL CHECK (pass_at_k IS NULL OR (pass_at_k >= 0.0 AND pass_at_k <= 1.0)),
    pass_pow_k REAL CHECK (pass_pow_k IS NULL OR (pass_pow_k >= 0.0 AND pass_pow_k <= 1.0)),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_created_at
    ON eval_runs (created_at DESC);
