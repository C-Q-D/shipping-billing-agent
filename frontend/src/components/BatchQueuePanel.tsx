import { useMemo, useRef, type ChangeEvent } from "react";
import type {
  BatchDetail,
  BatchSummary,
  BillingRecordDetail,
} from "../api/client.ts";
import { formatLosslessAmount, readDecimalString } from "../api/decimalJson.ts";
import type { PendingBatchDetailRecovery } from "../api/reviewState.ts";
import { LumnoButton, LumnoSegmentedControl } from "./LumnoPrimitives.tsx";

export type RecordFilterTab =
  | "all"
  | "pending_review"
  | "create"
  | "update"
  | "no_change"
  | "rejected";

const STAGE_STEPS: ReadonlyArray<{
  key: BatchSummary["stage"];
  label: string;
}> = [
  { key: "UNPACKING", label: "拆包" },
  { key: "EXTRACTING", label: "提取" },
  { key: "MATCHING", label: "匹配" },
  { key: "VALIDATING", label: "校验" },
  { key: "COMPLETED", label: "就绪" },
];

const STAGE_ORDER: Record<BatchSummary["stage"], number> = {
  QUEUED: 0,
  UNPACKING: 1,
  EXTRACTING: 2,
  MATCHING: 3,
  VALIDATING: 4,
  COMPLETED: 5,
  FAILED: 5,
};

function getOperationBadge(rec: BillingRecordDetail): {
  label: string;
  tone: "ok" | "info" | "warn" | "error" | "muted";
} {
  if (rec.review_status === "REJECTED") {
    return { label: "已驳回", tone: "muted" };
  }
  if (rec.review_status === "APPROVED") {
    return { label: `${rec.operation} · 已批`, tone: "ok" };
  }
  switch (rec.operation) {
    case "CREATE":
      return { label: "CREATE", tone: "ok" };
    case "UPDATE":
      return { label: "UPDATE", tone: "info" };
    case "NO_CHANGE":
      return { label: "NO_CHANGE", tone: "muted" };
    case "PENDING_REVIEW":
      return { label: "PENDING_REVIEW", tone: "warn" };
  }
}

export interface BatchQueuePanelProps {
  batch: BatchDetail | null;
  filterTab: RecordFilterTab;
  activeDocFilterId: string | null;
  selectedRecordId: string | null;
  dirtyRecordIds?: ReadonlySet<string>;
  retryingDocId: string | null;
  pollTransientError?: string | null;
  pendingBatchRecovery?: PendingBatchDetailRecovery | null;
  entityNotice: { count: number; message: string } | null;
  onChangeFilterTab: (tab: RecordFilterTab) => void;
  onClearDocFilter: () => void;
  onSelectRecord: (recordId: string) => void;
  onInspectDoc: (docId: string) => void;
  onRetryOrReplaceDoc: (docId: string, file?: File) => void;
  onRefreshBatch?: () => void;
  onRetryPendingBatch?: () => void;
  onDismissEntityNotice: () => void;
  onOpenManualRecord: () => void;
}

export function BatchQueuePanel({
  batch,
  filterTab,
  activeDocFilterId,
  selectedRecordId,
  dirtyRecordIds,
  retryingDocId,
  pollTransientError = null,
  pendingBatchRecovery = null,
  entityNotice,
  onChangeFilterTab,
  onClearDocFilter,
  onSelectRecord,
  onInspectDoc,
  onRetryOrReplaceDoc,
  onRefreshBatch,
  onRetryPendingBatch,
  onDismissEntityNotice,
  onOpenManualRecord,
}: BatchQueuePanelProps) {
  const replaceInputRef = useRef<HTMLInputElement | null>(null);
  const replaceDocIdRef = useRef<string | null>(null);

  const filteredRecords = useMemo(() => {
    if (!batch) return [];
    return (batch.records ?? []).filter((rec) => {
      if (activeDocFilterId && rec.source_doc_id !== activeDocFilterId) {
        return false;
      }
      if (filterTab === "all") return true;
      if (filterTab === "rejected") return rec.review_status === "REJECTED";
      if (rec.review_status === "REJECTED") return false;
      if (filterTab === "pending_review") {
        return (
          rec.operation === "PENDING_REVIEW" ||
          rec.review_status === "NEEDS_REVIEW"
        );
      }
      if (filterTab === "create") return rec.operation === "CREATE";
      if (filterTab === "update") return rec.operation === "UPDATE";
      if (filterTab === "no_change") return rec.operation === "NO_CHANGE";
      return true;
    });
  }, [batch, filterTab, activeDocFilterId]);

  const activeDocName = useMemo(() => {
    if (!batch || !activeDocFilterId) return null;
    return (
      (batch.documents ?? []).find((d) => d.id === activeDocFilterId)
        ?.sanitized_filename ?? null
    );
  }, [batch, activeDocFilterId]);

  const handleReplaceFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    const docId = replaceDocIdRef.current;
    replaceDocIdRef.current = null;
    if (docId && file) {
      onRetryOrReplaceDoc(docId, file);
    }
  };

  const currentStageRank = batch ? STAGE_ORDER[batch.stage] : 0;

  return (
    <>
      <div className="wb-pane-header">
        <h2 className="wb-pane-title">
          <i className="ri-icon ri-list-check-2" aria-hidden="true" />
          <span>解析阶段与账单队列</span>
        </h2>
        {batch ? (
          <div style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
            <span className="wb-badge wb-badge--muted wb-tabular">
              {batch.short_code}
            </span>
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-add-line"
              onClick={onOpenManualRecord}
            >
              手工补行
            </LumnoButton>
          </div>
        ) : pendingBatchRecovery ? (
          <span className="wb-badge wb-badge--muted wb-tabular">
            {pendingBatchRecovery.shortCode}
          </span>
        ) : null}
      </div>

      <div className="wb-pane-body">
        {!batch ? (
          pendingBatchRecovery ? (
            pendingBatchRecovery.errorMessage ? (
              <div
                className="wb-alert-item lumno-paper-card"
                data-severity="BLOCKER"
                role="alert"
              >
                <strong>批次详情读取失败</strong>
                <div>
                  批次 {pendingBatchRecovery.shortCode}（ID：
                  <span className="wb-mono">{pendingBatchRecovery.id}</span>
                  ）已创建入队，但首次读取详情失败，请点击下方按钮重试读取（无需重复上传）。
                </div>
                <div className="wb-field-error">
                  {pendingBatchRecovery.errorMessage}
                </div>
                <div className="wb-action-row" style={{ marginTop: "4px" }}>
                  <LumnoButton
                    variant="primary"
                    compact
                    icon="ri-refresh-line"
                    busy={pendingBatchRecovery.loading}
                    onClick={onRetryPendingBatch}
                  >
                    重试读取此批次
                  </LumnoButton>
                </div>
              </div>
            ) : (
              <div className="wb-empty-state lumno-paper-card" role="status">
                <strong>
                  正在读取新建批次 {pendingBatchRecovery.shortCode} 详情…
                </strong>
                <span className="wb-mono wb-helper-note">
                  批次 ID：{pendingBatchRecovery.id}
                </span>
              </div>
            )
          ) : (
            <div className="wb-empty-state lumno-paper-card">
              <i
                className="ri-icon ri-file-list-3-line"
                style={{ "--ri-size": "28px" } as React.CSSProperties}
                aria-hidden="true"
              />
              <strong>当前尚未选中解析批次</strong>
              <span>请在左栏投递多格式单据或点击“一键载入 4 份真样本”</span>
            </div>
          )
        ) : (
          <>
            <div className="wb-stage-bar" aria-label="实时解析阶段">
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "8px",
                  flexWrap: "wrap",
                }}
              >
                <span style={{ fontSize: "12px", fontWeight: 600 }}>
                  阶段：{batch.stage}
                </span>
                <span className="wb-helper-note wb-tabular">
                  成功 {batch.succeeded_files} · 失败 {batch.failed_files}
                </span>
              </div>
              <div className="wb-stage-steps">
                {STAGE_STEPS.map((step, idx) => {
                  const stepOrder = idx + 1;
                  let state: "pending" | "active" | "done" | "failed" = "pending";
                  if (batch.stage === "FAILED" && step.key === "COMPLETED") {
                    state = "failed";
                  } else if (batch.stage === "COMPLETED") {
                    state = "done";
                  } else if (currentStageRank > stepOrder) {
                    state = "done";
                  } else if (currentStageRank === stepOrder) {
                    state = "active";
                  }
                  return (
                    <div
                      key={step.key}
                      className="wb-stage-step"
                      data-state={state}
                    >
                      {step.label}
                    </div>
                  );
                })}
              </div>
              {batch.stage_message ? (
                <div className="wb-helper-note">{batch.stage_message}</div>
              ) : null}
              {pollTransientError ? (
                <div
                  className="wb-alert-item"
                  data-severity="WARNING"
                  role="status"
                  style={{ marginTop: "4px" }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: "8px",
                      flexWrap: "wrap",
                    }}
                  >
                    <span>{pollTransientError}</span>
                    {onRefreshBatch ? (
                      <LumnoButton
                        variant="ghost"
                        compact
                        icon="ri-refresh-line"
                        onClick={onRefreshBatch}
                      >
                        立即刷新
                      </LumnoButton>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </div>

            {entityNotice ? (
              <div className="wb-notice-banner" role="status">
                <span>{entityNotice.message}</span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-close-line"
                  aria-label="关闭主体切换提醒"
                  onClick={onDismissEntityNotice}
                />
              </div>
            ) : null}

            {/* Pinned FailedDocumentAlert cards: always visible at top even with 0 affected records */}
            {(batch.failed_documents?.length ?? 0) > 0 ? (
              <section
                aria-label="失败文档置顶告警"
                style={{ display: "flex", flexDirection: "column", gap: "8px" }}
              >
                {(batch.failed_documents ?? []).map((failDoc) => {
                  const affectedIds = failDoc.affected_record_ids ?? [];
                  return (
                    <div key={failDoc.doc_id} className="wb-failed-doc-card">
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "8px",
                        }}
                      >
                        <span
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "6px",
                            fontWeight: 600,
                            color: "var(--wb-error-text)",
                            minWidth: 0,
                            overflowWrap: "anywhere",
                          }}
                        >
                          <i
                            className="ri-icon ri-icon--compact ri-error-warning-line"
                            aria-hidden="true"
                          />
                          <span>失败附件：{failDoc.filename}</span>
                        </span>
                        <span className="wb-badge wb-badge--error wb-tabular">
                          影响记录 {affectedIds.length} 条
                        </span>
                      </div>

                      <div className="wb-field-error">{failDoc.error_message}</div>

                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "8px",
                          flexWrap: "wrap",
                        }}
                      >
                        <span className="wb-helper-note">
                          {affectedIds.length === 0
                            ? "该附件产出 0 条费用行，同批其余正常单据结果已保留；可直接重试或替换恢复。"
                            : `受影响记录：${affectedIds.length} 条`}
                        </span>
                        <div style={{ display: "inline-flex", gap: "6px" }}>
                          {affectedIds.length > 0 ? (
                            <LumnoButton
                              variant="ghost"
                              compact
                              onClick={() => onSelectRecord(affectedIds[0])}
                            >
                              定位记录
                            </LumnoButton>
                          ) : null}
                          <LumnoButton
                            variant="ghost"
                            compact
                            icon="ri-tools-line"
                            onClick={() => onInspectDoc(failDoc.doc_id)}
                          >
                            详情修理
                          </LumnoButton>
                          <LumnoButton
                            variant="secondary"
                            compact
                            icon="ri-refresh-line"
                            busy={retryingDocId === failDoc.doc_id}
                            onClick={() => onRetryOrReplaceDoc(failDoc.doc_id)}
                          >
                            原地重试
                          </LumnoButton>
                          <LumnoButton
                            variant="primary"
                            compact
                            icon="ri-upload-2-line"
                            onClick={() => {
                              replaceDocIdRef.current = failDoc.doc_id;
                              replaceInputRef.current?.click();
                            }}
                          >
                            替换上传
                          </LumnoButton>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </section>
            ) : null}

            <div style={{ display: "flex", flexDirection: "column", gap: "6px", flexShrink: 0 }}>
              <LumnoSegmentedControl
                value={filterTab}
                wrap
                ariaLabel="按状态筛选账单记录"
                options={[
                  { value: "all", label: "全部", count: (batch.records ?? []).length },
                  {
                    value: "pending_review",
                    label: "待复核",
                    count: batch.counts.pending_review,
                  },
                  { value: "create", label: "新增", count: batch.counts.create },
                  { value: "update", label: "更新", count: batch.counts.update },
                  {
                    value: "no_change",
                    label: "无变化",
                    count: batch.counts.no_change,
                  },
                  {
                    value: "rejected",
                    label: "已驳回",
                    count: batch.counts.rejected,
                  },
                ]}
                onChange={onChangeFilterTab}
              />

              {activeDocName ? (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "6px",
                  }}
                >
                  <span className="wb-badge wb-badge--info">
                    筛选文档：{activeDocName}
                  </span>
                  <LumnoButton variant="ghost" compact onClick={onClearDocFilter}>
                    显示全部文档
                  </LumnoButton>
                </div>
              ) : null}
            </div>

            {filteredRecords.length === 0 ? (
              <div className="wb-empty-state lumno-paper-card">
                <span>当前筛选条件下暂无匹配账单明细</span>
              </div>
            ) : (
              <div className="wb-record-list" role="listbox" aria-label="账单明细列表">
                {filteredRecords.map((rec) => {
                  const selected = rec.id === selectedRecordId;
                  const isDirty = dirtyRecordIds?.has(rec.id) ?? false;
                  const opMeta = getOperationBadge(rec);
                  const blockers = (rec.validation_alerts ?? []).filter(
                    (a) => a.severity === "BLOCKER"
                  ).length;
                  const warnings = (rec.validation_alerts ?? []).filter(
                    (a) => a.severity === "WARNING"
                  ).length;
                  const rawAmountStr = readDecimalString(
                    rec.charge_details,
                    "amount"
                  );
                  const isCredit =
                    (rawAmountStr?.startsWith("-") ?? false) ||
                    (rec.document_info.document_type ?? "")
                      .toLowerCase()
                      .includes("credit");
                  const amountText = formatLosslessAmount(
                    rec.charge_details,
                    "amount"
                  );
                  const mblLabel =
                    rec.shipment_refs?.mbl_nos?.[0]?.value ??
                    rec.shipment_refs?.booking_no ??
                    null;

                  return (
                    <button
                      key={rec.id}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      data-selected={selected ? "true" : "false"}
                      className="wb-record-card"
                      onClick={() => onSelectRecord(rec.id)}
                    >
                      <div className="wb-record-top">
                        <div className="wb-record-name-wrap">
                          <span className="wb-record-name">
                            {rec.charge_details.charge_item ?? "未命名费用"}
                          </span>
                          {rec.charge_details.raw_charge_name ? (
                            <span className="wb-record-sub">
                              ({rec.charge_details.raw_charge_name})
                            </span>
                          ) : null}
                          {isCredit ? (
                            <span className="wb-badge wb-badge--info">贷项</span>
                          ) : null}
                          {isDirty ? (
                            <span className="wb-badge wb-badge--warn">
                              未保存
                            </span>
                          ) : null}
                        </div>
                        <span className="wb-record-amount wb-tabular">
                          {amountText} {rec.charge_details.currency ?? "—"}
                        </span>
                      </div>

                      <div className="wb-record-badges">
                        <span className={`wb-badge wb-badge--${opMeta.tone}`}>
                          {opMeta.label}
                        </span>
                        <span className="wb-badge wb-badge--muted">
                          {rec.financial_attributes.posting_type ?? "待定AR/AP"}
                        </span>
                        {rec.charge_details.payment_term ? (
                          <span className="wb-badge wb-badge--muted">
                            {rec.charge_details.payment_term}
                          </span>
                        ) : null}
                        <span className="wb-badge wb-badge--muted">
                          匹配：{rec.match_level === "NONE" ? "未匹配" : rec.match_level}
                        </span>
                        {blockers > 0 ? (
                          <span className="wb-badge wb-badge--error wb-tabular">
                            BLOCKER ×{blockers}
                          </span>
                        ) : null}
                        {warnings > 0 ? (
                          <span className="wb-badge wb-badge--warn wb-tabular">
                            WARNING ×{warnings}
                          </span>
                        ) : null}
                      </div>

                      <div className="wb-record-footer">
                        <span className="wb-mono">
                          {rec.document_info.reference_no ??
                            mblLabel ??
                            rec.document_info.document_type ??
                            "无独立单号"}
                        </span>
                        <span>·</span>
                        <span>
                          {rec.transaction_party.issuer ?? "未知签发方"} →{" "}
                          {rec.transaction_party.recipient ?? "未知接收方"}
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}

            <input
              ref={replaceInputRef}
              type="file"
              style={{ display: "none" }}
              onChange={handleReplaceFileChange}
            />
          </>
        )}
      </div>
    </>
  );
}
