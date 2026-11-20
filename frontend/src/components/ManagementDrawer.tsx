import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import { createPortal } from "react-dom";
import type {
  AccountingEntity,
  BatchDetail,
  BillingApiClient,
  ChargeDictionaryEntry,
  EvalRunSummary,
  ExistingBill,
} from "../api/client.ts";
import { formatLosslessAmount } from "../api/decimalJson.ts";
import {
  buildEvalRunViewModel,
  NO_EVAL_RESULT_LABEL,
  parseExistingBillsImportInput,
  validateAccountingEntityInput,
  validateChargeDictionaryInput,
} from "../api/managementHelpers.ts";
import {
  LumnoButton,
  LumnoInput,
  LumnoPopconfirm,
  LumnoSegmentedControl,
  LumnoSelect,
  LumnoSwitch,
  LumnoTextarea,
} from "./LumnoPrimitives.tsx";

export type ManagementDrawerTab =
  | "entities"
  | "dictionary"
  | "existing_bills"
  | "eval_runs";

export interface ManagementDrawerProps {
  open: boolean;
  initialTab: ManagementDrawerTab;
  api: BillingApiClient;
  activeBatch: BatchDetail | null;
  entities: ReadonlyArray<AccountingEntity>;
  dictionaryList: ReadonlyArray<ChargeDictionaryEntry>;
  evalRuns: ReadonlyArray<EvalRunSummary>;
  selectedEntity: string;
  reapplyingDictionary: boolean;
  onClose: () => void;
  onEntitiesChanged: (entities: AccountingEntity[]) => void;
  onDictionaryChanged: (entries: ChargeDictionaryEntry[]) => void;
  onExistingBillsCountChanged: (count: number) => void;
  onEvalRunsChanged: (runs: EvalRunSummary[]) => void;
  onReapplyDictionary: () => Promise<void>;
  onNotify: (message: string, tone?: "info" | "error") => void;
}

const DICT_CATEGORY_OPTIONS = [
  { value: "origin_dest", label: "起运/目的港操作费 (origin_dest)" },
  { value: "freight", label: "海运与附加费 (freight)" },
  { value: "customs", label: "报关与查验费 (customs)" },
  { value: "general", label: "通用港杂费用 (general)" },
] as const;

const EVAL_MODE_OPTIONS: ReadonlyArray<{
  value: EvalRunSummary["run_mode"];
  label: string;
  sublabel: string;
}> = [
  {
    value: "OFFLINE_DETERMINISTIC",
    label: "离线确定性基准 (OFFLINE_DETERMINISTIC)",
    sublabel: "执行 12 组内置真值与对抗用例，验证确定性提取、匹配与门禁",
  },
  {
    value: "LIVE_LLM",
    label: "联机 LLM 稳定性评测 (LIVE_LLM)",
    sublabel: "重复运行 k 轮度量 pass@k / pass^k；无真实密钥时如实标 UNVERIFIED",
  },
  {
    value: "LIVE_E2E",
    label: "联机全链路端到端 (LIVE_E2E)",
    sublabel: "串联远端 OCR 网关与 LLM 多轮评测；失败中断时置无结果",
  },
];

export function ManagementDrawer({
  open,
  initialTab,
  api,
  activeBatch,
  entities,
  dictionaryList,
  evalRuns,
  selectedEntity,
  reapplyingDictionary,
  onClose,
  onEntitiesChanged,
  onDictionaryChanged,
  onExistingBillsCountChanged,
  onEvalRunsChanged,
  onReapplyDictionary,
  onNotify,
}: ManagementDrawerProps) {
  const [activeTab, setActiveTab] = useState<ManagementDrawerTab>(initialTab);

  // Tab 1: Accounting Entities
  const [entitySearch, setEntitySearch] = useState("");
  const [editingEntityId, setEditingEntityId] = useState<string | null>(null);
  const [entityNameInput, setEntityNameInput] = useState("");
  const [entityAliasesInput, setEntityAliasesInput] = useState("");
  const [entityIsDefault, setEntityIsDefault] = useState(false);
  const [entityError, setEntityError] = useState<string | null>(null);
  const [savingEntity, setSavingEntity] = useState(false);

  // Tab 2: Charge Dictionary & Reapply
  const [dictSearch, setDictSearch] = useState("");
  const [dictAliasInput, setDictAliasInput] = useState("");
  const [dictStandardInput, setDictStandardInput] = useState("");
  const [dictCategoryInput, setDictCategoryInput] = useState("origin_dest");
  const [editingDictId, setEditingDictId] = useState<string | null>(null);
  const [dictError, setDictError] = useState<string | null>(null);
  const [savingDict, setSavingDict] = useState(false);

  // Tab 3: Shared Existing Bills Baseline
  const [billEntityFilter, setBillEntityFilter] = useState<string>("");
  const [existingBills, setExistingBills] = useState<ExistingBill[]>([]);
  const [totalBillsCount, setTotalBillsCount] = useState<number>(0);
  const [loadingBills, setLoadingBills] = useState(false);
  const [billsImportText, setBillsImportText] = useState("");
  const [billsImportFilename, setBillsImportFilename] = useState<string | null>(
    null
  );
  const [billsImportEntity, setBillsImportEntity] = useState<string>(
    selectedEntity || "NEPTUNE SHIPPING LIMITED"
  );
  const [replaceExistingBills, setReplaceExistingBills] = useState(false);
  const [billsImportError, setBillsImportError] = useState<string | null>(null);
  const [lastImportSummary, setLastImportSummary] = useState<string | null>(
    null
  );
  const [importingBills, setImportingBills] = useState(false);
  const billsFileInputRef = useRef<HTMLInputElement | null>(null);

  // Tab 4: Harness Eval Dashboard
  const [evalMode, setEvalMode] =
    useState<EvalRunSummary["run_mode"]>("OFFLINE_DETERMINISTIC");
  const [kTrials, setKTrials] = useState<"1" | "3" | "5">("3");
  const [runningEval, setRunningEval] = useState(false);
  const [loadingEval, setLoadingEval] = useState(false);
  const [selectedEvalRunId, setSelectedEvalRunId] = useState<string | null>(
    null
  );

  useEffect(() => {
    if (open) {
      setActiveTab(initialTab);
    }
  }, [open, initialTab]);

  useEffect(() => {
    if (selectedEntity && !billsImportEntity) {
      setBillsImportEntity(selectedEntity);
    }
  }, [selectedEntity, billsImportEntity]);

  const loadBillsData = useCallback(
    async (entityFilter: string) => {
      setLoadingBills(true);
      try {
        const [filtered, all] = await Promise.all([
          api.listExistingBills(entityFilter || undefined),
          entityFilter ? api.listExistingBills() : Promise.resolve(null),
        ]);
        setExistingBills(filtered);
        const nextTotal = all ? all.length : filtered.length;
        setTotalBillsCount(nextTotal);
        onExistingBillsCountChanged(nextTotal);
      } catch (err) {
        onNotify(
          err instanceof Error ? err.message : "查询历史账单基准失败",
          "error"
        );
      } finally {
        setLoadingBills(false);
      }
    },
    [api, onExistingBillsCountChanged, onNotify]
  );

  const refreshEvalRuns = useCallback(async () => {
    setLoadingEval(true);
    try {
      const list = await api.listEvalRuns();
      onEvalRunsChanged(list);
      setSelectedEvalRunId((prev) =>
        prev && list.some((r) => r.id === prev) ? prev : list[0]?.id ?? null
      );
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "刷新 Harness 评测记录失败",
        "error"
      );
    } finally {
      setLoadingEval(false);
    }
  }, [api, onEvalRunsChanged, onNotify]);

  useEffect(() => {
    if (!open) return;
    void loadBillsData(billEntityFilter);
    void refreshEvalRuns();
  }, [open, billEntityFilter, loadBillsData, refreshEvalRuns]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const filteredEntities = useMemo(() => {
    const q = entitySearch.trim().toLowerCase();
    if (!q) return entities;
    return entities.filter(
      (ent) =>
        ent.canonical_name.toLowerCase().includes(q) ||
        ent.aliases.some((a) => a.toLowerCase().includes(q))
    );
  }, [entities, entitySearch]);

  const filteredDictionary = useMemo(() => {
    const q = dictSearch.trim().toLowerCase();
    if (!q) return dictionaryList;
    return dictionaryList.filter(
      (d) =>
        d.raw_alias.toLowerCase().includes(q) ||
        d.standard_name.toLowerCase().includes(q) ||
        d.category.toLowerCase().includes(q)
    );
  }, [dictionaryList, dictSearch]);

  const standardChargeSuggestions = useMemo(() => {
    const set = new Set<string>();
    for (const d of dictionaryList) {
      if (d.standard_name.trim()) set.add(d.standard_name.trim());
    }
    return Array.from(set);
  }, [dictionaryList]);

  const selectedEvalRun = useMemo(() => {
    if (evalRuns.length === 0) return null;
    if (selectedEvalRunId) {
      return evalRuns.find((r) => r.id === selectedEvalRunId) ?? evalRuns[0];
    }
    return evalRuns[0];
  }, [evalRuns, selectedEvalRunId]);

  const evalViewModel = useMemo(
    () => (selectedEvalRun ? buildEvalRunViewModel(selectedEvalRun) : null),
    [selectedEvalRun]
  );

  // Handlers: Tab 1 Entities
  const resetEntityForm = () => {
    setEditingEntityId(null);
    setEntityNameInput("");
    setEntityAliasesInput("");
    setEntityIsDefault(false);
    setEntityError(null);
  };

  const handleSaveEntity = async () => {
    const check = validateAccountingEntityInput(
      {
        canonicalName: entityNameInput,
        aliasesText: entityAliasesInput,
        isDefault: entityIsDefault,
      },
      entities,
      editingEntityId
    );
    if (!check.valid) {
      setEntityError(check.error);
      return;
    }
    setEntityError(null);
    setSavingEntity(true);
    try {
      if (editingEntityId) {
        await api.updateAccountingEntity(editingEntityId, check.payload);
        onNotify(`已更新记账主体「${check.payload.canonical_name}」`);
      } else {
        await api.createAccountingEntity(check.payload);
        onNotify(`已新增记账主体「${check.payload.canonical_name}」`);
      }
      const nextList = await api.listAccountingEntities();
      onEntitiesChanged(nextList);
      resetEntityForm();
    } catch (err) {
      setEntityError(
        err instanceof Error ? err.message : "保存记账主体档案失败"
      );
    } finally {
      setSavingEntity(false);
    }
  };

  // Handlers: Tab 2 Dictionary
  const resetDictForm = () => {
    setEditingDictId(null);
    setDictAliasInput("");
    setDictStandardInput("");
    setDictCategoryInput("origin_dest");
    setDictError(null);
  };

  const handleSaveDictionaryEntry = async () => {
    const check = validateChargeDictionaryInput(
      dictAliasInput,
      dictStandardInput,
      dictCategoryInput
    );
    if (!check.valid) {
      setDictError(check.error);
      return;
    }
    setDictError(null);
    setSavingDict(true);
    try {
      await api.upsertChargeDictionaryEntry(check.payload);
      const nextDict = await api.listChargeDictionary();
      onDictionaryChanged(nextDict);
      onNotify(
        editingDictId
          ? `已更新词典映射：${check.payload.raw_alias} → ${check.payload.standard_name}`
          : `已新增词典映射：${check.payload.raw_alias} → ${check.payload.standard_name}`
      );
      resetDictForm();
    } catch (err) {
      setDictError(
        err instanceof Error ? err.message : "保存费用标准映射失败"
      );
    } finally {
      setSavingDict(false);
    }
  };

  const handleDeleteDictionaryEntry = async (entry: ChargeDictionaryEntry) => {
    try {
      await api.deleteChargeDictionaryEntry(entry.id);
      const nextDict = await api.listChargeDictionary();
      onDictionaryChanged(nextDict);
      if (editingDictId === entry.id) {
        resetDictForm();
      }
      onNotify(`已删除自定义映射「${entry.raw_alias}」`);
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "删除词典条目失败",
        "error"
      );
    }
  };

  // Handlers: Tab 3 Existing Bills
  const handlePickBillsFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await file.text();
    setBillsImportFilename(file.name);
    setBillsImportText(text);
    setBillsImportError(null);
  };

  const handleImportBills = async () => {
    const parsed = parseExistingBillsImportInput(billsImportText, {
      filename: billsImportFilename ?? undefined,
      defaultAccountingEntity: billsImportEntity || selectedEntity,
    });
    if (!parsed.valid) {
      setBillsImportError(parsed.error);
      return;
    }
    setBillsImportError(null);
    setImportingBills(true);
    try {
      const res = await api.importExistingBills({
        default_accounting_entity: billsImportEntity || selectedEntity || null,
        replace_existing: replaceExistingBills,
        bills: parsed.bills,
      });
      setLastImportSummary(
        `本次导入 ${res.imported_count} 条，共享基准库当前共 ${res.total_count} 条`
      );
      setBillsImportText("");
      setBillsImportFilename(null);
      await loadBillsData(billEntityFilter);
      onNotify(`已成功导入 ${res.imported_count} 条历史账单基准`);
    } catch (err) {
      setBillsImportError(
        err instanceof Error ? err.message : "导入历史账单基准失败"
      );
    } finally {
      setImportingBills(false);
    }
  };

  const handleClearBills = async () => {
    try {
      await api.clearExistingBills(billEntityFilter || undefined);
      setLastImportSummary(null);
      await loadBillsData(billEntityFilter);
      onNotify(
        billEntityFilter
          ? `已清理主体「${billEntityFilter}」的共享历史基准账单`
          : "已清空工作台共享历史基准库"
      );
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "清理历史基准账单失败",
        "error"
      );
    }
  };

  // Handlers: Tab 4 Harness Eval
  const handleTriggerEval = async () => {
    setRunningEval(true);
    try {
      const k = Number.parseInt(kTrials, 10);
      const created = await api.triggerEvalRun({
        run_mode: evalMode,
        k_trials: k,
      });
      const latest = await api.listEvalRuns();
      onEvalRunsChanged(latest);
      setSelectedEvalRunId(created.id);
      if (created.status === "COMPLETED") {
        onNotify(`评测完成：${created.run_mode}（共 ${created.total_cases} 组）`);
      } else {
        onNotify(
          `${created.run_mode} 状态为 ${created.status}，指标置为「${NO_EVAL_RESULT_LABEL}」`,
          created.status === "FAILED" ? "error" : "info"
        );
      }
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "触发 Harness 评测失败",
        "error"
      );
    } finally {
      setRunningEval(false);
    }
  };

  if (!open || typeof document === "undefined") {
    return null;
  }

  const entityFilterOptions = [
    { value: "", label: "全部记账主体" },
    ...entities.map((e) => ({
      value: e.canonical_name,
      label: e.canonical_name,
    })),
  ];

  const importEntityOptions = entities.map((e) => ({
    value: e.canonical_name,
    label: e.canonical_name,
  }));
  if (
    billsImportEntity &&
    !importEntityOptions.some((o) => o.value === billsImportEntity)
  ) {
    importEntityOptions.push({
      value: billsImportEntity,
      label: billsImportEntity,
    });
  }

  return createPortal(
    <div
      className="wb-drawer-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <aside
        className="wb-drawer-panel"
        role="dialog"
        aria-modal="true"
        aria-label="主数据管理与 Harness 评测看板"
      >
        <div className="wb-drawer-header">
          <div style={{ minWidth: 0 }}>
            <h2 className="lumno-modal-title">主数据管理与 Harness 评测看板</h2>
            <div className="wb-helper-note">
              维护记账主体、费用词典重算、共享基准库与双轨稳定性评测
            </div>
          </div>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
            {activeBatch ? (
              <span className="wb-badge wb-badge--muted wb-tabular">
                当前批 {activeBatch.short_code}
              </span>
            ) : null}
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-close-line"
              aria-label="关闭管理抽屉"
              onClick={onClose}
            />
          </div>
        </div>

        <div className="wb-drawer-tabs">
          <LumnoSegmentedControl<ManagementDrawerTab>
            value={activeTab}
            wrap
            ariaLabel="管理抽屉功能页签"
            options={[
              { value: "entities", label: "记账主体", count: entities.length },
              {
                value: "dictionary",
                label: "费用词典",
                count: dictionaryList.length,
              },
              {
                value: "existing_bills",
                label: "历史基准库",
                count: totalBillsCount,
              },
              {
                value: "eval_runs",
                label: "Harness 评测",
                count: evalRuns.length,
              },
            ]}
            onChange={setActiveTab}
          />
        </div>

        <div className="wb-drawer-body">
          {/* Tab 1: Accounting Entities (Query / Add / Edit only - NO delete) */}
          {activeTab === "entities" ? (
            <div className="wb-drawer-stack">
              <div className="wb-notice-banner">
                <span>
                  记账主体档案仅支持查询、新增与修订别名（为保障历史批次 AR/AP 与审计可追溯，系统不提供删除主体操作）。
                </span>
              </div>

              <div className="wb-section-box" aria-label="新增或编辑记账主体">
                <div className="wb-field-label">
                  <span>
                    {editingEntityId ? "编辑记账主体档案" : "新增记账主体档案"}
                  </span>
                  {editingEntityId ? (
                    <LumnoButton
                      variant="ghost"
                      compact
                      onClick={resetEntityForm}
                    >
                      取消编辑
                    </LumnoButton>
                  ) : null}
                </div>

                <label className="wb-form-field">
                  <span className="wb-form-label">主体规范名称 (canonical_name) *</span>
                  <LumnoInput
                    value={entityNameInput}
                    placeholder="如 NEPTUNE SHIPPING LIMITED"
                    aria-label="记账主体规范名称"
                    onChange={(e) => {
                      setEntityNameInput(e.target.value);
                      setEntityError(null);
                    }}
                  />
                </label>

                <label className="wb-form-field">
                  <span className="wb-form-label">别名列表 (aliases，逗号或换行分隔)</span>
                  <LumnoInput
                    value={entityAliasesInput}
                    placeholder="如 Neptune Shipping, NEPTUNE (LAX), 海王星航运"
                    aria-label="记账主体别名列表"
                    onChange={(e) => setEntityAliasesInput(e.target.value)}
                  />
                </label>

                <div className="wb-switch-row">
                  <div>
                    <div style={{ fontSize: "12px", fontWeight: 600 }}>
                      设为默认记账主体
                    </div>
                    <div className="wb-helper-note">
                      工作台启动或新投递未指定主体时默认采用该航运主体
                    </div>
                  </div>
                  <LumnoSwitch
                    ariaLabel="设为默认记账主体"
                    compact
                    checked={entityIsDefault}
                    onChange={setEntityIsDefault}
                  />
                </div>

                {entityError ? (
                  <div className="wb-field-error">{entityError}</div>
                ) : null}

                <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                  <LumnoButton
                    variant="primary"
                    icon={editingEntityId ? "ri-save-line" : "ri-add-line"}
                    busy={savingEntity}
                    onClick={() => void handleSaveEntity()}
                  >
                    {editingEntityId ? "保存主体修改" : "新增记账主体"}
                  </LumnoButton>
                </div>
              </div>

              <div className="wb-section-box" aria-label="记账主体列表">
                <div className="wb-field-label">
                  <span>已登记记账主体 ({filteredEntities.length})</span>
                </div>
                <LumnoInput
                  value={entitySearch}
                  placeholder="搜索记账主体名称或别名..."
                  aria-label="搜索记账主体"
                  onChange={(e) => setEntitySearch(e.target.value)}
                />

                {filteredEntities.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>暂无匹配的记账主体档案</span>
                  </div>
                ) : (
                  <div className="wb-mgmt-list">
                    {filteredEntities.map((ent) => (
                      <div key={ent.id} className="wb-mgmt-card">
                        <div className="wb-mgmt-card-head">
                          <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap", minWidth: 0 }}>
                            <strong style={{ overflowWrap: "anywhere" }}>
                              {ent.canonical_name}
                            </strong>
                            {ent.is_default ? (
                              <span className="wb-badge wb-badge--ok">
                                默认航运主体
                              </span>
                            ) : null}
                          </div>
                          <LumnoButton
                            variant="secondary"
                            compact
                            icon="ri-edit-line"
                            onClick={() => {
                              setEditingEntityId(ent.id);
                              setEntityNameInput(ent.canonical_name);
                              setEntityAliasesInput(ent.aliases.join(", "));
                              setEntityIsDefault(ent.is_default);
                              setEntityError(null);
                            }}
                          >
                            编辑
                          </LumnoButton>
                        </div>

                        <div className="wb-chip-row">
                          <span className="wb-helper-note">别名：</span>
                          {ent.aliases.length === 0 ? (
                            <span className="wb-helper-note">无别名</span>
                          ) : (
                            ent.aliases.map((alias) => (
                              <span key={alias} className="wb-badge wb-badge--muted">
                                {alias}
                              </span>
                            ))
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : null}

          {/* Tab 2: Charge Dictionary & Current Batch Reapply */}
          {activeTab === "dictionary" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="当前批次词典重标准化">
                <div className="wb-field-label">
                  <span>按最新词典重新标准化当前批次</span>
                  {activeBatch ? (
                    <span className="wb-badge wb-badge--info wb-tabular">
                      目标批次：{activeBatch.short_code}
                    </span>
                  ) : (
                    <span className="wb-badge wb-badge--muted">未选中批次</span>
                  )}
                </div>
                <div className="wb-helper-note">
                  只重算当前批次 charge_item，不干扰其他批次且保留已有人工修订（human_overrides 锁定字段不会被覆写）。
                </div>
                <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                  <LumnoButton
                    variant="primary"
                    icon="ri-sparkling-line"
                    busy={reapplyingDictionary}
                    disabled={!activeBatch}
                    onClick={() => void onReapplyDictionary()}
                  >
                    仅重新标准化当前批次
                  </LumnoButton>
                </div>
              </div>

              <div className="wb-section-box" aria-label="新增或编辑费用映射词典">
                <div className="wb-field-label">
                  <span>
                    {editingDictId ? "编辑自定义费用映射" : "新增自定义费用映射"}
                  </span>
                  {editingDictId ? (
                    <LumnoButton variant="ghost" compact onClick={resetDictForm}>
                      取消编辑
                    </LumnoButton>
                  ) : null}
                </div>

                <div className="wb-form-grid-2">
                  <label className="wb-form-field">
                    <span className="wb-form-label">原始费用别名 (raw_alias) *</span>
                    <LumnoInput
                      value={dictAliasInput}
                      placeholder="如 HIGH PRECISION TEST FEE"
                      aria-label="原始费用别名"
                      onChange={(e) => {
                        setDictAliasInput(e.target.value);
                        setDictError(null);
                      }}
                    />
                  </label>
                  <label className="wb-form-field">
                    <span className="wb-form-label">标准费用名称 (standard_name) *</span>
                    <LumnoInput
                      list="wb-mgmt-std-charges"
                      value={dictStandardInput}
                      placeholder="如 Handling Charge"
                      aria-label="标准费用名称"
                      onChange={(e) => {
                        setDictStandardInput(e.target.value);
                        setDictError(null);
                      }}
                    />
                  </label>
                </div>

                <div className="wb-form-field">
                  <span className="wb-form-label">费用分类 (category)</span>
                  <LumnoSelect
                    value={dictCategoryInput}
                    ariaLabel="费用分类"
                    options={DICT_CATEGORY_OPTIONS}
                    onChange={setDictCategoryInput}
                  />
                </div>

                {dictError ? <div className="wb-field-error">{dictError}</div> : null}

                <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                  <LumnoButton
                    variant="primary"
                    icon={editingDictId ? "ri-save-line" : "ri-add-line"}
                    busy={savingDict}
                    onClick={() => void handleSaveDictionaryEntry()}
                  >
                    {editingDictId ? "保存词典映射" : "保存并添加到词典"}
                  </LumnoButton>
                </div>
              </div>

              <div className="wb-section-box" aria-label="费用标准映射列表">
                <div className="wb-field-label">
                  <span>词典映射条目 ({filteredDictionary.length})</span>
                </div>
                <LumnoInput
                  value={dictSearch}
                  placeholder="筛选原始别名、标准费用名或分类..."
                  aria-label="筛选费用词典"
                  onChange={(e) => setDictSearch(e.target.value)}
                />

                {filteredDictionary.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>暂无匹配的词典映射条目</span>
                  </div>
                ) : (
                  <div className="wb-mgmt-list">
                    {filteredDictionary.map((entry) => (
                      <div key={entry.id} className="wb-mgmt-card">
                        <div className="wb-mgmt-card-head">
                          <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: "3px" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                              <span className="wb-mono" style={{ fontWeight: 600, overflowWrap: "anywhere" }}>
                                {entry.raw_alias}
                              </span>
                              <span className="wb-helper-note">→</span>
                              <strong>{entry.standard_name}</strong>
                            </div>
                            <div className="wb-chip-row">
                              <span
                                className={`wb-badge ${
                                  entry.is_builtin ? "wb-badge--muted" : "wb-badge--info"
                                }`}
                              >
                                {entry.is_builtin ? "系统内置（不可删）" : "自定义映射"}
                              </span>
                              <span className="wb-badge wb-badge--muted wb-mono">
                                {entry.category}
                              </span>
                            </div>
                          </div>

                          {!entry.is_builtin ? (
                            <div style={{ display: "inline-flex", gap: "4px", flexShrink: 0 }}>
                              <LumnoButton
                                variant="ghost"
                                compact
                                icon="ri-edit-line"
                                onClick={() => {
                                  setEditingDictId(entry.id);
                                  setDictAliasInput(entry.raw_alias);
                                  setDictStandardInput(entry.standard_name);
                                  setDictCategoryInput(entry.category);
                                  setDictError(null);
                                }}
                              >
                                编辑
                              </LumnoButton>
                              <LumnoPopconfirm
                                title="确认删除自定义映射？"
                                description={`删除「${entry.raw_alias} → ${entry.standard_name}」后不会自动改写已有批次，需重新标准化才会生效。`}
                                confirmLabel="确认删除"
                                onConfirm={() =>
                                  void handleDeleteDictionaryEntry(entry)
                                }
                              >
                                {(triggerProps) => (
                                  <LumnoButton
                                    variant="ghost"
                                    compact
                                    icon="ri-delete-bin-line"
                                    {...triggerProps}
                                  >
                                    删除
                                  </LumnoButton>
                                )}
                              </LumnoPopconfirm>
                            </div>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <datalist id="wb-mgmt-std-charges">
                {standardChargeSuggestions.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </div>
          ) : null}

          {/* Tab 3: Shared Existing Bills Baseline */}
          {activeTab === "existing_bills" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="共享历史基准库筛选与清理">
                <div className="wb-field-label">
                  <span>按记账主体查看共享基准库</span>
                  <span className="wb-tabular wb-helper-note">
                    当前视图 {existingBills.length} 条 / 全局共 {totalBillsCount} 条
                  </span>
                </div>
                <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                  <div style={{ flex: "1 1 220px", minWidth: 0 }}>
                    <LumnoSelect
                      value={billEntityFilter}
                      ariaLabel="按记账主体筛选共享历史账单"
                      options={entityFilterOptions}
                      onChange={setBillEntityFilter}
                    />
                  </div>
                  <div className="wb-action-row">
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-refresh-line"
                      busy={loadingBills}
                      onClick={() => void loadBillsData(billEntityFilter)}
                    >
                      刷新
                    </LumnoButton>
                    <LumnoPopconfirm
                      title={
                        billEntityFilter
                          ? `确认清理「${billEntityFilter}」历史账单？`
                          : "确认清空全部共享历史账单基准？"
                      }
                      description="仅清理工作台共享历史基准库，绝不会修改或污染各批次已保存的随批隔离快照（existing_bills_json / 复核包快照）。"
                      confirmLabel="确认清理"
                      onConfirm={() => void handleClearBills()}
                    >
                      {(triggerProps) => (
                        <LumnoButton
                          variant="ghost"
                          compact
                          icon="ri-delete-bin-6-line"
                          disabled={existingBills.length === 0}
                          {...triggerProps}
                        >
                          {billEntityFilter ? "清理该主体基准" : "清空共享库"}
                        </LumnoButton>
                      )}
                    </LumnoPopconfirm>
                  </div>
                </div>
              </div>

              <div className="wb-section-box" aria-label="无损导入历史账单基准">
                <div className="wb-field-label">
                  <span>无损导入历史账单基准（JSON / CSV）</span>
                  <LumnoButton
                    variant="secondary"
                    compact
                    icon="ri-file-upload-line"
                    onClick={() => billsFileInputRef.current?.click()}
                  >
                    {billsImportFilename
                      ? `已选：${billsImportFilename}`
                      : "选择 .json / .csv"}
                  </LumnoButton>
                </div>

                <input
                  ref={billsFileInputRef}
                  type="file"
                  accept=".json,.csv,application/json,text/csv"
                  style={{ display: "none" }}
                  onChange={(e) => void handlePickBillsFile(e)}
                />

                <LumnoTextarea
                  value={billsImportText}
                  placeholder={
                    '粘贴 JSON 数组（如 [{"reference_no":"DN-202609-001","charge_item":"Handling Charge","amount":9007199254740991.01,"currency":"USD","posting_type":"AP"}]）或带表头 CSV'
                  }
                  aria-label="历史账单 JSON 或 CSV 导入内容"
                  style={{ minHeight: "100px", fontFamily: "var(--lumno-font-mono)" }}
                  onChange={(e) => {
                    setBillsImportText(e.target.value);
                    setBillsImportError(null);
                  }}
                />

                <div className="wb-form-grid-2">
                  <div className="wb-form-field">
                    <span className="wb-form-label">缺省归属记账主体</span>
                    <LumnoSelect
                      value={billsImportEntity}
                      ariaLabel="导入历史账单默认归属主体"
                      options={importEntityOptions}
                      onChange={setBillsImportEntity}
                    />
                  </div>
                  <div className="wb-switch-row">
                    <div>
                      <div style={{ fontSize: "12px", fontWeight: 600 }}>
                        覆盖已有基准 (replace_existing)
                      </div>
                      <div className="wb-helper-note">
                        开启后先清空对应主体共享旧账再导入
                      </div>
                    </div>
                    <LumnoSwitch
                      ariaLabel="导入前覆盖已有历史账单"
                      compact
                      checked={replaceExistingBills}
                      onChange={setReplaceExistingBills}
                    />
                  </div>
                </div>

                {billsImportError ? (
                  <div className="wb-field-error">{billsImportError}</div>
                ) : null}
                {lastImportSummary ? (
                  <div className="wb-notice-banner">
                    <span>{lastImportSummary}</span>
                  </div>
                ) : null}

                <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                  <LumnoButton
                    variant="primary"
                    icon="ri-database-2-line"
                    busy={importingBills}
                    onClick={() => void handleImportBills()}
                  >
                    导入共享历史基准
                  </LumnoButton>
                </div>
              </div>

              <div className="wb-section-box" aria-label="共享历史账单列表">
                <div className="wb-field-label">
                  <span>共享基准账单明细</span>
                </div>
                {existingBills.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>当前筛选下暂无共享历史账单基准</span>
                  </div>
                ) : (
                  <div className="wb-mgmt-list">
                    {existingBills.map((bill) => (
                      <div key={bill.id} className="wb-mgmt-card">
                        <div className="wb-mgmt-card-head">
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontWeight: 600, overflowWrap: "anywhere" }}>
                              {bill.charge_item}
                              {bill.reference_no ? (
                                <span className="wb-mono wb-helper-note" style={{ marginLeft: "6px" }}>
                                  #{bill.reference_no}
                                </span>
                              ) : null}
                            </div>
                            <div className="wb-helper-note" style={{ overflowWrap: "anywhere" }}>
                              {bill.accounting_entity}
                            </div>
                          </div>
                          <div className="wb-tabular" style={{ fontWeight: 700, flexShrink: 0 }}>
                            {formatLosslessAmount(bill, "amount")} {bill.currency}
                          </div>
                        </div>
                        <div className="wb-chip-row">
                          <span className="wb-badge wb-badge--muted">
                            {bill.posting_type}
                          </span>
                          {bill.payment_term ? (
                            <span className="wb-badge wb-badge--muted">
                              {bill.payment_term}
                            </span>
                          ) : null}
                          <span className="wb-badge wb-badge--muted wb-tabular">
                            v{bill.version}
                          </span>
                          {bill.mbl_no ? (
                            <span className="wb-badge wb-badge--muted wb-mono">
                              MBL {bill.mbl_no}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : null}

          {/* Tab 4: Harness Evaluation Dashboard */}
          {activeTab === "eval_runs" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="Harness 评测触发控制台">
                <div className="wb-field-label">
                  <span>执行或刷新 Harness 评测基准</span>
                  <span className="wb-helper-note">区分确定性真值与 k 轮稳定性</span>
                </div>

                <div className="wb-form-field">
                  <span className="wb-form-label">评测模式 (run_mode)</span>
                  <LumnoSelect
                    value={evalMode}
                    ariaLabel="选择 Harness 评测模式"
                    options={EVAL_MODE_OPTIONS}
                    onChange={(val) =>
                      setEvalMode(val as EvalRunSummary["run_mode"])
                    }
                  />
                </div>

                <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                    <span className="wb-form-label">采样轮次 (k_trials)</span>
                    <LumnoSegmentedControl<"1" | "3" | "5">
                      value={kTrials}
                      ariaLabel="评测重复轮次 k_trials"
                      options={[
                        { value: "1", label: "k = 1" },
                        { value: "3", label: "k = 3" },
                        { value: "5", label: "k = 5" },
                      ]}
                      onChange={setKTrials}
                    />
                  </div>
                  <div className="wb-action-row">
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-refresh-line"
                      busy={loadingEval}
                      onClick={() => void refreshEvalRuns()}
                    >
                      刷新记录
                    </LumnoButton>
                    <LumnoButton
                      variant="primary"
                      compact
                      icon="ri-play-line"
                      busy={runningEval}
                      onClick={() => void handleTriggerEval()}
                    >
                      运行评测
                    </LumnoButton>
                  </div>
                </div>
              </div>

              {evalRuns.length === 0 ? (
                <div className="wb-empty-state lumno-paper-card">
                  <i
                    className="ri-icon ri-bar-chart-box-line"
                    style={{ "--ri-size": "28px" } as React.CSSProperties}
                    aria-hidden="true"
                  />
                  <strong>尚未运行过 Harness 评测基准</strong>
                  <span>
                    本看板绝不伪造任何示例高分。请选择上方「离线确定性基准」或「联机 LLM 稳定性」运行并查看真实观测指标。
                  </span>
                </div>
              ) : (
                <>
                  <div className="wb-section-box" aria-label="历史评测运行记录切换">
                    <div className="wb-field-label">
                      <span>最近评测运行记录 ({evalRuns.length})</span>
                    </div>
                    <div className="wb-eval-run-tabs">
                      {evalRuns.map((run) => {
                        const active = selectedEvalRun?.id === run.id;
                        return (
                          <button
                            key={run.id}
                            type="button"
                            className="wb-eval-run-chip"
                            data-active={active ? "true" : "false"}
                            onClick={() => setSelectedEvalRunId(run.id)}
                          >
                            <span style={{ fontWeight: 600 }}>{run.run_mode}</span>
                            <span className="wb-tabular">
                              {run.status} · {run.actual_trials}/{run.k_trials} 轮
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {evalViewModel ? (
                    <>
                      <div className="wb-section-box" aria-label="评测运行概要">
                        <div className="wb-mgmt-card-head">
                          <div style={{ minWidth: 0 }}>
                            <strong style={{ overflowWrap: "anywhere" }}>
                              {evalViewModel.runModeLabel}
                            </strong>
                            <div className="wb-helper-note wb-mono">
                              模型：{evalViewModel.modelName} · {evalViewModel.createdAt}
                            </div>
                          </div>
                          <span className={`wb-badge wb-badge--${evalViewModel.statusTone}`}>
                            {evalViewModel.statusLabel}
                          </span>
                        </div>

                        <div className="wb-chip-row wb-tabular">
                          <span className="wb-badge wb-badge--muted">
                            实际完成轮次：{evalViewModel.trialsText}
                          </span>
                          <span className="wb-badge wb-badge--muted">
                            用例总数：{evalViewModel.totalCases} 组
                          </span>
                        </div>

                        {evalViewModel.hasFailureBanner ? (
                          <div
                            className="wb-alert-item"
                            data-severity={
                              evalViewModel.status === "FAILED"
                                ? "BLOCKER"
                                : "WARNING"
                            }
                            role="status"
                          >
                            <strong>
                              {evalViewModel.status === "FAILED"
                                ? "评测执行失败 — 全部未完成指标置为「无结果」（绝不假标 0%）"
                                : "联机前置条件未验证（UNVERIFIED）— 指标置为「无结果」（绝不假标 0%）"}
                            </strong>
                            <div>{evalViewModel.failureReason}</div>
                            <div className="wb-helper-note wb-tabular">
                              实际完成轮次：{evalViewModel.trialsText}
                            </div>
                          </div>
                        ) : null}
                      </div>

                      <div className="wb-section-box" aria-label="真值准确率指标区">
                        <div className="wb-field-label">
                          <span>真值准确率与异常拦截</span>
                          <span className="wb-helper-note">确定性真值对比</span>
                        </div>
                        <div className="wb-eval-metrics-grid">
                          {evalViewModel.truthMetrics.map((m) => (
                            <div key={m.key} className="wb-eval-metric-card">
                              <div className="wb-kv-label">{m.label}</div>
                              <div className="wb-eval-metric-value wb-tabular">
                                {m.isUnavailable ? (
                                  <span className="wb-no-result-pill">
                                    {m.displayText}
                                  </span>
                                ) : (
                                  <span>{m.displayText}</span>
                                )}
                              </div>
                              {m.fractionText ? (
                                <div className="wb-helper-note wb-tabular">
                                  分子/分母：{m.fractionText}
                                </div>
                              ) : null}
                              <div className="wb-helper-note">{m.description}</div>
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="wb-section-box" aria-label="多次运行稳定性指标区">
                        <div className="wb-field-label">
                          <span>多次运行稳定性 (pass@k / pass^k)</span>
                          <span className="wb-tabular wb-helper-note">
                            轮次 {evalViewModel.trialsText}
                          </span>
                        </div>
                        <div className="wb-eval-metrics-grid">
                          {evalViewModel.stabilityMetrics.map((m) => (
                            <div key={m.key} className="wb-eval-metric-card">
                              <div className="wb-kv-label">{m.label}</div>
                              <div className="wb-eval-metric-value wb-tabular">
                                {m.isUnavailable ? (
                                  <span className="wb-no-result-pill">
                                    {m.displayText}
                                  </span>
                                ) : (
                                  <span>{m.displayText}</span>
                                )}
                              </div>
                              {m.fractionText ? (
                                <div className="wb-helper-note wb-tabular">
                                  分子/分母：{m.fractionText}
                                </div>
                              ) : null}
                              <div className="wb-helper-note">{m.description}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </>
                  ) : null}
                </>
              )}
            </div>
          ) : null}
        </div>
      </aside>
    </div>,
    document.body
  );
}
