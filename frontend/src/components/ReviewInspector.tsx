import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  isAllowedUploadFilename,
  isValidPublicHttpUrl,
  MAX_SINGLE_FILE_BYTES,
  type BatchDetail,
  type BillingRecordDetail,
  type ChargeDictionaryEntry,
  type OcrMode,
  type SourceDocumentSummary,
  type ValidationAlert,
} from "../api/client.ts";
import { formatLosslessAmount, readDecimalString } from "../api/decimalJson.ts";
import {
  buildDocTotalsDiffPatch,
  buildRecordDiffPatch,
  countCodePoints,
  extractServerRecordFormValues,
  getEffectiveDocTotalsValues,
  getEffectiveRecordFormValues,
  hasDocTotalsDraft,
  hasRecordDraft,
  validateReviewReason,
  type DebitCreditOption,
  type DocTotalsFormValues,
  type DraftStoreState,
  type MutationErrorBanner,
  type RecordFormValues,
} from "../api/reviewState.ts";
import {
  LumnoButton,
  LumnoInput,
  LumnoPopconfirm,
  LumnoSegmentedControl,
  LumnoSwitch,
  LumnoTextarea,
  LumnoTooltip,
} from "./LumnoPrimitives.tsx";

export type { MutationErrorBanner };

export interface RecordAuditHistoryState {
  loading: boolean;
  error: string | null;
  retryCount: number | null;
  extractionChannel: string | null;
  history: Array<{
    action: string;
    operator: string;
    timestamp: string;
    reason?: string;
  }>;
}

export interface ReviewInspectorProps {
  batch: BatchDetail | null;
  selectedRecord: BillingRecordDetail | null;
  inspectedDocId: string | null;
  draftStore: DraftStoreState;
  dictionaryList: ReadonlyArray<ChargeDictionaryEntry>;
  savingRecord: boolean;
  approvingOrRejecting: boolean;
  savingDocTotals: boolean;
  retryingDocId: string | null;
  forcingDocId: string | null;
  mutationError: MutationErrorBanner | null;
  auditHistoryByKey: Record<string, RecordAuditHistoryState>;
  onClearMutationError: () => void;
  onRefreshBatch: () => void;
  onClearInspectedDoc: () => void;
  onUpdateRecordField: <K extends keyof RecordFormValues>(
    record: BillingRecordDetail,
    field: K,
    value: RecordFormValues[K]
  ) => void;
  onResetRecordDraft: (recordId: string) => void;
  onSaveRecordPatch: (record: BillingRecordDetail) => Promise<void>;
  onApproveRecord: (
    record: BillingRecordDetail,
    reason: string
  ) => Promise<void>;
  onRejectRecord: (
    record: BillingRecordDetail,
    reason: string
  ) => Promise<void>;
  onUpdateDocField: <K extends keyof DocTotalsFormValues>(
    doc: SourceDocumentSummary,
    field: K,
    value: DocTotalsFormValues[K]
  ) => void;
  onResetDocDraft: (docId: string) => void;
  onSaveDocTotals: (doc: SourceDocumentSummary) => Promise<void>;
  onRetryDoc: (
    docId: string,
    options: { ocrMode: OcrMode; publicUrl?: string; file?: File }
  ) => Promise<void>;
  onForceParseDoc: (docId: string) => Promise<void>;
  onOpenManualRecordModal: (defaultDocId?: string) => void;
  onRequestAuditHistory: (batchId: string, recordId: string) => void;
}

const COMMON_ISO_CURRENCIES = [
  "USD",
  "CNY",
  "EUR",
  "HKD",
  "GBP",
  "JPY",
  "SGD",
  "AUD",
  "CAD",
] as const;

type ProvenanceKind = "draft" | "override" | "inherited" | "extracted";

function getProvenanceMeta(kind: ProvenanceKind): {
  label: string;
  className: string;
} {
  switch (kind) {
    case "draft":
      return { label: "未保存", className: "wb-badge wb-badge--warn" };
    case "override":
      return { label: "人工修订", className: "wb-badge wb-badge--info" };
    case "inherited":
      return { label: "历史继承", className: "wb-badge wb-badge--ok" };
    case "extracted":
      return { label: "本单抽取", className: "wb-badge wb-badge--muted" };
  }
}

function getFieldProvenance(
  fieldKey: string,
  formKey: keyof RecordFormValues,
  record: BillingRecordDetail,
  effective: RecordFormValues,
  server: RecordFormValues
): ProvenanceKind {
  if (effective[formKey] !== server[formKey]) {
    return "draft";
  }
  const overrides = record.human_overrides ?? {};
  if (
    Object.prototype.hasOwnProperty.call(overrides, fieldKey) ||
    (fieldKey === "mbl_no" &&
      Object.prototype.hasOwnProperty.call(overrides, "shipment_refs"))
  ) {
    return "override";
  }
  const inheritedFields = record.inherited_fields ?? [];
  if (
    inheritedFields.includes(fieldKey) ||
    (fieldKey === "mbl_no" && inheritedFields.includes("mbl_nos"))
  ) {
    return "inherited";
  }
  return "extracted";
}

function formatDiffScalar(val: unknown, fallbackHolder: unknown, key: string): string {
  const dec = readDecimalString(fallbackHolder, key);
  if (dec !== null) return dec;
  if (val === null || val === undefined) return "null";
  if (Array.isArray(val)) return val.join(", ") || "[]";
  return String(val);
}

export function ReviewInspector({
  batch,
  selectedRecord,
  inspectedDocId,
  draftStore,
  dictionaryList,
  savingRecord,
  approvingOrRejecting,
  savingDocTotals,
  retryingDocId,
  forcingDocId,
  mutationError,
  auditHistoryByKey,
  onClearMutationError,
  onRefreshBatch,
  onClearInspectedDoc,
  onUpdateRecordField,
  onResetRecordDraft,
  onSaveRecordPatch,
  onApproveRecord,
  onRejectRecord,
  onUpdateDocField,
  onResetDocDraft,
  onSaveDocTotals,
  onRetryDoc,
  onForceParseDoc,
  onOpenManualRecordModal,
  onRequestAuditHistory,
}: ReviewInspectorProps) {
  const scrollPaneRef = useRef<HTMLDivElement | null>(null);
  const replaceFileInputRef = useRef<HTMLInputElement | null>(null);
  const replaceTargetDocIdRef = useRef<string | null>(null);
  const fieldRefs = useRef<Record<string, HTMLElement | null>>({});
  const lastAuditStampRef = useRef<string | null>(null);

  const [highlightedField, setHighlightedField] = useState<string | null>(null);
  const [recordValidationErrors, setRecordValidationErrors] = useState<
    Record<string, string>
  >({});
  const [docValidationErrors, setDocValidationErrors] = useState<
    Record<string, string>
  >({});

  const [approvePanelOpen, setApprovePanelOpen] = useState(false);
  const [approveReason, setApproveReason] = useState("");
  const [approveReasonError, setApproveReasonError] = useState<string | null>(
    null
  );

  const [auditExpanded, setAuditExpanded] = useState(false);
  const [retryOcrMode, setRetryOcrMode] = useState<OcrMode>("REMOTE_ONLY");
  const [retryPublicUrl, setRetryPublicUrl] = useState("");
  const [stagedRetryFile, setStagedRetryFile] = useState<{
    docId: string;
    file: File;
  } | null>(null);
  const [retrySourceError, setRetrySourceError] = useState<string | null>(null);

  useLayoutEffect(() => {
    scrollPaneRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [batch?.id, selectedRecord?.id, inspectedDocId]);

  useEffect(() => {
    setRecordValidationErrors({});
    setApprovePanelOpen(false);
    setApproveReason("");
    setApproveReasonError(null);
    setAuditExpanded(false);
    lastAuditStampRef.current = null;
    setHighlightedField(null);
  }, [batch?.id, selectedRecord?.id]);

  useEffect(() => {
    if (!auditExpanded || !batch || !selectedRecord) {
      return;
    }
    if (lastAuditStampRef.current === null) {
      return;
    }
    const key = `${batch.id}::${selectedRecord.id}`;
    const stamp = `${key}::${selectedRecord.updated_at ?? ""}`;
    const currentEntry = auditHistoryByKey[key];
    if (!currentEntry) {
      lastAuditStampRef.current = stamp;
      onRequestAuditHistory(batch.id, selectedRecord.id);
      return;
    }
    if (lastAuditStampRef.current !== stamp && !currentEntry.loading) {
      lastAuditStampRef.current = stamp;
      onRequestAuditHistory(batch.id, selectedRecord.id);
    }
  }, [
    auditExpanded,
    auditHistoryByKey,
    batch?.id,
    selectedRecord?.id,
    selectedRecord?.updated_at,
    onRequestAuditHistory,
  ]);

  useEffect(() => {
    setDocValidationErrors({});
    setRetryPublicUrl("");
    setStagedRetryFile(null);
    setRetrySourceError(null);
  }, [batch?.id, inspectedDocId, selectedRecord?.source_doc_id]);

  useEffect(() => {
    if (batch) {
      setRetryOcrMode(batch.ocr_mode);
    }
  }, [batch?.id]);

  const activeRecordDirty =
    batch && selectedRecord
      ? hasRecordDraft(draftStore, batch.id, selectedRecord.id)
      : false;

  useEffect(() => {
    if (activeRecordDirty) {
      setApprovePanelOpen(false);
      setApproveReasonError(null);
    }
  }, [activeRecordDirty]);

  const inspectedDoc = useMemo(() => {
    if (!batch || !inspectedDocId) return null;
    return (batch.documents ?? []).find((d) => d.id === inspectedDocId) ?? null;
  }, [batch, inspectedDocId]);

  const recordSourceDoc = useMemo(() => {
    if (!batch || !selectedRecord) return null;
    return (
      (batch.documents ?? []).find((d) => d.id === selectedRecord.source_doc_id) ??
      null
    );
  }, [batch, selectedRecord]);

  const dictionaryStandardNames = useMemo(() => {
    const names = new Set<string>();
    for (const entry of dictionaryList) {
      if (entry.standard_name.trim()) {
        names.add(entry.standard_name.trim());
      }
    }
    return Array.from(names);
  }, [dictionaryList]);

  const focusField = (fieldKey: string) => {
    const mapKey = fieldKey === "container_nos" ? "container_nos_text" : fieldKey;
    const el = fieldRefs.current[mapKey];
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.focus();
      setHighlightedField(mapKey);
      window.setTimeout(() => {
        setHighlightedField((curr) => (curr === mapKey ? null : curr));
      }, 1500);
    }
  };

  const applySuggestedAlertValue = (alert: ValidationAlert) => {
    if (!selectedRecord || !alert.field || alert.expected === undefined || alert.expected === null) {
      return;
    }
    const val = alert.expected;
    switch (alert.field) {
      case "container_nos": {
        const current = getEffectiveRecordFormValues(
          selectedRecord,
          draftStore,
          batch!.id
        ).container_nos_text;
        const nextVal =
          alert.actual && current.includes(alert.actual)
            ? current.replace(alert.actual, val)
            : val;
        onUpdateRecordField(selectedRecord, "container_nos_text", nextVal);
        break;
      }
      case "currency":
        onUpdateRecordField(selectedRecord, "currency", val.toUpperCase());
        break;
      case "amount":
        onUpdateRecordField(selectedRecord, "amount", val);
        break;
      case "unit_price":
        onUpdateRecordField(selectedRecord, "unit_price", val);
        break;
      case "quantity":
        onUpdateRecordField(selectedRecord, "quantity", val);
        break;
      case "document_date":
        onUpdateRecordField(selectedRecord, "document_date", val);
        break;
      case "posting_type":
        if (val === "AR" || val === "AP") {
          onUpdateRecordField(selectedRecord, "posting_type", val);
        }
        break;
      default:
        break;
    }
  };

  const handleSaveRecordClick = async () => {
    if (!batch || !selectedRecord) return;
    const diff = buildRecordDiffPatch(selectedRecord, draftStore, batch.id);
    if (!diff.valid) {
      setRecordValidationErrors(diff.fieldErrors);
      const firstErrField = Object.keys(diff.fieldErrors)[0];
      if (firstErrField) {
        focusField(firstErrField);
      }
      return;
    }
    setRecordValidationErrors({});
    await onSaveRecordPatch(selectedRecord);
  };

  const handleConfirmApproveClick = async () => {
    if (!batch || !selectedRecord) return;
    if (hasRecordDraft(draftStore, batch.id, selectedRecord.id)) {
      setApproveReasonError("请先保存当前未保存修改，或放弃草稿后再执行批准/驳回");
      return;
    }
    const err = validateReviewReason(approveReason);
    if (err) {
      setApproveReasonError(err);
      return;
    }
    setApproveReasonError(null);
    await onApproveRecord(selectedRecord, approveReason.trim());
    setApprovePanelOpen(false);
    setApproveReason("");
  };

  const handleSaveDocTotalsClick = async (doc: SourceDocumentSummary) => {
    if (!batch) return;
    const diff = buildDocTotalsDiffPatch(doc, draftStore, batch.id);
    if (!diff.valid) {
      setDocValidationErrors(diff.fieldErrors);
      return;
    }
    setDocValidationErrors({});
    await onSaveDocTotals(doc);
  };

  const handleReplaceFilePick = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    const docId = replaceTargetDocIdRef.current;
    replaceTargetDocIdRef.current = null;
    if (!docId || !file) return;
    if (!isAllowedUploadFilename(file.name)) {
      setRetrySourceError(
        "不支持的替换文件格式（仅支持 .eml/.pdf/.jpg/.jpeg/.png/.tif/.tiff/.xls/.xlsx/.doc/.docx）"
      );
      return;
    }
    if (file.size <= 0) {
      setRetrySourceError("替换文件大小为 0 字节，无法上传");
      return;
    }
    if (file.size > MAX_SINGLE_FILE_BYTES) {
      setRetrySourceError("替换文件超过 50 MiB 单文件上限");
      return;
    }
    // A8: Mutually exclusive — selecting a replacement file clears public URL
    setRetryPublicUrl("");
    setRetrySourceError(null);
    setStagedRetryFile({ docId, file });
  };

  const handleSubmitDocRetry = async (docId: string) => {
    const activeFile =
      stagedRetryFile && stagedRetryFile.docId === docId
        ? stagedRetryFile.file
        : undefined;
    const trimmedUrl = retryPublicUrl.trim();
    if (activeFile && trimmedUrl) {
      setRetrySourceError("替换文件与公网 URL 互斥，请仅保留其中一项");
      return;
    }
    if (trimmedUrl && !isValidPublicHttpUrl(trimmedUrl)) {
      setRetrySourceError("公网 URL 格式无效，须以 http:// 或 https:// 开头");
      return;
    }
    setRetrySourceError(null);
    await onRetryDoc(docId, {
      ocrMode: retryOcrMode,
      publicUrl: activeFile ? undefined : trimmedUrl || undefined,
      file: activeFile,
    });
    if (activeFile) {
      setStagedRetryFile(null);
    }
  };

  const renderDocRepairSection = (doc: SourceDocumentSummary, isStandaloneDocView: boolean) => {
    if (!batch) return null;
    const docValues = getEffectiveDocTotalsValues(doc, draftStore, batch.id);
    const isDocDirty = hasDocTotalsDraft(draftStore, batch.id, doc.id);
    const docDiff = buildDocTotalsDiffPatch(doc, draftStore, batch.id);
    const isRetrying = retryingDocId === doc.id;
    const isForcing = forcingDocId === doc.id;

    return (
      <div className="wb-section-box" aria-label="单据修复与汇总总额修订">
        <div className="wb-field-label">
          <span>
            {isStandaloneDocView
              ? `文档修理：${doc.sanitized_filename}`
              : `所属单据总额与文档修理：${doc.sanitized_filename}`}
          </span>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
            <span
              className={`wb-badge wb-badge--${
                doc.status === "FAILED"
                  ? "error"
                  : doc.status === "PARSED"
                    ? "ok"
                    : doc.status === "METADATA_ONLY"
                      ? "info"
                      : "muted"
              }`}
            >
              {doc.status}
            </span>
            {isDocDirty ? (
              <span className="wb-badge wb-badge--warn">汇总未保存</span>
            ) : null}
            {isStandaloneDocView && selectedRecord ? (
              <LumnoButton
                variant="ghost"
                compact
                icon="ri-close-line"
                onClick={onClearInspectedDoc}
              >
                收起独立检视
              </LumnoButton>
            ) : null}
          </div>
        </div>

        <div className="wb-helper-note">
          提取通道：<span className="wb-mono">{doc.extraction_channel}</span> ·
          关联账单行 {doc.affected_record_ids?.length ?? 0} 条
        </div>

        {doc.error_message ? (
          <div
            className={
              doc.status === "FAILED" ? "wb-field-error" : "wb-helper-note"
            }
          >
            {doc.error_message}
          </div>
        ) : null}

        <div className="wb-doc-repair-controls">
          <div className="wb-field-label">
            <span>重试 / 替换来源（替换文件与公网 URL 严格互斥）</span>
          </div>
          <div className="wb-form-row-wrap">
            <LumnoSegmentedControl
              value={retryOcrMode}
              ariaLabel="单文件重试 OCR 通道"
              options={[
                { value: "REMOTE_ONLY", label: "远端 OCR" },
                { value: "REMOTE_WITH_LOCAL_FALLBACK", label: "本地兜底" },
              ]}
              onChange={setRetryOcrMode}
            />
            <LumnoInput
              value={retryPublicUrl}
              placeholder="可选：填写公网 URL 重试（输入 URL 将清空待传替换文件）"
              aria-label="补充公网 URL 重试"
              onChange={(e) => {
                const nextUrl = e.target.value;
                setRetryPublicUrl(nextUrl);
                setRetrySourceError(null);
                if (nextUrl.trim() && stagedRetryFile?.docId === doc.id) {
                  setStagedRetryFile(null);
                }
              }}
            />
          </div>
          {stagedRetryFile && stagedRetryFile.docId === doc.id ? (
            <div className="wb-staged-item">
              <div className="wb-staged-row">
                <span className="wb-staged-name" title={stagedRetryFile.file.name}>
                  待替换文件：{stagedRetryFile.file.name}
                </span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-close-line"
                  onClick={() => {
                    setStagedRetryFile(null);
                    setRetrySourceError(null);
                  }}
                >
                  取消替换
                </LumnoButton>
              </div>
            </div>
          ) : null}
          {retrySourceError ? (
            <div className="wb-field-error">{retrySourceError}</div>
          ) : null}
          <div className="wb-action-row">
            {doc.status === "SKIPPED_NOISE" ? (
              <LumnoButton
                variant="secondary"
                compact
                icon="ri-scan-2-line"
                busy={isForcing}
                disabled={isRetrying}
                onClick={() => void onForceParseDoc(doc.id)}
              >
                强制 OCR 提取 (force-parse)
              </LumnoButton>
            ) : null}
            <LumnoButton
              variant="secondary"
              compact
              icon="ri-refresh-line"
              busy={isRetrying}
              disabled={isForcing}
              onClick={() => void handleSubmitDocRetry(doc.id)}
            >
              {stagedRetryFile && stagedRetryFile.docId === doc.id
                ? "上传替换文件并重算"
                : retryPublicUrl.trim()
                  ? "使用公网 URL 重试"
                  : "按所选通道原地重试"}
            </LumnoButton>
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-upload-2-line"
              disabled={isRetrying || isForcing}
              onClick={() => {
                replaceTargetDocIdRef.current = doc.id;
                replaceFileInputRef.current?.click();
              }}
            >
              选择替换文件
            </LumnoButton>
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-add-line"
              onClick={() => onOpenManualRecordModal(doc.id)}
            >
              同单补充漏提行
            </LumnoButton>
          </div>
        </div>

        <div className="wb-form-divider" />

        <div className="wb-field-label">
          <span>单据汇总金额修订（保存后重算同单所有费用行）</span>
          {isDocDirty ? (
            <LumnoButton
              variant="ghost"
              compact
              onClick={() => onResetDocDraft(doc.id)}
            >
              重置汇总草稿
            </LumnoButton>
          ) : null}
        </div>

        <div className="wb-form-grid-2">
          <label className="wb-form-field">
            <span className="wb-form-label">未税小计 (subtotal_amount)</span>
            <LumnoInput
              className="wb-tabular"
              inputMode="decimal"
              value={docValues.subtotal_amount}
              placeholder="如 1410.40"
              onChange={(e) =>
                onUpdateDocField(doc, "subtotal_amount", e.target.value)
              }
            />
            {docValidationErrors.subtotal_amount ? (
              <span className="wb-field-error">
                {docValidationErrors.subtotal_amount}
              </span>
            ) : null}
          </label>

          <label className="wb-form-field">
            <span className="wb-form-label">税额 (tax_amount)</span>
            <LumnoInput
              className="wb-tabular"
              inputMode="decimal"
              value={docValues.tax_amount}
              placeholder="如 0.00"
              onChange={(e) =>
                onUpdateDocField(doc, "tax_amount", e.target.value)
              }
            />
            {docValidationErrors.tax_amount ? (
              <span className="wb-field-error">
                {docValidationErrors.tax_amount}
              </span>
            ) : null}
          </label>

          <label className="wb-form-field">
            <span className="wb-form-label">单据总计 (total_amount)</span>
            <LumnoInput
              className="wb-tabular"
              inputMode="decimal"
              value={docValues.total_amount}
              placeholder="如 1410.40"
              onChange={(e) =>
                onUpdateDocField(doc, "total_amount", e.target.value)
              }
            />
            {docValidationErrors.total_amount ? (
              <span className="wb-field-error">
                {docValidationErrors.total_amount}
              </span>
            ) : null}
          </label>

          <label className="wb-form-field">
            <span className="wb-form-label">汇总币种 (total_currency)</span>
            <LumnoInput
              className="wb-mono"
              maxLength={3}
              list="wb-iso-currency-list"
              value={docValues.total_currency}
              placeholder="USD"
              onChange={(e) =>
                onUpdateDocField(
                  doc,
                  "total_currency",
                  e.target.value.toUpperCase()
                )
              }
            />
            {docValidationErrors.total_currency ? (
              <span className="wb-field-error">
                {docValidationErrors.total_currency}
              </span>
            ) : null}
          </label>
        </div>

        <label className="wb-form-field">
          <span className="wb-form-label">单据汇总修订说明 (review_note)</span>
          <LumnoInput
            value={docValues.review_note}
            placeholder="例如：修正 OCR 误识单据印章总计为实际应付总额"
            onChange={(e) =>
              onUpdateDocField(doc, "review_note", e.target.value)
            }
          />
        </label>

        <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
          <LumnoButton
            variant="secondary"
            compact
            icon="ri-save-line"
            busy={savingDocTotals}
            disabled={!docDiff.hasChanges && !isDocDirty}
            onClick={() => void handleSaveDocTotalsClick(doc)}
          >
            保存单据汇总并重验
          </LumnoButton>
        </div>
      </div>
    );
  };

  if (!batch) {
    return (
      <>
        <div className="wb-pane-header">
          <h2 className="wb-pane-title">
            <i className="ri-icon ri-file-search-line" aria-hidden="true" />
            <span>全字段复核与文档修理</span>
          </h2>
        </div>
        <div ref={scrollPaneRef} className="wb-pane-body">
          <div className="wb-empty-state lumno-paper-card">
            <i
              className="ri-icon ri-article-line"
              style={{ "--ri-size": "28px" } as React.CSSProperties}
              aria-hidden="true"
            />
            <strong>当前尚未选中解析批次</strong>
            <span>请在左栏投递单据或点击“一键载入 4 份真样本”</span>
          </div>
        </div>
      </>
    );
  }

  const recordDirty = selectedRecord
    ? hasRecordDraft(draftStore, batch.id, selectedRecord.id)
    : false;
  const serverValues = selectedRecord
    ? extractServerRecordFormValues(selectedRecord)
    : null;
  const formValues = selectedRecord
    ? getEffectiveRecordFormValues(selectedRecord, draftStore, batch.id)
    : null;
  const diffPreview = selectedRecord
    ? buildRecordDiffPatch(selectedRecord, draftStore, batch.id)
    : null;

  const blockers = (selectedRecord?.validation_alerts ?? []).filter(
    (a) => a.severity === "BLOCKER"
  );
  const hasBlockers = blockers.length > 0;
  const auditKey = selectedRecord
    ? `${batch.id}::${selectedRecord.id}`
    : `${batch.id}::none`;
  const auditState = auditHistoryByKey[auditKey];

  const renderLabelWithProvenance = (
    title: string,
    fieldKey: string,
    formKey: keyof RecordFormValues
  ) => {
    if (!selectedRecord || !formValues || !serverValues) {
      return <span className="wb-form-label">{title}</span>;
    }
    const kind = getFieldProvenance(
      fieldKey,
      formKey,
      selectedRecord,
      formValues,
      serverValues
    );
    const meta = getProvenanceMeta(kind);
    return (
      <span className="wb-form-label-row">
        <span className="wb-form-label">{title}</span>
        <span className={meta.className}>{meta.label}</span>
      </span>
    );
  };

  return (
    <>
      <div className="wb-pane-header">
        <h2 className="wb-pane-title">
          <i className="ri-icon ri-file-search-line" aria-hidden="true" />
          <span>全字段复核与文档修理</span>
        </h2>
        <div style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          {recordDirty ? (
            <span className="wb-badge wb-badge--warn">有未保存修改</span>
          ) : null}
          {selectedRecord ? (
            <span className="wb-badge wb-badge--muted">
              {selectedRecord.review_status}
            </span>
          ) : null}
        </div>
      </div>

      <div ref={scrollPaneRef} className="wb-pane-body">
        {mutationError ? (
          <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "8px",
              }}
            >
              <strong>
                {mutationError.title}
                {mutationError.code ? ` (${mutationError.code})` : ""}
              </strong>
              <LumnoButton
                variant="ghost"
                compact
                icon="ri-close-line"
                aria-label="关闭错误提示"
                onClick={onClearMutationError}
              />
            </div>
            <div>{mutationError.message}</div>
            {mutationError.blockers && mutationError.blockers.length > 0 ? (
              <ul style={{ margin: "4px 0 0", paddingLeft: "18px", fontSize: "12px" }}>
                {mutationError.blockers.map((b, i) => (
                  <li key={`${b.code}-${i}`}>
                    <span>
                      [{b.code}]{b.field ? ` (${b.field})` : ""} {b.message}
                      {b.expected || b.actual
                        ? `（期望 ${b.expected ?? "—"} / 实际 ${b.actual ?? "—"}）`
                        : ""}
                    </span>
                    {b.field ? (
                      <LumnoButton
                        variant="ghost"
                        compact
                        onClick={() => focusField(b.field!)}
                        style={{ marginLeft: "6px" }}
                      >
                        定位字段
                      </LumnoButton>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            {mutationError.isConflict ? (
              <div style={{ marginTop: "6px" }}>
                <LumnoButton
                  variant="secondary"
                  compact
                  icon="ri-refresh-line"
                  onClick={onRefreshBatch}
                >
                  刷新最新批次状态
                </LumnoButton>
              </div>
            ) : null}
          </div>
        ) : null}

        {inspectedDoc &&
        (!selectedRecord || inspectedDoc.id !== selectedRecord.source_doc_id)
          ? renderDocRepairSection(inspectedDoc, true)
          : null}

        {!selectedRecord || !formValues || !serverValues ? (
          <div className="wb-empty-state lumno-paper-card">
            <i
              className="ri-icon ri-article-line"
              style={{ "--ri-size": "28px" } as React.CSSProperties}
              aria-hidden="true"
            />
            <strong>请在中栏选择一条账单记录</strong>
            <span>
              右栏支持全字段无损修订、软告警解除、历史匹配绑定与同单文档修理
            </span>
          </div>
        ) : (
          <>
            {recordDirty ? (
              <div className="wb-draft-banner" role="status">
                <span>
                  当前记录有未保存修改（切换其它记录后草稿仍会独立保留）
                </span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-arrow-go-back-line"
                  onClick={() => onResetRecordDraft(selectedRecord.id)}
                >
                  重置草稿
                </LumnoButton>
              </div>
            ) : null}

            {/* Summary Header Card */}
            <div className="wb-section-box">
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                  gap: "10px",
                  flexWrap: "wrap",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "15px", fontWeight: 700 }}>
                    {selectedRecord.charge_details.charge_item ?? "未命名费用"}
                  </div>
                  <div className="wb-helper-note">
                    {selectedRecord.operation} · 匹配{" "}
                    {selectedRecord.match_level === "NONE"
                      ? "未匹配"
                      : selectedRecord.match_level}
                    {selectedRecord.matched_bill_ref
                      ? ` · ${selectedRecord.matched_bill_ref}`
                      : ""}
                  </div>
                </div>
                <div
                  className="wb-tabular"
                  style={{ fontSize: "18px", fontWeight: 700 }}
                >
                  {formatLosslessAmount(selectedRecord.charge_details, "amount")}{" "}
                  {selectedRecord.charge_details.currency ?? "—"}
                </div>
              </div>

              {/* Strictly read-only raw_charge_name comparison box */}
              <div className="wb-readonly-box">
                <span className="wb-field-label">
                  <span>原始费用名（raw_charge_name · 仅只读原文对照）</span>
                  <span className="wb-badge wb-badge--muted">只读锁定</span>
                </span>
                <code className="wb-mono" style={{ fontSize: "12px" }}>
                  {selectedRecord.charge_details.raw_charge_name ?? "（原文未提供）"}
                </code>
              </div>

              {selectedRecord.reject_reason ? (
                <div className="wb-alert-item" data-severity="RESOLVED">
                  <strong>已驳回原因：</strong>
                  <span>{selectedRecord.reject_reason}</span>
                </div>
              ) : null}
            </div>

            {/* Validation Alerts */}
            {(selectedRecord.validation_alerts?.length ?? 0) > 0 ? (
              <div className="wb-section-box" aria-label="校验门禁告警">
                <div className="wb-field-label">
                  <span>校验门禁告警</span>
                  <span className="wb-tabular">
                    {selectedRecord.validation_alerts?.length} 项
                  </span>
                </div>
                {(selectedRecord.validation_alerts ?? []).map((alert, idx) => (
                  <div
                    key={`${alert.code}-${alert.field ?? ""}-${idx}`}
                    className="wb-alert-item"
                    data-severity={alert.severity}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "6px",
                        flexWrap: "wrap",
                      }}
                    >
                      <strong style={{ fontSize: "12px" }}>
                        [{alert.severity}] {alert.code}
                        {alert.field ? ` · ${alert.field}` : ""}
                      </strong>
                      <div style={{ display: "inline-flex", gap: "4px" }}>
                        {alert.field ? (
                          <LumnoButton
                            variant="ghost"
                            compact
                            onClick={() => focusField(alert.field!)}
                          >
                            定位字段
                          </LumnoButton>
                        ) : null}
                        {alert.severity !== "RESOLVED" &&
                        alert.field &&
                        alert.expected ? (
                          <LumnoButton
                            variant="ghost"
                            compact
                            onClick={() => applySuggestedAlertValue(alert)}
                          >
                            采用建议值
                          </LumnoButton>
                        ) : null}
                      </div>
                    </div>

                    <div>{alert.message}</div>

                    {alert.expected || alert.actual ? (
                      <div className="wb-mono wb-tabular" style={{ fontSize: "11px" }}>
                        期望: {alert.expected ?? "—"} | 实际:{" "}
                        {alert.actual ?? "—"}
                      </div>
                    ) : null}

                    {alert.severity === "BLOCKER" ? (
                      <div style={{ fontSize: "11px", opacity: 0.9 }}>
                        硬性阻断严禁强行解除，请修订对应字段、修正单据总额或选择驳回。
                      </div>
                    ) : null}

                    {alert.severity === "WARNING" ? (
                      <div style={{ fontSize: "11px", opacity: 0.85 }}>
                        软告警可通过修订对应字段保存重验，或直接在底部填写批准理由特例放行。
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            {/* Provenance, Field Diffs & Candidate Bill Disambiguation */}
            <div className="wb-section-box" aria-label="历史账单继承对比与候选关联">
              <div className="wb-field-label">
                <span>溯源继承与历史账单关联</span>
                <span>
                  {selectedRecord.matched_bill_ref ?? "未关联历史账单"}
                </span>
              </div>

              {(selectedRecord.inherited_fields?.length ?? 0) > 0 ? (
                <div className="wb-chip-row">
                  <span className="wb-helper-note">历史继承字段：</span>
                  {(selectedRecord.inherited_fields ?? []).map((fld) => (
                    <span key={fld} className="wb-badge wb-badge--ok">
                      {fld}
                    </span>
                  ))}
                </div>
              ) : null}

              {Object.keys(selectedRecord.human_overrides ?? {}).length > 0 ? (
                <div className="wb-chip-row">
                  <span className="wb-helper-note">已锁定人工修订：</span>
                  {Object.keys(selectedRecord.human_overrides ?? {}).map((k) => (
                    <span key={k} className="wb-badge wb-badge--info">
                      {k}
                    </span>
                  ))}
                </div>
              ) : null}

              {(selectedRecord.field_diffs?.length ?? 0) > 0 ? (
                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  <div className="wb-helper-note">历史账单字段新旧对比：</div>
                  {selectedRecord.field_diffs?.map((diff) => (
                    <div
                      key={diff.field}
                      className="wb-diff-row"
                      data-changed={diff.changed ? "true" : "false"}
                    >
                      <span className="wb-mono">{diff.field}</span>
                      <span className="wb-tabular">
                        旧: {formatDiffScalar(diff.old_value, diff, "old_value")}
                      </span>
                      <span
                        className="wb-tabular"
                        style={{ fontWeight: diff.changed ? 700 : 400 }}
                      >
                        新: {formatDiffScalar(diff.new_value, diff, "new_value")}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="wb-switch-row">
                <div>
                  <div style={{ fontSize: "12px", fontWeight: 600 }}>
                    强制新增独立一笔（force_create_new）
                  </div>
                  <div className="wb-helper-note">
                    开启后解除历史账单匹配关联，按全新账单入账
                  </div>
                </div>
                <LumnoSwitch
                  ariaLabel="强制新增独立一笔账单"
                  compact
                  checked={formValues.force_create_new}
                  onChange={(checked) =>
                    onUpdateRecordField(
                      selectedRecord,
                      "force_create_new",
                      checked
                    )
                  }
                />
              </div>

              {(selectedRecord.candidate_bills?.length ?? 0) > 0 ||
              formValues.matched_existing_bill_id ? (
                <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
                  <div className="wb-helper-note">候选历史账单（单选绑定）：</div>
                  {selectedRecord.candidate_bills?.map((cand) => {
                    const checked =
                      !formValues.force_create_new &&
                      formValues.matched_existing_bill_id === cand.id;
                    return (
                      <label key={cand.id} className="wb-candidate-option">
                        <input
                          type="radio"
                          name={`cand-${selectedRecord.id}`}
                          checked={checked}
                          onChange={() =>
                            onUpdateRecordField(
                              selectedRecord,
                              "matched_existing_bill_id",
                              cand.id
                            )
                          }
                        />
                        <div style={{ minWidth: 0, flex: "1 1 auto" }}>
                          <div style={{ fontWeight: 600, fontSize: "12px" }}>
                            {cand.label}
                          </div>
                          <div className="wb-helper-note wb-tabular">
                            {cand.reference_no ?? "无单号"} · {cand.charge_item ?? "—"} ·{" "}
                            {readDecimalString(cand, "amount") ?? "—"} {cand.currency} ·{" "}
                            {cand.document_date ?? "无日期"}
                          </div>
                        </div>
                      </label>
                    );
                  })}
                  {formValues.matched_existing_bill_id &&
                  !selectedRecord.candidate_bills?.some(
                    (c) => c.id === formValues.matched_existing_bill_id
                  ) ? (
                    <div className="wb-helper-note">
                      当前关联基准：{selectedRecord.matched_bill_ref ?? "已绑定历史账单"}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>

            {/* Full-Field Editing Form (Strict Decimal Strings + Diff-Only PATCH) */}
            <div className="wb-section-box" aria-label="全字段人工复核表单">
              <div className="wb-field-label">
                <span>全字段人工复核修订（仅提交修改差量）</span>
                <span className="wb-helper-note">支持精确十进制长数值</span>
              </div>

              <label className="wb-form-field">
                {renderLabelWithProvenance(
                  "标准费用名称 (charge_item)",
                  "charge_item",
                  "charge_item"
                )}
                <LumnoInput
                  className={
                    highlightedField === "charge_item" ? "wb-field-flash" : ""
                  }
                  list="wb-charge-dict-list"
                  value={formValues.charge_item}
                  placeholder="如 Handling Charge"
                  ref={(node) => {
                    fieldRefs.current.charge_item = node;
                  }}
                  onChange={(e) =>
                    onUpdateRecordField(
                      selectedRecord,
                      "charge_item",
                      e.target.value
                    )
                  }
                />
                {recordValidationErrors.charge_item ? (
                  <span className="wb-field-error">
                    {recordValidationErrors.charge_item}
                  </span>
                ) : null}
              </label>

              <div className="wb-form-grid-3">
                <label className="wb-form-field">
                  {renderLabelWithProvenance("数量 (quantity)", "quantity", "quantity")}
                  <LumnoInput
                    className={`wb-tabular ${
                      highlightedField === "quantity" ? "wb-field-flash" : ""
                    }`}
                    inputMode="decimal"
                    value={formValues.quantity}
                    placeholder="可空"
                    ref={(node) => {
                      fieldRefs.current.quantity = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "quantity",
                        e.target.value
                      )
                    }
                  />
                  {recordValidationErrors.quantity ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.quantity}
                    </span>
                  ) : null}
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance("单价 (unit_price)", "unit_price", "unit_price")}
                  <LumnoInput
                    className={`wb-tabular ${
                      highlightedField === "unit_price" ? "wb-field-flash" : ""
                    }`}
                    inputMode="decimal"
                    value={formValues.unit_price}
                    placeholder="如 0.0025"
                    ref={(node) => {
                      fieldRefs.current.unit_price = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "unit_price",
                        e.target.value
                      )
                    }
                  />
                  {recordValidationErrors.unit_price ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.unit_price}
                    </span>
                  ) : null}
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance("金额 (amount)", "amount", "amount")}
                  <LumnoInput
                    className={`wb-tabular ${
                      highlightedField === "amount" ? "wb-field-flash" : ""
                    }`}
                    inputMode="decimal"
                    value={formValues.amount}
                    placeholder="如 9007199254740991.01"
                    ref={(node) => {
                      fieldRefs.current.amount = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(selectedRecord, "amount", e.target.value)
                    }
                  />
                  {recordValidationErrors.amount ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.amount}
                    </span>
                  ) : null}
                </label>
              </div>

              <div className="wb-form-grid-2">
                <label className="wb-form-field">
                  {renderLabelWithProvenance("币种 (currency)", "currency", "currency")}
                  <LumnoInput
                    className={`wb-mono ${
                      highlightedField === "currency" ? "wb-field-flash" : ""
                    }`}
                    maxLength={3}
                    list="wb-iso-currency-list"
                    value={formValues.currency}
                    placeholder="USD"
                    ref={(node) => {
                      fieldRefs.current.currency = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "currency",
                        e.target.value.toUpperCase()
                      )
                    }
                  />
                  {recordValidationErrors.currency ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.currency}
                    </span>
                  ) : null}
                </label>

                <div className="wb-form-field">
                  {renderLabelWithProvenance(
                    "付款条款 (payment_term)",
                    "payment_term",
                    "payment_term"
                  )}
                  <LumnoSegmentedControl
                    value={formValues.payment_term}
                    ariaLabel="付款条款"
                    options={[
                      { value: "", label: "未定" },
                      { value: "PP", label: "PP" },
                      { value: "CC", label: "CC" },
                    ]}
                    onChange={(val) =>
                      onUpdateRecordField(selectedRecord, "payment_term", val)
                    }
                  />
                </div>

                <div className="wb-form-field">
                  {renderLabelWithProvenance(
                    "记账方向 (posting_type)",
                    "posting_type",
                    "posting_type"
                  )}
                  <LumnoSegmentedControl
                    value={formValues.posting_type}
                    ariaLabel="记账方向 AR/AP"
                    options={[
                      { value: "", label: "待定" },
                      { value: "AR", label: "AR 应收" },
                      { value: "AP", label: "AP 应付" },
                    ]}
                    onChange={(val) =>
                      onUpdateRecordField(selectedRecord, "posting_type", val)
                    }
                  />
                </div>

                <div className="wb-form-field">
                  {renderLabelWithProvenance(
                    "借贷类型 (debit_credit)",
                    "document_type",
                    "debit_credit"
                  )}
                  <LumnoSegmentedControl<DebitCreditOption>
                    value={formValues.debit_credit}
                    ariaLabel="借贷类型"
                    options={[
                      { value: "DEBIT", label: "DEBIT 正向" },
                      { value: "CREDIT", label: "CREDIT 贷项" },
                    ]}
                    onChange={(val) =>
                      onUpdateRecordField(selectedRecord, "debit_credit", val)
                    }
                  />
                </div>
              </div>

              <div className="wb-form-grid-3">
                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "单据类型 (document_type)",
                    "document_type",
                    "document_type"
                  )}
                  <LumnoInput
                    value={formValues.document_type}
                    placeholder="Invoice / Debit Note / Credit Note"
                    ref={(node) => {
                      fieldRefs.current.document_type = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "document_type",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "单据编号 (reference_no)",
                    "reference_no",
                    "reference_no"
                  )}
                  <LumnoInput
                    className="wb-mono"
                    value={formValues.reference_no}
                    placeholder="如 DN-202609-001"
                    ref={(node) => {
                      fieldRefs.current.reference_no = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "reference_no",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "单据日期 (document_date)",
                    "document_date",
                    "document_date"
                  )}
                  <LumnoInput
                    className={`wb-mono ${
                      highlightedField === "document_date" ? "wb-field-flash" : ""
                    }`}
                    value={formValues.document_date}
                    placeholder="YYYY-MM-DD"
                    ref={(node) => {
                      fieldRefs.current.document_date = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "document_date",
                        e.target.value
                      )
                    }
                  />
                  {recordValidationErrors.document_date ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.document_date}
                    </span>
                  ) : null}
                </label>
              </div>

              <div className="wb-form-grid-2">
                <label className="wb-form-field">
                  {renderLabelWithProvenance("签发方 (issuer)", "issuer", "issuer")}
                  <LumnoInput
                    value={formValues.issuer}
                    placeholder="开票/收款主体名称"
                    ref={(node) => {
                      fieldRefs.current.issuer = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "issuer",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "接收方 (recipient)",
                    "recipient",
                    "recipient"
                  )}
                  <LumnoInput
                    value={formValues.recipient}
                    placeholder="付款/接收主体名称"
                    ref={(node) => {
                      fieldRefs.current.recipient = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "recipient",
                        e.target.value
                      )
                    }
                  />
                </label>
              </div>

              <div className="wb-form-grid-2">
                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "主提单号 (mbl_no)",
                    "mbl_no",
                    "mbl_no"
                  )}
                  <LumnoInput
                    className="wb-mono"
                    value={formValues.mbl_no}
                    placeholder="如 OOLU4055331281"
                    ref={(node) => {
                      fieldRefs.current.mbl_no = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "mbl_no",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "分提单号 (hbl_no)",
                    "hbl_no",
                    "hbl_no"
                  )}
                  <LumnoInput
                    className="wb-mono"
                    value={formValues.hbl_no}
                    placeholder="如 STE20264055HBL"
                    ref={(node) => {
                      fieldRefs.current.hbl_no = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "hbl_no",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "订舱号 (booking_no)",
                    "booking_no",
                    "booking_no"
                  )}
                  <LumnoInput
                    className="wb-mono"
                    value={formValues.booking_no}
                    placeholder="如 4055331281"
                    ref={(node) => {
                      fieldRefs.current.booking_no = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "booking_no",
                        e.target.value
                      )
                    }
                  />
                </label>

                <label className="wb-form-field">
                  {renderLabelWithProvenance(
                    "箱号列表 (container_nos)",
                    "container_nos",
                    "container_nos_text"
                  )}
                  <LumnoInput
                    className={`wb-mono ${
                      highlightedField === "container_nos_text"
                        ? "wb-field-flash"
                        : ""
                    }`}
                    value={formValues.container_nos_text}
                    placeholder="多个箱号以逗号分隔"
                    ref={(node) => {
                      fieldRefs.current.container_nos_text = node;
                    }}
                    onChange={(e) =>
                      onUpdateRecordField(
                        selectedRecord,
                        "container_nos_text",
                        e.target.value
                      )
                    }
                  />
                </label>
              </div>

              {(selectedRecord.shipment_refs?.mbl_nos?.length ?? 0) > 0 ? (
                <div className="wb-chip-row">
                  <span className="wb-helper-note">多源 MBL 对照：</span>
                  {selectedRecord.shipment_refs?.mbl_nos?.map((m, idx) => (
                    <span
                      key={`${m.value}-${m.source}-${idx}`}
                      className="wb-badge wb-badge--muted wb-mono"
                    >
                      {m.value} ({m.source})
                    </span>
                  ))}
                </div>
              ) : null}

              <label className="wb-form-field">
                {renderLabelWithProvenance(
                  "本次修订说明 (review_note)",
                  "review_note",
                  "review_note"
                )}
                <LumnoTextarea
                  value={formValues.review_note}
                  placeholder="仅本次提交写入审计历史，保存后恢复空白（既往记录请见下方审计历史）"
                  ref={(node) => {
                    fieldRefs.current.review_note = node;
                  }}
                  onChange={(e) =>
                    onUpdateRecordField(
                      selectedRecord,
                      "review_note",
                      e.target.value
                    )
                  }
                />
              </label>

              {diffPreview && diffPreview.hasChanges ? (
                <div className="wb-helper-note wb-mono">
                  待提交差量字段：{Object.keys(diffPreview.patch).join(", ")}
                </div>
              ) : null}
            </div>

            {/* Source Document Totals & Repair for Selected Record */}
            {recordSourceDoc ? renderDocRepairSection(recordSourceDoc, false) : null}

            {/* Pure-Text Source Evidence, AI Reasoning, Excluded Clauses & On-Demand Audit */}
            <div className="wb-section-box" aria-label="纯文本原文证据与按需审计历史">
              <div className="wb-field-label">
                <span>原文安全纯文本证据（XSS 免疫）</span>
                <span>
                  {recordSourceDoc?.sanitized_filename ?? "来源文档"}
                </span>
              </div>

              {typeof selectedRecord.source_evidence?.source_file === "string" ||
              typeof selectedRecord.source_evidence?.page === "number" ||
              typeof selectedRecord.source_evidence?.sheet === "string" ||
              typeof selectedRecord.source_evidence?.line_number === "number" ||
              typeof selectedRecord.source_evidence?.row === "number" ? (
                <div className="wb-helper-note">
                  {typeof selectedRecord.source_evidence?.source_file === "string"
                    ? `文件：${selectedRecord.source_evidence.source_file}`
                    : "来源位置"}
                  {typeof selectedRecord.source_evidence?.page === "number"
                    ? ` · 第 ${selectedRecord.source_evidence.page} 页`
                    : ""}
                  {typeof selectedRecord.source_evidence?.sheet === "string"
                    ? ` · 工作表：${selectedRecord.source_evidence.sheet}`
                    : ""}
                  {typeof selectedRecord.source_evidence?.line_number === "number"
                    ? ` · 第 ${selectedRecord.source_evidence.line_number} 行`
                    : typeof selectedRecord.source_evidence?.row === "number"
                      ? ` · 第 ${selectedRecord.source_evidence.row} 行`
                      : ""}
                </div>
              ) : null}

              {Array.isArray(selectedRecord.source_evidence?.source_paths) ? (
                <div className="wb-helper-note wb-mono">
                  来源路径：
                  {(selectedRecord.source_evidence.source_paths as unknown[])
                    .map((p) => String(p))
                    .join(" ; ")}
                </div>
              ) : null}

              <pre className="wb-evidence-pre">
                {typeof selectedRecord.source_evidence?.snippet === "string"
                  ? selectedRecord.source_evidence.snippet
                  : "暂无片段摘要"}
              </pre>

              <div className="wb-field-label" style={{ marginTop: "4px" }}>
                <span>AI 推理依据</span>
              </div>
              <ul
                style={{
                  margin: 0,
                  paddingLeft: "18px",
                  fontSize: "12px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "3px",
                }}
              >
                {(selectedRecord.ai_reasoning ?? []).length > 0 ? (
                  (selectedRecord.ai_reasoning ?? []).map((line, i) => (
                    <li key={i}>{line}</li>
                  ))
                ) : (
                  <li className="wb-helper-note">暂无推理说明</li>
                )}
              </ul>

              {(recordSourceDoc?.excluded_clauses?.length ?? 0) > 0 ? (
                <div style={{ marginTop: "4px" }}>
                  <div className="wb-field-label">
                    <span>已排除非计费条款</span>
                  </div>
                  {recordSourceDoc?.excluded_clauses?.map((c, idx) => (
                    <div key={idx} className="wb-helper-note">
                      • {c.text}（{c.reason}）
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="wb-form-divider" />

              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "8px",
                }}
              >
                <span className="wb-field-label">
                  <span>审计历史与提取重试次数（按需加载）</span>
                </span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon={auditExpanded ? "ri-arrow-up-s-line" : "ri-history-line"}
                  aria-expanded={auditExpanded}
                  onClick={() => {
                    const nextOpen = !auditExpanded;
                    setAuditExpanded(nextOpen);
                    if (nextOpen) {
                      lastAuditStampRef.current = `${batch.id}::${selectedRecord.id}::${selectedRecord.updated_at ?? ""}`;
                      onRequestAuditHistory(batch.id, selectedRecord.id);
                    } else {
                      lastAuditStampRef.current = null;
                    }
                  }}
                >
                  {auditExpanded ? "收起审计详情" : "展开审计详情"}
                </LumnoButton>
              </div>

              {auditExpanded ? (
                <div className="wb-audit-box">
                  {auditState?.loading || !auditState ? (
                    <div className="wb-helper-note">正在按需拉取审计轨迹...</div>
                  ) : auditState.error ? (
                    <div className="wb-field-error">{auditState.error}</div>
                  ) : (
                    <>
                      <div className="wb-chip-row wb-tabular">
                        {auditState.extractionChannel ? (
                          <span className="wb-badge wb-badge--muted">
                            提取通道：{auditState.extractionChannel}
                          </span>
                        ) : null}
                        <span className="wb-badge wb-badge--muted">
                          OCR/解析重试次数 (retry_count)：
                          {auditState.retryCount ?? 0}
                        </span>
                      </div>
                      {auditState.history.length === 0 ? (
                        <div className="wb-helper-note">
                          暂无人工作业审计记录（记录首次保存或裁决后自动留痕）
                        </div>
                      ) : (
                        <ul className="wb-audit-list">
                          {auditState.history.map((item, idx) => (
                            <li key={`${item.timestamp}-${idx}`}>
                              <strong>{item.action}</strong> · {item.operator} ·{" "}
                              <span className="wb-mono">{item.timestamp}</span>
                              {item.reason ? ` — ${item.reason}` : ""}
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  )}
                </div>
              ) : null}
            </div>

            {/* Bottom Review Decision & Save Action Bar */}
            <div className="wb-section-box wb-review-footer" aria-label="人工裁决与保存操作栏">
              {recordDirty ? (
                <div className="wb-draft-banner" role="status">
                  <span>
                    请先保存当前未保存修改，或放弃草稿后再执行批准/驳回
                  </span>
                </div>
              ) : null}

              {hasBlockers ? (
                <div className="wb-field-error">
                  存在未解除的 BLOCKER 硬性门禁阻断，人工批准按钮已锁定；请修正以上标红字段/单据总额，或通过“驳回排除”移出正式口径。
                </div>
              ) : null}

              {approvePanelOpen && !recordDirty ? (
                <div className="wb-approve-inline">
                  <div className="wb-field-label">
                    <span>填写批准理由（2–500 字符）</span>
                    <span className="wb-tabular">
                      {countCodePoints(approveReason)} / 500
                    </span>
                  </div>
                  <LumnoInput
                    value={approveReason}
                    placeholder="例如：已核对原始提单与水单无误，确认特例批准入账"
                    aria-label="人工批准理由"
                    onChange={(e) => {
                      setApproveReason(e.target.value);
                      if (!validateReviewReason(e.target.value)) {
                        setApproveReasonError(null);
                      }
                    }}
                  />
                  {approveReasonError ? (
                    <div className="wb-field-error">{approveReasonError}</div>
                  ) : null}
                  <div
                    className="wb-action-row"
                    style={{ justifyContent: "flex-end" }}
                  >
                    <LumnoButton
                      variant="secondary"
                      compact
                      onClick={() => {
                        setApprovePanelOpen(false);
                        setApproveReasonError(null);
                      }}
                    >
                      取消
                    </LumnoButton>
                    <LumnoButton
                      variant="primary"
                      compact
                      icon="ri-check-double-line"
                      busy={approvingOrRejecting}
                      disabled={recordDirty || approvingOrRejecting}
                      onClick={() => void handleConfirmApproveClick()}
                    >
                      确认批准入账
                    </LumnoButton>
                  </div>
                </div>
              ) : null}

              <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                <LumnoPopconfirm
                  title="确认驳回并排除该条费用？"
                  description="驳回后该记录将立即移出 official 与 full_standard 正式导出口径，仍完整保留于 audit 审计导出中。"
                  confirmLabel="确认驳回排除"
                  requireReason
                  reasonPlaceholder="请填写驳回原因（2–500 字符）"
                  defaultReason="非真实计费/确认无效"
                  disabled={
                    recordDirty ||
                    approvingOrRejecting ||
                    savingRecord ||
                    selectedRecord.review_status === "REJECTED"
                  }
                  onConfirm={(reason) => onRejectRecord(selectedRecord, reason)}
                >
                  {(triggerProps) => (
                    <LumnoButton
                      variant="ghost"
                      icon="ri-close-circle-line"
                      title={
                        recordDirty
                          ? "请先保存当前未保存修改，或放弃草稿后再执行批准/驳回"
                          : undefined
                      }
                      disabled={
                        recordDirty ||
                        approvingOrRejecting ||
                        savingRecord ||
                        selectedRecord.review_status === "REJECTED"
                      }
                      {...triggerProps}
                    >
                      驳回排除
                    </LumnoButton>
                  )}
                </LumnoPopconfirm>

                <div className="wb-action-row">
                  <LumnoTooltip
                    content={
                      recordDirty
                        ? "请先保存当前未保存修改，或放弃草稿后再执行批准/驳回"
                        : hasBlockers
                          ? "存在未解决的硬性数值或平账阻断，禁止直接批准"
                          : selectedRecord.review_status !== "NEEDS_REVIEW"
                            ? "仅待复核（NEEDS_REVIEW）状态的记录需要人工批准"
                            : "填写 2–500 字符核对理由后批准入账"
                    }
                  >
                    <LumnoButton
                      variant="secondary"
                      icon="ri-check-double-line"
                      busy={approvingOrRejecting}
                      disabled={
                        recordDirty ||
                        savingRecord ||
                        selectedRecord.review_status !== "NEEDS_REVIEW" ||
                        hasBlockers
                      }
                      onClick={() => setApprovePanelOpen((open) => !open)}
                    >
                      {hasBlockers ? "红灯阻断禁批" : "人工批准"}
                    </LumnoButton>
                  </LumnoTooltip>

                  <LumnoButton
                    variant="primary"
                    icon="ri-save-3-line"
                    busy={savingRecord}
                    disabled={approvingOrRejecting || !diffPreview?.hasChanges}
                    onClick={() => void handleSaveRecordClick()}
                  >
                    保存修订并重验
                  </LumnoButton>
                </div>
              </div>
            </div>
          </>
        )}

        <datalist id="wb-iso-currency-list">
          {COMMON_ISO_CURRENCIES.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>

        <datalist id="wb-charge-dict-list">
          {dictionaryStandardNames.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>

        <input
          ref={replaceFileInputRef}
          type="file"
          style={{ display: "none" }}
          onChange={handleReplaceFilePick}
        />
      </div>
    </>
  );
}
