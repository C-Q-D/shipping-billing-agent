import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  BatchDetail,
  BillingApiClient,
  ExportMode,
  SyncBaselineResult,
} from "../api/client.ts";
import {
  addDecimalStrings,
  formatDecimalLiteral,
  readDecimalString,
  stringifyLosslessJson,
} from "../api/decimalJson.ts";
import {
  beginExportModalLoad,
  completeExportModalLoadFailure,
  completeExportModalLoadSuccess,
  createExportModalCacheState,
  getExportModalActionEligibility,
  getExportResponseForBatchMode,
  resetExportModalForBatch,
  type ExportModalCacheState,
} from "../api/managementHelpers.ts";
import {
  LumnoButton,
  LumnoModal,
  LumnoSegmentedControl,
} from "./LumnoPrimitives.tsx";

export interface SyncResultRecord {
  batchId: string;
  result: SyncBaselineResult;
  syncedAt: string;
}

export interface ExportAndSyncModalProps {
  open: boolean;
  batch: BatchDetail | null;
  batchRevision: number;
  api: BillingApiClient;
  syncingBaseline: boolean;
  lastSyncResult: SyncResultRecord | null;
  onClose: () => void;
  onSyncBaseline: () => Promise<void>;
  onNotify: (message: string, tone?: "info" | "error") => void;
}

const MODE_LABELS: Record<ExportMode, string> = {
  official: "可入账账单",
  full_standard: "含待复核全量",
  audit: "完整审计留痕",
};

export const MODE_SCOPE_LABELS: Record<ExportMode, string> = {
  official: "导出范围为整批可入账/已批准记录",
  full_standard: "导出范围为整批含待复核的非驳回记录",
  audit: "导出范围为整批全部状态记录与完整审计留痕",
};

function getExportOpLabel(op: string): string {
  switch (op) {
    case "CREATE":
      return "新增";
    case "UPDATE":
      return "更新";
    case "NO_CHANGE":
      return "无变化";
    case "PENDING_REVIEW":
      return "待复核";
    default:
      return op;
  }
}

export interface ExportCurrencySummaryItem {
  currency: string;
  count: number;
  validCount: number;
  missingCount: number;
  formattedSum: string | null;
  badgeText: string;
}

export function buildExportCurrencySummary(
  records: ReadonlyArray<{
    charge_details?: {
      amount?: unknown;
      currency?: string | null;
    } | null;
  }>
): ExportCurrencySummaryItem[] {
  if (!records || records.length === 0) {
    return [];
  }
  const grouped = new Map<
    string,
    {
      count: number;
      validCount: number;
      missingCount: number;
      sum: string | null;
    }
  >();

  for (const rec of records) {
    const currency = (rec.charge_details?.currency ?? "").trim() || "未定币种";
    const rawAmt = readDecimalString(rec.charge_details, "amount");
    const trimmedAmt = rawAmt !== null ? rawAmt.trim() : "";
    let validAmt: string | null = null;
    if (trimmedAmt.length > 0) {
      try {
        addDecimalStrings("0", trimmedAmt);
        validAmt = trimmedAmt;
      } catch {
        validAmt = null;
      }
    }

    const prev = grouped.get(currency) ?? {
      count: 0,
      validCount: 0,
      missingCount: 0,
      sum: null,
    };

    if (validAmt === null) {
      grouped.set(currency, {
        count: prev.count + 1,
        validCount: prev.validCount,
        missingCount: prev.missingCount + 1,
        sum: prev.sum,
      });
    } else {
      const nextSum =
        prev.sum === null ? validAmt : addDecimalStrings(prev.sum, validAmt);
      grouped.set(currency, {
        count: prev.count + 1,
        validCount: prev.validCount + 1,
        missingCount: prev.missingCount,
        sum: nextSum,
      });
    }
  }

  return Array.from(grouped.entries()).map(([currency, meta]) => {
    if (meta.validCount === 0 || meta.sum === null) {
      return {
        currency,
        count: meta.count,
        validCount: 0,
        missingCount: meta.missingCount,
        formattedSum: null,
        badgeText: `${currency} 金额待补（${meta.missingCount} 条）`,
      };
    }
    const formattedSum = formatDecimalLiteral(meta.sum, "0.00");
    const badgeText =
      meta.missingCount === 0
        ? `${currency} ${formattedSum}（${meta.count} 条）`
        : `${currency} ${formattedSum}（${meta.validCount} 条已计，${meta.missingCount} 条金额待补）`;
    return {
      currency,
      count: meta.count,
      validCount: meta.validCount,
      missingCount: meta.missingCount,
      formattedSum,
      badgeText,
    };
  });
}

export function ExportAndSyncModal({
  open,
  batch,
  batchRevision,
  api,
  syncingBaseline,
  lastSyncResult,
  onClose,
  onSyncBaseline,
  onNotify,
}: ExportAndSyncModalProps) {
  const [activeMode, setActiveMode] = useState<ExportMode>("official");
  const [cacheState, setCacheState] = useState<ExportModalCacheState>(() =>
    createExportModalCacheState(batch?.id ?? null)
  );
  const [downloading, setDownloading] = useState(false);
  const [advancedExpanded, setAdvancedExpanded] = useState(false);
  const [jsonExpanded, setJsonExpanded] = useState(false);
  const requestSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const loadAllModePreviews = useCallback(
    async (targetBatchId: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = ++requestSeqRef.current;

      setCacheState((prev) => beginExportModalLoad(prev, targetBatchId));
      try {
        const [official, fullStandard, audit] = await Promise.all([
          api.exportBatchJson(targetBatchId, "official", controller.signal),
          api.exportBatchJson(
            targetBatchId,
            "full_standard",
            controller.signal
          ),
          api.exportBatchJson(targetBatchId, "audit", controller.signal),
        ]);
        if (controller.signal.aborted || requestSeqRef.current !== seq) {
          return;
        }
        setCacheState(
          completeExportModalLoadSuccess(targetBatchId, {
            official,
            full_standard: fullStandard,
            audit,
          })
        );
      } catch (err) {
        if (controller.signal.aborted || requestSeqRef.current !== seq) {
          return;
        }
        const msg =
          err instanceof Error ? err.message : "读取导出数据失败";
        setCacheState(completeExportModalLoadFailure(targetBatchId, msg));
        onNotify(msg, "error");
      }
    },
    [api, onNotify]
  );

  useEffect(() => {
    setAdvancedExpanded(false);
    setJsonExpanded(false);
  }, [open, batch?.id]);

  // A3: Strictly clear stale cache on batch change or modal close/reopen
  useEffect(() => {
    const targetId = batch?.id ?? null;
    if (!open || !targetId) {
      abortRef.current?.abort();
      requestSeqRef.current += 1;
      setCacheState((prev) => resetExportModalForBatch(prev, targetId));
      return;
    }
    setCacheState((prev) => resetExportModalForBatch(prev, targetId));
    void loadAllModePreviews(targetId);
    return () => {
      abortRef.current?.abort();
    };
  }, [open, batch?.id, batchRevision, loadAllModePreviews]);

  const activeBatchId = batch?.id ?? null;
  const officialExport = getExportResponseForBatchMode(
    cacheState,
    activeBatchId,
    "official"
  );
  const fullStandardExport = getExportResponseForBatchMode(
    cacheState,
    activeBatchId,
    "full_standard"
  );
  const auditExport = getExportResponseForBatchMode(
    cacheState,
    activeBatchId,
    "audit"
  );
  const { activeExport, canCopy, canDownload } = getExportModalActionEligibility(
    cacheState,
    activeBatchId,
    activeMode
  );
  const serializedRecordsText = activeExport
    ? stringifyLosslessJson(activeExport.records, 2)
    : "";

  const exportCurrencySummary = useMemo(
    () => (activeExport ? buildExportCurrencySummary(activeExport.records) : []),
    [activeExport]
  );

  const missingCounterpartyCount = useMemo(() => {
    if (!activeExport) return 0;
    return activeExport.records.filter((rec) => {
      const posting = rec.financial_attributes?.posting_type;
      const issuer = (rec.transaction_party?.issuer ?? "").trim();
      const recipient = (rec.transaction_party?.recipient ?? "").trim();
      return (
        (posting === "AR" && recipient.length === 0) ||
        (posting === "AP" && issuer.length === 0)
      );
    }).length;
  }, [activeExport]);

  const handleCopyJson = async () => {
    if (!canCopy || !activeExport) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(serializedRecordsText);
      } else {
        const ta = document.createElement("textarea");
        ta.value = serializedRecordsText;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      onNotify(`已复制「${MODE_LABELS[activeMode]}」数据`);
    } catch {
      onNotify("复制到剪贴板失败，请展开数据区域手动复制", "error");
    }
  };

  const handleDownload = async () => {
    if (!batch || !canDownload) return;
    setDownloading(true);
    try {
      const { rawJsonText } = await api.downloadBatchJsonArray(
        batch.id,
        activeMode
      );
      const blob = new Blob([rawJsonText], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${batch.short_code}-${activeMode}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      onNotify(`已下载 ${batch.short_code}-${activeMode}.json`);
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "下载账单文件失败",
        "error"
      );
    } finally {
      setDownloading(false);
    }
  };

  const currentBatchSync =
    batch && lastSyncResult?.batchId === batch.id ? lastSyncResult : null;

  const isProcessing = batch?.status === "PROCESSING";
  const officialCount = officialExport?.included_count ?? 0;
  const syncDisabled = !batch || isProcessing || officialCount === 0;

  const primaryDownloadLabel = activeExport
    ? activeMode === "official"
      ? `下载可入账的 ${activeExport.included_count} 条`
      : activeMode === "full_standard"
        ? `下载含待复核的 ${activeExport.included_count} 条`
        : `下载完整审计的 ${activeExport.included_count} 条`
    : "下载账单文件";

  return (
    <LumnoModal
      open={open}
      title={
        batch ? `导出与入账结果 · ${batch.short_code}` : "导出与入账结果"
      }
      onClose={onClose}
      footer={
        <>
          <LumnoButton variant="secondary" onClick={onClose}>
            关闭
          </LumnoButton>
          <LumnoButton
            variant="primary"
            icon="ri-download-2-line"
            disabled={!canDownload}
            busy={downloading}
            onClick={() => void handleDownload()}
          >
            {primaryDownloadLabel}
          </LumnoButton>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
        {isProcessing ? (
          <div className="wb-alert-item" data-severity="WARNING" role="status">
            <strong>当前批次仍在后台解析中</strong>
            <span>
              当前批次仍在后台解析中，以下导出仅包含截至当前已就绪的记录，建议等待解析完成后下载完整账单。
            </span>
          </div>
        ) : null}

        <LumnoSegmentedControl
          value={activeMode}
          wrap
          ariaLabel="选择导出范围"
          options={[
            {
              value: "official",
              label: "可入账账单",
              count: officialExport?.included_count,
            },
            {
              value: "full_standard",
              label: "含待复核全量",
              count: fullStandardExport?.included_count,
            },
            {
              value: "audit",
              label: "完整审计留痕",
              count: auditExport?.included_count,
            },
          ]}
          onChange={setActiveMode}
        />

        {batch ? (
          <div className="wb-helper-note">
            当前批次：{batch.short_code} · 记账公司：{batch.accounting_entity}
            （{MODE_SCOPE_LABELS[activeMode]}）
          </div>
        ) : null}

        {cacheState.loading ? (
          <div className="wb-helper-note">正在汇总本批可导出账单数据...</div>
        ) : cacheState.loadError ? (
          <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
            <div>{cacheState.loadError}</div>
            {batch ? (
              <div style={{ marginTop: "4px" }}>
                <LumnoButton
                  variant="secondary"
                  compact
                  icon="ri-refresh-line"
                  onClick={() => void loadAllModePreviews(batch.id)}
                >
                  重试读取
                </LumnoButton>
              </div>
            ) : null}
          </div>
        ) : activeExport ? (
          <>
            <div className="wb-notice-banner">
              <span className="wb-tabular">
                {activeMode === "audit"
                  ? `完整审计共包含 ${activeExport.included_count} 条记录（包含批内 ${batch?.counts.rejected ?? 0} 条已驳回记录、驳回原因与审批留痕）。`
                  : activeMode === "official"
                    ? `可入账 ${activeExport.included_count} 条${
                        activeExport.excluded_pending_count > 0 ||
                        activeExport.excluded_rejected_count > 0
                          ? `（另有 ${activeExport.excluded_pending_count} 条待复核、${activeExport.excluded_rejected_count} 条已驳回不入账）`
                          : ""
                      }。`
                    : `含待复核共 ${activeExport.included_count} 条（已剔除 ${activeExport.excluded_rejected_count} 条已驳回记录）。`}
              </span>
            </div>

            {missingCounterpartyCount > 0 ? (
              <div className="wb-alert-item" data-severity="WARNING" role="status">
                <span>
                  其中 {missingCounterpartyCount} 条记录原件未列明交易对手公司名称（已标为「付款方待确认」或「收款方待确认」），金额与应收/应付方向已完整保留。
                </span>
              </div>
            ) : null}

            <div className="wb-section-box" aria-label="导出内容结构化摘要与预览">
              <div className="wb-field-label">
                <span>导出财务摘要（{MODE_LABELS[activeMode]}）</span>
                <span className="wb-tabular">{activeExport.included_count} 条</span>
              </div>

              {activeExport.records.length === 0 ? (
                <div className="wb-helper-note">
                  当前范围下暂无可导出的账单记录。
                </div>
              ) : (
                <>
                  <div className="wb-chip-row wb-tabular">
                    <span className="wb-helper-note">分币种合计：</span>
                    {exportCurrencySummary.map((item) => (
                      <span
                        key={item.currency}
                        className={`wb-badge wb-badge--${
                          item.validCount === 0 ? "warn" : "info"
                        } wb-tabular`}
                      >
                        {item.badgeText}
                      </span>
                    ))}
                  </div>

                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "4px",
                    }}
                  >
                    {activeExport.records.slice(0, 4).map((rec, idx) => {
                      const amtText = formatDecimalLiteral(
                        readDecimalString(rec.charge_details, "amount"),
                        "—"
                      );
                      const posting = rec.financial_attributes?.posting_type;
                      const isMissingPayer =
                        posting === "AR" &&
                        !(rec.transaction_party?.recipient ?? "").trim();
                      const isMissingPayee =
                        posting === "AP" &&
                        !(rec.transaction_party?.issuer ?? "").trim();
                      return (
                        <div
                          key={`${rec.document_info?.reference_no ?? "row"}-${idx}`}
                          className="wb-diff-row wb-tabular"
                        >
                          <span>
                            {getExportOpLabel(rec.operation)}
                            {posting ? ` · ${posting}` : ""}
                          </span>
                          <span style={{ overflowWrap: "anywhere" }}>
                            {rec.charge_details?.charge_item ?? "未命名费用"}
                            {rec.document_info?.reference_no
                              ? ` · ${rec.document_info.reference_no}`
                              : ""}
                            {isMissingPayer
                              ? "（付款方待确认）"
                              : isMissingPayee
                                ? "（收款方待确认）"
                                : ""}
                          </span>
                          <span style={{ fontWeight: 600, textAlign: "right" }}>
                            {amtText} {rec.charge_details?.currency ?? "—"}
                          </span>
                        </div>
                      );
                    })}
                    {activeExport.records.length > 4 ? (
                      <div className="wb-helper-note wb-tabular">
                        其余 {activeExport.records.length - 4} 条记录均已包含在下载文件中，可在下方展开完整数据检视。
                      </div>
                    ) : null}
                  </div>
                </>
              )}
            </div>

            {/* Collapsed Advanced Section: JSON Preview, Copy & Sync Baseline */}
            <div className="wb-collapse-block">
              <button
                type="button"
                className="wb-collapse-trigger"
                aria-expanded={advancedExpanded}
                onClick={() => setAdvancedExpanded((v) => !v)}
              >
                <span>高级操作：查看原始 JSON 数据、复制与写入历史库</span>
                <i
                  className={`ri-icon ri-icon--compact ${
                    advancedExpanded
                      ? "ri-arrow-up-s-line"
                      : "ri-arrow-down-s-line"
                  }`}
                  aria-hidden="true"
                />
              </button>

              {advancedExpanded ? (
                <div className="wb-collapse-body">
                  <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-file-copy-line"
                      disabled={!canCopy}
                      onClick={() => void handleCopyJson()}
                    >
                      复制完整 JSON
                    </LumnoButton>
                    <LumnoButton
                      variant="ghost"
                      compact
                      icon={
                        jsonExpanded
                          ? "ri-arrow-up-s-line"
                          : "ri-code-s-slash-line"
                      }
                      aria-expanded={jsonExpanded}
                      onClick={() => setJsonExpanded((prev) => !prev)}
                    >
                      {jsonExpanded
                        ? "收起完整 JSON 预览"
                        : `展开完整 JSON 预览（共 ${activeExport.records.length} 条）`}
                    </LumnoButton>
                  </div>

                  {jsonExpanded ? (
                    <pre
                      className="wb-evidence-pre wb-tabular"
                      style={{ maxHeight: "none", overflowY: "visible" }}
                    >
                      {serializedRecordsText}
                    </pre>
                  ) : null}

                  <div className="wb-form-divider" />

                  <div className="wb-field-label">
                    <span>写入共享历史账单库（供后续新批查重与比对）</span>
                  </div>
                  <div className="wb-helper-note">
                    {isProcessing
                      ? "解析完成后可将已就绪记录写入历史库。"
                      : officialCount === 0
                        ? "当前暂无可入账记录可写入历史库。"
                        : `将把本批 ${officialCount} 条已就绪（新增/更新）记录写入「${batch?.accounting_entity}」历史账单库。`}
                  </div>

                  {currentBatchSync ? (
                    <div className="wb-export-counts-grid">
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">本次新增</div>
                        <div className="wb-kv-value wb-tabular">
                          {currentBatchSync.result.created_count} 条
                        </div>
                      </div>
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">本次更新</div>
                        <div className="wb-kv-value wb-tabular">
                          {currentBatchSync.result.updated_count} 条
                        </div>
                      </div>
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">已存在跳过</div>
                        <div className="wb-kv-value wb-tabular">
                          {currentBatchSync.result.skipped_count} 条
                        </div>
                      </div>
                    </div>
                  ) : null}

                  <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-save-3-line"
                      disabled={syncDisabled}
                      busy={syncingBaseline}
                      onClick={() => void onSyncBaseline()}
                    >
                      写入历史账单库
                    </LumnoButton>
                  </div>
                </div>
              ) : null}
            </div>
          </>
        ) : null}
      </div>
    </LumnoModal>
  );
}
