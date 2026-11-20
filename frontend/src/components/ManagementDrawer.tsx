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
} from "../api/client.ts";
import { formatLosslessAmount } from "../api/decimalJson.ts";
import {
  beginExistingBillsLoad,
  buildEvalRunViewModel,
  completeExistingBillsLoadFailure,
  completeExistingBillsLoadSuccess,
  createExistingBillsViewState,
  formatEvalBenchmarkCasesText,
  getExistingBillsHeaderStatus,
  NO_EVAL_RESULT_LABEL,
  parseExistingBillsImportInput,
  setExistingBillsFilter,
  validateAccountingEntityInput,
  validateChargeDictionaryInput,
  type ExistingBillsViewState,
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
  { value: "origin_dest", label: "起运/目的港操作费" },
  { value: "freight", label: "海运与附加费" },
  { value: "customs", label: "报关与查验费" },
  { value: "general", label: "通用港杂费用" },
] as const;

const EVAL_MODE_OPTIONS: ReadonlyArray<{
  value: EvalRunSummary["run_mode"];
  label: string;
  sublabel: string;
}> = [
  {
    value: "OFFLINE_DETERMINISTIC",
    label: "离线确定性基准 (OFFLINE_DETERMINISTIC)",
    sublabel: "执行 13 组（16 项）内置真值与对抗用例，验证确定性提取、匹配与门禁",
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
  const [entityFormOpen, setEntityFormOpen] = useState(false);
  const [editingEntityId, setEditingEntityId] = useState<string | null>(null);
  const [entityNameInput, setEntityNameInput] = useState("");
  const [entityAliasesInput, setEntityAliasesInput] = useState("");
  const [entityIsDefault, setEntityIsDefault] = useState(false);
  const [entityError, setEntityError] = useState<string | null>(null);
  const [savingEntity, setSavingEntity] = useState(false);
  const [loadingEntities, setLoadingEntities] = useState(false);
  const [entitiesLoadError, setEntitiesLoadError] = useState<string | null>(
    null
  );
  const entitiesReqSeqRef = useRef(0);

  // Tab 2: Charge Dictionary & Reapply
  const [dictSearch, setDictSearch] = useState("");
  const [dictFormOpen, setDictFormOpen] = useState(false);
  const [dictAliasInput, setDictAliasInput] = useState("");
  const [dictStandardInput, setDictStandardInput] = useState("");
  const [dictCategoryInput, setDictCategoryInput] = useState("origin_dest");
  const [editingDictId, setEditingDictId] = useState<string | null>(null);
  const [dictError, setDictError] = useState<string | null>(null);
  const [savingDict, setSavingDict] = useState(false);
  const [loadingDict, setLoadingDict] = useState(false);
  const [dictLoadError, setDictLoadError] = useState<string | null>(null);
  const dictReqSeqRef = useRef(0);

  // Tab 3: Shared Existing Bills Baseline
  const [billEntityFilter, setBillEntityFilter] = useState<string>("");
  const [billsSearch, setBillsSearch] = useState<string>("");
  const [billsImportOpen, setBillsImportOpen] = useState(false);
  const billEntityFilterRef = useRef<string>(billEntityFilter);
  billEntityFilterRef.current = billEntityFilter;
  const [billsViewState, setBillsViewState] = useState<ExistingBillsViewState>(
    () => createExistingBillsViewState("")
  );
  const billsReqSeqRef = useRef(0);
  const existingBills = billsViewState.bills;
  const billsLoadedForFilter = billsViewState.loadedForFilter;
  const loadingBills = billsViewState.loading;
  const billsLoadError = billsViewState.loadError;
  const billsHeaderStatus = getExistingBillsHeaderStatus(billsViewState);
  const [billsImportText, setBillsImportText] = useState("");
  const [billsImportFilename, setBillsImportFilename] = useState<string | null>(
    null
  );
  const [billsImportEntity, setBillsImportEntity] = useState<string>(
    selectedEntity || "NEPTUNE SHIPPING LIMITED"
  );
  const [replaceExistingBills, setReplaceExistingBills] = useState(false);
  const [billsImportError, setBillsImportError] = useState<string | null>(null);
  const [billsImportRowErrors, setBillsImportRowErrors] = useState<string[]>(
    []
  );
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
  const [evalLoadError, setEvalLoadError] = useState<string | null>(null);
  const evalReqSeqRef = useRef(0);
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

  const loadEntitiesData = useCallback(async () => {
    const seq = ++entitiesReqSeqRef.current;
    setLoadingEntities(true);
    setEntitiesLoadError(null);
    try {
      const list = await api.listAccountingEntities();
      if (seq !== entitiesReqSeqRef.current) return;
      onEntitiesChanged(list);
      setEntitiesLoadError(null);
    } catch (err) {
      if (seq !== entitiesReqSeqRef.current) return;
      setEntitiesLoadError(
        err instanceof Error ? err.message : "查询记账主体档案失败"
      );
    } finally {
      if (seq === entitiesReqSeqRef.current) {
        setLoadingEntities(false);
      }
    }
  }, [api, onEntitiesChanged]);

  const loadDictionaryData = useCallback(async () => {
    const seq = ++dictReqSeqRef.current;
    setLoadingDict(true);
    setDictLoadError(null);
    try {
      const list = await api.listChargeDictionary();
      if (seq !== dictReqSeqRef.current) return;
      onDictionaryChanged(list);
      setDictLoadError(null);
    } catch (err) {
      if (seq !== dictReqSeqRef.current) return;
      setDictLoadError(
        err instanceof Error ? err.message : "查询费用标准映射词典失败"
      );
    } finally {
      if (seq === dictReqSeqRef.current) {
        setLoadingDict(false);
      }
    }
  }, [api, onDictionaryChanged]);

  const loadBillsData = useCallback(
    async (entityFilter: string) => {
      const seq = ++billsReqSeqRef.current;
      setBillsViewState((prev) =>
        beginExistingBillsLoad(prev, entityFilter, seq).nextState
      );
      try {
        const [filtered, all] = await Promise.all([
          api.listExistingBills(entityFilter || undefined),
          entityFilter ? api.listExistingBills() : Promise.resolve(null),
        ]);
        if (
          seq !== billsReqSeqRef.current ||
          entityFilter !== billEntityFilterRef.current
        ) {
          return;
        }
        const nextTotal = all ? all.length : filtered.length;
        onExistingBillsCountChanged(nextTotal);
        setBillsViewState((prev) =>
          completeExistingBillsLoadSuccess(
            prev,
            seq,
            entityFilter,
            billEntityFilterRef.current,
            filtered,
            nextTotal
          ).nextState
        );
      } catch (err) {
        if (
          seq !== billsReqSeqRef.current ||
          entityFilter !== billEntityFilterRef.current
        ) {
          return;
        }
        const msg =
          err instanceof Error ? err.message : "查询历史账单库失败";
        setBillsViewState((prev) =>
          completeExistingBillsLoadFailure(
            prev,
            seq,
            entityFilter,
            billEntityFilterRef.current,
            msg
          ).nextState
        );
      }
    },
    [api, onExistingBillsCountChanged]
  );

  const refreshEvalRuns = useCallback(async () => {
    const seq = ++evalReqSeqRef.current;
    setLoadingEval(true);
    setEvalLoadError(null);
    try {
      const list = await api.listEvalRuns();
      if (seq !== evalReqSeqRef.current) return;
      onEvalRunsChanged(list);
      setSelectedEvalRunId((prev) =>
        prev && list.some((r) => r.id === prev) ? prev : list[0]?.id ?? null
      );
      setEvalLoadError(null);
    } catch (err) {
      if (seq !== evalReqSeqRef.current) return;
      setEvalLoadError(
        err instanceof Error ? err.message : "查询系统自检评测记录失败"
      );
    } finally {
      if (seq === evalReqSeqRef.current) {
        setLoadingEval(false);
      }
    }
  }, [api, onEvalRunsChanged]);

  useEffect(() => {
    if (!open) return;
    void loadEntitiesData();
    void loadDictionaryData();
    void refreshEvalRuns();
  }, [open, loadEntitiesData, loadDictionaryData, refreshEvalRuns]);

  useEffect(() => {
    if (!open) return;
    void loadBillsData(billEntityFilter);
  }, [open, billEntityFilter, loadBillsData]);

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

  const filteredExistingBills = useMemo(() => {
    const q = billsSearch.trim().toLowerCase();
    if (!q) return existingBills;
    return existingBills.filter(
      (bill) =>
        bill.charge_item.toLowerCase().includes(q) ||
        (bill.reference_no ?? "").toLowerCase().includes(q) ||
        (bill.mbl_no ?? "").toLowerCase().includes(q) ||
        bill.accounting_entity.toLowerCase().includes(q)
    );
  }, [existingBills, billsSearch]);

  const standardChargeSuggestions = useMemo(() => {
    const set = new Set<string>();
    for (const d of dictionaryList) {
      if (d.standard_name.trim()) set.add(d.standard_name.trim());
    }
    return Array.from(set);
  }, [dictionaryList]);

  const editingDictEntry = useMemo(
    () =>
      editingDictId
        ? dictionaryList.find((d) => d.id === editingDictId) ?? null
        : null,
    [dictionaryList, editingDictId]
  );

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
      setEntityFormOpen(false);
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
    const aliasToSave = editingDictEntry
      ? editingDictEntry.raw_alias
      : dictAliasInput;
    const check = validateChargeDictionaryInput(
      aliasToSave,
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
      setDictLoadError(null);
      onNotify(
        editingDictId
          ? `已更新费用映射：${check.payload.raw_alias} → ${check.payload.standard_name}`
          : `已新增费用映射：${check.payload.raw_alias} → ${check.payload.standard_name}`
      );
      resetDictForm();
      setDictFormOpen(false);
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
    setBillsImportRowErrors([]);
  };

  const handleImportBills = async () => {
    const parsed = parseExistingBillsImportInput(billsImportText, {
      filename: billsImportFilename ?? undefined,
      defaultAccountingEntity: billsImportEntity || selectedEntity,
    });
    if (!parsed.valid) {
      setBillsImportError(parsed.error);
      setBillsImportRowErrors(parsed.rowErrors);
      return;
    }
    setBillsImportError(null);
    setBillsImportRowErrors([]);
    setImportingBills(true);
    try {
      const res = await api.importExistingBills({
        default_accounting_entity: billsImportEntity || selectedEntity || null,
        replace_existing: replaceExistingBills,
        bills: parsed.bills,
      });
      setLastImportSummary(
        `本次导入 ${res.imported_count} 条，共享历史账单库当前共 ${res.total_count} 条`
      );
      setBillsImportText("");
      setBillsImportFilename(null);
      setBillsImportRowErrors([]);
      await loadBillsData(billEntityFilterRef.current);
      onNotify(`已成功导入 ${res.imported_count} 条历史账单`);
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "导入历史账单失败";
      setBillsImportError(msg);
      setBillsImportRowErrors([]);
    } finally {
      setImportingBills(false);
    }
  };

  const handleClearBills = async () => {
    const targetFilter = billEntityFilterRef.current;
    try {
      await api.clearExistingBills(targetFilter || undefined);
      setLastImportSummary(null);
      await loadBillsData(billEntityFilterRef.current);
      onNotify(
        targetFilter
          ? `已清理主体「${targetFilter}」的共享历史账单`
          : "已清空工作台共享历史账单库"
      );
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "清理历史账单失败",
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
      const casesLabel = formatEvalBenchmarkCasesText(
        created.total_cases,
        created.report
      );
      if (created.status === "COMPLETED") {
        onNotify(
          `系统自检完成：${casesLabel}，实际 ${created.actual_trials} 轮`
        );
      } else {
        onNotify(
          `${created.run_mode} 状态为 ${created.status}，指标置为「${NO_EVAL_RESULT_LABEL}」`,
          created.status === "FAILED" ? "error" : "info"
        );
      }
    } catch (err) {
      onNotify(
        err instanceof Error ? err.message : "触发系统自检评测失败",
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
    { value: "", label: "全部记账公司" },
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
        aria-label="主数据管理与系统自检"
      >
        <div className="wb-drawer-header">
          <div style={{ minWidth: 0 }}>
            <h2 className="lumno-modal-title">主数据管理与系统工具</h2>
            <div className="wb-helper-note">
              查询维护记账公司、费用词典映射、历史账单库与系统自检
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
                label: "历史账单库",
                count: billsHeaderStatus.tabCount,
              },
              {
                value: "eval_runs",
                label: "系统自检 (Harness)",
                count: evalRuns.length,
              },
            ]}
            onChange={setActiveTab}
          />
        </div>

        <div className="wb-drawer-body">
          {/* Tab 1: Accounting Entities — Lookup First, Add/Edit Below */}
          {activeTab === "entities" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="记账主体列表">
                <div className="wb-field-label">
                  <span>已登记记账主体 ({filteredEntities.length})</span>
                  <div style={{ display: "inline-flex", gap: "4px" }}>
                    <LumnoButton
                      variant="ghost"
                      compact
                      icon="ri-refresh-line"
                      busy={loadingEntities}
                      onClick={() => void loadEntitiesData()}
                    >
                      刷新
                    </LumnoButton>
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon={entityFormOpen ? "ri-arrow-up-s-line" : "ri-add-line"}
                      onClick={() => {
                        if (entityFormOpen && !editingEntityId) {
                          setEntityFormOpen(false);
                        } else {
                          resetEntityForm();
                          setEntityFormOpen(true);
                        }
                      }}
                    >
                      {entityFormOpen && !editingEntityId ? "收起表单" : "新增主体"}
                    </LumnoButton>
                  </div>
                </div>
                <LumnoInput
                  id="wb-mgmt-entity-search"
                  name="entity_search"
                  value={entitySearch}
                  placeholder="搜索公司名称或常用别名..."
                  aria-label="搜索记账主体"
                  onChange={(e) => setEntitySearch(e.target.value)}
                />

                {entitiesLoadError ? (
                  <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
                    <strong>查询记账主体档案失败</strong>
                    <div>{entitiesLoadError}</div>
                    <div>
                      <LumnoButton
                        variant="secondary"
                        compact
                        icon="ri-refresh-line"
                        onClick={() => void loadEntitiesData()}
                      >
                        重试加载
                      </LumnoButton>
                    </div>
                  </div>
                ) : filteredEntities.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>
                      {loadingEntities
                        ? "正在读取记账主体档案..."
                        : "暂无匹配的记账主体档案"}
                    </span>
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
                                默认记账公司
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
                              setEntityFormOpen(true);
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

              {(entityFormOpen || editingEntityId) ? (
                <div className="wb-section-box" aria-label="新增或编辑记账主体">
                  <div className="wb-field-label">
                    <span>
                      {editingEntityId ? "编辑记账主体档案" : "新增记账主体档案"}
                    </span>
                    <LumnoButton
                      variant="ghost"
                      compact
                      onClick={() => {
                        resetEntityForm();
                        setEntityFormOpen(false);
                      }}
                    >
                      取消
                    </LumnoButton>
                  </div>

                  <label htmlFor="wb-mgmt-entity-name" className="wb-form-field">
                    <span className="wb-form-label">公司规范全称 *</span>
                    <LumnoInput
                      id="wb-mgmt-entity-name"
                      name="canonical_name"
                      value={entityNameInput}
                      placeholder="如 NEPTUNE SHIPPING LIMITED"
                      aria-label="记账主体规范名称"
                      onChange={(e) => {
                        setEntityNameInput(e.target.value);
                        setEntityError(null);
                      }}
                    />
                  </label>

                  <label htmlFor="wb-mgmt-entity-aliases" className="wb-form-field">
                    <span className="wb-form-label">常用别名与分支简称（逗号或换行分隔）</span>
                    <LumnoInput
                      id="wb-mgmt-entity-aliases"
                      name="aliases"
                      value={entityAliasesInput}
                      placeholder="如 Neptune Shipping, NEPTUNE (LAX), 海王星航运"
                      aria-label="记账主体别名列表"
                      onChange={(e) => setEntityAliasesInput(e.target.value)}
                    />
                  </label>

                  <div className="wb-switch-row">
                    <div>
                      <div style={{ fontSize: "12px", fontWeight: 600 }}>
                        设为默认记账公司
                      </div>
                      <div className="wb-helper-note">
                        新上传单据未指定临时公司时默认按此公司记账
                      </div>
                    </div>
                    <LumnoSwitch
                      id="wb-mgmt-entity-default"
                      name="is_default"
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
                      {editingEntityId ? "保存主体修改" : "保存新增主体"}
                    </LumnoButton>
                  </div>
                </div>
              ) : null}

              <div className="wb-helper-note">
                为保证历史批次审计可追溯，已登记记账主体仅支持新增与修订别名，不提供删除操作。
              </div>
            </div>
          ) : null}

          {/* Tab 2: Charge Dictionary — Lookup First, Add/Edit & Reapply Below */}
          {activeTab === "dictionary" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="费用标准映射列表">
                <div className="wb-field-label">
                  <span>原收费名 → 账务标准名 ({filteredDictionary.length})</span>
                  <div style={{ display: "inline-flex", gap: "4px" }}>
                    <LumnoButton
                      variant="ghost"
                      compact
                      icon="ri-refresh-line"
                      busy={loadingDict}
                      onClick={() => void loadDictionaryData()}
                    >
                      刷新
                    </LumnoButton>
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon={dictFormOpen ? "ri-arrow-up-s-line" : "ri-add-line"}
                      onClick={() => {
                        if (dictFormOpen && !editingDictId) {
                          setDictFormOpen(false);
                        } else {
                          resetDictForm();
                          setDictFormOpen(true);
                        }
                      }}
                    >
                      {dictFormOpen && !editingDictId ? "收起表单" : "新增映射"}
                    </LumnoButton>
                  </div>
                </div>
                <LumnoInput
                  id="wb-mgmt-dict-search"
                  name="dictionary_search"
                  value={dictSearch}
                  placeholder="搜索原收费名、标准费用名或分类..."
                  aria-label="筛选费用词典"
                  onChange={(e) => setDictSearch(e.target.value)}
                />

                {dictLoadError ? (
                  <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
                    <strong>查询费用映射词典失败</strong>
                    <div>{dictLoadError}</div>
                    <div>
                      <LumnoButton
                        variant="secondary"
                        compact
                        icon="ri-refresh-line"
                        onClick={() => void loadDictionaryData()}
                      >
                        重试加载
                      </LumnoButton>
                    </div>
                  </div>
                ) : filteredDictionary.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>
                      {loadingDict
                        ? "正在读取费用映射词典..."
                        : "暂无匹配的词典映射条目"}
                    </span>
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
                                {entry.is_builtin ? "系统内置" : "自定义映射"}
                              </span>
                              <span className="wb-badge wb-badge--muted">
                                {DICT_CATEGORY_OPTIONS.find(
                                  (o) => o.value === entry.category
                                )?.label ?? entry.category}
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
                                  setDictFormOpen(true);
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

              {(dictFormOpen || editingDictId) ? (
                <div className="wb-section-box" aria-label="新增或编辑费用映射词典">
                  <div className="wb-field-label">
                    <span>
                      {editingDictId ? "编辑自定义费用映射" : "新增自定义费用映射"}
                    </span>
                    <LumnoButton
                      variant="ghost"
                      compact
                      onClick={() => {
                        resetDictForm();
                        setDictFormOpen(false);
                      }}
                    >
                      取消
                    </LumnoButton>
                  </div>

                  <div className="wb-form-grid-2">
                    <label htmlFor="wb-mgmt-dict-alias" className="wb-form-field">
                      <span className="wb-form-label">原件收费名 *</span>
                      <LumnoInput
                        id="wb-mgmt-dict-alias"
                        name="raw_alias"
                        value={
                          editingDictEntry
                            ? editingDictEntry.raw_alias
                            : dictAliasInput
                        }
                        placeholder="如 HIGH PRECISION TEST FEE"
                        aria-label="原始费用别名"
                        readOnly={Boolean(editingDictId)}
                        disabled={Boolean(editingDictId)}
                        onChange={(e) => {
                          setDictAliasInput(e.target.value);
                          setDictError(null);
                        }}
                      />
                    </label>
                    <label htmlFor="wb-mgmt-dict-standard" className="wb-form-field">
                      <span className="wb-form-label">账务标准名 *</span>
                      <LumnoInput
                        id="wb-mgmt-dict-standard"
                        name="standard_name"
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
                    <span className="wb-form-label">费用分类</span>
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
              ) : null}

              <div className="wb-section-box" aria-label="当前批次词典重标准化">
                <div className="wb-field-label">
                  <span>按最新词典重算当前批次费用名</span>
                  {activeBatch ? (
                    <span className="wb-badge wb-badge--info wb-tabular">
                      当前批 {activeBatch.short_code}
                    </span>
                  ) : null}
                </div>
                <div className="wb-helper-note">
                  只更新当前批次未手工改过的费用名称，保留已做过的人工修订。
                </div>
                <div className="wb-action-row" style={{ justifyContent: "flex-end" }}>
                  <LumnoButton
                    variant="secondary"
                    icon="ri-sparkling-line"
                    busy={reapplyingDictionary}
                    disabled={!activeBatch}
                    onClick={() => void onReapplyDictionary()}
                  >
                    仅重算当前批次费用名
                  </LumnoButton>
                </div>
              </div>

              <datalist id="wb-mgmt-std-charges">
                {standardChargeSuggestions.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </div>
          ) : null}

          {/* Tab 3: Shared Existing Bills Baseline — Search & List First, Import/Clear Below */}
          {activeTab === "existing_bills" ? (
            <div className="wb-drawer-stack">
              <div className="wb-section-box" aria-label="共享历史账单列表">
                <div className="wb-field-label">
                  <span>历史账单查重与核对</span>
                  <span className="wb-tabular wb-helper-note">
                    {billsHeaderStatus.statusText}
                  </span>
                </div>

                <div className="wb-form-grid-2">
                  <LumnoSelect
                    value={billEntityFilter}
                    ariaLabel="按记账公司筛选共享历史账单"
                    options={entityFilterOptions}
                    onChange={(nextFilter) => {
                      billEntityFilterRef.current = nextFilter;
                      setBillEntityFilter(nextFilter);
                      setBillsViewState((prev) =>
                        setExistingBillsFilter(prev, nextFilter)
                      );
                    }}
                  />
                  <LumnoInput
                    id="wb-mgmt-bills-search"
                    name="existing_bills_search"
                    value={billsSearch}
                    placeholder="按单据号、提单号或费用名称筛选..."
                    aria-label="筛选历史账单"
                    onChange={(e) => setBillsSearch(e.target.value)}
                  />
                </div>

                <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                  <LumnoButton
                    variant="secondary"
                    compact
                    icon="ri-refresh-line"
                    busy={loadingBills}
                    onClick={() =>
                      void loadBillsData(billEntityFilterRef.current)
                    }
                  >
                    刷新列表
                  </LumnoButton>
                  <LumnoButton
                    variant="secondary"
                    compact
                    icon={billsImportOpen ? "ri-arrow-up-s-line" : "ri-upload-2-line"}
                    onClick={() => setBillsImportOpen((v) => !v)}
                  >
                    {billsImportOpen ? "收起导入与清理" : "导入与清理管理"}
                  </LumnoButton>
                </div>

                {billsLoadError ? (
                  <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
                    <strong>查询共享历史账单失败</strong>
                    <div>{billsLoadError}</div>
                    <div>
                      <LumnoButton
                        variant="secondary"
                        compact
                        icon="ri-refresh-line"
                        onClick={() =>
                          void loadBillsData(billEntityFilterRef.current)
                        }
                      >
                        重试加载
                      </LumnoButton>
                    </div>
                  </div>
                ) : loadingBills || billsLoadedForFilter !== billEntityFilter ? (
                  <div className="wb-empty-state">
                    <span>正在读取历史账单库...</span>
                  </div>
                ) : filteredExistingBills.length === 0 ? (
                  <div className="wb-empty-state">
                    <span>
                      {existingBills.length === 0
                        ? "当前记账公司下暂无历史账单"
                        : "没有符合搜索条件的历史账单"}
                    </span>
                  </div>
                ) : (
                  <div className="wb-mgmt-list">
                    {filteredExistingBills.map((bill) => (
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

              {billsImportOpen ? (
                <div className="wb-section-box" aria-label="导入共享历史账单与清理管理">
                  <div className="wb-field-label">
                    <span>导入共享历史账单（JSON / CSV）</span>
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
                    id="wb-mgmt-bills-file"
                    name="existing_bills_import_file"
                    ref={billsFileInputRef}
                    type="file"
                    accept=".json,.csv,application/json,text/csv"
                    style={{ display: "none" }}
                    onChange={(e) => void handlePickBillsFile(e)}
                  />

                  <LumnoTextarea
                    id="wb-mgmt-bills-import"
                    name="existing_bills_import_text"
                    value={billsImportText}
                    placeholder={
                      '粘贴 JSON 数组（每行必填标准费用名称、金额、币种、AR/AP 方向）或含必填表头的 CSV'
                    }
                    aria-label="历史账单 JSON 或 CSV 导入内容"
                    style={{ minHeight: "100px", fontFamily: "var(--lumno-font-mono)" }}
                    onChange={(e) => {
                      setBillsImportText(e.target.value);
                      setBillsImportError(null);
                      setBillsImportRowErrors([]);
                    }}
                  />

                  <div className="wb-form-grid-2">
                    <div className="wb-form-field">
                      <span className="wb-form-label">缺省归属记账公司</span>
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
                          覆盖已有历史账单
                        </div>
                        <div className="wb-helper-note">
                          危险操作：开启后先清空对应公司旧账再导入
                        </div>
                      </div>
                      <LumnoSwitch
                        id="wb-mgmt-bills-replace"
                        name="replace_existing_bills"
                        ariaLabel="导入前覆盖已有历史账单"
                        compact
                        checked={replaceExistingBills}
                        onChange={setReplaceExistingBills}
                      />
                    </div>
                  </div>

                  {billsImportRowErrors.length > 0 ? (
                    <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
                      <strong>
                        共发现 {billsImportRowErrors.length} 处必填或格式错误（已拦截导入）
                      </strong>
                      <ul style={{ margin: "4px 0 0", paddingLeft: "18px", fontSize: "12px" }}>
                        {billsImportRowErrors.map((errMsg, idx) => (
                          <li key={idx}>{errMsg}</li>
                        ))}
                      </ul>
                    </div>
                  ) : billsImportError ? (
                    <div className="wb-field-error">{billsImportError}</div>
                  ) : null}
                  {lastImportSummary ? (
                    <div className="wb-notice-banner">
                      <span>{lastImportSummary}</span>
                    </div>
                  ) : null}

                  <div className="wb-action-row" style={{ justifyContent: "space-between" }}>
                    <LumnoPopconfirm
                      title={
                        billEntityFilter
                          ? `确认清理「${billEntityFilter}」历史账单？`
                          : "确认清空全部共享历史账单库？"
                      }
                      description="仅清理工作台共享历史账单库，不会影响已保存的各批次独立快照。"
                      confirmLabel="确认清理"
                      disabled={
                        loadingBills ||
                        Boolean(billsLoadError) ||
                        existingBills.length === 0 ||
                        billsLoadedForFilter !== billEntityFilter
                      }
                      onConfirm={() => void handleClearBills()}
                    >
                      {(triggerProps) => (
                        <LumnoButton
                          variant="ghost"
                          compact
                          icon="ri-delete-bin-6-line"
                          disabled={
                            loadingBills ||
                            Boolean(billsLoadError) ||
                            existingBills.length === 0 ||
                            billsLoadedForFilter !== billEntityFilter
                          }
                          {...triggerProps}
                        >
                          {billEntityFilter ? "清理该公司历史账单" : "清空共享历史库"}
                        </LumnoButton>
                      )}
                    </LumnoPopconfirm>

                    <LumnoButton
                      variant="primary"
                      icon="ri-database-2-line"
                      busy={importingBills}
                      onClick={() => void handleImportBills()}
                    >
                      导入共享历史账单
                    </LumnoButton>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}

          {/* Tab 4: Administrator Harness Eval Dashboard (with fixed 13 组（16 项） counts) */}
          {activeTab === "eval_runs" ? (
            <div className="wb-drawer-stack">
              <div className="wb-notice-banner">
                <span>
                  管理员自检区：用于运行 13 组（16 项）内置真值与多轮稳定性测试基准，不会修改您的业务账单数据。
                </span>
              </div>

              <div className="wb-section-box" aria-label="Harness 评测触发控制台">
                <div className="wb-field-label">
                  <span>执行或刷新 Harness 评测基准</span>
                  <span className="wb-helper-note">13 组（16 项）真值与稳定性度量</span>
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
                      {runningEval ? "评测运行中..." : "运行自检"}
                    </LumnoButton>
                  </div>
                </div>

                {runningEval ? (
                  <div className="wb-notice-banner" role="status">
                    <i
                      className="ri-icon ri-icon--compact ri-loader-4-line"
                      aria-hidden="true"
                    />
                    <span>
                      评测运行中（含本地 OCR 样本，约需 30–60 秒）...
                    </span>
                  </div>
                ) : null}
              </div>

              {evalLoadError ? (
                <div className="wb-alert-item" data-severity="BLOCKER" role="alert">
                  <strong>查询评测记录失败</strong>
                  <div>{evalLoadError}</div>
                  <div>
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-refresh-line"
                      onClick={() => void refreshEvalRuns()}
                    >
                      重试加载
                    </LumnoButton>
                  </div>
                </div>
              ) : null}

              {!evalLoadError && evalRuns.length === 0 ? (
                <div className="wb-empty-state lumno-paper-card">
                  <i
                    className="ri-icon ri-bar-chart-box-line"
                    style={{ "--ri-size": "28px" } as React.CSSProperties}
                    aria-hidden="true"
                  />
                  <strong>
                    {loadingEval
                      ? "正在读取系统自检评测记录..."
                      : "尚未运行过 Harness 评测基准"}
                  </strong>
                  <span>
                    本看板绝不伪造任何示例高分。请选择上方基准模式运行并查看真实观测指标。
                  </span>
                </div>
              ) : evalRuns.length > 0 ? (
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
                            基准用例：{evalViewModel.benchmarkCasesText}
                          </span>
                          <span className="wb-badge wb-badge--muted">
                            累计执行：{evalViewModel.executedCasesText}
                          </span>
                          <span className="wb-badge wb-badge--muted">
                            平均耗时：{evalViewModel.avgLatencyText}
                          </span>
                          <span className="wb-badge wb-badge--muted">
                            LLM 调用：
                            {evalViewModel.llmCalls ?? NO_EVAL_RESULT_LABEL}
                          </span>
                          <span className="wb-badge wb-badge--muted">
                            OCR 调用：
                            {evalViewModel.ocrCalls ?? NO_EVAL_RESULT_LABEL}
                          </span>
                          {evalViewModel.selfRepairEvidence ? (
                            <span className="wb-badge wb-badge--info">
                              自修复证据：{evalViewModel.selfRepairEvidence}
                            </span>
                          ) : null}
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

                      {evalViewModel.failingCases.length > 0 ? (
                        <div className="wb-section-box" aria-label="未通过用例明细">
                          <div className="wb-field-label">
                            <span>
                              未通过用例明细 ({evalViewModel.failingCases.length})
                            </span>
                            <span className="wb-helper-note">
                              逐项对照期望与实测结果
                            </span>
                          </div>
                          <div className="wb-mgmt-list">
                            {evalViewModel.failingCases.map((fc, idx) => (
                              <div
                                key={`${fc.caseId ?? "case"}-${fc.trial ?? idx}-${idx}`}
                                className="wb-mgmt-card"
                              >
                                <div className="wb-mgmt-card-head">
                                  <strong className="wb-mono">
                                    {fc.caseId ?? fc.groupId ?? `用例 #${idx + 1}`}
                                  </strong>
                                  {fc.trial !== null ? (
                                    <span className="wb-badge wb-badge--error wb-tabular">
                                      第 {fc.trial} 轮
                                    </span>
                                  ) : null}
                                </div>
                                <div className="wb-field-error">{fc.reason}</div>
                                {fc.expected || fc.actual ? (
                                  <div className="wb-helper-note wb-mono">
                                    期望：{fc.expected ?? "—"} | 实际：
                                    {fc.actual ?? "—"}
                                  </div>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      </aside>
    </div>,
    document.body
  );
}
