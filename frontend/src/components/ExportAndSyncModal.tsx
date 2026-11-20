import { useCallback, useEffect, useRef, useState } from "react";
import type {
  BatchDetail,
  BillingApiClient,
  ExportMode,
  ExportResponse,
  SyncBaselineResult,
} from "../api/client.ts";
import { stringifyLosslessJson } from "../api/decimalJson.ts";
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

type ExportsByMode = Partial<Record<ExportMode, ExportResponse>>;

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
  const [exportsByMode, setExportsByMode] = useState<ExportsByMode>({});
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const requestSeqRef = useRef(0);

  const loadAllModePreviews = useCallback(
    async (targetBatchId: string) => {
      const seq = ++requestSeqRef.current;
      setLoading(true);
      setLoadError(null);
      try {
        const [official, fullStandard, audit] = await Promise.all([
          api.exportBatchJson(targetBatchId, "official"),
          api.exportBatchJson(targetBatchId, "full_standard"),
          api.exportBatchJson(targetBatchId, "audit"),
        ]);
        if (requestSeqRef.current !== seq) return;
        setExportsByMode({
          official,
          full_standard: fullStandard,
          audit,
        });
      } catch (err) {
        if (requestSeqRef.current !== seq) return;
        const msg =
          err instanceof Error ? err.message : "读取三口径导出预览失败";
        setLoadError(msg);
        onNotify(msg, "error");
      } finally {
        if (requestSeqRef.current === seq) {
          setLoading(false);
        }
      }
    },
    [api, onNotify]
  );

  useEffect(() => {
    if (!open || !batch) {
      return;
    }
    void loadAllModePreviews(batch.id);
  }, [open, batch, batchRevision, loadAllModePreviews]);

  const activeExport = exportsByMode[activeMode] ?? null;
  const serializedRecordsText = activeExport
    ? stringifyLosslessJson(activeExport.records, 2)
    : "";

  const handleCopyJson = async () => {
    if (!activeExport) return;
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
    if (!batch) return;
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
            disabled={!activeExport || loading}
            onClick={() => void handleCopyJson()}
          >
            复制完整 JSON
          </LumnoButton>
          <LumnoButton
            variant="primary"
            icon="ri-download-2-line"
            disabled={!batch || loading}
            busy={downloading}
            onClick={() => void handleDownload()}
          >
            下载标准 JSON 文件
          </LumnoButton>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
        <LumnoSegmentedControl
          value={activeMode}
          wrap
          ariaLabel="切换导出口径"
          options={[
            {
              value: "official",
              label: "正式标准 official",
              count: exportsByMode.official?.included_count,
            },
            {
              value: "full_standard",
              label: "四态全量 full_standard",
              count: exportsByMode.full_standard?.included_count,
            },
            {
              value: "audit",
              label: "完整审计 audit",
              count: exportsByMode.audit?.included_count,
            },
          ]}
          onChange={setActiveMode}
        />

        {exportsByMode.official &&
        exportsByMode.full_standard &&
        exportsByMode.audit ? (
          <div className="wb-helper-note wb-tabular">
            三口径总览计数：正式入账{" "}
            <strong>{exportsByMode.official.included_count}</strong> 条 /
            四态全量{" "}
            <strong>{exportsByMode.full_standard.included_count}</strong> 条 /
            完整审计 <strong>{exportsByMode.audit.included_count}</strong> 条
            {batch ? `（批内共 ${batch.records.length} 条记录）` : ""}
          </div>
        ) : null}

        {loading && !activeExport ? (
          <div className="wb-helper-note">正在读取三口径实时导出数据...</div>
        ) : loadError ? (
          <div className="wb-field-error">{loadError}</div>
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

            <pre
              className="wb-evidence-pre wb-tabular"
              style={{ maxHeight: "240px", overflowY: "auto" }}
            >
              {serializedRecordsText}
            </pre>
          </>
        ) : null}

        <div className="wb-section-box" aria-label="基准库同步状态">
          <div className="wb-field-label">
            <span>写入工作台历史账单基准库（幂等可重跑）</span>
            <span className="wb-helper-note">仅请求中防连点，同步后可再次增量同步</span>
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
              点击底部“同步至基准库 (sync-baseline)”可将当前已就绪的 CREATE / UPDATE 记录幂等写入全局历史基准库；补审或修订后可再次点击。
            </div>
          )}
        </div>
      </div>
    </LumnoModal>
  );
}
