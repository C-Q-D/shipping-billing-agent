import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  ApiClientError,
  BillingApiClient,
  isStubModeActive,
  type AccountingEntity,
  type BatchDetail,
  type BatchSummary,
  type ChargeDictionaryEntry,
  type EvalRunSummary,
  type ExportMode,
  type ExportResponse,
  type HealthStatus,
  type OcrMode,
  type ReviewBundleV1,
  type SourceDocumentSummary,
} from "./api/client.ts";
import {
  formatLosslessAmount,
  parseLosslessJson,
  readDecimalString,
  stringifyLosslessJson,
} from "./api/decimalJson.ts";
import {
  BatchQueuePanel,
  type RecordFilterTab,
} from "./components/BatchQueuePanel.tsx";
import {
  LumnoBackgroundStage,
  LumnoButton,
  LumnoModal,
  LumnoPopconfirm,
  LumnoSegmentedControl,
  LumnoTextarea,
  LumnoToastHost,
  type LumnoTheme,
  type ToastItem,
} from "./components/LumnoPrimitives.tsx";
import { TopBar } from "./components/TopBar.tsx";
import { UploadAndFileTree } from "./components/UploadAndFileTree.tsx";

const LAST_BATCH_STORAGE_KEY = "wb_last_batch_id";
const POLL_INTERVAL_MS = 500;
const ACTIVE_STAGES = new Set<BatchSummary["stage"]>([
  "QUEUED",
  "UNPACKING",
  "EXTRACTING",
  "MATCHING",
  "VALIDATING",
]);

type MobileViewTab = "upload" | "queue" | "detail";

function isActiveBatchStage(stage: BatchSummary["stage"]): boolean {
  return ACTIVE_STAGES.has(stage);
}

function getInitialBatchIdFromUrlOrStorage(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const params = new URLSearchParams(window.location.search);
    return (
      params.get("batch") || window.localStorage.getItem(LAST_BATCH_STORAGE_KEY)
    );
  } catch {
    return null;
  }
}

export default function App() {
  const api = useMemo(() => new BillingApiClient(), []);
  const stubMode = useMemo(() => isStubModeActive(), []);

  const [theme, setTheme] = useState<LumnoTheme>("light");
  const [mobileTab, setMobileTab] = useState<MobileViewTab>("queue");

  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [entities, setEntities] = useState<AccountingEntity[]>([]);
  const [selectedEntity, setSelectedEntity] = useState<string>(
    "NEPTUNE SHIPPING LIMITED"
  );
  const [existingBillCount, setExistingBillCount] = useState<number>(0);

  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(
    getInitialBatchIdFromUrlOrStorage
  );
  const [activeBatch, setActiveBatch] = useState<BatchDetail | null>(null);

  const [filterTab, setFilterTab] = useState<RecordFilterTab>("all");
  const [activeDocFilterId, setActiveDocFilterId] = useState<string | null>(
    null
  );
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);

  const [submittingBatch, setSubmittingBatch] = useState(false);
  const [submittingDemo, setSubmittingDemo] = useState(false);
  const [retryingDocId, setRetryingDocId] = useState<string | null>(null);
  const [forcingDocId, setForcingDocId] = useState<string | null>(null);
  const [switchingEntity, setSwitchingEntity] = useState(false);
  const [syncingBaseline, setSyncingBaseline] = useState(false);
  const [approvingOrRejecting, setApprovingOrRejecting] = useState(false);
  const [entityNotice, setEntityNotice] = useState<{
    count: number;
    message: string;
  } | null>(null);

  // Modals (T01 placeholders wired to real APIs; full editing/drawers completed in T02/T03)
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [exportMode, setExportMode] = useState<ExportMode>("official");
  const [exportPreview, setExportPreview] = useState<ExportResponse | null>(
    null
  );
  const [loadingExport, setLoadingExport] = useState(false);

  const [importModalOpen, setImportModalOpen] = useState(false);
  const [bundleJsonText, setBundleJsonText] = useState("");
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [importingBundle, setImportingBundle] = useState(false);

  const [masterModalOpen, setMasterModalOpen] = useState(false);
  const [dictionaryList, setDictionaryList] = useState<
    ChargeDictionaryEntry[]
  >([]);
  const [evalRuns, setEvalRuns] = useState<EvalRunSummary[]>([]);

  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastIdRef = useRef(0);
  const rightPaneScrollRef = useRef<HTMLDivElement | null>(null);

  // Monotonic sequence counters so stale polling responses never overwrite newer mutations
  const mutationSeqRef = useRef(0);
  const activeBatchIdRef = useRef<string | null>(activeBatchId);
  activeBatchIdRef.current = activeBatchId;

  const pushToast = useCallback(
    (message: string, tone: "info" | "error" = "info") => {
      const id = ++toastIdRef.current;
      setToasts((prev) => [...prev.slice(-2), { id, message, tone }]);
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((item) => item.id !== id));
      }, 2800);
    },
    []
  );

  useEffect(() => {
    document.body.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (activeBatchId) {
      try {
        window.localStorage.setItem(LAST_BATCH_STORAGE_KEY, activeBatchId);
      } catch {
        // ignore storage quota or privacy restrictions
      }
    }
  }, [activeBatchId]);

  const applyFreshBatchDetail = useCallback((detail: BatchDetail) => {
    setActiveBatch(detail);
    setActiveBatchId(detail.id);
    setSelectedEntity(detail.accounting_entity);
    setBatches((prev) => {
      const summary: BatchSummary = {
        id: detail.id,
        short_code: detail.short_code,
        accounting_entity: detail.accounting_entity,
        source_channel: detail.source_channel,
        status: detail.status,
        stage: detail.stage,
        stage_message: detail.stage_message,
        ocr_mode: detail.ocr_mode,
        total_files: detail.total_files,
        succeeded_files: detail.succeeded_files,
        failed_files: detail.failed_files,
        counts: detail.counts,
        created_at: detail.created_at,
        updated_at: detail.updated_at,
      };
      const idx = prev.findIndex((item) => item.id === detail.id);
      if (idx === -1) {
        return [summary, ...prev];
      }
      const next = [...prev];
      next[idx] = summary;
      return next;
    });
    setSelectedRecordId((prevSelected) => {
      if (
        prevSelected &&
        detail.records.some((r) => r.id === prevSelected)
      ) {
        return prevSelected;
      }
      return detail.records[0]?.id ?? null;
    });
  }, []);

  const refreshBatchesList = useCallback(
    async (signal?: AbortSignal) => {
      const list = await api.listBatches(20, signal);
      setBatches(list);
      return list;
    },
    [api]
  );

  // Initial workspace bootstrap
  useEffect(() => {
    const controller = new AbortController();
    const bootstrap = async () => {
      try {
        const [healthRes, entitiesRes, billsRes, batchList] = await Promise.all(
          [
            api.getHealth(controller.signal).catch((err) => {
              setHealthError(
                err instanceof Error ? err.message : "服务连接失败"
              );
              return null;
            }),
            api.listAccountingEntities(controller.signal).catch(() => []),
            api.listExistingBills(undefined, controller.signal).catch(() => []),
            api.listBatches(20, controller.signal).catch(() => []),
          ]
        );
        if (controller.signal.aborted) return;
        if (healthRes) {
          setHealth(healthRes);
          setHealthError(null);
        }
        setEntities(entitiesRes);
        const defaultEnt =
          entitiesRes.find((e) => e.is_default)?.canonical_name ??
          entitiesRes[0]?.canonical_name;
        if (defaultEnt) {
          setSelectedEntity(defaultEnt);
        }
        setExistingBillCount(billsRes.length);
        setBatches(batchList);

        const preferredId = getInitialBatchIdFromUrlOrStorage();
        const targetId =
          preferredId && batchList.some((b) => b.id === preferredId)
            ? preferredId
            : batchList[0]?.id ?? null;

        if (targetId) {
          const seqAtStart = mutationSeqRef.current;
          const detail = await api.getBatchDetail(targetId, controller.signal);
          if (
            !controller.signal.aborted &&
            mutationSeqRef.current === seqAtStart
          ) {
            applyFreshBatchDetail(detail);
          }
        }
      } catch (err) {
        if (
          controller.signal.aborted ||
          (err instanceof ApiClientError && err.code === "REQUEST_ABORTED")
        ) {
          return;
        }
        pushToast(
          err instanceof Error ? err.message : "初始化工作台失败",
          "error"
        );
      }
    };

    void bootstrap();
    return () => controller.abort();
  }, [api, applyFreshBatchDetail, pushToast]);

  // Real-time polling while batch stage is active (masked by mutationSeqRef to drop stale responses)
  useEffect(() => {
    if (!activeBatch || !isActiveBatchStage(activeBatch.stage)) {
      return;
    }
    const targetBatchId = activeBatch.id;
    const seqSnapshot = mutationSeqRef.current;
    const controller = new AbortController();

    const timer = window.setTimeout(async () => {
      try {
        const detail = await api.getBatchDetail(targetBatchId, controller.signal);
        if (
          controller.signal.aborted ||
          activeBatchIdRef.current !== targetBatchId ||
          mutationSeqRef.current !== seqSnapshot
        ) {
          return;
        }
        applyFreshBatchDetail(detail);
      } catch (err) {
        if (
          controller.signal.aborted ||
          (err instanceof ApiClientError && err.code === "REQUEST_ABORTED")
        ) {
          return;
        }
        pushToast(
          err instanceof Error ? err.message : "轮询批次状态失败",
          "error"
        );
      }
    }, POLL_INTERVAL_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, activeBatch, applyFreshBatchDetail, pushToast]);

  // Reset right pane scroll top whenever selected record changes
  useEffect(() => {
    rightPaneScrollRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [selectedRecordId]);

  const handleSelectBatch = async (batchId: string) => {
    if (!batchId) return;
    mutationSeqRef.current++;
    const seq = mutationSeqRef.current;
    setActiveDocFilterId(null);
    setFilterTab("all");
    setEntityNotice(null);
    setActiveBatchId(batchId);
    try {
      const detail = await api.getBatchDetail(batchId);
      if (mutationSeqRef.current === seq) {
        applyFreshBatchDetail(detail);
      }
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "加载批次详情失败",
        "error"
      );
    }
  };

  const handleSubmitBatch = async (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
    files: File[];
    publicUrls: string[];
    existingBillsJsonText: string;
  }): Promise<boolean> => {
    setSubmittingBatch(true);
    mutationSeqRef.current++;
    try {
      const res = await api.createBatch({
        accounting_entity: args.accountingEntity,
        ocr_mode: args.ocrMode,
        files: args.files,
        public_urls: args.publicUrls,
        existing_bills_json: args.existingBillsJsonText,
        wait: false,
      });
      setActiveDocFilterId(null);
      setEntityNotice(null);
      setActiveBatchId(res.id);
      const detail = await api.getBatchDetail(res.id);
      applyFreshBatchDetail(detail);
      await refreshBatchesList().catch(() => undefined);
      setMobileTab("queue");
      pushToast(`批次 ${res.short_code} 已入队启动解析`);
      return true;
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "提交解析批次失败",
        "error"
      );
      return false;
    } finally {
      setSubmittingBatch(false);
    }
  };

  const handleSubmitDemo = async (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
  }) => {
    setSubmittingDemo(true);
    mutationSeqRef.current++;
    try {
      const res = await api.createDemoBatch({
        accounting_entity: args.accountingEntity,
        ocr_mode: args.ocrMode,
        wait: false,
      });
      setActiveDocFilterId(null);
      setEntityNotice(null);
      setActiveBatchId(res.id);
      const detail = await api.getBatchDetail(res.id);
      applyFreshBatchDetail(detail);
      await refreshBatchesList().catch(() => undefined);
      setMobileTab("queue");
      pushToast(`已载入 4 份真样本批次 ${res.short_code}`);
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "载入演示样本失败",
        "error"
      );
    } finally {
      setSubmittingDemo(false);
    }
  };

  const handleConfirmSwitchEntity = async (nextEntity: string) => {
    if (!activeBatch || nextEntity === activeBatch.accounting_entity) {
      setSelectedEntity(nextEntity);
      return;
    }
    setSwitchingEntity(true);
    mutationSeqRef.current++;
    try {
      const res = await api.switchBatchAccountingEntity(
        activeBatch.id,
        nextEntity
      );
      applyFreshBatchDetail(res.batch);
      setEntityNotice({
        count: res.reset_approval_count,
        message: res.notice,
      });
      pushToast(`已切换主体为 ${nextEntity}`);
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "切换记账主体失败",
        "error"
      );
    } finally {
      setSwitchingEntity(false);
    }
  };

  const handleForceParseDoc = async (docId: string) => {
    if (!activeBatch) return;
    setForcingDocId(docId);
    mutationSeqRef.current++;
    try {
      const updated = await api.forceParseSkippedDocument(
        activeBatch.id,
        docId
      );
      applyFreshBatchDetail(updated);
      pushToast("已强制将该跳过图片纳入 OCR 解析");
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "强制提取文档失败",
        "error"
      );
    } finally {
      setForcingDocId(null);
    }
  };

  const handleRetryOrReplaceDoc = async (docId: string, file?: File) => {
    if (!activeBatch) return;
    setRetryingDocId(docId);
    mutationSeqRef.current++;
    try {
      const updated = await api.retryOrReplaceDocument(activeBatch.id, docId, {
        ocr_mode: activeBatch.ocr_mode,
        file,
      });
      applyFreshBatchDetail(updated);
      pushToast(file ? "已上传替换文件并重新计算" : "已触发单文件重试");
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "重试或替换文件失败",
        "error"
      );
    } finally {
      setRetryingDocId(null);
    }
  };

  const handleSyncBaseline = async () => {
    if (!activeBatch) return;
    setSyncingBaseline(true);
    try {
      const res = await api.syncBatchToBaseline(activeBatch.id);
      const bills = await api.listExistingBills().catch(() => []);
      setExistingBillCount(bills.length);
      pushToast(
        `基准库已同步：新增 ${res.created_count}，更新 ${res.updated_count}，跳过 ${res.skipped_count}`
      );
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "同步至历史基准库失败",
        "error"
      );
    } finally {
      setSyncingBaseline(false);
    }
  };

  const loadExportPreview = useCallback(
    async (batchId: string, mode: ExportMode) => {
      setLoadingExport(true);
      try {
        const res = await api.exportBatchJson(batchId, mode);
        setExportPreview(res);
      } catch (err) {
        pushToast(
          err instanceof Error ? err.message : "获取导出预览失败",
          "error"
        );
      } finally {
        setLoadingExport(false);
      }
    },
    [api, pushToast]
  );

  const handleOpenExport = () => {
    if (!activeBatch) return;
    setExportModalOpen(true);
    void loadExportPreview(activeBatch.id, exportMode);
  };

  const handleDownloadExport = async () => {
    if (!activeBatch) return;
    try {
      const { rawJsonText } = await api.downloadBatchJsonArray(
        activeBatch.id,
        exportMode
      );
      const blob = new Blob([rawJsonText], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${activeBatch.short_code}-${exportMode}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      pushToast(`已下载 ${exportMode} 标准 JSON 文件`);
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "下载 JSON 失败",
        "error"
      );
    }
  };

  const handleOpenMasterData = async () => {
    setMasterModalOpen(true);
    try {
      const [dict, runs, bills] = await Promise.all([
        api.listChargeDictionary(),
        api.listEvalRuns(),
        api.listExistingBills(),
      ]);
      setDictionaryList(dict);
      setEvalRuns(runs);
      setExistingBillCount(bills.length);
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "读取主数据与评测概览失败",
        "error"
      );
    }
  };

  const handleBundleFilePick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await file.text();
    setBundleJsonText(text);
    setBundleError(null);
  };

  const handleImportBundle = async () => {
    setBundleError(null);
    if (!bundleJsonText.trim()) {
      setBundleError("请先粘贴或选择 CLI 生成的 ReviewBundleV1 JSON");
      return;
    }
    let parsed: ReviewBundleV1;
    try {
      parsed = parseLosslessJson<ReviewBundleV1>(bundleJsonText);
    } catch {
      setBundleError("复核包 JSON 语法无效");
      return;
    }
    if (
      !parsed ||
      parsed.bundle_version !== "1.0" ||
      !Array.isArray(parsed.documents) ||
      !Array.isArray(parsed.records)
    ) {
      setBundleError(
        "不符合 ReviewBundleV1 契约：必须包含 bundle_version: \"1.0\"、documents 数组与 records 数组"
      );
      return;
    }

    setImportingBundle(true);
    mutationSeqRef.current++;
    try {
      const imported = await api.importReviewBundle(parsed);
      applyFreshBatchDetail(imported);
      await refreshBatchesList().catch(() => undefined);
      setImportModalOpen(false);
      setBundleJsonText("");
      pushToast(`已接手 CLI 复核包 ${imported.short_code}`);
    } catch (err) {
      setBundleError(
        err instanceof Error ? err.message : "导入复核包失败"
      );
    } finally {
      setImportingBundle(false);
    }
  };

  const selectedRecord = useMemo(() => {
    if (!activeBatch || !selectedRecordId) return null;
    return (
      activeBatch.records.find((r) => r.id === selectedRecordId) ?? null
    );
  }, [activeBatch, selectedRecordId]);

  const selectedSourceDoc = useMemo(() => {
    if (!activeBatch || !selectedRecord) return null;
    return (
      activeBatch.documents.find(
        (d) => d.id === selectedRecord.source_doc_id
      ) ?? null
    );
  }, [activeBatch, selectedRecord]);

  const handleApproveQuick = async () => {
    if (!activeBatch || !selectedRecord) return;
    setApprovingOrRejecting(true);
    mutationSeqRef.current++;
    try {
      const updated = await api.approveBillingRecord(
        activeBatch.id,
        selectedRecord.id,
        "核对原文无误，批准入账"
      );
      applyFreshBatchDetail(updated);
      pushToast("已批准该条费用记录");
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "批准失败",
        "error"
      );
    } finally {
      setApprovingOrRejecting(false);
    }
  };

  const handleRejectQuick = async (reason: string) => {
    if (!activeBatch || !selectedRecord) return;
    setApprovingOrRejecting(true);
    mutationSeqRef.current++;
    try {
      const updated = await api.rejectBillingRecord(
        activeBatch.id,
        selectedRecord.id,
        reason
      );
      applyFreshBatchDetail(updated);
      pushToast("已将该费用记录驳回并排除");
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "驳回失败",
        "error"
      );
    } finally {
      setApprovingOrRejecting(false);
    }
  };

  const hasBlockers = Boolean(
    selectedRecord?.validation_alerts?.some((a) => a.severity === "BLOCKER")
  );

  return (
    <>
      <LumnoBackgroundStage theme={theme} />
      <LumnoToastHost toasts={toasts} />

      <div className="wb-shell">
        <TopBar
          health={health}
          healthError={healthError}
          isStubMode={stubMode}
          batches={batches}
          activeBatchId={activeBatchId}
          activeBatch={activeBatch}
          entities={entities}
          selectedEntity={selectedEntity}
          existingBillCount={existingBillCount}
          theme={theme}
          syncingBaseline={syncingBaseline}
          switchingEntity={switchingEntity}
          onSelectBatch={(id) => void handleSelectBatch(id)}
          onConfirmSwitchEntity={handleConfirmSwitchEntity}
          onToggleTheme={() =>
            setTheme((prev) => (prev === "light" ? "dark" : "light"))
          }
          onOpenImportBundle={() => setImportModalOpen(true)}
          onOpenMasterData={() => void handleOpenMasterData()}
          onSyncBaseline={() => void handleSyncBaseline()}
          onOpenExport={handleOpenExport}
        />

        <div className="wb-mobile-bar">
          <LumnoSegmentedControl
            value={mobileTab}
            ariaLabel="移动端工作台视图切换"
            options={[
              { value: "upload", label: "投递与文件" },
              { value: "queue", label: "账单列表" },
              { value: "detail", label: "详情复核" },
            ]}
            onChange={setMobileTab}
          />
        </div>

        <main className="wb-grid">
          <section
            className="wb-pane lumno-glass-panel"
            aria-label="单据投递与上下文文件树"
            data-mobile-active={mobileTab === "upload" ? "true" : "false"}
          >
            <UploadAndFileTree
              selectedEntity={selectedEntity}
              activeBatch={activeBatch}
              activeDocFilterId={activeDocFilterId}
              submittingBatch={submittingBatch}
              submittingDemo={submittingDemo}
              retryingDocId={retryingDocId}
              forcingDocId={forcingDocId}
              onSubmitBatch={handleSubmitBatch}
              onSubmitDemo={handleSubmitDemo}
              onSelectDocFilter={(docId) => setActiveDocFilterId(docId)}
              onForceParseDoc={(docId) => void handleForceParseDoc(docId)}
              onRetryOrReplaceDoc={(docId, file) =>
                void handleRetryOrReplaceDoc(docId, file)
              }
              onOpenDocTotalsHint={(doc: SourceDocumentSummary) =>
                pushToast(
                  `${doc.sanitized_filename} 汇总：${formatLosslessAmount(doc, "total_amount")} ${doc.total_currency ?? ""}（完整单据总额修订将在 T02 开放）`
                )
              }
            />
          </section>

          <section
            className="wb-pane lumno-glass-panel"
            aria-label="阶段进度与分状态账单队列"
            data-mobile-active={mobileTab === "queue" ? "true" : "false"}
          >
            <BatchQueuePanel
              batch={activeBatch}
              filterTab={filterTab}
              activeDocFilterId={activeDocFilterId}
              selectedRecordId={selectedRecordId}
              retryingDocId={retryingDocId}
              entityNotice={entityNotice}
              onChangeFilterTab={setFilterTab}
              onClearDocFilter={() => setActiveDocFilterId(null)}
              onSelectRecord={(recId) => {
                setSelectedRecordId(recId);
                setMobileTab("detail");
              }}
              onRetryOrReplaceDoc={(docId, file) =>
                void handleRetryOrReplaceDoc(docId, file)
              }
              onDismissEntityNotice={() => setEntityNotice(null)}
              onOpenManualRecordHint={() =>
                pushToast("同单手工新增漏提行表单将在 T02 开放")
              }
            />
          </section>

          <section
            className="wb-pane lumno-glass-panel"
            aria-label="选中账单上下文与复核预览"
            data-mobile-active={mobileTab === "detail" ? "true" : "false"}
          >
            <div className="wb-pane-header">
              <h2 className="wb-pane-title">
                <i className="ri-icon ri-file-search-line" aria-hidden="true" />
                <span>详情上下文与安全证据</span>
              </h2>
              {selectedRecord ? (
                <span className="wb-badge wb-badge--muted">
                  {selectedRecord.review_status}
                </span>
              ) : null}
            </div>

            <div ref={rightPaneScrollRef} className="wb-pane-body">
              {!selectedRecord ? (
                <div className="wb-empty-state lumno-paper-card">
                  <i
                    className="ri-icon ri-article-line"
                    style={{ "--ri-size": "28px" } as React.CSSProperties}
                    aria-hidden="true"
                  />
                  <strong>请在中栏选择一条账单记录</strong>
                  <span>右栏将展示精确无损数值、字段继承对比与纯文本安全原文证据</span>
                </div>
              ) : (
                <>
                  <div className="wb-section-box">
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "baseline",
                        gap: "8px",
                        flexWrap: "wrap",
                      }}
                    >
                      <div>
                        <div style={{ fontSize: "15px", fontWeight: 700 }}>
                          {selectedRecord.charge_details.charge_item ??
                            "未命名费用"}
                        </div>
                        <div className="wb-helper-note">
                          原名（只读对照）：
                          {selectedRecord.charge_details.raw_charge_name ?? "—"}
                        </div>
                      </div>
                      <div
                        className="wb-tabular"
                        style={{ fontSize: "18px", fontWeight: 700 }}
                      >
                        {formatLosslessAmount(
                          selectedRecord.charge_details,
                          "amount"
                        )}{" "}
                        {selectedRecord.charge_details.currency ?? "—"}
                      </div>
                    </div>

                    <div className="wb-kv-grid">
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">数量 × 单价（无损字符串）</div>
                        <div className="wb-kv-value wb-tabular">
                          {readDecimalString(
                            selectedRecord.charge_details,
                            "quantity"
                          ) ?? "null"}{" "}
                          ×{" "}
                          {readDecimalString(
                            selectedRecord.charge_details,
                            "unit_price"
                          ) ?? "null"}
                        </div>
                      </div>
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">AR/AP · 付款条款</div>
                        <div className="wb-kv-value">
                          {selectedRecord.financial_attributes.posting_type ??
                            "待定"}{" "}
                          ·{" "}
                          {selectedRecord.charge_details.payment_term ?? "未指定"}
                        </div>
                      </div>
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">单据类型 / 编号 / 日期</div>
                        <div className="wb-kv-value wb-mono">
                          {selectedRecord.document_info.document_type ?? "—"} ·{" "}
                          {selectedRecord.document_info.reference_no ?? "null"} ·{" "}
                          {selectedRecord.document_info.document_date ?? "null"}
                        </div>
                      </div>
                      <div className="wb-kv-item">
                        <div className="wb-kv-label">签发方 → 接收方</div>
                        <div className="wb-kv-value">
                          {selectedRecord.transaction_party.issuer ?? "—"} →{" "}
                          {selectedRecord.transaction_party.recipient ?? "—"}
                        </div>
                      </div>
                    </div>

                    {selectedRecord.shipment_refs?.mbl_nos?.length ? (
                      <div className="wb-helper-note">
                        多源 MBL：
                        {selectedRecord.shipment_refs.mbl_nos
                          .map((m) => `${m.value} (${m.source})`)
                          .join(" · ")}
                      </div>
                    ) : null}
                  </div>

                  {(selectedRecord.validation_alerts?.length ?? 0) > 0 ? (
                    <div className="wb-section-box">
                      <div className="wb-field-label">
                        <span>校验门禁告警</span>
                        <span>
                          {selectedRecord.validation_alerts?.length} 项
                        </span>
                      </div>
                      {selectedRecord.validation_alerts?.map((alert, idx) => (
                        <div
                          key={`${alert.code}-${idx}`}
                          className="wb-alert-item"
                          data-severity={alert.severity}
                        >
                          <div style={{ fontWeight: 600 }}>
                            [{alert.severity}] {alert.code}
                          </div>
                          <div>{alert.message}</div>
                          {alert.expected || alert.actual ? (
                            <div className="wb-mono" style={{ fontSize: "11px" }}>
                              期望: {alert.expected ?? "—"} | 实际:{" "}
                              {alert.actual ?? "—"}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {(selectedRecord.field_diffs?.length ?? 0) > 0 ||
                  selectedRecord.inherited_fields.length > 0 ? (
                    <div className="wb-section-box">
                      <div className="wb-field-label">
                        <span>历史账单对比与字段继承</span>
                        <span>
                          {selectedRecord.matched_bill_ref ?? "已关联基准"}
                        </span>
                      </div>
                      {selectedRecord.inherited_fields.length > 0 ? (
                        <div className="wb-helper-note">
                          继承保留字段：
                          {selectedRecord.inherited_fields.join(", ")}
                        </div>
                      ) : null}
                      {selectedRecord.field_diffs?.map((diff) => (
                        <div key={diff.field} className="wb-diff-row">
                          <span className="wb-mono">{diff.field}</span>
                          <span className="wb-tabular">
                            旧: {readDecimalString(diff, "old_value") ?? String(diff.old_value ?? "—")}
                          </span>
                          <span className="wb-tabular" style={{ fontWeight: diff.changed ? 700 : 400 }}>
                            新: {readDecimalString(diff, "new_value") ?? String(diff.new_value ?? "—")}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  <div className="wb-section-box">
                    <div className="wb-field-label">
                      <span>原文安全纯文本证据（XSS 免疫）</span>
                      <span>
                        {selectedSourceDoc?.sanitized_filename ?? "来源文档"}
                      </span>
                    </div>
                    <pre className="wb-evidence-pre">
                      {typeof selectedRecord.source_evidence?.snippet ===
                      "string"
                        ? selectedRecord.source_evidence.snippet
                        : "暂无片段摘要"}
                    </pre>
                    <div className="wb-field-label" style={{ marginTop: "4px" }}>
                      <span>AI 推理依据</span>
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
                      {selectedRecord.ai_reasoning.map((line, i) => (
                        <li key={i}>{line}</li>
                      ))}
                    </ul>
                    {(selectedSourceDoc?.excluded_clauses?.length ?? 0) > 0 ? (
                      <div style={{ marginTop: "4px" }}>
                        <div className="wb-field-label">
                          <span>已排除非计费条款</span>
                        </div>
                        {selectedSourceDoc?.excluded_clauses?.map((c, idx) => (
                          <div key={idx} className="wb-helper-note">
                            • {c.text}（{c.reason}）
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>

                  <div className="wb-section-box">
                    <div className="wb-field-label">
                      <span>人工复核快捷操作（T02 开放全字段修订表单）</span>
                    </div>
                    {hasBlockers ? (
                      <div className="wb-field-error">
                        当前记录存在未解决的 BLOCKER 硬性数值或对账阻断，禁止直接批准入账；请先在 T02 修订修正或选择驳回排除。
                      </div>
                    ) : null}
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "flex-end",
                        gap: "8px",
                        flexWrap: "wrap",
                      }}
                    >
                      <LumnoPopconfirm
                        title="确认驳回并排除该条费用？"
                        description="驳回后将从 official 与 full_standard 正式导出中自动排除。"
                        confirmLabel="确认驳回"
                        requireReason
                        defaultReason="非真实计费/确认无效"
                        onConfirm={handleRejectQuick}
                      >
                        {(triggerProps) => (
                          <LumnoButton
                            variant="ghost"
                            icon="ri-close-circle-line"
                            disabled={
                              approvingOrRejecting ||
                              selectedRecord.review_status === "REJECTED"
                            }
                            {...triggerProps}
                          >
                            驳回排除
                          </LumnoButton>
                        )}
                      </LumnoPopconfirm>

                      <LumnoButton
                        variant="primary"
                        icon="ri-check-double-line"
                        busy={approvingOrRejecting}
                        disabled={
                          selectedRecord.review_status !== "NEEDS_REVIEW" ||
                          hasBlockers
                        }
                        onClick={() => void handleApproveQuick()}
                      >
                        {hasBlockers ? "存在红灯阻断" : "批准入账"}
                      </LumnoButton>
                    </div>
                  </div>
                </>
              )}
            </div>
          </section>
        </main>
      </div>

      {/* Export Preview Modal (T01 baseline preview + T02 full controls) */}
      <LumnoModal
        open={exportModalOpen}
        title="三口径标准 JSON 导出预览"
        onClose={() => setExportModalOpen(false)}
        footer={
          <>
            <LumnoButton
              variant="secondary"
              onClick={() => setExportModalOpen(false)}
            >
              关闭
            </LumnoButton>
            <LumnoButton
              variant="primary"
              icon="ri-download-2-line"
              disabled={!activeBatch}
              onClick={() => void handleDownloadExport()}
            >
              下载顶层数组 JSON
            </LumnoButton>
          </>
        }
      >
        <LumnoSegmentedControl
          value={exportMode}
          ariaLabel="选择导出模式"
          options={[
            { value: "official", label: "正式标准 (official)" },
            { value: "full_standard", label: "四态全量 (full_standard)" },
            { value: "audit", label: "完整审计 (audit)" },
          ]}
          onChange={(mode) => {
            setExportMode(mode);
            if (activeBatch) {
              void loadExportPreview(activeBatch.id, mode);
            }
          }}
        />
        {loadingExport ? (
          <div className="wb-helper-note">正在读取实时导出统计...</div>
        ) : exportPreview ? (
          <>
            <div className="wb-notice-banner">
              <span className="wb-tabular">
                本次将导出 <strong>{exportPreview.included_count}</strong> 条记录，
                自动排除 <strong>{exportPreview.excluded_pending_count}</strong> 条待复核与{" "}
                <strong>{exportPreview.excluded_rejected_count}</strong> 条已驳回记录。
              </span>
            </div>
            <pre
              className="wb-evidence-pre"
              style={{ maxHeight: "260px", overflowY: "auto" }}
            >
              {stringifyLosslessJson(exportPreview.records, 2)}
            </pre>
          </>
        ) : null}
      </LumnoModal>

      {/* Import ReviewBundleV1 Modal */}
      <LumnoModal
        open={importModalOpen}
        title="导入 CLI 复核包（ReviewBundleV1）"
        onClose={() => setImportModalOpen(false)}
        footer={
          <>
            <LumnoButton
              variant="secondary"
              onClick={() => setImportModalOpen(false)}
            >
              取消
            </LumnoButton>
            <LumnoButton
              variant="primary"
              busy={importingBundle}
              onClick={() => void handleImportBundle()}
            >
              导入并接手复核
            </LumnoButton>
          </>
        }
      >
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <label className="lumno-btn lumno-pill lumno-btn--secondary lumno-btn--compact">
            <i className="ri-icon ri-file-code-line" aria-hidden="true" />
            <span>选择 .json 文件</span>
            <input
              type="file"
              accept=".json,application/json"
              style={{ display: "none" }}
              onChange={(e) => void handleBundleFilePick(e)}
            />
          </label>
          <span className="wb-helper-note">
            须包含 bundle_version: "1.0"、documents 与 records
          </span>
        </div>
        <LumnoTextarea
          value={bundleJsonText}
          placeholder='{"bundle_version":"1.0","accounting_entity":"NEPTUNE SHIPPING LIMITED","ocr_mode":"REMOTE_ONLY","documents":[],"records":[]}'
          aria-label="ReviewBundleV1 JSON 内容"
          style={{ minHeight: "160px", fontFamily: "var(--lumno-font-mono)" }}
          onChange={(e) => {
            setBundleJsonText(e.target.value);
            setBundleError(null);
          }}
        />
        {bundleError ? <div className="wb-field-error">{bundleError}</div> : null}
      </LumnoModal>

      {/* Master Data & Eval Overview Modal (full 4-tab drawer finalized in T03) */}
      <LumnoModal
        open={masterModalOpen}
        title="主数据档案与 Harness 评测概览（T03 完善全管理页签）"
        onClose={() => setMasterModalOpen(false)}
        footer={
          <LumnoButton
            variant="primary"
            onClick={() => setMasterModalOpen(false)}
          >
            完成
          </LumnoButton>
        }
      >
        <div className="wb-kv-grid">
          <div className="wb-kv-item">
            <div className="wb-kv-label">记账主体档案数</div>
            <div className="wb-kv-value wb-tabular">{entities.length} 家</div>
          </div>
          <div className="wb-kv-item">
            <div className="wb-kv-label">费用标准映射词典</div>
            <div className="wb-kv-value wb-tabular">
              {dictionaryList.length} 条
            </div>
          </div>
          <div className="wb-kv-item">
            <div className="wb-kv-label">全局已有历史账单库</div>
            <div className="wb-kv-value wb-tabular">
              {existingBillCount} 条
            </div>
          </div>
          <div className="wb-kv-item">
            <div className="wb-kv-label">Harness 评测历史</div>
            <div className="wb-kv-value wb-tabular">
              {evalRuns.length === 0
                ? "尚未运行（无伪造分数）"
                : `${evalRuns.length} 次运行`}
            </div>
          </div>
        </div>
      </LumnoModal>
    </>
  );
}
