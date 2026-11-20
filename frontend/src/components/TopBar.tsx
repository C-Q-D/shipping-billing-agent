import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
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
  batches: ReadonlyArray<BatchSummary>;
  activeBatchId: string | null;
  activeBatch: BatchDetail | null;
  switchingBatchId?: string | null;
  entities: ReadonlyArray<AccountingEntity>;
  selectedEntity: string;
  existingBillCount: number;
  exportableCount: number;
  unsavedDraftCount?: number;
  theme: LumnoTheme;
  syncingBaseline: boolean;
  switchingEntity: boolean;
  onSelectBatch: (batchId: string) => void;
  onConfirmSwitchEntity: (entityName: string) => Promise<void> | void;
  onToggleTheme: () => void;
  onOpenImportBundle: () => void;
  onOpenMasterData: (tab?: "entities" | "dictionary" | "existing_bills") => void;
  onOpenEvalDashboard: () => void;
  onSyncBaseline: () => void;
  onOpenExport: () => void;
}

function formatCompactTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  const hh = String(dt.getHours()).padStart(2, "0");
  const min = String(dt.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${min}`;
}

export function TopBar({
  health,
  healthError,
  batches,
  activeBatchId,
  activeBatch,
  switchingBatchId = null,
  entities,
  selectedEntity,
  existingBillCount,
  exportableCount,
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
  const [toolsMenuOpen, setToolsMenuOpen] = useState(false);
  const [mobileContextOpen, setMobileContextOpen] = useState(false);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number }>({
    top: 56,
    right: 12,
  });

  const toolsBtnRef = useRef<HTMLButtonElement | null>(null);
  const toolsMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setPendingEntitySwitch(null);
  }, [activeBatchId, selectedEntity]);

  const repositionMenu = useCallback(() => {
    const btn = toolsBtnRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    setMenuPos({
      top: Math.min(window.innerHeight - 120, rect.bottom + 6),
      right: Math.max(12, window.innerWidth - rect.right),
    });
  }, []);

  useLayoutEffect(() => {
    if (toolsMenuOpen) {
      repositionMenu();
    }
  }, [toolsMenuOpen, repositionMenu]);

  useEffect(() => {
    if (!toolsMenuOpen) return;
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (
        toolsBtnRef.current?.contains(target) ||
        toolsMenuRef.current?.contains(target)
      ) {
        return;
      }
      setToolsMenuOpen(false);
    };
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setToolsMenuOpen(false);
        toolsBtnRef.current?.focus();
      }
    };
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", repositionMenu);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", repositionMenu);
    };
  }, [toolsMenuOpen, repositionMenu]);

  const batchOptions: LumnoSelectOption[] = batches.map((b) => {
    const timeStr = formatCompactTime(b.created_at);
    return {
      value: b.id,
      label: `${b.short_code} · ${b.accounting_entity}`,
      sublabel: `${timeStr ? `${timeStr} · ` : ""}${b.succeeded_files}/${b.total_files} 份 · 待核 ${b.counts.pending_review}`,
    };
  });

  const entityMap = new Map<string, LumnoSelectOption>();
  for (const ent of entities) {
    entityMap.set(ent.canonical_name, {
      value: ent.canonical_name,
      label: ent.canonical_name,
      sublabel:
        ent.aliases.length > 0
          ? `别名：${ent.aliases.join(" / ")}`
          : ent.is_default
            ? "默认记账公司"
            : "已登记记账公司",
    });
  }
  if (selectedEntity && !entityMap.has(selectedEntity)) {
    entityMap.set(selectedEntity, {
      value: selectedEntity,
      label: selectedEntity,
      sublabel: "当前批次临时记账公司",
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

  const isProcessing = activeBatch?.status === "PROCESSING";
  const syncDisabled = !activeBatch || isProcessing || exportableCount === 0;
  const syncSublabel = !activeBatch
    ? "请先选择或上传账单批次"
    : isProcessing
      ? "解析完成后可同步（避免处理中冲突）"
      : exportableCount === 0
        ? "当前批次暂无已就绪的可入账记录"
        : `预计写入 ${exportableCount} 条已就绪记录（${activeBatch.accounting_entity}）`;

  const remoteOcrUnavailable =
    Boolean(health) && !health!.ocr_direct_mode && !health!.public_relay_configured;

  const healthDotClass = healthError
    ? "wb-health-dot--error"
    : !health
      ? "wb-health-dot--warn"
      : !health.llm_configured || remoteOcrUnavailable
        ? "wb-health-dot--warn"
        : "wb-health-dot--ok";

  const healthShortLabel = healthError
    ? "服务离线"
    : !health
      ? "连接中"
      : !health.llm_configured
        ? "仅规则识别"
        : remoteOcrUnavailable
          ? "远端扫描受限"
          : "正常";

  const healthTooltipContent = (
    <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
      <div>
        <strong>工作台服务：</strong>
        {healthError ? healthError : health ? "连接正常" : "检测中..."}
      </div>
      {health ? (
        <>
          <div>
            <strong>智能提取：</strong>
            {health.llm_configured
              ? `已就绪 (${health.llm_model ?? "默认模型"})`
              : "未配置智能模型，将使用规则提取"}
          </div>
          <div>
            <strong>扫描件识别：</strong>
            {health.ocr_direct_mode || health.public_relay_configured
              ? "远端识别可用，并支持本地自动回退"
              : "远端扫描件识别暂不可用，上传本地图片/PDF 将自动使用本地识别兜底"}
          </div>
        </>
      ) : null}
    </div>
  );

  return (
    <>
      <header className="wb-topbar lumno-glass-panel">
        <div className="wb-topbar-left">
          <div className="wb-brand" title="航运智能账单复核工作台">
            <i className="ri-icon ri-ship-2-line" aria-hidden="true" />
            <span className="wb-desktop-only">账单复核</span>
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
            />
          </div>

          <div
            className="wb-topbar-select-entity"
            title={`本批记账公司：${selectedEntity}（更换后将重算当前批次全部应收/应付）`}
          >
            <span className="wb-topbar-field-tag wb-desktop-only">本批公司</span>
            <LumnoSelect
              value={selectedEntity}
              options={entityOptions}
              ariaLabel="更换本批记账公司"
              placeholder="选择本批记账公司"
              disabled={switchingEntity}
              onChange={handleEntitySelectChange}
            />
          </div>

          {activeBatch ? (
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-information-line"
              aria-label="查看当前批次与记账公司全称"
              className="wb-mobile-only"
              onClick={() => setMobileContextOpen(true)}
            />
          ) : null}
        </div>

        <div className="wb-topbar-actions">
          <LumnoButton
            ref={toolsBtnRef}
            variant="secondary"
            compact
            icon="ri-settings-3-line"
            aria-expanded={toolsMenuOpen}
            aria-haspopup="menu"
            onClick={() => setToolsMenuOpen((v) => !v)}
          >
            <span className="wb-desktop-only">管理与工具</span>
          </LumnoButton>

          <LumnoButton
            variant="ghost"
            compact
            icon={theme === "dark" ? "ri-sun-line" : "ri-moon-clear-fill"}
            aria-label={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            title={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={onToggleTheme}
          />

          <LumnoButton
            variant="primary"
            compact
            icon="ri-download-2-line"
            disabled={!activeBatch}
            onClick={onOpenExport}
          >
            <span className="wb-desktop-only">
              {activeBatch ? `导出可入账 (${exportableCount} 条)` : "导出账单"}
            </span>
            <span className="wb-mobile-only">
              {activeBatch ? `导出 (${exportableCount})` : "导出"}
            </span>
          </LumnoButton>
        </div>
      </header>

      {toolsMenuOpen && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={toolsMenuRef}
              role="menu"
              aria-label="管理与工具菜单"
              className="lumno-select-menu wb-tools-menu"
              style={{
                top: `${menuPos.top}px`,
                right: `${menuPos.right}px`,
              }}
            >
              <button
                type="button"
                role="menuitem"
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onOpenMasterData("entities");
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-building-line" aria-hidden="true" />
                  <span>记账主体管理</span>
                </span>
                <span className="lumno-select-option-sub">
                  查看公司档案、别名与默认记账主体
                </span>
              </button>

              <button
                type="button"
                role="menuitem"
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onOpenMasterData("dictionary");
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-book-open-line" aria-hidden="true" />
                  <span>费用标准词典</span>
                </span>
                <span className="lumno-select-option-sub">
                  维护原始收费名到标准费用名的映射
                </span>
              </button>

              <button
                type="button"
                role="menuitem"
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onOpenMasterData("existing_bills");
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-database-2-line" aria-hidden="true" />
                  <span>历史账单库（{existingBillCount} 条）</span>
                </span>
                <span className="lumno-select-option-sub">
                  查询已入库账单或导入历史对账基准
                </span>
              </button>

              <button
                type="button"
                role="menuitem"
                disabled={syncDisabled || syncingBaseline}
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onSyncBaseline();
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-save-3-line" aria-hidden="true" />
                  <span>
                    {syncingBaseline
                      ? "正在写入历史账单库..."
                      : "将本批已确认记录写入历史库"}
                  </span>
                </span>
                <span className="lumno-select-option-sub">{syncSublabel}</span>
              </button>

              <div className="lumno-select-divider" role="presentation" />

              <button
                type="button"
                role="menuitem"
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onOpenImportBundle();
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-folder-upload-line" aria-hidden="true" />
                  <span>导入他人复核结果（复核包）</span>
                </span>
                <span className="lumno-select-option-sub">
                  载入外部导出的复核包文件继续核对
                </span>
              </button>

              <button
                type="button"
                role="menuitem"
                className="lumno-select-option"
                onClick={() => {
                  setToolsMenuOpen(false);
                  onOpenEvalDashboard();
                }}
              >
                <span className="wb-menu-item-row">
                  <i className="ri-icon ri-icon--compact ri-bar-chart-box-line" aria-hidden="true" />
                  <span>管理员自检（Harness 评测）</span>
                </span>
                <span className="lumno-select-option-sub">
                  仅供测试/管理员运行 13 组（16 项）回归基准
                </span>
              </button>
            </div>,
            document.body
          )
        : null}

      <LumnoModal
        open={pendingEntitySwitch !== null}
        title="确认更换本批记账公司"
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
              确认更换并重算整批
            </LumnoButton>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          即将把<strong>当前正在复核批次（{activeBatch?.short_code}）</strong>
          的记账公司由 <strong>{activeBatch?.accounting_entity}</strong> 更换为{" "}
          <strong>{pendingEntitySwitch}</strong>。
        </p>
        <div
          className="wb-alert-item"
          data-severity="WARNING"
          style={{ marginTop: "4px" }}
        >
          更换本批记账公司后，系统将按新公司重新计算本批全部记录的应收/应付方向与历史账单匹配，并重置此前人工批准结论；不会影响下一批新上传单据的默认公司设置。
        </div>
        {unsavedDraftCount > 0 ? (
          <div className="wb-field-error" style={{ marginTop: "4px" }}>
            注意：当前批次仍有 {unsavedDraftCount} 项未保存草稿，更换并重算后这些未保存修改可能失效，建议先保存或取消更换。
          </div>
        ) : null}
      </LumnoModal>

      <LumnoModal
        open={mobileContextOpen && Boolean(activeBatch)}
        title="当前复核批次信息"
        onClose={() => setMobileContextOpen(false)}
        footer={
          <LumnoButton
            variant="primary"
            onClick={() => setMobileContextOpen(false)}
          >
            知道了
          </LumnoButton>
        }
      >
        {activeBatch ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
            <div className="wb-kv-item">
              <div className="wb-kv-label">当前批次编号</div>
              <div className="wb-kv-value wb-mono">{activeBatch.short_code}</div>
            </div>
            <div className="wb-kv-item">
              <div className="wb-kv-label">本批记账公司</div>
              <div className="wb-kv-value">{activeBatch.accounting_entity}</div>
            </div>
            <div className="wb-kv-item">
              <div className="wb-kv-label">创建时间与文件统计</div>
              <div className="wb-kv-value">
                {formatCompactTime(activeBatch.created_at) || activeBatch.created_at} ·{" "}
                {activeBatch.succeeded_files}/{activeBatch.total_files} 份成功 · 待核{" "}
                {activeBatch.counts.pending_review} 条
              </div>
            </div>
          </div>
        ) : null}
      </LumnoModal>
    </>
  );
}
