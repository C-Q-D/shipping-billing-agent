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
  official: "正式标准 (official)",
  full_standard: "四态全量 (full_standard)",
  audit: "完整审计 (audit)",
};

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
          err instanceof Error ? err.message : "读取三口径导出预览失败";
        setCacheState(completeExportModalLoadFailure(targetBatchId, msg));
        onNotify(msg, "error");
      }
    },
    [api, onNotify]
  );

  useEffect(() => {
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

  const exportCurrencySummary = useMemo(() => {
    if (!activeExport || activeExport.records.length === 0) {
      return [];
    }
    const grouped = new Map<string, { count: number; sum: string }>();
    for (const rec of activeExport.records) {
      const currency = (rec.charge_details?.currency ?? "").trim() || "未定币种";
      const amtStr = readDecimalString(rec.charge_details, "amount") ?? "0";
      const prev = grouped.get(currency);
      if (!prev) {
        grouped.set(currency, { count: 1, sum: amtStr });
      } else {
        let nextSum = prev.sum;
        try {
          nextSum = addDecimalStrings(prev.sum, amtStr);
        } catch {
          // keep previous sum if malformed
        }
        grouped.set(currency, { count: prev.count + 1, sum: nextSum });
      }
    }
    return Array.from(grouped.entries()).map(([currency, meta]) => ({
      currency,
      count: meta.count,
      formattedSum: formatDecimalLiteral(meta.sum, "0.00"),
    }));
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
      onNotify(`已复制 ${MODE_LABELS[activeMode]} 标准 JSON`);
    } catch {
      onNotify("复制到剪贴板失败，请手动选中预览区域复制", "error");
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
        err instanceof Error ? err.message : "下载 JSON 文件失败",
        "error"
      );
    } finally {
      setDownloading(false);
    }
  };

  const currentBatchSync =
    batch && lastSyncResult?.batchId === batch.id ? lastSyncResult : null;

  return (
    <LumnoModal
      open={open}
      title={
        batch
          ? `三口径导出与基准同步 · ${batch.short_code}`
          : "三口径导出与基准同步"
      }
      onClose={onClose}
      footer={
        <>
          <LumnoButton variant="secondary" onClick={onClose}>
            关闭
          </LumnoButton>
          <LumnoButton
            variant="secondary"
            icon="ri-save-3-line"
            disabled={!batch}
            busy={syncingBaseline}
            onClick={() => void onSyncBaseline()}
          >
            同步至基准库 (sync-baseline)
          </LumnoButton>
          <LumnoButton
            variant="secondary"
            icon="ri-file-copy-line"
            disabled={!canCopy}
            onClick={() => void handleCopyJson()}
          >
            复制完整 JSON
          </LumnoButton>
          <LumnoButton
            variant="primary"
            icon="ri-download-2-line"
            disabled={!canDownload}
            busy={downloading}
            onClick={() => void handleDownload()}
          >
            下载标准 JSON 文件
          </LumnoButton>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
        {batch?.status === "PROCESSING" ? (
          <div className="wb-alert-item" data-severity="WARNING" role="status">
            <strong>当前批次仍在后台解析中（PROCESSING）</strong>
            <span>
              当前批次仍在后台解析中，以下导出仅包含截至当前已就绪的记录，建议等待状态变为 COMPLETED 后导出完整账单。
            </span>
          </div>
        ) : null}

        <LumnoSegmentedControl
          value={activeMode}
          wrap
          ariaLabel="切换导出口径"
          options={[
            {
              value: "official",
              label: "正式标准 official",
              count: officialExport?.included_count,
            },
            {
              value: "full_standard",
              label: "四态全量 full_standard",
              count: fullStandardExport?.included_count,
            },
            {
              value: "audit",
              label: "完整审计 audit",
              count: auditExport?.included_count,
            },
          ]}
          onChange={setActiveMode}
        />

        {officialExport && fullStandardExport && auditExport ? (
          <div className="wb-helper-note wb-tabular">
            三口径总览计数：正式入账{" "}
            <strong>{officialExport.included_count}</strong> 条 / 四态全量{" "}
            <strong>{fullStandardExport.included_count}</strong> 条 / 完整审计{" "}
            <strong>{auditExport.included_count}</strong> 条
            {batch ? `（批内共 ${batch.records.length} 条记录）` : ""}
          </div>
        ) : null}

        {cacheState.loading ? (
          <div className="wb-helper-note">正在读取三口径实时导出数据...</div>
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
                  重试读取导出数据
                </LumnoButton>
              </div>
            ) : null}
          </div>
        ) : activeExport ? (
          <>
            <div className="wb-export-counts-grid">
              <div className="wb-kv-item">
                <div className="wb-kv-label">本次导出包含 (included_count)</div>
                <div className="wb-kv-value wb-tabular">
                  {activeExport.included_count} 条
                </div>
              </div>
              <div className="wb-kv-item">
                <div className="wb-kv-label">
                  待复核排除 (excluded_pending_count)
                </div>
                <div className="wb-kv-value wb-tabular">
                  {activeExport.excluded_pending_count} 条
                </div>
              </div>
              <div className="wb-kv-item">
                <div className="wb-kv-label">
                  已驳回剔除 (excluded_rejected_count)
                </div>
                <div className="wb-kv-value wb-tabular">
                  {activeExport.excluded_rejected_count} 条
                </div>
              </div>
            </div>

            <div className="wb-notice-banner">
              <span className="wb-tabular">
                {activeMode === "audit"
                  ? `完整审计口径导出 ${activeExport.included_count} 条（excluded_rejected_count = 0，已含批内 ${batch?.counts.rejected ?? 0} 条已驳回记录及驳回原因）。`
                  : `${MODE_LABELS[activeMode]} 将导出 ${activeExport.included_count} 条，排除 ${activeExport.excluded_pending_count} 条待复核与 ${activeExport.excluded_rejected_count} 条已驳回（excluded_rejected_count = ${activeExport.excluded_rejected_count}）。`}
              </span>
            </div>

            <div className="wb-section-box" aria-label="导出内容结构化摘要与预览">
              <div className="wb-field-label">
                <span>导出结构化摘要与无损 JSON 预览</span>
                <LumnoButton
                  variant="ghost"
                  compact
                  icon={jsonExpanded ? "ri-arrow-up-s-line" : "ri-code-s-slash-line"}
                  aria-expanded={jsonExpanded}
                  onClick={() => setJsonExpanded((prev) => !prev)}
                >
                  {jsonExpanded
                    ? "收起完整 JSON 预览"
                    : `展开完整 JSON 预览（共 ${activeExport.records.length} 条）`}
                </LumnoButton>
              </div>

              {activeExport.records.length === 0 ? (
                <div className="wb-helper-note">
                  当前口径下暂无可导出的账单记录。
                </div>
              ) : (
                <>
                  <div className="wb-chip-row wb-tabular">
                    <span className="wb-helper-note">分币种合计：</span>
                    {exportCurrencySummary.map((item) => (
                      <span
                        key={item.currency}
                        className="wb-badge wb-badge--info wb-tabular"
                      >
                        {item.currency} {item.formattedSum}（{item.count} 条）
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
                    {activeExport.records.slice(0, 3).map((rec, idx) => {
                      const amtText = formatDecimalLiteral(
                        readDecimalString(rec.charge_details, "amount"),
                        "—"
                      );
                      return (
                        <div
                          key={`${rec.document_info?.reference_no ?? "row"}-${idx}`}
                          className="wb-diff-row wb-tabular"
                        >
                          <span className="wb-mono">{rec.operation}</span>
                          <span>
                            {rec.charge_details?.charge_item ?? "未命名费用"}
                            {rec.document_info?.reference_no
                              ? ` · ${rec.document_info.reference_no}`
                              : ""}
                          </span>
                          <span style={{ fontWeight: 600, textAlign: "right" }}>
                            {amtText} {rec.charge_details?.currency ?? "—"}
                          </span>
                        </div>
                      );
                    })}
                    {activeExport.records.length > 3 && !jsonExpanded ? (
                      <div className="wb-helper-note wb-tabular">
                        其余 {activeExport.records.length - 3} 条记录已包含在下载与复制内容中，可点击右上角展开完整 JSON 检视。
                      </div>
                    ) : null}
                  </div>
                </>
              )}

              {jsonExpanded ? (
                <pre
                  className="wb-evidence-pre wb-tabular"
                  style={{ maxHeight: "none", overflowY: "visible" }}
                >
                  {serializedRecordsText}
                </pre>
              ) : null}
            </div>
          </>
        ) : null}

        <div className="wb-section-box" aria-label="基准库同步状态">
          <div className="wb-field-label">
            <span>写入工作台历史账单基准库（幂等可重跑）</span>
            <span className="wb-helper-note">
              仅请求中防连点，同步后可再次增量同步
            </span>
          </div>
          {currentBatchSync ? (
            <div className="wb-export-counts-grid">
              <div className="wb-kv-item">
                <div className="wb-kv-label">新增基准 (created_count)</div>
                <div className="wb-kv-value wb-tabular">
                  {currentBatchSync.result.created_count} 条
                </div>
              </div>
              <div className="wb-kv-item">
                <div className="wb-kv-label">更新基准 (updated_count)</div>
                <div className="wb-kv-value wb-tabular">
                  {currentBatchSync.result.updated_count} 条
                </div>
              </div>
              <div className="wb-kv-item">
                <div className="wb-kv-label">幂等跳过 (skipped_count)</div>
                <div className="wb-kv-value wb-tabular">
                  {currentBatchSync.result.skipped_count} 条
                </div>
              </div>
            </div>
          ) : (
            <div className="wb-helper-note">
              点击底部“同步至基准库 (sync-baseline)”可将当前已就绪的 CREATE /
              UPDATE 记录幂等写入全局历史基准库；补审或修订后可再次点击。
            </div>
          )}
        </div>
      </div>
    </LumnoModal>
  );
}
