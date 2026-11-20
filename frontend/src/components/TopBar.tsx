import { useEffect, useState } from "react";
import type {
  AccountingEntity,
  BatchDetail,
  BatchSummary,
  HealthStatus,
} from "../api/client.ts";
import {
  LumnoButton,
  LumnoModal,
  LumnoSelect,
  LumnoTooltip,
  type LumnoSelectOption,
  type LumnoTheme,
} from "./LumnoPrimitives.tsx";

export interface TopBarProps {
  health: HealthStatus | null;
  healthError: string | null;
  isStubMode: boolean;
  batches: ReadonlyArray<BatchSummary>;
  activeBatchId: string | null;
  activeBatch: BatchDetail | null;
  switchingBatchId?: string | null;
  entities: ReadonlyArray<AccountingEntity>;
  selectedEntity: string;
  existingBillCount: number;
  unsavedDraftCount?: number;
  theme: LumnoTheme;
  syncingBaseline: boolean;
  switchingEntity: boolean;
  onSelectBatch: (batchId: string) => void;
  onConfirmSwitchEntity: (entityName: string) => Promise<void> | void;
  onToggleTheme: () => void;
  onOpenImportBundle: () => void;
  onOpenMasterData: () => void;
  onOpenEvalDashboard: () => void;
  onSyncBaseline: () => void;
  onOpenExport: () => void;
}

export function TopBar({
  health,
  healthError,
  isStubMode,
  batches,
  activeBatchId,
  activeBatch,
  switchingBatchId = null,
  entities,
  selectedEntity,
  existingBillCount,
  unsavedDraftCount = 0,
  theme,
  syncingBaseline,
  switchingEntity,
  onSelectBatch,
  onConfirmSwitchEntity,
  onToggleTheme,
  onOpenImportBundle,
  onOpenMasterData,
  onOpenEvalDashboard,
  onSyncBaseline,
  onOpenExport,
}: TopBarProps) {
  const [pendingEntitySwitch, setPendingEntitySwitch] = useState<string | null>(
    null
  );
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  useEffect(() => {
    setPendingEntitySwitch(null);
  }, [activeBatchId, selectedEntity]);

  const batchOptions: LumnoSelectOption[] = batches.map((b) => ({
    value: b.id,
    label: `${b.short_code} · ${b.accounting_entity}`,
    sublabel: `文件 ${b.succeeded_files}/${b.total_files} · 待核 ${b.counts.pending_review}`,
  }));

  const entityMap = new Map<string, LumnoSelectOption>();
  for (const ent of entities) {
    entityMap.set(ent.canonical_name, {
      value: ent.canonical_name,
      label: ent.canonical_name,
      sublabel:
        ent.aliases.length > 0
          ? `别名：${ent.aliases.join(" / ")}`
          : ent.is_default
            ? "默认航运记账主体"
            : "记账主体档案",
    });
  }
  if (selectedEntity && !entityMap.has(selectedEntity)) {
    entityMap.set(selectedEntity, {
      value: selectedEntity,
      label: selectedEntity,
      sublabel: "当前批次临时主体",
    });
  }
  const entityOptions = Array.from(entityMap.values());

  const handleEntitySelectChange = (nextEntity: string) => {
    if (nextEntity === selectedEntity) {
      return;
    }
    if (activeBatch && nextEntity !== activeBatch.accounting_entity) {
      setPendingEntitySwitch(nextEntity);
      return;
    }
    void onConfirmSwitchEntity(nextEntity);
  };

  const healthDotClass = healthError
    ? "wb-health-dot--error"
    : health?.llm_configured
      ? "wb-health-dot--ok"
      : "wb-health-dot--warn";

  const healthShortLabel = healthError
    ? "服务离线"
    : isStubMode
      ? "演示桩"
      : health?.llm_configured
        ? "能力就绪"
        : "规则兜底";

  const healthTooltipContent = (
    <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
      <div>
        <strong>服务状态：</strong>
        {healthError
          ? healthError
          : health
            ? `在线 (${health.status})`
            : "检测中..."}
      </div>
      {health ? (
        <>
          <div>
            <strong>LLM 提取：</strong>
            {health.llm_configured
              ? `已配置 (${health.llm_model ?? "默认模型"})`
              : "未配置密钥（仅规则提取）"}
          </div>
          <div>
            <strong>OCR 网关：</strong>
            {health.ocr_direct_mode ? "直连就绪" : "受限"} · 公网中转：
            {health.public_relay_configured ? "已配置" : "未配置"}
          </div>
        </>
      ) : null}
      {isStubMode ? (
        <div>当前运行于前端独立演示桩（?mode=stub），用于界面与契约联调</div>
      ) : null}
    </div>
  );

  return (
    <>
      <header className="wb-topbar lumno-glass-panel">
        <div className="wb-topbar-left">
          <div className="wb-brand">
            <i className="ri-icon ri-ship-2-line" aria-hidden="true" />
            <span className="wb-desktop-only">航运账单智能体</span>
          </div>

          <LumnoTooltip content={healthTooltipContent}>
            <span className="wb-health-badge" tabIndex={0}>
              <span className={`wb-health-dot ${healthDotClass}`} />
              <span className="wb-health-label">{healthShortLabel}</span>
            </span>
          </LumnoTooltip>

          <div className="wb-topbar-select-batch">
            <LumnoSelect
              value={activeBatchId ?? ""}
              options={batchOptions}
              ariaLabel="选择最近处理批次"
              placeholder="暂无批次"
              disabled={Boolean(switchingBatchId)}
              onChange={onSelectBatch}
              footerAction={{
                label: "导入 CLI 复核包 (v1.0)...",
                icon: "ri-upload-cloud-2-line",
                onClick: onOpenImportBundle,
              }}
            />
          </div>

          <div className="wb-topbar-select-entity">
            <LumnoSelect
              value={selectedEntity}
              options={entityOptions}
              ariaLabel="切换记账主体"
              placeholder="选择记账主体"
              disabled={switchingEntity}
              onChange={handleEntitySelectChange}
            />
          </div>
        </div>

        <div className="wb-topbar-actions">
          <LumnoButton
            variant="ghost"
            compact
            icon="ri-folder-upload-line"
            className="wb-desktop-only"
            onClick={onOpenImportBundle}
          >
            导入复核包
          </LumnoButton>

          <LumnoButton
            variant="secondary"
            compact
            icon="ri-database-2-line"
            className="wb-desktop-only"
            title={`记账主体、费用词典与共享历史基准库（当前历史基准 ${existingBillCount} 条）`}
            aria-label={`主数据与历史基准管理，当前历史基准 ${existingBillCount} 条`}
            onClick={onOpenMasterData}
          >
            主数据 · 基准 {existingBillCount}
          </LumnoButton>

          <LumnoButton
            variant="secondary"
            compact
            icon="ri-bar-chart-box-line"
            className="wb-desktop-only"
            onClick={onOpenEvalDashboard}
          >
            Harness 评测
          </LumnoButton>

          <LumnoButton
            variant="secondary"
            compact
            icon="ri-save-3-line"
            className="wb-desktop-only"
            disabled={!activeBatch}
            busy={syncingBaseline}
            onClick={onSyncBaseline}
          >
            写入基准库
          </LumnoButton>

          <LumnoButton
            variant="ghost"
            compact
            icon={theme === "dark" ? "ri-sun-line" : "ri-moon-clear-fill"}
            aria-label={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={onToggleTheme}
          />

          <LumnoButton
            variant="ghost"
            compact
            icon="ri-more-2-line"
            aria-label="更多工作台工具"
            className="wb-mobile-only"
            onClick={() => setMobileMenuOpen(true)}
          />

          <LumnoButton
            variant="primary"
            compact
            icon="ri-download-2-line"
            disabled={!activeBatch}
            onClick={onOpenExport}
          >
            <span className="wb-desktop-only">导出标准 JSON</span>
            <span className="wb-mobile-only">导出</span>
          </LumnoButton>
        </div>
      </header>

      <LumnoModal
        open={pendingEntitySwitch !== null}
        title="确认切换记账主体"
        onClose={() => setPendingEntitySwitch(null)}
        footer={
          <>
            <LumnoButton
              variant="secondary"
              onClick={() => setPendingEntitySwitch(null)}
            >
              取消
            </LumnoButton>
            <LumnoButton
              variant="primary"
              busy={switchingEntity}
              onClick={() => {
                const target = pendingEntitySwitch;
                if (!target) return;
                setPendingEntitySwitch(null);
                void onConfirmSwitchEntity(target);
              }}
            >
              确认切换并重算
            </LumnoButton>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          即将把当前批次记账主体由{" "}
          <strong>{activeBatch?.accounting_entity}</strong> 切换为{" "}
          <strong>{pendingEntitySwitch}</strong>。
        </p>
        <div
          className="wb-alert-item"
          data-severity="WARNING"
          style={{ marginTop: "4px" }}
        >
          切换主体后，系统将按新主体重新计算全部记录的 AR/AP 方向与历史账单匹配，并自动清除原主体下的人工关联及跨主体审批结论。
        </div>
        {unsavedDraftCount > 0 ? (
          <div className="wb-field-error" style={{ marginTop: "4px" }}>
            注意：当前批次仍有 {unsavedDraftCount} 项未保存草稿，切换主体重算后这些未保存修改可能失效，建议先保存或取消切换。
          </div>
        ) : null}
      </LumnoModal>

      <LumnoModal
        open={mobileMenuOpen}
        title="工作台更多操作"
        onClose={() => setMobileMenuOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <LumnoButton
            variant="secondary"
            icon="ri-folder-upload-line"
            onClick={() => {
              setMobileMenuOpen(false);
              onOpenImportBundle();
            }}
          >
            导入 CLI 复核包 (v1.0)
          </LumnoButton>
          <LumnoButton
            variant="secondary"
            icon="ri-database-2-line"
            onClick={() => {
              setMobileMenuOpen(false);
              onOpenMasterData();
            }}
          >
            主数据管理 · 历史基准 {existingBillCount} 条
          </LumnoButton>
          <LumnoButton
            variant="secondary"
            icon="ri-bar-chart-box-line"
            onClick={() => {
              setMobileMenuOpen(false);
              onOpenEvalDashboard();
            }}
          >
            Harness 评测看板
          </LumnoButton>
          <LumnoButton
            variant="secondary"
            icon="ri-save-3-line"
            disabled={!activeBatch}
            busy={syncingBaseline}
            onClick={() => {
              setMobileMenuOpen(false);
              onSyncBaseline();
            }}
          >
            将已确认记录写入历史基准库
          </LumnoButton>
        </div>
      </LumnoModal>
    </>
  );
}
