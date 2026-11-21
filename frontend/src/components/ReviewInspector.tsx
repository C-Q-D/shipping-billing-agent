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
  CONTEXT_INFERRED_WARNING_GUIDANCE,
  countCodePoints,
  ENTITY_NOT_MATCHED_GUIDANCE,
  extractServerRecordFormValues,
  formatOcrChannelLabel,
  getAlertFriendlyTitle,
  getConfirmedRecordFields,
  getEffectiveDocTotalsValues,
  getEffectiveRecordFormValues,
  getFieldFriendlyLabel,
  getRecordPartyDisplayMeta,
  groupExcludedClauses,
  hasDocTotalsDraft,
  hasRecordDraft,
  hasStaleConfirmedFields,
  resolveAlertFocusTargetKey,
  resolveRecordAlertActions,
  sortValidationAlerts,
  splitRecordAiReasoning,
  validateReviewReason,
  withPendingRecordConfirmation,
  type AlertActionButtonSpec,
  type ConfirmedFieldMap,
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
  hasActiveScopeFilter?: boolean;
  previewOpen?: boolean;
  previewDocId?: string | null;
  previewPage?: number | null;
  recordIndex?: number;
  totalVisibleRecords?: number;
  pendingRecordCount?: number;
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
  onResetFilters?: () => void;
  onClearInspectedDoc: () => void;
  onOpenPreview?: (docId: string, page?: number | null) => void;
  onClosePreview?: () => void;
  onStepPrevRecord?: () => void;
  onStepNextRecord?: () => void;
  onStepNextPendingRecord?: () => void;
  onUpdateRecordField: <K extends keyof RecordFormValues>(
    record: BillingRecordDetail,
    field: K,
    value: RecordFormValues[K]
  ) => void;
  onConfirmRecordFields?: (
    record: BillingRecordDetail,
    fields: ReadonlyArray<string>
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

const SECONDARY_RECORD_FIELDS = new Set([
  "quantity",
  "unit_price",
  "document_type",
  "mbl_no",
  "mbl_nos",
  "hbl_no",
  "booking_no",
  "container_nos",
  "container_nos_text",
  "matched_existing_bill_id",
  "force_create_new",
]);

type ProvenanceKind =
  | "draft"
  | "confirmed_draft"
  | "override"
  | "inherited"
  | "extracted";

function getProvenanceMeta(kind: ProvenanceKind): {
  label: string;
  className: string;
} {
  switch (kind) {
    case "draft":
      return { label: "未保存", className: "wb-badge wb-badge--warn" };
    case "confirmed_draft":
      return { label: "待保存确认", className: "wb-badge wb-badge--warn" };
    case "override":
      return { label: "人工修订", className: "wb-badge wb-badge--info" };
    case "inherited":
      return { label: "历史沿用", className: "wb-badge wb-badge--ok" };
    case "extracted":
      return { label: "本单提取", className: "wb-badge wb-badge--muted" };
  }
}

function getFieldProvenance(
  fieldKey: string,
  formKey: keyof RecordFormValues,
  record: BillingRecordDetail,
  effective: RecordFormValues,
  server: RecordFormValues,
  confirmedFields?: ConfirmedFieldMap
): ProvenanceKind {
  if (effective[formKey] !== server[formKey]) {
    return "draft";
  }
  if (confirmedFields && Boolean(confirmedFields[fieldKey])) {
    return "confirmed_draft";
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
  if (val === null || val === undefined) return "空";
  if (Array.isArray(val)) return val.join(", ") || "空";
  return String(val);
}

function getReviewStatusBadgeLabel(status: BillingRecordDetail["review_status"]): string {
  switch (status) {
    case "AUTO_READY":
      return "已自动就绪";
    case "NEEDS_REVIEW":
      return "待复核";
    case "APPROVED":
      return "已人工批准";
    case "REJECTED":
      return "已驳回";
  }
}

function getSeverityBadgeLabel(severity: ValidationAlert["severity"]): string {
  switch (severity) {
    case "BLOCKER":
      return "暂不能入账";
    case "WARNING":
      return "请核对";
    case "RESOLVED":
      return "已处理";
  }
}

export function ReviewInspector({
  batch,
  selectedRecord,
  inspectedDocId,
  hasActiveScopeFilter = false,
  previewOpen = false,
  previewDocId = null,
  recordIndex = -1,
  totalVisibleRecords = 0,
  pendingRecordCount = 0,
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
  onResetFilters,
  onClearInspectedDoc,
  onOpenPreview,
  onClosePreview,
  onStepPrevRecord,
  onStepNextRecord,
  onStepNextPendingRecord,
  onUpdateRecordField,
  onConfirmRecordFields,
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
  const approveReasonInputRef = useRef<HTMLInputElement | null>(null);
  const fieldRefs = useRef<Record<string, HTMLElement | null>>({});
  const lastAuditStampRef = useRef<string | null>(null);

  const [highlightedField, setHighlightedField] = useState<string | null>(null);
  const [alertActionFeedback, setAlertActionFeedback] = useState<string | null>(
    null
  );
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

  const [secondaryFieldsOpen, setSecondaryFieldsOpen] = useState(false);
  const [resolvedAlertsExpanded, setResolvedAlertsExpanded] = useState(false);
  const [excludedExpanded, setExcludedExpanded] = useState(false);
  const [docRepairExpanded, setDocRepairExpanded] = useState(false);
  const [auditExpanded, setAuditExpanded] = useState(false);

  const [retryOcrMode, setRetryOcrMode] = useState<OcrMode>(
    "REMOTE_WITH_LOCAL_FALLBACK"
  );
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
    setAlertActionFeedback(null);
    setApprovePanelOpen(false);
    setApproveReason("");
    setApproveReasonError(null);
    setResolvedAlertsExpanded(false);
    setExcludedExpanded(false);
    setAuditExpanded(false);
    lastAuditStampRef.current = null;
    setHighlightedField(null);

    setSecondaryFieldsOpen(false);
    if (selectedRecord) {
      const hasDocTotalAlert = (selectedRecord.validation_alerts ?? []).some(
        (a) =>
          a.severity !== "RESOLVED" &&
          (a.code === "AMOUNT_SUBTOTAL_TAX_MISMATCH" ||
            a.code === "AMOUNT_TOTAL_MISMATCH" ||
            a.code === "MULTI_CURRENCY_TOTAL_AMBIGUITY")
      );
      setDocRepairExpanded(hasDocTotalAlert);
    } else {
      setDocRepairExpanded(false);
    }
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
      ? hasRecordDraft(draftStore, batch.id, selectedRecord.id, selectedRecord)
      : false;

  useEffect(() => {
    if (activeRecordDirty) {
      setApprovePanelOpen(false);
      setApproveReasonError(null);
    }
  }, [activeRecordDirty]);

  useEffect(() => {
    if (
      !batch ||
      !selectedRecord ||
      savingRecord ||
      !hasStaleConfirmedFields(draftStore, batch.id, selectedRecord)
    ) {
      return;
    }
    setAlertActionFeedback(
      "记录已在服务端更新，先前的待确认项已自动失效，请按最新内容重新核对。"
    );
    if (
      !hasRecordDraft(draftStore, batch.id, selectedRecord.id, selectedRecord)
    ) {
      onResetRecordDraft(selectedRecord.id);
    }
  }, [batch, selectedRecord, draftStore, savingRecord, onResetRecordDraft]);

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

  const clearFieldError = (fieldKey: string) => {
    setRecordValidationErrors((prev) => {
      if (!(fieldKey in prev)) return prev;
      const next = { ...prev };
      delete next[fieldKey];
      return next;
    });
  };

  const focusField = (fieldKey: string) => {
    const resolvedKey =
      resolveAlertFocusTargetKey(fieldKey, Boolean(recordSourceDoc)) ??
      (fieldKey === "container_nos" ? "container_nos_text" : fieldKey);
    if (SECONDARY_RECORD_FIELDS.has(resolvedKey)) {
      setSecondaryFieldsOpen(true);
    }
    if (resolvedKey.startsWith("doc_")) {
      setDocRepairExpanded(true);
    }
    window.setTimeout(() => {
      const el = fieldRefs.current[resolvedKey];
      if (!el) return;
      el.scrollIntoView?.({ behavior: "smooth", block: "center" });
      if (
        el.tagName === "INPUT" ||
        el.tagName === "TEXTAREA" ||
        el.tagName === "SELECT" ||
        el.tagName === "BUTTON"
      ) {
        el.focus?.();
      } else {
        const inner = el.querySelector?.("button, input, textarea, select") as
          | HTMLElement
          | null
          | undefined;
        inner?.focus?.();
      }
      setHighlightedField(resolvedKey);
      window.setTimeout(() => {
        setHighlightedField((curr) => (curr === resolvedKey ? null : curr));
      }, 1500);
    }, 0);
  };

  const handleAlertActionButtonClick = (spec: AlertActionButtonSpec) => {
    if (!batch || !selectedRecord) return;
    const currentFormValues = getEffectiveRecordFormValues(
      selectedRecord,
      draftStore,
      batch.id
    );

    if (spec.missingRequiredCheck) {
      const { focusKey, fieldKey, errorMessage, feedbackMessage } =
        spec.missingRequiredCheck;
      setRecordValidationErrors((prev) => ({
        ...prev,
        [fieldKey]: errorMessage,
      }));
      setAlertActionFeedback(feedbackMessage);
      focusField(focusKey);
      return;
    }

    setRecordValidationErrors((prev) => {
      const next = { ...prev };
      for (const cf of spec.confirmFields) {
        delete next[cf];
      }
      if (spec.updateField) {
        delete next[spec.updateField.formKey];
      }
      return next;
    });

    if (spec.updateField) {
      const { formKey, value } = spec.updateField;
      if (onConfirmRecordFields && spec.confirmFields.length > 0) {
        onUpdateRecordField(selectedRecord, formKey, value as never);
        onConfirmRecordFields(selectedRecord, spec.confirmFields);
      } else if (spec.confirmFields.length > 0) {
        withPendingRecordConfirmation(
          batch.id,
          selectedRecord.id,
          spec.confirmFields,
          () => {
            onUpdateRecordField(selectedRecord, formKey, value as never);
          }
        );
      } else {
        onUpdateRecordField(selectedRecord, formKey, value as never);
      }
    } else if (spec.confirmFields.length > 0) {
      if (onConfirmRecordFields) {
        onConfirmRecordFields(selectedRecord, spec.confirmFields);
      } else {
        const anchorKey: keyof RecordFormValues =
          spec.confirmFields[0] === "container_nos"
            ? "container_nos_text"
            : (spec.confirmFields[0] as keyof RecordFormValues);
        withPendingRecordConfirmation(
          batch.id,
          selectedRecord.id,
          spec.confirmFields,
          () => {
            onUpdateRecordField(
              selectedRecord,
              anchorKey,
              currentFormValues[anchorKey] as never
            );
          }
        );
      }
    }

    setAlertActionFeedback(spec.feedbackMessage);
    if (
      !spec.updateField &&
      spec.confirmFields.length === 0 &&
      !hasRecordDraft(draftStore, batch.id, selectedRecord.id, selectedRecord) &&
      selectedRecord.review_status === "NEEDS_REVIEW" &&
      !(selectedRecord.validation_alerts ?? []).some(
        (a) => a.severity === "BLOCKER"
      )
    ) {
      setApprovePanelOpen(true);
      window.setTimeout(() => {
        focusField("approve_reason");
      }, 0);
      return;
    }
    focusField(spec.focusFieldKey);
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
    setAlertActionFeedback(null);
    await onSaveRecordPatch(selectedRecord);
  };

  const handleConfirmApproveClick = async () => {
    if (!batch || !selectedRecord) return;
    if (
      hasRecordDraft(draftStore, batch.id, selectedRecord.id, selectedRecord)
    ) {
      setApproveReasonError("请先保存当前未保存修改，或放弃草稿后再批准");
      return;
    }
    const effectiveReason = approveReasonInputRef.current?.value ?? approveReason;
    if (effectiveReason !== approveReason) {
      setApproveReason(effectiveReason);
    }
    const err = validateReviewReason(effectiveReason);
    if (err) {
      setApproveReasonError(err);
      return;
    }
    setApproveReasonError(null);
    await onApproveRecord(selectedRecord, effectiveReason.trim());
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
      setRetrySourceError("替换文件与公网链接互斥，请仅保留其中一项");
      return;
    }
    if (trimmedUrl && !isValidPublicHttpUrl(trimmedUrl)) {
      setRetrySourceError("链接格式无效，须以 http:// 或 https:// 开头");
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

  const renderDocRepairSection = (
    doc: SourceDocumentSummary,
    isStandaloneDocView: boolean
  ) => {
    if (!batch) return null;
    const docValues = getEffectiveDocTotalsValues(doc, draftStore, batch.id);
    const isDocDirty = hasDocTotalsDraft(draftStore, batch.id, doc.id);
    const docDiff = buildDocTotalsDiffPatch(doc, draftStore, batch.id);
    const isRetrying = retryingDocId === doc.id;
    const isForcing = forcingDocId === doc.id;
    const needsAttention =
      doc.status === "FAILED" ||
      doc.status === "SKIPPED_NOISE" ||
      isDocDirty ||
      isStandaloneDocView;
    const expanded = needsAttention || docRepairExpanded;

    return (
      <div className="wb-section-box" aria-label="单据合计核对与文件修理">
        <div className="wb-field-label">
          <span style={{ overflowWrap: "anywhere" }}>
            {isStandaloneDocView
              ? `单据处理与合计核对：${doc.sanitized_filename}`
              : `核对单据合计与重识别：${doc.sanitized_filename}`}
          </span>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "6px", flexShrink: 0 }}>
            {isDocDirty ? (
              <span className="wb-badge wb-badge--warn">汇总未保存</span>
            ) : null}
            {!needsAttention ? (
              <LumnoButton
                variant="ghost"
                compact
                aria-expanded={expanded}
                onClick={() => setDocRepairExpanded((v) => !v)}
              >
                {expanded ? "收起" : "展开"}
              </LumnoButton>
            ) : null}
            {isStandaloneDocView && selectedRecord ? (
              <LumnoButton
                variant="ghost"
                compact
                icon="ri-close-line"
                onClick={onClearInspectedDoc}
              >
                收起文件详情
              </LumnoButton>
            ) : null}
          </div>
        </div>

        <div className="wb-helper-note">
          识别方式：{formatOcrChannelLabel(doc.extraction_channel)} · 关联费用{" "}
          {doc.affected_record_ids?.length ?? 0} 条
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

        {expanded ? (
          <>
            <div className="wb-form-grid-2">
              <label htmlFor={`wb-doc-subtotal-${doc.id}`} className="wb-form-field">
                <span className="wb-form-label">未税小计</span>
                <LumnoInput
                  id={`wb-doc-subtotal-${doc.id}`}
                  name="subtotal_amount"
                  className={`wb-tabular ${
                    !isStandaloneDocView && highlightedField === "doc_subtotal_amount"
                      ? "wb-field-flash"
                      : ""
                  }`}
                  inputMode="decimal"
                  value={docValues.subtotal_amount}
                  placeholder="如 1410.40"
                  ref={(node) => {
                    if (!isStandaloneDocView) {
                      fieldRefs.current.doc_subtotal_amount = node;
                    }
                  }}
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

              <label htmlFor={`wb-doc-tax-${doc.id}`} className="wb-form-field">
                <span className="wb-form-label">税额</span>
                <LumnoInput
                  id={`wb-doc-tax-${doc.id}`}
                  name="tax_amount"
                  className={`wb-tabular ${
                    !isStandaloneDocView && highlightedField === "doc_tax_amount"
                      ? "wb-field-flash"
                      : ""
                  }`}
                  inputMode="decimal"
                  value={docValues.tax_amount}
                  placeholder="如 0.00"
                  ref={(node) => {
                    if (!isStandaloneDocView) {
                      fieldRefs.current.doc_tax_amount = node;
                    }
                  }}
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

              <label htmlFor={`wb-doc-total-${doc.id}`} className="wb-form-field">
                <span className="wb-form-label">单据总计</span>
                <LumnoInput
                  id={`wb-doc-total-${doc.id}`}
                  name="total_amount"
                  className={`wb-tabular ${
                    !isStandaloneDocView && highlightedField === "doc_total_amount"
                      ? "wb-field-flash"
                      : ""
                  }`}
                  inputMode="decimal"
                  value={docValues.total_amount}
                  placeholder="如 1410.40"
                  ref={(node) => {
                    if (!isStandaloneDocView) {
                      fieldRefs.current.doc_total_amount = node;
                    }
                  }}
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

              <label htmlFor={`wb-doc-currency-${doc.id}`} className="wb-form-field">
                <span className="wb-form-label">汇总币种</span>
                <LumnoInput
                  id={`wb-doc-currency-${doc.id}`}
                  name="total_currency"
                  className={`wb-mono ${
                    !isStandaloneDocView && highlightedField === "doc_total_currency"
                      ? "wb-field-flash"
                      : ""
                  }`}
                  maxLength={3}
                  list="wb-iso-currency-list"
                  value={docValues.total_currency}
                  placeholder="USD"
                  ref={(node) => {
                    if (!isStandaloneDocView) {
                      fieldRefs.current.doc_total_currency = node;
                    }
                  }}
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

            <label htmlFor={`wb-doc-review-note-${doc.id}`} className="wb-form-field">
              <span className="wb-form-label">单据合计修订说明</span>
              <LumnoInput
                id={`wb-doc-review-note-${doc.id}`}
                name="doc_review_note"
                value={docValues.review_note}
                placeholder="例如：修正单据印章识别的应付总计"
                onChange={(e) =>
                  onUpdateDocField(doc, "review_note", e.target.value)
                }
              />
            </label>

            <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
              {isDocDirty ? (
                <LumnoButton
                  variant="ghost"
                  compact
                  onClick={() => onResetDocDraft(doc.id)}
                >
                  重置草稿
                </LumnoButton>
              ) : (
                <span />
              )}
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

            <div className="wb-form-divider" />

            <div className="wb-doc-repair-controls">
              <div className="wb-field-label">
                <span>重新识别 / 替换原件</span>
              </div>
              <div className="wb-form-row-wrap">
                <LumnoSegmentedControl
                  value={retryOcrMode}
                  ariaLabel="单文件重新识别方式"
                  options={[
                    {
                      value: "REMOTE_WITH_LOCAL_FALLBACK",
                      label: "自动（含本地识别）",
                    },
                    { value: "REMOTE_ONLY", label: "仅远端识别" },
                  ]}
                  onChange={setRetryOcrMode}
                />
                <LumnoInput
                  id={`wb-doc-retry-url-${doc.id}`}
                  name="retry_public_url"
                  value={retryPublicUrl}
                  placeholder="可选：填写单据链接重试（输入链接将清空待传替换文件）"
                  aria-label="补充单据链接重试"
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
                    强制识别此文件
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
                      ? "使用链接重试"
                      : "重新识别"}
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
          </>
        ) : null}
      </div>
    );
  };

  if (!batch) {
    return (
      <>
        <div className="wb-pane-header">
          <h2 className="wb-pane-title">
            <i className="ri-icon ri-file-search-line" aria-hidden="true" />
            <span>费用详情与核对</span>
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
            <span>请在左栏拖入单据文件开始解析</span>
          </div>
        </div>
      </>
    );
  }

  const recordDirty = selectedRecord
    ? hasRecordDraft(draftStore, batch.id, selectedRecord.id, selectedRecord)
    : false;
  const serverValues = selectedRecord
    ? extractServerRecordFormValues(selectedRecord)
    : null;
  const formValues = selectedRecord
    ? getEffectiveRecordFormValues(selectedRecord, draftStore, batch.id)
    : null;
  const confirmedFields = selectedRecord
    ? getConfirmedRecordFields(
        draftStore,
        batch.id,
        selectedRecord.id,
        selectedRecord
      )
    : {};
  const diffPreview = selectedRecord
    ? buildRecordDiffPatch(selectedRecord, draftStore, batch.id)
    : null;

  const sortedAlerts = sortValidationAlerts(selectedRecord?.validation_alerts);
  const blockers = sortedAlerts.filter((a) => a.severity === "BLOCKER");
  const activeWarnings = sortedAlerts.filter((a) => a.severity === "WARNING");
  const inferredWarnings = activeWarnings.filter(
    (a) => a.code === "CONTEXT_INFERRED_FIELD"
  );
  const otherWarnings = activeWarnings.filter(
    (a) => a.code !== "CONTEXT_INFERRED_FIELD"
  );
  const resolvedAlerts = sortedAlerts.filter((a) => a.severity === "RESOLVED");
  const hasBlockers = blockers.length > 0;

  const partyMeta = selectedRecord
    ? getRecordPartyDisplayMeta(selectedRecord)
    : null;
  const reasoningParts = selectedRecord
    ? splitRecordAiReasoning(selectedRecord)
    : { activeReasons: [], archivedReasons: [] };
  const excludedGroups = groupExcludedClauses(recordSourceDoc?.excluded_clauses);
  const rawExcludedCount = excludedGroups.reduce(
    (sum, g) => sum + g.totalCount,
    0
  );

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
      serverValues,
      confirmedFields
    );
    const meta = getProvenanceMeta(kind);
    return (
      <span className="wb-form-label-row">
        <span className="wb-form-label">{title}</span>
        <span className={meta.className}>{meta.label}</span>
      </span>
    );
  };

  const sourceSnippet =
    typeof selectedRecord?.source_evidence?.snippet === "string"
      ? selectedRecord.source_evidence.snippet
      : "";

  const evidencePage =
    typeof selectedRecord?.source_evidence?.page === "number" &&
    Number.isFinite(selectedRecord.source_evidence.page) &&
    selectedRecord.source_evidence.page >= 1
      ? Math.floor(selectedRecord.source_evidence.page)
      : null;

  const targetPreviewDocId =
    selectedRecord?.source_doc_id ?? inspectedDoc?.id ?? null;
  const targetPreviewDoc = targetPreviewDocId
    ? (batch.documents ?? []).find((d) => d.id === targetPreviewDocId) ?? null
    : null;
  const activePreviewDoc = previewDocId
    ? (batch.documents ?? []).find((d) => d.id === previewDocId) ?? null
    : null;
  const isPreviewOpenForTargetDoc = Boolean(
    previewOpen &&
      targetPreviewDocId &&
      (previewDocId === targetPreviewDocId ||
        (targetPreviewDoc?.source_kind.toUpperCase() === "EML" &&
          activePreviewDoc?.parent_doc_id === targetPreviewDocId &&
          activePreviewDoc.source_kind.toUpperCase() === "EML_BODY"))
  );

  const previewToggleLabel = isPreviewOpenForTargetDoc
    ? "收起原件"
    : selectedRecord && evidencePage !== null
      ? `对照原件 · 第 ${evidencePage} 页`
      : "对照原件";

  const handleToggleTargetPreview = () => {
    if (!targetPreviewDocId) return;
    if (isPreviewOpenForTargetDoc) {
      onClosePreview?.();
    } else {
      onOpenPreview?.(targetPreviewDocId, selectedRecord ? evidencePage : null);
    }
  };

  const sourceFileLabel = selectedRecord
    ? typeof selectedRecord.source_evidence?.source_file === "string"
      ? selectedRecord.source_evidence.source_file
      : recordSourceDoc?.sanitized_filename ?? null
    : null;
  const sourceSheetLabel =
    selectedRecord && typeof selectedRecord.source_evidence?.sheet === "string"
      ? `工作表：${selectedRecord.source_evidence.sheet}`
      : null;
  const sourceLineLabel =
    selectedRecord &&
    typeof selectedRecord.source_evidence?.line_number === "number"
      ? `第 ${selectedRecord.source_evidence.line_number} 行`
      : selectedRecord && typeof selectedRecord.source_evidence?.row === "number"
        ? `第 ${selectedRecord.source_evidence.row} 行`
        : null;

  const sourceLocationText = selectedRecord
    ? [
        sourceFileLabel,
        evidencePage !== null ? `第 ${evidencePage} 页` : null,
        sourceSheetLabel,
        sourceLineLabel,
      ]
        .filter(Boolean)
        .join(" · ")
    : "";

  const renderSourceLocationNode = () => {
    if (!selectedRecord) return null;
    const parts: Array<{ key: string; node: React.ReactNode }> = [];
    if (sourceFileLabel) {
      parts.push({ key: "file", node: sourceFileLabel });
    }
    if (evidencePage !== null) {
      parts.push({
        key: "page",
        node: onOpenPreview ? (
          <button
            type="button"
            className="wb-page-jump-btn"
            title={`跳转到原件第 ${evidencePage} 页`}
            onClick={() => onOpenPreview(selectedRecord.source_doc_id, evidencePage)}
          >
            第 {evidencePage} 页
          </button>
        ) : (
          `第 ${evidencePage} 页`
        ),
      });
    }
    if (sourceSheetLabel) {
      parts.push({ key: "sheet", node: sourceSheetLabel });
    }
    if (sourceLineLabel) {
      parts.push({ key: "line", node: sourceLineLabel });
    }
    if (parts.length === 0) return null;
    return (
      <>
        {parts.map((part, idx) => (
          <span key={part.key}>
            {idx > 0 ? " · " : null}
            {part.node}
          </span>
        ))}
      </>
    );
  };

  const renderAlertCard = (alert: ValidationAlert, idx: number) => {
    if (!formValues) return null;
    const resolvedActions = resolveRecordAlertActions(alert, formValues, {
      hasSourceDoc: Boolean(recordSourceDoc),
    });
    const badgeLabel = getSeverityBadgeLabel(alert.severity);
    return (
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
            [{badgeLabel}] {getAlertFriendlyTitle(alert)}
          </strong>
          {resolvedActions.locateButton ||
          resolvedActions.actionButtons.length > 0 ? (
            <div
              style={{
                display: "inline-flex",
                gap: "4px",
                flexWrap: "wrap",
              }}
            >
              {resolvedActions.locateButton ? (
                <LumnoButton
                  variant="ghost"
                  compact
                  onClick={() => {
                    if (resolvedActions.locateButton?.emptyFieldHint) {
                      setAlertActionFeedback(
                        resolvedActions.locateButton.emptyFieldHint
                      );
                    }
                    focusField(resolvedActions.locateButton!.focusFieldKey);
                  }}
                >
                  {resolvedActions.locateButton.label}
                </LumnoButton>
              ) : null}
              {resolvedActions.actionButtons.map((btn, bIdx) => (
                <LumnoButton
                  key={`${btn.kind}-${btn.label}-${bIdx}`}
                  variant="ghost"
                  compact
                  title={btn.title}
                  onClick={() => handleAlertActionButtonClick(btn)}
                >
                  {btn.label}
                </LumnoButton>
              ))}
            </div>
          ) : null}
        </div>

        <div>{alert.message}</div>

        {resolvedActions.helperNote ? (
          <div style={{ fontSize: "11px", opacity: 0.92 }}>
            {resolvedActions.helperNote}
          </div>
        ) : null}

        {alert.expected || alert.actual ? (
          <div className="wb-mono wb-tabular" style={{ fontSize: "11px" }}>
            线索参考: {alert.expected ?? "—"} | 当前值: {alert.actual ?? "—"}
          </div>
        ) : null}

        {alert.severity === "BLOCKER" ? (
          <div style={{ fontSize: "11px", opacity: 0.9 }}>
            需先修正上方对应字段或单据合计后保存重验，或通过「驳回排除」移出正式账单。
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <>
      <div
        className={`wb-pane-header ${
          previewOpen ? "wb-pane-header--stack" : ""
        }`.trim()}
      >
        <div className="wb-pane-header-row">
          <h2 className="wb-pane-title">
            <i className="ri-icon ri-file-search-line" aria-hidden="true" />
            <span>费用详情与核对</span>
          </h2>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
            {onOpenPreview && targetPreviewDocId ? (
              <LumnoButton
                variant={isPreviewOpenForTargetDoc ? "secondary" : "ghost"}
                compact
                icon={isPreviewOpenForTargetDoc ? "ri-eye-off-line" : "ri-eye-line"}
                onClick={handleToggleTargetPreview}
              >
                {previewToggleLabel}
              </LumnoButton>
            ) : null}
            {recordDirty ? (
              <span className="wb-badge wb-badge--warn">有未保存修改</span>
            ) : null}
            {selectedRecord ? (
              <span
                className={`wb-badge ${
                  selectedRecord.review_status === "AUTO_READY" ||
                  selectedRecord.review_status === "APPROVED"
                    ? "wb-badge--ok"
                    : selectedRecord.review_status === "NEEDS_REVIEW"
                      ? "wb-badge--warn"
                      : "wb-badge--muted"
                }`}
              >
                {getReviewStatusBadgeLabel(selectedRecord.review_status)}
              </span>
            ) : null}
          </div>
        </div>

        {previewOpen && onStepPrevRecord && onStepNextRecord ? (
          <div className="wb-inspector-step-bar" aria-label="记录对照步进栏">
            <div className="wb-action-row">
              <LumnoButton
                variant="ghost"
                compact
                disabled={recordIndex <= 0}
                onClick={onStepPrevRecord}
              >
                ◀ 上一条
              </LumnoButton>
              <span className="wb-badge wb-badge--muted wb-tabular">
                第 {recordIndex >= 0 ? recordIndex + 1 : 0} / {totalVisibleRecords} 条
              </span>
              <LumnoButton
                variant="ghost"
                compact
                disabled={
                  recordIndex < 0 || recordIndex >= totalVisibleRecords - 1
                }
                onClick={onStepNextRecord}
              >
                下一条 ▶
              </LumnoButton>
            </div>
            {onStepNextPendingRecord ? (
              <LumnoButton
                variant="secondary"
                compact
                icon="ri-focus-3-line"
                disabled={pendingRecordCount <= 0}
                onClick={onStepNextPendingRecord}
              >
                下一条待核
              </LumnoButton>
            ) : null}
          </div>
        ) : null}
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
                      {getAlertFriendlyTitle({ code: b.code, field: b.field })}：
                      {b.message}
                      {b.expected || b.actual
                        ? `（线索 ${b.expected ?? "—"} / 当前 ${b.actual ?? "—"}）`
                        : ""}
                    </span>
                    {b.code === "ENTITY_NOT_MATCHED" ? (
                      <LumnoButton
                        variant="ghost"
                        compact
                        onClick={() => {
                          setAlertActionFeedback(ENTITY_NOT_MATCHED_GUIDANCE);
                          focusField(
                            !formValues?.issuer.trim()
                              ? "issuer"
                              : !formValues?.posting_type
                                ? "posting_type"
                                : "issuer"
                          );
                        }}
                        style={{ marginLeft: "6px" }}
                      >
                        定位主体与方向
                      </LumnoButton>
                    ) : b.field &&
                      resolveAlertFocusTargetKey(
                        b.field,
                        Boolean(recordSourceDoc)
                      ) !== null ? (
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

        {!selectedRecord || !formValues || !serverValues || !partyMeta ? (
          <div className="wb-empty-state lumno-paper-card" role="status">
            <i
              className="ri-icon ri-article-line"
              style={{ "--ri-size": "28px" } as React.CSSProperties}
              aria-hidden="true"
            />
            <strong>
              {hasActiveScopeFilter
                ? "当前文件或筛选条件下暂无匹配账单记录"
                : "请在中栏选择一条账单记录"}
            </strong>
            <span>
              {hasActiveScopeFilter
                ? "为避免误改其他单据，右栏已清空当前编辑视图；请切换筛选或清除条件。"
                : "选择左侧费用记录后可核对金额、交易双方、原文凭据并保存重验"}
            </span>
            {hasActiveScopeFilter && onResetFilters ? (
              <LumnoButton variant="secondary" compact onClick={onResetFilters}>
                显示全部记录
              </LumnoButton>
            ) : null}
          </div>
        ) : (
          <>
            {recordDirty ? (
              <div className="wb-draft-banner" role="status">
                <span>当前记录有未保存修改</span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-arrow-go-back-line"
                  onClick={() => {
                    setAlertActionFeedback(null);
                    setRecordValidationErrors({});
                    onResetRecordDraft(selectedRecord.id);
                  }}
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
                    票号：
                    <span className="wb-mono">
                      {selectedRecord.document_info.reference_no ??
                        selectedRecord.shipment_refs?.mbl_nos?.[0]?.value ??
                        "未识别单号"}
                    </span>{" "}
                    · 记账公司：{batch.accounting_entity}
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

              <div className="wb-chip-row">
                <span
                  className={`wb-badge ${
                    partyMeta.postingType === "AR"
                      ? "wb-badge--ok"
                      : partyMeta.postingType === "AP"
                        ? "wb-badge--info"
                        : "wb-badge--warn"
                  }`}
                >
                  {partyMeta.directionLabel}
                </span>
                {partyMeta.missingPartyHint ? (
                  <span className="wb-badge wb-badge--warn">
                    {partyMeta.missingPartyHint}
                  </span>
                ) : null}
                <span className="wb-badge wb-badge--muted">
                  {selectedRecord.match_level === "NONE"
                    ? "新开账单（未匹配旧账）"
                    : `匹配旧账 (${selectedRecord.match_level})${
                        selectedRecord.matched_bill_ref
                          ? ` · ${selectedRecord.matched_bill_ref}`
                          : ""
                      }`}
                </span>
              </div>

              {/* Strictly read-only raw_charge_name comparison box */}
              <div className="wb-readonly-box">
                <span className="wb-field-label">
                  <span>原件费用原文 / 代码（只读对照）</span>
                  <span className="wb-badge wb-badge--muted">只读原文</span>
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

            {/* 1. Blockers FIRST + Compact Source Snippet Right Beside */}
            {(blockers.length > 0 ||
              otherWarnings.length > 0 ||
              inferredWarnings.length > 0 ||
              resolvedAlerts.length > 0) && (
              <div className="wb-section-box" aria-label="核对提示与阻断">
                <div className="wb-field-label">
                  <span>
                    核对处理清单
                    {blockers.length > 0
                      ? `（${blockers.length} 项暂不能入账${
                          activeWarnings.length > 0
                            ? `，${activeWarnings.length} 项请核对`
                            : ""
                        }）`
                      : activeWarnings.length > 0
                        ? `（${activeWarnings.length} 项请核对）`
                        : "（均已处理）"}
                  </span>
                </div>

                {alertActionFeedback ? (
                  <div className="wb-notice-banner" role="status">
                    <span>{alertActionFeedback}</span>
                  </div>
                ) : null}

                {/* Compact Source Snippet right beside active blockers/warnings */}
                {(blockers.length > 0 || activeWarnings.length > 0) &&
                sourceSnippet ? (
                  <div className="wb-inline-snippet">
                    <div className="wb-inline-snippet-head">
                      <div className="wb-helper-note">
                        原文凭据摘录
                        {sourceLocationText ? (
                          <>
                            {" · "}
                            {renderSourceLocationNode()}
                          </>
                        ) : null}
                      </div>
                      {onOpenPreview && targetPreviewDocId ? (
                        <LumnoButton
                          variant="ghost"
                          compact
                          icon={
                            isPreviewOpenForTargetDoc
                              ? "ri-eye-off-line"
                              : "ri-eye-line"
                          }
                          onClick={handleToggleTargetPreview}
                        >
                          {previewToggleLabel}
                        </LumnoButton>
                      ) : null}
                    </div>
                    <pre className="wb-evidence-pre wb-evidence-pre--compact">
                      {sourceSnippet}
                    </pre>
                  </div>
                ) : null}

                {/* Red BLOCKER alerts always first */}
                {blockers.map((alert, idx) => renderAlertCard(alert, idx))}

                {/* Non-inferred WARNING alerts */}
                {otherWarnings.map((alert, idx) =>
                  renderAlertCard(alert, blockers.length + idx)
                )}

                {/* Grouped CONTEXT_INFERRED_FIELD alerts */}
                {inferredWarnings.length > 0 ? (
                  <div className="wb-alert-item" data-severity="WARNING">
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
                        [请核对] 上下文自动补全线索（{inferredWarnings.length} 项）
                      </strong>
                      <LumnoButton
                        variant="ghost"
                        compact
                        onClick={() => {
                          setAlertActionFeedback(CONTEXT_INFERRED_WARNING_GUIDANCE);
                          if (
                            !activeRecordDirty &&
                            selectedRecord.review_status === "NEEDS_REVIEW" &&
                            !hasBlockers
                          ) {
                            setApprovePanelOpen(true);
                            window.setTimeout(() => {
                              focusField("approve_reason");
                            }, 0);
                          } else {
                            const firstField = inferredWarnings[0]?.field;
                            if (firstField) focusField(firstField);
                          }
                        }}
                      >
                        查看批准理由
                      </LumnoButton>
                    </div>
                    <div className="wb-inferred-list">
                      {inferredWarnings.map((inf, idx) => {
                        const actions = resolveRecordAlertActions(inf, formValues, {
                          hasSourceDoc: Boolean(recordSourceDoc),
                        });
                        return (
                          <div
                            key={`${inf.field ?? "inf"}-${idx}`}
                            className="wb-inferred-row"
                          >
                            <span>
                              <strong>{getFieldFriendlyLabel(inf.field)}</strong>：
                              <span className="wb-mono">
                                {inf.actual ?? inf.expected ?? "—"}
                              </span>
                              <span className="wb-helper-note">（{inf.message}）</span>
                            </span>
                            <span style={{ display: "inline-flex", gap: "4px" }}>
                              {actions.locateButton ? (
                                <LumnoButton
                                  variant="ghost"
                                  compact
                                  onClick={() =>
                                    focusField(actions.locateButton!.focusFieldKey)
                                  }
                                >
                                  定位
                                </LumnoButton>
                              ) : null}
                              {actions.actionButtons
                                .filter((b) => b.kind === "apply_suggestion")
                                .map((btn, bIdx) => (
                                  <LumnoButton
                                    key={bIdx}
                                    variant="ghost"
                                    compact
                                    onClick={() => handleAlertActionButtonClick(btn)}
                                  >
                                    {btn.label}
                                  </LumnoButton>
                                ))}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                    <div style={{ fontSize: "11px", opacity: 0.9 }}>
                      {CONTEXT_INFERRED_WARNING_GUIDANCE}
                    </div>
                  </div>
                ) : null}

                {/* Collapsed RESOLVED alerts */}
                {resolvedAlerts.length > 0 ? (
                  <div>
                    <LumnoButton
                      variant="ghost"
                      compact
                      icon={
                        resolvedAlertsExpanded
                          ? "ri-arrow-up-s-line"
                          : "ri-arrow-down-s-line"
                      }
                      aria-expanded={resolvedAlertsExpanded}
                      onClick={() => setResolvedAlertsExpanded((v) => !v)}
                    >
                      {resolvedAlertsExpanded
                        ? `收起已处理提示 (${resolvedAlerts.length})`
                        : `已处理提示 (${resolvedAlerts.length} 项)`}
                    </LumnoButton>
                    {resolvedAlertsExpanded ? (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: "6px",
                          marginTop: "6px",
                        }}
                      >
                        {resolvedAlerts.map((alert, idx) =>
                          renderAlertCard(alert, 1000 + idx)
                        )}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )}

            {/* 2. Core Financial Fields & Party Roles */}
            <div className="wb-section-box" aria-label="核心财务与交易双方核对">
              <div className="wb-field-label">
                <span>交易双方与核心账单字段</span>
                <span className="wb-helper-note">支持精确十进制数值</span>
              </div>

              <div className="wb-party-flow-box">
                <div style={{ fontSize: "12px", fontWeight: 600 }}>
                  {partyMeta.flowSummary}
                </div>
                {partyMeta.isPayerMissing ? (
                  <div className="wb-helper-note" style={{ color: "var(--wb-warn-text)" }}>
                    提示：原件写明款付给我方（应收已确定），但单据未列明付款客户名称，请核对「付款方待确认」后再正式入账。
                  </div>
                ) : partyMeta.isPayeeMissing ? (
                  <div className="wb-helper-note" style={{ color: "var(--wb-warn-text)" }}>
                    提示：原件未列明收款签发方，请在下方补充确认。
                  </div>
                ) : null}
              </div>

              <label htmlFor="wb-rec-charge-item" className="wb-form-field">
                {renderLabelWithProvenance("标准费用名称", "charge_item", "charge_item")}
                <LumnoInput
                  id="wb-rec-charge-item"
                  name="charge_item"
                  className={
                    highlightedField === "charge_item" ? "wb-field-flash" : ""
                  }
                  list="wb-charge-dict-list"
                  value={formValues.charge_item}
                  placeholder="如 Handling Charge"
                  ref={(node) => {
                    fieldRefs.current.charge_item = node;
                  }}
                  onChange={(e) => {
                    clearFieldError("charge_item");
                    onUpdateRecordField(
                      selectedRecord,
                      "charge_item",
                      e.target.value
                    );
                  }}
                />
                {recordValidationErrors.charge_item ? (
                  <span className="wb-field-error">
                    {recordValidationErrors.charge_item}
                  </span>
                ) : null}
              </label>

              <div className="wb-form-grid-2">
                <label htmlFor="wb-rec-amount" className="wb-form-field">
                  {renderLabelWithProvenance("金额", "amount", "amount")}
                  <LumnoInput
                    id="wb-rec-amount"
                    name="amount"
                    className={`wb-tabular ${
                      highlightedField === "amount" ? "wb-field-flash" : ""
                    }`}
                    inputMode="decimal"
                    value={formValues.amount}
                    placeholder="如 245.00"
                    ref={(node) => {
                      fieldRefs.current.amount = node;
                    }}
                    onChange={(e) => {
                      clearFieldError("amount");
                      onUpdateRecordField(selectedRecord, "amount", e.target.value);
                    }}
                  />
                  {recordValidationErrors.amount ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.amount}
                    </span>
                  ) : null}
                </label>

                <label htmlFor="wb-rec-currency" className="wb-form-field">
                  {renderLabelWithProvenance("币种", "currency", "currency")}
                  <LumnoInput
                    id="wb-rec-currency"
                    name="currency"
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
                    onChange={(e) => {
                      clearFieldError("currency");
                      onUpdateRecordField(
                        selectedRecord,
                        "currency",
                        e.target.value.toUpperCase()
                      );
                    }}
                  />
                  {recordValidationErrors.currency ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.currency}
                    </span>
                  ) : null}
                </label>

                <div
                  className="wb-form-field"
                  ref={(node) => {
                    fieldRefs.current.posting_type = node;
                  }}
                >
                  {renderLabelWithProvenance("记账方向", "posting_type", "posting_type")}
                  <LumnoSegmentedControl
                    value={formValues.posting_type}
                    ariaLabel="记账方向 AR/AP"
                    options={[
                      { value: "", label: "待定" },
                      { value: "AR", label: "AR 应收" },
                      { value: "AP", label: "AP 应付" },
                    ]}
                    onChange={(val) => {
                      clearFieldError("posting_type");
                      onUpdateRecordField(selectedRecord, "posting_type", val);
                    }}
                  />
                  {recordValidationErrors.posting_type ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.posting_type}
                    </span>
                  ) : null}
                </div>

                <div
                  className="wb-form-field"
                  ref={(node) => {
                    fieldRefs.current.payment_term = node;
                  }}
                >
                  {renderLabelWithProvenance("付款条款", "payment_term", "payment_term")}
                  <LumnoSegmentedControl
                    value={formValues.payment_term}
                    ariaLabel="付款条款"
                    options={[
                      { value: "", label: "未定" },
                      { value: "PP", label: "预付 PP" },
                      { value: "CC", label: "到付 CC" },
                    ]}
                    onChange={(val) => {
                      clearFieldError("payment_term");
                      onUpdateRecordField(selectedRecord, "payment_term", val);
                    }}
                  />
                  {recordValidationErrors.payment_term ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.payment_term}
                    </span>
                  ) : null}
                </div>
              </div>

              <div className="wb-form-grid-2">
                <label htmlFor="wb-rec-issuer" className="wb-form-field">
                  {renderLabelWithProvenance("签发方（收款/开票方）", "issuer", "issuer")}
                  <LumnoInput
                    id="wb-rec-issuer"
                    name="issuer"
                    className={
                      highlightedField === "issuer" ? "wb-field-flash" : ""
                    }
                    value={formValues.issuer}
                    placeholder="开票/收款公司名称"
                    ref={(node) => {
                      fieldRefs.current.issuer = node;
                    }}
                    onChange={(e) => {
                      clearFieldError("issuer");
                      onUpdateRecordField(
                        selectedRecord,
                        "issuer",
                        e.target.value
                      );
                    }}
                  />
                  {recordValidationErrors.issuer ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.issuer}
                    </span>
                  ) : null}
                </label>

                <label htmlFor="wb-rec-recipient" className="wb-form-field">
                  {renderLabelWithProvenance(
                    "接收方（付款/客户方）",
                    "recipient",
                    "recipient"
                  )}
                  <LumnoInput
                    id="wb-rec-recipient"
                    name="recipient"
                    className={
                      highlightedField === "recipient" ? "wb-field-flash" : ""
                    }
                    value={formValues.recipient}
                    placeholder={
                      partyMeta.isPayerMissing
                        ? "付款方待确认（原件未列明）"
                        : "付款/接收公司名称"
                    }
                    ref={(node) => {
                      fieldRefs.current.recipient = node;
                    }}
                    onChange={(e) => {
                      clearFieldError("recipient");
                      onUpdateRecordField(
                        selectedRecord,
                        "recipient",
                        e.target.value
                      );
                    }}
                  />
                  {recordValidationErrors.recipient ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.recipient}
                    </span>
                  ) : null}
                </label>

                <label htmlFor="wb-rec-reference-no" className="wb-form-field">
                  {renderLabelWithProvenance(
                    "单据参考号（账单 / 运单参考）",
                    "reference_no",
                    "reference_no"
                  )}
                  <LumnoInput
                    id="wb-rec-reference-no"
                    name="reference_no"
                    className={`wb-mono ${
                      highlightedField === "reference_no" ? "wb-field-flash" : ""
                    }`}
                    value={formValues.reference_no}
                    placeholder="如 DC-108478 / CMDUCHN4053601"
                    ref={(node) => {
                      fieldRefs.current.reference_no = node;
                    }}
                    onChange={(e) => {
                      clearFieldError("reference_no");
                      onUpdateRecordField(
                        selectedRecord,
                        "reference_no",
                        e.target.value
                      );
                    }}
                  />
                  {recordValidationErrors.reference_no ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.reference_no}
                    </span>
                  ) : null}
                </label>

                <label htmlFor="wb-rec-document-date" className="wb-form-field">
                  {renderLabelWithProvenance("单据日期", "document_date", "document_date")}
                  <LumnoInput
                    id="wb-rec-document-date"
                    name="document_date"
                    className={`wb-mono ${
                      highlightedField === "document_date" ? "wb-field-flash" : ""
                    }`}
                    value={formValues.document_date}
                    placeholder="YYYY-MM-DD"
                    ref={(node) => {
                      fieldRefs.current.document_date = node;
                    }}
                    onChange={(e) => {
                      clearFieldError("document_date");
                      onUpdateRecordField(
                        selectedRecord,
                        "document_date",
                        e.target.value
                      );
                    }}
                  />
                  {recordValidationErrors.document_date ? (
                    <span className="wb-field-error">
                      {recordValidationErrors.document_date}
                    </span>
                  ) : null}
                </label>
              </div>

              {/* Collapsible Secondary Fields & Historical Matching */}
              <div className="wb-collapse-block">
                <button
                  type="button"
                  className="wb-collapse-trigger"
                  aria-expanded={secondaryFieldsOpen}
                  onClick={() => setSecondaryFieldsOpen((v) => !v)}
                >
                  <span>
                    更多字段与运输/历史匹配（数量、单价、提单、箱号、历史关联）
                  </span>
                  <i
                    className={`ri-icon ri-icon--compact ${
                      secondaryFieldsOpen
                        ? "ri-arrow-up-s-line"
                        : "ri-arrow-down-s-line"
                    }`}
                    aria-hidden="true"
                  />
                </button>

                {secondaryFieldsOpen ? (
                  <div className="wb-collapse-body">
                    <div className="wb-form-grid-2">
                      <label htmlFor="wb-rec-quantity" className="wb-form-field">
                        {renderLabelWithProvenance("数量", "quantity", "quantity")}
                        <LumnoInput
                          id="wb-rec-quantity"
                          name="quantity"
                          className={`wb-tabular ${
                            highlightedField === "quantity" ? "wb-field-flash" : ""
                          }`}
                          inputMode="decimal"
                          value={formValues.quantity}
                          placeholder="可空"
                          ref={(node) => {
                            fieldRefs.current.quantity = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("quantity");
                            onUpdateRecordField(
                              selectedRecord,
                              "quantity",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.quantity ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.quantity}
                          </span>
                        ) : null}
                      </label>

                      <label htmlFor="wb-rec-unit-price" className="wb-form-field">
                        {renderLabelWithProvenance("单价", "unit_price", "unit_price")}
                        <LumnoInput
                          id="wb-rec-unit-price"
                          name="unit_price"
                          className={`wb-tabular ${
                            highlightedField === "unit_price"
                              ? "wb-field-flash"
                              : ""
                          }`}
                          inputMode="decimal"
                          value={formValues.unit_price}
                          placeholder="如 0.0025"
                          ref={(node) => {
                            fieldRefs.current.unit_price = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("unit_price");
                            onUpdateRecordField(
                              selectedRecord,
                              "unit_price",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.unit_price ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.unit_price}
                          </span>
                        ) : null}
                      </label>

                      <div className="wb-form-field">
                        {renderLabelWithProvenance(
                          "借贷属性",
                          "document_type",
                          "debit_credit"
                        )}
                        <LumnoSegmentedControl<DebitCreditOption>
                          value={formValues.debit_credit}
                          ariaLabel="借贷属性"
                          options={[
                            { value: "DEBIT", label: "正向账单" },
                            { value: "CREDIT", label: "贷项冲减" },
                          ]}
                          onChange={(val) =>
                            onUpdateRecordField(selectedRecord, "debit_credit", val)
                          }
                        />
                      </div>

                      <label htmlFor="wb-rec-document-type" className="wb-form-field">
                        {renderLabelWithProvenance(
                          "单据类型",
                          "document_type",
                          "document_type"
                        )}
                        <LumnoInput
                          id="wb-rec-document-type"
                          name="document_type"
                          className={
                            highlightedField === "document_type"
                              ? "wb-field-flash"
                              : ""
                          }
                          value={formValues.document_type}
                          placeholder="Invoice / Debit Note / Credit Note"
                          ref={(node) => {
                            fieldRefs.current.document_type = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("document_type");
                            onUpdateRecordField(
                              selectedRecord,
                              "document_type",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.document_type ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.document_type}
                          </span>
                        ) : null}
                      </label>

                      <label htmlFor="wb-rec-mbl-no" className="wb-form-field">
                        {renderLabelWithProvenance("主提单号", "mbl_no", "mbl_no")}
                        <LumnoInput
                          id="wb-rec-mbl-no"
                          name="mbl_no"
                          className={`wb-mono ${
                            highlightedField === "mbl_no" ? "wb-field-flash" : ""
                          }`}
                          value={formValues.mbl_no}
                          placeholder="如 OOLU4055331281"
                          ref={(node) => {
                            fieldRefs.current.mbl_no = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("mbl_no");
                            onUpdateRecordField(
                              selectedRecord,
                              "mbl_no",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.mbl_no ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.mbl_no}
                          </span>
                        ) : null}
                      </label>

                      <label htmlFor="wb-rec-hbl-no" className="wb-form-field">
                        {renderLabelWithProvenance("分提单号", "hbl_no", "hbl_no")}
                        <LumnoInput
                          id="wb-rec-hbl-no"
                          name="hbl_no"
                          className={`wb-mono ${
                            highlightedField === "hbl_no" ? "wb-field-flash" : ""
                          }`}
                          value={formValues.hbl_no}
                          placeholder="如 STE20264055HBL"
                          ref={(node) => {
                            fieldRefs.current.hbl_no = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("hbl_no");
                            onUpdateRecordField(
                              selectedRecord,
                              "hbl_no",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.hbl_no ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.hbl_no}
                          </span>
                        ) : null}
                      </label>

                      <label htmlFor="wb-rec-booking-no" className="wb-form-field">
                        {renderLabelWithProvenance(
                          "订舱号",
                          "booking_no",
                          "booking_no"
                        )}
                        <LumnoInput
                          id="wb-rec-booking-no"
                          name="booking_no"
                          className={`wb-mono ${
                            highlightedField === "booking_no"
                              ? "wb-field-flash"
                              : ""
                          }`}
                          value={formValues.booking_no}
                          placeholder="如 4055331281"
                          ref={(node) => {
                            fieldRefs.current.booking_no = node;
                          }}
                          onChange={(e) => {
                            clearFieldError("booking_no");
                            onUpdateRecordField(
                              selectedRecord,
                              "booking_no",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.booking_no ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.booking_no}
                          </span>
                        ) : null}
                      </label>

                      <label htmlFor="wb-rec-container-nos" className="wb-form-field">
                        {renderLabelWithProvenance(
                          "箱号列表",
                          "container_nos",
                          "container_nos_text"
                        )}
                        <LumnoInput
                          id="wb-rec-container-nos"
                          name="container_nos"
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
                          onChange={(e) => {
                            clearFieldError("container_nos");
                            onUpdateRecordField(
                              selectedRecord,
                              "container_nos_text",
                              e.target.value
                            );
                          }}
                        />
                        {recordValidationErrors.container_nos ? (
                          <span className="wb-field-error">
                            {recordValidationErrors.container_nos}
                          </span>
                        ) : null}
                      </label>
                    </div>

                    {(selectedRecord.shipment_refs?.mbl_nos?.length ?? 0) > 0 ? (
                      <div className="wb-chip-row">
                        <span className="wb-helper-note">多附件提单对照：</span>
                        {selectedRecord.shipment_refs?.mbl_nos?.map((m, idx) => (
                          <span
                            key={`${m.value}-${m.source}-${idx}`}
                            className="wb-badge wb-badge--muted wb-mono"
                          >
                            {m.value} · {m.source}
                          </span>
                        ))}
                      </div>
                    ) : null}

                    {/* Historical matching & diffs */}
                    {(selectedRecord.inherited_fields?.length ?? 0) > 0 ? (
                      <div className="wb-chip-row">
                        <span className="wb-helper-note">沿用历史字段：</span>
                        {(selectedRecord.inherited_fields ?? []).map((fld) => (
                          <span key={fld} className="wb-badge wb-badge--ok">
                            {getFieldFriendlyLabel(fld)}
                          </span>
                        ))}
                      </div>
                    ) : null}

                    {(selectedRecord.field_diffs?.length ?? 0) > 0 ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                        <div className="wb-helper-note">与历史账单字段对比：</div>
                        {selectedRecord.field_diffs?.map((diff) => (
                          <div
                            key={diff.field}
                            className="wb-diff-row"
                            data-changed={diff.changed ? "true" : "false"}
                          >
                            <span>{getFieldFriendlyLabel(diff.field)}</span>
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
                          按全新独立账单入账（解除旧账匹配）
                        </div>
                        <div className="wb-helper-note">
                          开启后不再沿用或更新旧账单，直接按新账单处理
                        </div>
                      </div>
                      <LumnoSwitch
                        id="wb-rec-force-create-new"
                        name="force_create_new"
                        ariaLabel="按全新独立账单入账"
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
                            <label
                              key={cand.id}
                              htmlFor={`wb-cand-${selectedRecord.id}-${cand.id}`}
                              className="wb-candidate-option"
                            >
                              <input
                                id={`wb-cand-${selectedRecord.id}-${cand.id}`}
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
                            当前关联旧账：{selectedRecord.matched_bill_ref ?? "已绑定历史账单"}
                          </div>
                        ) : null}
                      </div>
                    ) : null}

                    <label htmlFor="wb-rec-review-note" className="wb-form-field">
                      {renderLabelWithProvenance(
                        "本次修订备注（写入历史留痕）",
                        "review_note",
                        "review_note"
                      )}
                      <LumnoTextarea
                        id="wb-rec-review-note"
                        name="review_note"
                        value={formValues.review_note}
                        placeholder="仅本次提交写入审计历史，保存后恢复空白"
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
                  </div>
                ) : null}
              </div>
            </div>

            {/* 3. Evidence, AI Reasoning (filtered stale lines) & Deduplicated Excluded Clauses */}
            <div className="wb-section-box" aria-label="原文凭据、判定说明与排除条款">
              <div className="wb-field-label wb-field-label--wrap">
                <span style={{ flexShrink: 0 }}>原文凭据与判定说明</span>
                <div
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "6px",
                    flexWrap: "wrap",
                  }}
                >
                  <span className="wb-helper-note" style={{ overflowWrap: "anywhere" }}>
                    {renderSourceLocationNode() ||
                      recordSourceDoc?.sanitized_filename ||
                      "来源文档"}
                  </span>
                  {onOpenPreview && targetPreviewDocId ? (
                    <LumnoButton
                      variant="ghost"
                      compact
                      icon={
                        isPreviewOpenForTargetDoc
                          ? "ri-eye-off-line"
                          : "ri-eye-line"
                      }
                      onClick={handleToggleTargetPreview}
                    >
                      {previewToggleLabel}
                    </LumnoButton>
                  ) : null}
                </div>
              </div>

              {Array.isArray(selectedRecord.source_evidence?.source_paths) ? (
                <div className="wb-helper-note wb-mono" style={{ overflowWrap: "anywhere" }}>
                  文件位置：
                  {(selectedRecord.source_evidence.source_paths as unknown[])
                    .map((p) => String(p))
                    .join(" ; ")}
                </div>
              ) : null}

              <pre className="wb-evidence-pre">
                {sourceSnippet || "暂无片段摘要"}
              </pre>

              <div className="wb-field-label" style={{ marginTop: "4px" }}>
                <span>判定依据</span>
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
                {reasoningParts.activeReasons.length > 0 ? (
                  reasoningParts.activeReasons.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))
                ) : (
                  <li className="wb-helper-note">暂无补充说明</li>
                )}
              </ul>

              {excludedGroups.length > 0 ? (
                <div className="wb-collapse-block">
                  <button
                    type="button"
                    className="wb-collapse-trigger"
                    aria-expanded={excludedExpanded}
                    onClick={() => setExcludedExpanded((v) => !v)}
                  >
                    <span>
                      已排除 {excludedGroups.length} 类非计费条款（共{" "}
                      {rawExcludedCount} 条原文）
                    </span>
                    <i
                      className={`ri-icon ri-icon--compact ${
                        excludedExpanded
                          ? "ri-arrow-up-s-line"
                          : "ri-arrow-down-s-line"
                      }`}
                      aria-hidden="true"
                    />
                  </button>
                  {excludedExpanded ? (
                    <div className="wb-collapse-body">
                      {excludedGroups.map((group) => (
                        <div key={group.reason} className="wb-excluded-group">
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "space-between",
                              gap: "6px",
                            }}
                          >
                            <strong style={{ fontSize: "12px" }}>{group.reason}</strong>
                            <span className="wb-badge wb-badge--muted wb-tabular">
                              {group.items.length} 条（原文出现 {group.totalCount} 次）
                            </span>
                          </div>
                          {group.items.map((item, idx) => (
                            <div key={idx} className="wb-helper-note">
                              • {item.text}
                              {item.pages.length > 0
                                ? `（第 ${item.pages.join("/")} 页）`
                                : ""}
                              {item.count > 1 ? ` ×${item.count}` : ""}
                            </div>
                          ))}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="wb-chip-row" style={{ padding: "0 8px 8px" }}>
                      {excludedGroups.map((g) => (
                        <span key={g.reason} className="wb-badge wb-badge--muted wb-tabular">
                          {g.reason} ({g.items.length})
                        </span>
                      ))}
                    </div>
                  )}
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
                  <span>审计与修改记录（按需加载）</span>
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
                    <div className="wb-helper-note">正在读取审计历史...</div>
                  ) : auditState.error ? (
                    <div className="wb-field-error">{auditState.error}</div>
                  ) : (
                    <>
                      <div className="wb-chip-row wb-tabular">
                        {auditState.extractionChannel ? (
                          <span className="wb-badge wb-badge--muted">
                            识别通道：{formatOcrChannelLabel(auditState.extractionChannel)}
                          </span>
                        ) : null}
                        <span className="wb-badge wb-badge--muted">
                          解析重试：{auditState.retryCount ?? 0} 次
                        </span>
                      </div>
                      {reasoningParts.archivedReasons.length > 0 ? (
                        <div className="wb-helper-note">
                          已过时判定说明（已被重验覆盖）：
                          {reasoningParts.archivedReasons.join("；")}
                        </div>
                      ) : null}
                      {auditState.history.length === 0 ? (
                        <div className="wb-helper-note">
                          暂无人工作业审计记录（首次保存或裁决后自动留痕）
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

            {/* 4. Source Document Totals & Repair */}
            {recordSourceDoc ? renderDocRepairSection(recordSourceDoc, false) : null}

            {/* 5. Slim Sticky Bottom Action Bar */}
            <div className="wb-section-box wb-review-footer" aria-label="人工裁决与保存操作栏">
              {approvePanelOpen && !recordDirty ? (
                <div className="wb-approve-inline">
                  <div className="wb-field-label">
                    <label htmlFor="wb-approve-reason">
                      填写批准理由（2–500 字符）
                    </label>
                    <span className="wb-tabular">
                      {countCodePoints(approveReason)} / 500
                    </span>
                  </div>
                  <div className="wb-approve-inline-row">
                    <LumnoInput
                      id="wb-approve-reason"
                      name="approve_reason"
                      value={approveReason}
                      placeholder="例如：已核对原始提单与水单无误，确认放行入账"
                      aria-label="人工批准理由"
                      ref={(node) => {
                        fieldRefs.current.approve_reason = node;
                        approveReasonInputRef.current = node;
                      }}
                      onChange={(e) => {
                        setApproveReason(e.target.value);
                        if (!validateReviewReason(e.target.value)) {
                          setApproveReasonError(null);
                        }
                      }}
                    />
                    <div className="wb-action-row" style={{ flexShrink: 0 }}>
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
                  {approveReasonError ? (
                    <div className="wb-field-error">{approveReasonError}</div>
                  ) : null}
                </div>
              ) : null}

              <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                <LumnoPopconfirm
                  title="确认驳回并排除该条费用？"
                  description="驳回后该记录将移出正式可入账导出，仍保留于审计记录中。"
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
                      compact
                      icon="ri-close-circle-line"
                      title={
                        recordDirty
                          ? "请先保存当前修改或重置草稿"
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
                  {recordDirty ? (
                    <LumnoButton
                      variant="primary"
                      compact
                      icon="ri-save-3-line"
                      busy={savingRecord}
                      disabled={approvingOrRejecting || !diffPreview?.hasChanges}
                      onClick={() => void handleSaveRecordClick()}
                    >
                      保存修订并重验
                    </LumnoButton>
                  ) : hasBlockers ? (
                    <>
                      <LumnoTooltip content="存在未解决的「暂不能入账」红灯，请先修正上方对应字段或单据合计后保存">
                        <LumnoButton
                          variant="secondary"
                          compact
                          disabled
                          icon="ri-error-warning-line"
                        >
                          有 {blockers.length} 项阻断
                        </LumnoButton>
                      </LumnoTooltip>
                      <LumnoButton
                        variant="primary"
                        compact
                        icon="ri-save-3-line"
                        busy={savingRecord}
                        disabled={approvingOrRejecting || !diffPreview?.hasChanges}
                        onClick={() => void handleSaveRecordClick()}
                      >
                        保存修订并重验
                      </LumnoButton>
                    </>
                  ) : selectedRecord.review_status === "AUTO_READY" ? (
                    <>
                      <LumnoTooltip content="当前记录无阻断且无待审提醒，无须再次点击「人工批准」，可直接前往顶部导出可入账账单">
                        <LumnoButton
                          variant="secondary"
                          compact
                          disabled
                          icon="ri-check-line"
                          title="已自动就绪（可直接导出）：无须再次点击「人工批准」"
                        >
                          已自动就绪（可直接导出）
                        </LumnoButton>
                      </LumnoTooltip>
                      <LumnoButton
                        variant="ghost"
                        compact
                        icon="ri-save-3-line"
                        busy={savingRecord}
                        disabled={approvingOrRejecting || !diffPreview?.hasChanges}
                        onClick={() => void handleSaveRecordClick()}
                      >
                        保存修订并重验
                      </LumnoButton>
                    </>
                  ) : selectedRecord.review_status === "APPROVED" ? (
                    <LumnoButton
                      variant="secondary"
                      compact
                      disabled
                      icon="ri-check-double-line"
                    >
                      已人工批准（可直接导出）
                    </LumnoButton>
                  ) : selectedRecord.review_status === "NEEDS_REVIEW" ? (
                    <>
                      <LumnoButton
                        variant="primary"
                        compact
                        icon="ri-check-double-line"
                        busy={approvingOrRejecting}
                        disabled={savingRecord}
                        onClick={() => setApprovePanelOpen((open) => !open)}
                      >
                        人工批准
                      </LumnoButton>
                      <LumnoButton
                        variant="secondary"
                        compact
                        icon="ri-save-3-line"
                        busy={savingRecord}
                        disabled={approvingOrRejecting || !diffPreview?.hasChanges}
                        onClick={() => void handleSaveRecordClick()}
                      >
                        保存修订并重验
                      </LumnoButton>
                    </>
                  ) : null}
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
          id="wb-inspector-replace-file"
          name="replace_document_file"
          ref={replaceFileInputRef}
          type="file"
          style={{ display: "none" }}
          onChange={handleReplaceFilePick}
        />
      </div>
    </>
  );
}
