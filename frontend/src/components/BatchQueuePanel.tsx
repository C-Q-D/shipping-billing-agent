import { useMemo, useRef, useState, type ChangeEvent } from "react";
import type {
  BatchDetail,
  BatchSummary,
  BillingRecordDetail,
} from "../api/client.ts";
import { formatLosslessAmount, readDecimalString } from "../api/decimalJson.ts";
import {
  getRecordPartyDisplayMeta,
  isActiveBatchStage,
  type PendingBatchDetailRecovery,
  type RecordFilterTab,
  type ScopedRecordCounts,
} from "../api/reviewState.ts";
import {
  LumnoButton,
  LumnoInput,
  LumnoSegmentedControl,
} from "./LumnoPrimitives.tsx";

export type { RecordFilterTab };

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
    return { label: "已人工批准", tone: "ok" };
  }
  switch (rec.operation) {
    case "CREATE":
      return { label: "可入账 · 新增", tone: "ok" };
    case "UPDATE":
      return { label: "可入账 · 更新", tone: "info" };
    case "NO_CHANGE":
      return { label: "无变更", tone: "muted" };
    case "PENDING_REVIEW":
      return { label: "待核对", tone: "warn" };
  }
}

interface RecordGroup {
  key: string;
  title: string;
  subtitle: string;
  directionLabel: string;
  postingType: "AR" | "AP" | null;
  missingPartyHint: string | null;
  pendingCount: number;
  records: BillingRecordDetail[];
}

export interface BatchQueuePanelProps {
  batch: BatchDetail | null;
  visibleRecords: ReadonlyArray<BillingRecordDetail>;
  scopedCounts: ScopedRecordCounts;
  batchCounts: ScopedRecordCounts;
  filterTab: RecordFilterTab;
  searchQuery: string;
  activeDocFilterId: string | null;
  selectedRecordId: string | null;
  dirtyRecordIds?: ReadonlySet<string>;
  retryingDocId: string | null;
  pollTransientError?: string | null;
  pendingBatchRecovery?: PendingBatchDetailRecovery | null;
  entityNotice: { count: number; message: string } | null;
  onChangeFilterTab: (tab: RecordFilterTab) => void;
  onChangeSearchQuery: (query: string) => void;
  onResetFilters: () => void;
  onSelectRecord: (recordId: string) => void;
  onJumpToNextPending: () => void;
  onInspectDoc: (docId: string) => void;
  onRetryOrReplaceDoc: (docId: string, file?: File) => void;
  onRefreshBatch?: () => void;
  onRetryPendingBatch?: () => void;
  onDismissEntityNotice: () => void;
  onOpenManualRecord: () => void;
}

export function BatchQueuePanel({
  batch,
  visibleRecords,
  scopedCounts,
  batchCounts,
  filterTab,
  searchQuery,
  activeDocFilterId,
  selectedRecordId,
  dirtyRecordIds,
  retryingDocId,
  pollTransientError = null,
  pendingBatchRecovery = null,
  entityNotice,
  onChangeFilterTab,
  onChangeSearchQuery,
  onResetFilters,
  onSelectRecord,
  onJumpToNextPending,
  onInspectDoc,
  onRetryOrReplaceDoc,
  onRefreshBatch,
  onRetryPendingBatch,
  onDismissEntityNotice,
  onOpenManualRecord,
}: BatchQueuePanelProps) {
  const replaceInputRef = useRef<HTMLInputElement | null>(null);
  const replaceDocIdRef = useRef<string | null>(null);
  const [showStageDetails, setShowStageDetails] = useState(false);

  const docNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const d of batch?.documents ?? []) {
      map.set(d.id, d.sanitized_filename);
    }
    return map;
  }, [batch?.documents]);

  const activeDocName = activeDocFilterId
    ? docNameById.get(activeDocFilterId) ?? null
    : null;

  const hasScopeFilter =
    Boolean(activeDocFilterId) || searchQuery.trim().length > 0;

  const groupedRecords = useMemo<RecordGroup[]>(() => {
    if (visibleRecords.length === 0) return [];
    const groups = new Map<string, RecordGroup>();
    for (const rec of visibleRecords) {
      const refTitle =
        rec.document_info.reference_no?.trim() ||
        rec.shipment_refs?.mbl_nos?.[0]?.value ||
        rec.shipment_refs?.booking_no ||
        docNameById.get(rec.source_doc_id) ||
        rec.document_info.document_type ||
        "未命名单据";
      const partyMeta = getRecordPartyDisplayMeta(rec);
      const key = `${rec.source_doc_id}::${refTitle}`;
      let group = groups.get(key);
      if (!group) {
        group = {
          key,
          title: refTitle,
          subtitle: partyMeta.flowSummary,
          directionLabel: partyMeta.directionLabel,
          postingType: partyMeta.postingType,
          missingPartyHint: partyMeta.missingPartyHint,
          pendingCount: 0,
          records: [],
        };
        groups.set(key, group);
      }
      group.records.push(rec);
      if (
        rec.review_status === "NEEDS_REVIEW" ||
        rec.operation === "PENDING_REVIEW"
      ) {
        group.pendingCount += 1;
      }
    }
    return Array.from(groups.values());
  }, [visibleRecords, docNameById]);

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
  const batchIsActive = batch ? isActiveBatchStage(batch.stage) : false;

  return (
    <>
      <div className="wb-pane-header">
        <h2 className="wb-pane-title">
          <i className="ri-icon ri-list-check-2" aria-hidden="true" />
          <span>账单待办队列</span>
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
              <span>请在左栏拖入多格式账单文件后开始解析</span>
            </div>
          )
        ) : (
          <>
            {/* Top Business Status Summary / Active Stage Progress */}
            {batchIsActive ? (
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
                    正在解析处理中...
                  </span>
                  <span className="wb-helper-note wb-tabular">
                    已就绪 {batch.succeeded_files} 份 · 失败 {batch.failed_files} 份
                  </span>
                </div>
                <div className="wb-stage-steps">
                  {STAGE_STEPS.map((step, idx) => {
                    const stepOrder = idx + 1;
                    let state: "pending" | "active" | "done" | "failed" = "pending";
                    if (currentStageRank > stepOrder) {
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
              </div>
            ) : (
              <div className="wb-queue-summary-bar" aria-label="本批复核进度概览">
                <div style={{ minWidth: 0 }}>
                  <div className="wb-queue-summary-main wb-tabular">
                    <strong>待核 {batchCounts.pending_review} 条</strong>
                    <span className="wb-helper-note">·</span>
                    <span>可正式导出 {batchCounts.exportable_official} 条</span>
                    {batch.failed_files > 0 ? (
                      <>
                        <span className="wb-helper-note">·</span>
                        <span style={{ color: "var(--wb-error-text)", fontWeight: 600 }}>
                          异常文件 {batch.failed_files} 份
                        </span>
                      </>
                    ) : null}
                  </div>
                  <div className="wb-helper-note">
                    记账公司：{batch.accounting_entity}
                  </div>
                </div>
                <div style={{ display: "inline-flex", gap: "6px", flexWrap: "wrap", flexShrink: 0 }}>
                  {batchCounts.pending_review > 0 ? (
                    <LumnoButton
                      variant="primary"
                      compact
                      icon="ri-focus-3-line"
                      onClick={onJumpToNextPending}
                    >
                      定位下一条待核
                    </LumnoButton>
                  ) : null}
                  <LumnoButton
                    variant="ghost"
                    compact
                    onClick={() => setShowStageDetails((v) => !v)}
                  >
                    {showStageDetails ? "收起阶段" : "处理阶段"}
                  </LumnoButton>
                </div>
              </div>
            )}

            {!batchIsActive && showStageDetails ? (
              <div className="wb-stage-bar" aria-label="解析阶段详情">
                <div className="wb-stage-steps">
                  {STAGE_STEPS.map((step) => (
                    <div
                      key={step.key}
                      className="wb-stage-step"
                      data-state={
                        batch.stage === "FAILED" && step.key === "COMPLETED"
                          ? "failed"
                          : "done"
                      }
                    >
                      {step.label}
                    </div>
                  ))}
                </div>
                {batch.stage_message ? (
                  <div className="wb-helper-note">{batch.stage_message}</div>
                ) : null}
              </div>
            ) : null}

            {pollTransientError ? (
              <div
                className="wb-alert-item"
                data-severity="WARNING"
                role="status"
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

            {entityNotice ? (
              <div className="wb-notice-banner" role="status">
                <span>{entityNotice.message}</span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-close-line"
                  aria-label="关闭公司切换提醒"
                  onClick={onDismissEntityNotice}
                />
              </div>
            ) : null}

            {/* Pinned FailedDocumentAlert cards */}
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
                          <span>识别失败：{failDoc.filename}</span>
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
                            ? "该附件暂未提取出费用行，同批其余正常单据结果已保留，可重试或替换恢复。"
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
                            查看与修理
                          </LumnoButton>
                          <LumnoButton
                            variant="secondary"
                            compact
                            icon="ri-refresh-line"
                            busy={retryingDocId === failDoc.doc_id}
                            onClick={() => onRetryOrReplaceDoc(failDoc.doc_id)}
                          >
                            重试
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

            {/* Search & Scoped Filter Tabs */}
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", flexShrink: 0 }}>
              <div className="wb-search-row">
                <i
                  className="ri-icon ri-icon--compact ri-search-line wb-search-icon"
                  aria-hidden="true"
                />
                <LumnoInput
                  id="wb-queue-search"
                  name="queue_search"
                  className="wb-search-input"
                  value={searchQuery}
                  placeholder="搜索单据号、提单号、箱号、费用或交易方..."
                  aria-label="搜索账单队列"
                  onChange={(e) => onChangeSearchQuery(e.target.value)}
                />
                {searchQuery.trim().length > 0 ? (
                  <LumnoButton
                    variant="ghost"
                    compact
                    icon="ri-close-line"
                    aria-label="清除搜索"
                    onClick={() => onChangeSearchQuery("")}
                  />
                ) : null}
              </div>

              <LumnoSegmentedControl
                value={filterTab}
                wrap
                ariaLabel="按复核状态筛选账单"
                options={[
                  { value: "all", label: "全部", count: scopedCounts.all },
                  {
                    value: "pending_review",
                    label: "待复核",
                    count: scopedCounts.pending_review,
                  },
                  { value: "create", label: "新增", count: scopedCounts.create },
                  { value: "update", label: "更新", count: scopedCounts.update },
                  {
                    value: "no_change",
                    label: "无变化",
                    count: scopedCounts.no_change,
                  },
                  {
                    value: "rejected",
                    label: "已驳回",
                    count: scopedCounts.rejected,
                  },
                ]}
                onChange={onChangeFilterTab}
              />

              {hasScopeFilter ? (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "6px",
                    flexWrap: "wrap",
                  }}
                >
                  <span className="wb-badge wb-badge--info">
                    {activeDocName ? `文件：${activeDocName}` : "搜索筛选"} ·
                    本范围 {scopedCounts.all} 条 / 全批 {batch.records.length} 条
                  </span>
                  <LumnoButton variant="ghost" compact onClick={onResetFilters}>
                    显示全部单据
                  </LumnoButton>
                </div>
              ) : null}
            </div>

            {/* Grouped Record List */}
            {groupedRecords.length === 0 ? (
              <div className="wb-empty-state lumno-paper-card">
                <span>
                  {hasScopeFilter || filterTab !== "all"
                    ? "当前文件或筛选条件下暂无匹配账单明细"
                    : "本批次暂未提取出账单明细"}
                </span>
                {hasScopeFilter || filterTab !== "all" ? (
                  <LumnoButton
                    variant="secondary"
                    compact
                    onClick={onResetFilters}
                  >
                    清除筛选条件
                  </LumnoButton>
                ) : null}
              </div>
            ) : (
              <div className="wb-record-groups" role="listbox" aria-label="账单明细列表">
                {groupedRecords.map((group) => (
                  <div key={group.key} className="wb-record-group">
                    <div className="wb-record-group-header">
                      <div style={{ minWidth: 0 }}>
                        <div className="wb-record-group-title">
                          <span className="wb-mono">{group.title}</span>
                          <span
                            className={`wb-badge ${
                              group.postingType === "AR"
                                ? "wb-badge--ok"
                                : group.postingType === "AP"
                                  ? "wb-badge--info"
                                  : "wb-badge--warn"
                            }`}
                          >
                            {group.directionLabel}
                          </span>
                          {group.missingPartyHint ? (
                            <span className="wb-badge wb-badge--warn">
                              {group.missingPartyHint}
                            </span>
                          ) : null}
                        </div>
                        <div className="wb-helper-note" style={{ overflowWrap: "anywhere" }}>
                          {group.subtitle}
                        </div>
                      </div>
                      <span
                        className={`wb-badge wb-tabular ${
                          group.pendingCount > 0
                            ? "wb-badge--warn"
                            : "wb-badge--muted"
                        }`}
                      >
                        {group.records.length} 条
                        {group.pendingCount > 0
                          ? ` · ${group.pendingCount} 待核`
                          : " · 已就绪"}
                      </span>
                    </div>

                    <div className="wb-record-list">
                      {group.records.map((rec) => {
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
                              {rec.charge_details.payment_term ? (
                                <span className="wb-badge wb-badge--muted">
                                  {rec.charge_details.payment_term === "PP"
                                    ? "预付 PP"
                                    : "到付 CC"}
                                </span>
                              ) : null}
                              {rec.match_level !== "NONE" ? (
                                <span className="wb-badge wb-badge--muted">
                                  已匹配旧账 ({rec.match_level})
                                </span>
                              ) : null}
                              {blockers > 0 ? (
                                <span className="wb-badge wb-badge--error wb-tabular">
                                  暂不能入账 ×{blockers}
                                </span>
                              ) : null}
                              {warnings > 0 ? (
                                <span className="wb-badge wb-badge--warn wb-tabular">
                                  请核对 ×{warnings}
                                </span>
                              ) : null}
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}

            <input
              id="wb-queue-replace-doc"
              name="queue_replace_doc_file"
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
