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
  type BillingRecordDetail,
  type ChargeDictionaryEntry,
  type EvalRunSummary,
  type HealthStatus,
  type OcrMode,
  type ReviewBundleV1,
  type SourceDocumentSummary,
} from "./api/client.ts";
import { rawDecimal, stringifyLosslessJson } from "./api/decimalJson.ts";
import {
  buildReviewBundleV1,
  validateReviewBundleInput,
} from "./api/managementHelpers.ts";
import {
  acceptPollResult,
  buildDocTotalsDiffPatch,
  buildRecordDiffPatch,
  clearDocTotalsDraft,
  clearRecordDraft,
  countUnsavedDraftsInBatch,
  createEmptyDraftStore,
  createPollRaceTracker,
  finishMutationError,
  finishMutationSuccess,
  getDirtyDocIds,
  getDirtyRecordIds,
  setDocTotalsDraftField,
  setRecordDraftField,
  setRecordWarningWaiver,
  removeRecordWarningWaiver,
  setTrackerActiveBatch,
  startMutation,
  startPoll,
  type DraftStoreState,
  type WarningWaiverDraft,
} from "./api/reviewState.ts";
import {
  BatchQueuePanel,
  type RecordFilterTab,
} from "./components/BatchQueuePanel.tsx";
import {
  ExportAndSyncModal,
  type SyncResultRecord,
} from "./components/ExportAndSyncModal.tsx";
import {
  LumnoBackgroundStage,
  LumnoButton,
  LumnoInput,
  LumnoModal,
  LumnoSegmentedControl,
  LumnoTextarea,
  LumnoToastHost,
  type LumnoTheme,
  type ToastItem,
} from "./components/LumnoPrimitives.tsx";
import {
  ReviewInspector,
  type MutationErrorBanner,
  type RecordAuditHistoryState,
} from "./components/ReviewInspector.tsx";
import {
  ManagementDrawer,
  type ManagementDrawerTab,
} from "./components/ManagementDrawer.tsx";
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

type PendingDraftGuardAction =
  | { kind: "switch_batch"; batchId: string }
  | { kind: "open_import_bundle" }
  | { kind: "confirm_import_bundle"; bundle: ReviewBundleV1 };

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

function toMutationErrorBanner(
  title: string,
  err: unknown
): MutationErrorBanner {
  if (err instanceof ApiClientError) {
    const rawBlockers = Array.isArray(err.details?.blockers)
      ? (err.details.blockers as Array<{
          code?: unknown;
          field?: unknown;
          message?: unknown;
          expected?: unknown;
          actual?: unknown;
        }>)
      : [];
    const blockers = rawBlockers.map((b) => ({
      code: String(b.code ?? "BLOCKER"),
      field: typeof b.field === "string" ? b.field : null,
      message: String(b.message ?? ""),
      expected: typeof b.expected === "string" ? b.expected : null,
      actual: typeof b.actual === "string" ? b.actual : null,
    }));

    return {
      title,
      code: err.code,
      message: err.message,
      blockers: blockers.length > 0 ? blockers : undefined,
      isConflict: err.status === 409,
    };
  }
  return {
    title,
    message: err instanceof Error ? err.message : "操作失败，请重试",
  };
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
  const [inspectedDocId, setInspectedDocId] = useState<string | null>(null);

  // T02: Isolated Draft Store keyed by (batch_id, record_id) and (batch_id, doc_id)
  const [draftStore, setDraftStore] = useState<DraftStoreState>(
    createEmptyDraftStore
  );
  const [mutationError, setMutationError] =
    useState<MutationErrorBanner | null>(null);
  const [auditHistoryByKey, setAuditHistoryByKey] = useState<
    Record<string, RecordAuditHistoryState>
  >({});

  // T02: Monotonic revision & poll-race guard
  const pollTrackerRef = useRef(
    createPollRaceTracker(getInitialBatchIdFromUrlOrStorage())
  );
  const [batchRevision, setBatchRevision] = useState<number>(0);

  const [submittingBatch, setSubmittingBatch] = useState(false);
  const [submittingDemo, setSubmittingDemo] = useState(false);
  const [retryingDocId, setRetryingDocId] = useState<string | null>(null);
  const [forcingDocId, setForcingDocId] = useState<string | null>(null);
  const [savingDocTotals, setSavingDocTotals] = useState(false);
  const [savingRecord, setSavingRecord] = useState(false);
  const [switchingEntity, setSwitchingEntity] = useState(false);
  const [syncingBaseline, setSyncingBaseline] = useState(false);
  const [approvingOrRejecting, setApprovingOrRejecting] = useState(false);
  const [entityNotice, setEntityNotice] = useState<{
    count: number;
    message: string;
  } | null>(null);

  const [lastSyncResult, setLastSyncResult] = useState<SyncResultRecord | null>(
    null
  );

  // Modals & Management Drawer
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [bundleModalMode, setBundleModalMode] = useState<"import" | "export">(
    "import"
  );
  const [bundleJsonText, setBundleJsonText] = useState("");
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [importingBundle, setImportingBundle] = useState(false);
  const [exportBundleJsonText, setExportBundleJsonText] = useState("");
  const [exportBundleLoading, setExportBundleLoading] = useState(false);
  const [exportBundleError, setExportBundleError] = useState<string | null>(
    null
  );

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] =
    useState<ManagementDrawerTab>("entities");
  const [reapplyingDictionary, setReapplyingDictionary] = useState(false);
  const [dictionaryList, setDictionaryList] = useState<
    ChargeDictionaryEntry[]
  >([]);
  const [evalRuns, setEvalRuns] = useState<EvalRunSummary[]>([]);

  // Manual Record Creation Modal
  const [manualModalOpen, setManualModalOpen] = useState(false);
  const [manualSourceDocId, setManualSourceDocId] = useState("");
  const [manualChargeItem, setManualChargeItem] = useState("");
  const [manualRawChargeName, setManualRawChargeName] = useState("");
  const [manualAmount, setManualAmount] = useState("");
  const [manualCurrency, setManualCurrency] = useState("USD");
  const [manualPaymentTerm, setManualPaymentTerm] = useState<"PP" | "CC" | "">(
    ""
  );
  const [manualPostingType, setManualPostingType] = useState<"AR" | "AP" | "">(
    ""
  );
  const [manualReferenceNo, setManualReferenceNo] = useState("");
  const [manualDocumentDate, setManualDocumentDate] = useState("");
  const [manualIssuer, setManualIssuer] = useState("");
  const [manualRecipient, setManualRecipient] = useState("");
  const [manualReason, setManualReason] = useState("人工补录漏提费用行");
  const [manualFormError, setManualFormError] = useState<string | null>(null);
  const [submittingManual, setSubmittingManual] = useState(false);

  const [pendingDraftGuard, setPendingDraftGuard] =
    useState<PendingDraftGuardAction | null>(null);

  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastIdRef = useRef(0);

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
    pollTrackerRef.current = {
      ...pollTrackerRef.current,
      activeBatchId: detail.id,
    };
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
    setInspectedDocId((prevDoc) => {
      if (prevDoc && detail.documents.some((d) => d.id === prevDoc)) {
        return prevDoc;
      }
      return null;
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
        const [healthRes, entitiesRes, billsRes, batchList, dictRes] =
          await Promise.all([
            api.getHealth(controller.signal).catch((err) => {
              setHealthError(
                err instanceof Error ? err.message : "服务连接失败"
              );
              return null;
            }),
            api.listAccountingEntities(controller.signal).catch(() => []),
            api.listExistingBills(undefined, controller.signal).catch(() => []),
            api.listBatches(20, controller.signal).catch(() => []),
            api.listChargeDictionary(controller.signal).catch(() => []),
          ]);
        if (controller.signal.aborted) return;
        if (healthRes) {
          setHealth(healthRes);
          setHealthError(null);
        }
        setEntities(entitiesRes);
        setDictionaryList(dictRes);
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
          pollTrackerRef.current = setTrackerActiveBatch(
            pollTrackerRef.current,
            targetId
          );
          const started = startPoll(pollTrackerRef.current, targetId);
          pollTrackerRef.current = started.nextTracker;
          const detail = await api.getBatchDetail(targetId, controller.signal);
          if (controller.signal.aborted) return;
          const check = acceptPollResult(pollTrackerRef.current, started.ticket);
          pollTrackerRef.current = check.nextTracker;
          if (check.accepted) {
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

  // Real-time polling while batch stage is active (guarded by PollRaceTracker)
  useEffect(() => {
    if (!activeBatch || !isActiveBatchStage(activeBatch.stage)) {
      return;
    }
    const targetBatchId = activeBatch.id;
    const controller = new AbortController();

    const timer = window.setTimeout(async () => {
      const started = startPoll(pollTrackerRef.current, targetBatchId);
      pollTrackerRef.current = started.nextTracker;
      try {
        const detail = await api.getBatchDetail(targetBatchId, controller.signal);
        if (controller.signal.aborted) return;
        const check = acceptPollResult(pollTrackerRef.current, started.ticket);
        pollTrackerRef.current = check.nextTracker;
        if (!check.accepted) {
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

  const dirtyRecordIds = useMemo(() => {
    if (!activeBatch) return new Set<string>();
    return getDirtyRecordIds(draftStore, activeBatch.id, activeBatch.records);
  }, [activeBatch, draftStore]);

  const dirtyDocIds = useMemo(() => {
    if (!activeBatch) return new Set<string>();
    return getDirtyDocIds(draftStore, activeBatch.id, activeBatch.documents);
  }, [activeBatch, draftStore]);

  const unsavedDraftCount = useMemo(
    () => countUnsavedDraftsInBatch(draftStore, activeBatch),
    [draftStore, activeBatch]
  );

  const executeSelectBatch = async (batchId: string) => {
    if (!batchId) return;
    pollTrackerRef.current = setTrackerActiveBatch(
      pollTrackerRef.current,
      batchId
    );
    setBatchRevision(pollTrackerRef.current.batchRevision);
    setActiveDocFilterId(null);
    setInspectedDocId(null);
    setFilterTab("all");
    setEntityNotice(null);
    setMutationError(null);
    setActiveBatchId(batchId);
    const started = startPoll(pollTrackerRef.current, batchId);
    pollTrackerRef.current = started.nextTracker;
    try {
      const detail = await api.getBatchDetail(batchId);
      const check = acceptPollResult(pollTrackerRef.current, started.ticket);
      pollTrackerRef.current = check.nextTracker;
      if (check.accepted) {
        applyFreshBatchDetail(detail);
      }
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "加载批次详情失败",
        "error"
      );
    }
  };

  const handleSelectBatch = (batchId: string) => {
    if (!batchId || batchId === activeBatchId) return;
    if (unsavedDraftCount > 0) {
      setPendingDraftGuard({ kind: "switch_batch", batchId });
      return;
    }
    void executeSelectBatch(batchId);
  };

  const handleRefreshActiveBatch = async () => {
    if (!activeBatch) return;
    const started = startPoll(pollTrackerRef.current, activeBatch.id);
    pollTrackerRef.current = started.nextTracker;
    try {
      const detail = await api.getBatchDetail(activeBatch.id);
      const check = acceptPollResult(pollTrackerRef.current, started.ticket);
      pollTrackerRef.current = check.nextTracker;
      if (check.accepted) {
        applyFreshBatchDetail(detail);
        setMutationError(null);
        pushToast("已刷新当前批次最新服务端状态");
      }
    } catch (err) {
      pushToast(
        err instanceof Error ? err.message : "刷新批次详情失败",
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
    try {
      const res = await api.createBatch({
        accounting_entity: args.accountingEntity,
        ocr_mode: args.ocrMode,
        files: args.files,
        public_urls: args.publicUrls,
        existing_bills_json: args.existingBillsJsonText,
        wait: false,
      });
      pollTrackerRef.current = setTrackerActiveBatch(
        pollTrackerRef.current,
        res.id
      );
      setBatchRevision(pollTrackerRef.current.batchRevision);
      setActiveDocFilterId(null);
      setInspectedDocId(null);
      setEntityNotice(null);
      setMutationError(null);
      setActiveBatchId(res.id);
      const started = startPoll(pollTrackerRef.current, res.id);
      pollTrackerRef.current = started.nextTracker;
      const detail = await api.getBatchDetail(res.id);
      const check = acceptPollResult(pollTrackerRef.current, started.ticket);
      pollTrackerRef.current = check.nextTracker;
      if (check.accepted) {
        applyFreshBatchDetail(detail);
      }
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
    try {
      const res = await api.createDemoBatch({
        accounting_entity: args.accountingEntity,
        ocr_mode: args.ocrMode,
        wait: false,
      });
      pollTrackerRef.current = setTrackerActiveBatch(
        pollTrackerRef.current,
        res.id
      );
      setBatchRevision(pollTrackerRef.current.batchRevision);
      setActiveDocFilterId(null);
      setInspectedDocId(null);
      setEntityNotice(null);
      setMutationError(null);
      setActiveBatchId(res.id);
      const started = startPoll(pollTrackerRef.current, res.id);
      pollTrackerRef.current = started.nextTracker;
      const detail = await api.getBatchDetail(res.id);
      const check = acceptPollResult(pollTrackerRef.current, started.ticket);
      pollTrackerRef.current = check.nextTracker;
      if (check.accepted) {
        applyFreshBatchDetail(detail);
      }
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
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const res = await api.switchBatchAccountingEntity(
        activeBatch.id,
        nextEntity
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(res.batch);
        setDraftStore(createEmptyDraftStore());
        setAuditHistoryByKey({});
        setMutationError(null);
        setEntityNotice({
          count: res.reset_approval_count,
          message: res.notice,
        });
      }
      pushToast(`已切换主体为 ${nextEntity}`);
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
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
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.forceParseSkippedDocument(
        activeBatch.id,
        docId
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
      }
      pushToast("已强制将该跳过图片纳入 OCR 解析");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("强制 OCR 提取失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setForcingDocId(null);
    }
  };

  const handleRetryDoc = async (
    docId: string,
    options: { ocrMode?: OcrMode; publicUrl?: string; file?: File } = {}
  ) => {
    if (!activeBatch) return;
    setRetryingDocId(docId);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.retryOrReplaceDocument(activeBatch.id, docId, {
        ocr_mode: options.ocrMode ?? activeBatch.ocr_mode,
        public_url: options.publicUrl,
        file: options.file,
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
        setAuditHistoryByKey({});
      }
      pushToast(
        options.file ? "已上传替换文件并重新计算" : "已触发单文件重试"
      );
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("重试或替换文件失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setRetryingDocId(null);
    }
  };

  const handleSaveDocTotals = async (doc: SourceDocumentSummary) => {
    if (!activeBatch) return;
    const diff = buildDocTotalsDiffPatch(doc, draftStore, activeBatch.id);
    if (!diff.valid || !diff.hasChanges) return;

    setSavingDocTotals(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.patchDocumentTotals(activeBatch.id, doc.id, {
        subtotal_amount: diff.patch.subtotal_amount,
        tax_amount: diff.patch.tax_amount,
        total_amount: diff.patch.total_amount,
        total_currency: diff.patch.total_currency,
        review_note: diff.patch.review_note,
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setDraftStore((prev) =>
          clearDocTotalsDraft(prev, activeBatch.id, doc.id)
        );
        setMutationError(null);
        setAuditHistoryByKey({});
      }
      pushToast(`已保存 ${doc.sanitized_filename} 汇总并重算同单所有行`);
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("保存单据汇总失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setSavingDocTotals(false);
    }
  };

  const handleSaveRecordPatch = async (record: BillingRecordDetail) => {
    if (!activeBatch) return;
    const diff = buildRecordDiffPatch(record, draftStore, activeBatch.id);
    if (!diff.valid || !diff.hasChanges) return;

    setSavingRecord(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.patchBillingRecord(
        activeBatch.id,
        record.id,
        diff.patch,
        {
          expectedRecordUpdatedAt: record.updated_at ?? null,
          resolvedWarnings: diff.resolvedWarnings,
        }
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setDraftStore((prev) =>
          clearRecordDraft(prev, activeBatch.id, record.id)
        );
        setMutationError(null);
        setAuditHistoryByKey((prev) => {
          const next = { ...prev };
          delete next[`${activeBatch.id}::${record.id}`];
          return next;
        });
      }
      pushToast("已保存修订并完成门禁重验");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("保存修订未通过", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setSavingRecord(false);
    }
  };

  const handleApproveRecord = async (
    record: BillingRecordDetail,
    reason: string
  ) => {
    if (!activeBatch) return;
    setApprovingOrRejecting(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.approveBillingRecord(
        activeBatch.id,
        record.id,
        reason,
        {
          expectedRecordUpdatedAt: record.updated_at ?? null,
        }
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setDraftStore((prev) =>
          clearRecordDraft(prev, activeBatch.id, record.id)
        );
        setMutationError(null);
        setAuditHistoryByKey((prev) => {
          const next = { ...prev };
          delete next[`${activeBatch.id}::${record.id}`];
          return next;
        });
      }
      pushToast("已批准该条费用记录入账");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("批准入账被拦截", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setApprovingOrRejecting(false);
    }
  };

  const handleRejectRecord = async (
    record: BillingRecordDetail,
    reason: string
  ) => {
    if (!activeBatch) return;
    setApprovingOrRejecting(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.rejectBillingRecord(
        activeBatch.id,
        record.id,
        reason
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setDraftStore((prev) =>
          clearRecordDraft(prev, activeBatch.id, record.id)
        );
        setMutationError(null);
        setAuditHistoryByKey((prev) => {
          const next = { ...prev };
          delete next[`${activeBatch.id}::${record.id}`];
          return next;
        });
      }
      pushToast("已将该费用记录驳回并从 official 口径排除");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = toMutationErrorBanner("驳回操作失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setApprovingOrRejecting(false);
    }
  };

  const handleRequestAuditHistory = useCallback(
    async (batchId: string, recordId: string) => {
      const key = `${batchId}::${recordId}`;
      setAuditHistoryByKey((prev) => ({
        ...prev,
        [key]: {
          loading: true,
          error: null,
          llmCallsUsed: prev[key]?.llmCallsUsed ?? null,
          retryCount: prev[key]?.retryCount ?? null,
          history: prev[key]?.history ?? [],
        },
      }));
      try {
        const res = await api.exportBatchJson(batchId, "audit");
        const matched = res.records.find(
          (item) => item.audit_trail.record_id === recordId
        );
        const trail: Record<string, unknown> = matched?.audit_trail ?? {};
        const llmCallsUsed =
          typeof trail.llm_calls_used === "number" ? trail.llm_calls_used : null;
        const retryCount =
          typeof trail.retry_count === "number" ? trail.retry_count : 0;
        const rawHist = Array.isArray(trail.human_review_history)
          ? (trail.human_review_history as Array<Record<string, unknown>>)
          : [];
        const history = rawHist.map((entry) => ({
          action: String(entry.action ?? "UPDATED"),
          operator: String(entry.operator ?? "财务复核员"),
          timestamp: String(entry.timestamp ?? ""),
          reason:
            typeof entry.reason === "string" ? entry.reason : undefined,
        }));

        setAuditHistoryByKey((prev) => ({
          ...prev,
          [key]: {
            loading: false,
            error: null,
            llmCallsUsed,
            retryCount,
            history,
          },
        }));
      } catch (err) {
        setAuditHistoryByKey((prev) => ({
          ...prev,
          [key]: {
            loading: false,
            error:
              err instanceof Error ? err.message : "按需加载审计历史失败",
            llmCallsUsed: null,
            retryCount: null,
            history: [],
          },
        }));
      }
    },
    [api]
  );

  const handleSyncBaseline = async () => {
    if (!activeBatch) return;
    setSyncingBaseline(true);
    try {
      const res = await api.syncBatchToBaseline(activeBatch.id);
      const bills = await api.listExistingBills().catch(() => []);
      setExistingBillCount(bills.length);
      setLastSyncResult({
        batchId: activeBatch.id,
        result: res,
        syncedAt: new Date().toISOString(),
      });
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

  const handleOpenManagementDrawer = async (tab: ManagementDrawerTab) => {
    setDrawerTab(tab);
    setDrawerOpen(true);
    try {
      const [ents, dict, runs, bills] = await Promise.all([
        api.listAccountingEntities(),
        api.listChargeDictionary(),
        api.listEvalRuns(),
        api.listExistingBills(),
      ]);
      setEntities(ents);
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

  const handleReapplyDictionary = async () => {
    if (!activeBatch) return;
    setReapplyingDictionary(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.reapplyBatchChargeDictionary(activeBatch.id);
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setAuditHistoryByKey({});
        setMutationError(null);
      }
      pushToast(
        `已按最新词典仅重新标准化批次 ${updated.short_code}（已保留人工修订）`
      );
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      pushToast(
        err instanceof Error ? err.message : "当前批次重标准化失败",
        "error"
      );
    } finally {
      setReapplyingDictionary(false);
    }
  };

  const handleOpenImportBundleClick = () => {
    setBundleModalMode("import");
    setBundleError(null);
    if (unsavedDraftCount > 0) {
      setPendingDraftGuard({ kind: "open_import_bundle" });
      return;
    }
    setImportModalOpen(true);
  };

  const handleOpenExportBundleClick = () => {
    setBundleModalMode("export");
    setBundleError(null);
    setImportModalOpen(true);
  };

  useEffect(() => {
    if (!importModalOpen || bundleModalMode !== "export" || !activeBatch) {
      return;
    }
    let cancelled = false;
    setExportBundleLoading(true);
    setExportBundleError(null);
    void Promise.all([
      api.listExistingBills(activeBatch.accounting_entity).catch(() => []),
      api.exportBatchJson(activeBatch.id, "audit").catch(() => null),
    ])
      .then(([billsSnapshot, auditExport]) => {
        if (cancelled) return;
        const bundle = buildReviewBundleV1(
          activeBatch,
          billsSnapshot,
          auditExport
        );
        setExportBundleJsonText(stringifyLosslessJson(bundle, 2));
      })
      .catch((err) => {
        if (cancelled) return;
        setExportBundleError(
          err instanceof Error ? err.message : "生成 ReviewBundleV1 复核包失败"
        );
      })
      .finally(() => {
        if (!cancelled) {
          setExportBundleLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [importModalOpen, bundleModalMode, activeBatch, batchRevision, api]);

  const handleBundleFilePick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await file.text();
    setBundleJsonText(text);
    setBundleError(null);
  };

  const executeImportBundle = async (bundle: ReviewBundleV1) => {
    setImportingBundle(true);
    setBundleError(null);
    try {
      const imported = await api.importReviewBundle(bundle);
      pollTrackerRef.current = setTrackerActiveBatch(
        pollTrackerRef.current,
        imported.id
      );
      setBatchRevision(pollTrackerRef.current.batchRevision);
      applyFreshBatchDetail(imported);
      setAuditHistoryByKey({});
      setEntityNotice(null);
      await refreshBatchesList().catch(() => undefined);
      setImportModalOpen(false);
      setBundleJsonText("");
      setMutationError(null);
      setMobileTab("queue");
      pushToast(`已接手 ReviewBundleV1 复核包 ${imported.short_code}`);
    } catch (err) {
      setBundleError(err instanceof Error ? err.message : "导入复核包失败");
    } finally {
      setImportingBundle(false);
    }
  };

  const handleImportBundle = async () => {
    const check = validateReviewBundleInput(bundleJsonText);
    if (!check.valid) {
      setBundleError(check.error);
      return;
    }
    if (unsavedDraftCount > 0) {
      setPendingDraftGuard({
        kind: "confirm_import_bundle",
        bundle: check.bundle,
      });
      return;
    }
    await executeImportBundle(check.bundle);
  };

  const handleCopyExportBundle = async () => {
    if (!exportBundleJsonText) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(exportBundleJsonText);
      } else {
        const ta = document.createElement("textarea");
        ta.value = exportBundleJsonText;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      pushToast("已复制 ReviewBundleV1 (v1.0) 无损复核包 JSON");
    } catch {
      pushToast("复制到剪贴板失败，请手动选中预览内容复制", "error");
    }
  };

  const handleDownloadExportBundle = () => {
    if (!activeBatch || !exportBundleJsonText) return;
    const blob = new Blob([exportBundleJsonText], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${activeBatch.short_code}.review-bundle.v1.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    pushToast(`已导出 ${activeBatch.short_code}.review-bundle.v1.json`);
  };

  const handleOpenManualModal = (defaultDocId?: string) => {
    if (!activeBatch) return;
    const fallbackDocId =
      defaultDocId ??
      inspectedDocId ??
      selectedRecord?.source_doc_id ??
      activeBatch.documents.find((d) => d.status !== "FAILED")?.id ??
      activeBatch.documents[0]?.id ??
      "";
    setManualSourceDocId(fallbackDocId);
    setManualChargeItem("");
    setManualRawChargeName("");
    setManualAmount("");
    setManualCurrency("USD");
    setManualPaymentTerm("");
    setManualPostingType("");
    setManualReferenceNo("");
    setManualDocumentDate("");
    setManualIssuer("");
    setManualRecipient("");
    setManualReason("人工补录漏提费用行");
    setManualFormError(null);
    setManualModalOpen(true);
  };

  const handleSubmitManualRecord = async () => {
    if (!activeBatch) return;
    setManualFormError(null);
    if (!manualSourceDocId) {
      setManualFormError("请选择所属来源文档");
      return;
    }
    if (!manualChargeItem.trim()) {
      setManualFormError("请输入标准费用名称 (charge_item)");
      return;
    }
    let validatedAmt;
    try {
      validatedAmt = rawDecimal(manualAmount);
    } catch (err) {
      setManualFormError(
        err instanceof Error ? err.message : "金额须为合法十进制数字"
      );
      return;
    }
    const currUpper = manualCurrency.trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(currUpper)) {
      setManualFormError("币种须为 3 位大写 ISO 4217 代码（如 USD）");
      return;
    }
    if (manualDocumentDate.trim() && !/^\d{4}-\d{2}-\d{2}$/.test(manualDocumentDate.trim())) {
      setManualFormError("单据日期格式须为 YYYY-MM-DD");
      return;
    }

    setSubmittingManual(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      activeBatch.id
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.addManualBillingRecord(activeBatch.id, {
        source_doc_id: manualSourceDocId,
        charge_item: manualChargeItem.trim(),
        raw_charge_name: manualRawChargeName.trim() || null,
        amount: validatedAmt,
        currency: currUpper,
        payment_term: manualPaymentTerm === "" ? null : manualPaymentTerm,
        posting_type: manualPostingType === "" ? null : manualPostingType,
        reference_no: manualReferenceNo.trim() || null,
        document_date: manualDocumentDate.trim() || null,
        issuer: manualIssuer.trim() || null,
        recipient: manualRecipient.trim() || null,
        reason: manualReason.trim() || "人工补录漏提费用行",
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        const newest = updated.records[updated.records.length - 1];
        if (newest) {
          setSelectedRecordId(newest.id);
        }
        setManualModalOpen(false);
        setMobileTab("detail");
      }
      pushToast("已从来源单据补充一条费用记录");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      setManualFormError(
        err instanceof Error ? err.message : "手工补充费用行失败"
      );
    } finally {
      setSubmittingManual(false);
    }
  };

  const selectedRecord = useMemo<BillingRecordDetail | null>(() => {
    if (!activeBatch || !selectedRecordId) return null;
    return (
      activeBatch.records.find((r) => r.id === selectedRecordId) ?? null
    );
  }, [activeBatch, selectedRecordId]);

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
          unsavedDraftCount={unsavedDraftCount}
          theme={theme}
          syncingBaseline={syncingBaseline}
          switchingEntity={switchingEntity}
          onSelectBatch={handleSelectBatch}
          onConfirmSwitchEntity={handleConfirmSwitchEntity}
          onToggleTheme={() =>
            setTheme((prev) => (prev === "light" ? "dark" : "light"))
          }
          onOpenImportBundle={handleOpenImportBundleClick}
          onOpenExportBundle={handleOpenExportBundleClick}
          onOpenMasterData={() => void handleOpenManagementDrawer("entities")}
          onOpenEvalDashboard={() => void handleOpenManagementDrawer("eval_runs")}
          onSyncBaseline={() => void handleSyncBaseline()}
          onOpenExport={() => setExportModalOpen(true)}
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
              dirtyDocIds={dirtyDocIds}
              submittingBatch={submittingBatch}
              submittingDemo={submittingDemo}
              retryingDocId={retryingDocId}
              forcingDocId={forcingDocId}
              onSubmitBatch={handleSubmitBatch}
              onSubmitDemo={handleSubmitDemo}
              onSelectDocFilter={(docId) => setActiveDocFilterId(docId)}
              onInspectDoc={(docId) => {
                setInspectedDocId(docId);
                setMobileTab("detail");
              }}
              onForceParseDoc={(docId) => void handleForceParseDoc(docId)}
              onRetryOrReplaceDoc={(docId, file) =>
                void handleRetryDoc(docId, { file })
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
              dirtyRecordIds={dirtyRecordIds}
              retryingDocId={retryingDocId}
              entityNotice={entityNotice}
              onChangeFilterTab={setFilterTab}
              onClearDocFilter={() => setActiveDocFilterId(null)}
              onSelectRecord={(recId) => {
                setSelectedRecordId(recId);
                setMutationError(null);
                setMobileTab("detail");
              }}
              onInspectDoc={(docId) => {
                setInspectedDocId(docId);
                setMobileTab("detail");
              }}
              onRetryOrReplaceDoc={(docId, file) =>
                void handleRetryDoc(docId, { file })
              }
              onDismissEntityNotice={() => setEntityNotice(null)}
              onOpenManualRecord={() => handleOpenManualModal()}
            />
          </section>

          <section
            className="wb-pane lumno-glass-panel"
            aria-label="右栏全字段复核与文档修理"
            data-mobile-active={mobileTab === "detail" ? "true" : "false"}
          >
            <ReviewInspector
              batch={activeBatch}
              selectedRecord={selectedRecord}
              inspectedDocId={inspectedDocId}
              draftStore={draftStore}
              dictionaryList={dictionaryList}
              savingRecord={savingRecord}
              approvingOrRejecting={approvingOrRejecting}
              savingDocTotals={savingDocTotals}
              retryingDocId={retryingDocId}
              forcingDocId={forcingDocId}
              mutationError={mutationError}
              auditHistoryByKey={auditHistoryByKey}
              onClearMutationError={() => setMutationError(null)}
              onRefreshBatch={() => void handleRefreshActiveBatch()}
              onClearInspectedDoc={() => setInspectedDocId(null)}
              onUpdateRecordField={(record, field, value) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  setRecordDraftField(
                    prev,
                    activeBatch.id,
                    record,
                    field,
                    value
                  )
                );
              }}
              onSetWarningWaiver={(recordId, waiver: WarningWaiverDraft) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  setRecordWarningWaiver(prev, activeBatch.id, recordId, waiver)
                );
              }}
              onRemoveWarningWaiver={(recordId, warningKey) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  removeRecordWarningWaiver(
                    prev,
                    activeBatch.id,
                    recordId,
                    warningKey
                  )
                );
              }}
              onResetRecordDraft={(recordId) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  clearRecordDraft(prev, activeBatch.id, recordId)
                );
                setMutationError(null);
              }}
              onSaveRecordPatch={handleSaveRecordPatch}
              onApproveRecord={handleApproveRecord}
              onRejectRecord={handleRejectRecord}
              onUpdateDocField={(doc, field, value) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  setDocTotalsDraftField(
                    prev,
                    activeBatch.id,
                    doc,
                    field,
                    value
                  )
                );
              }}
              onResetDocDraft={(docId) => {
                if (!activeBatch) return;
                setDraftStore((prev) =>
                  clearDocTotalsDraft(prev, activeBatch.id, docId)
                );
                setMutationError(null);
              }}
              onSaveDocTotals={handleSaveDocTotals}
              onRetryDoc={handleRetryDoc}
              onForceParseDoc={handleForceParseDoc}
              onOpenManualRecordModal={(docId) => handleOpenManualModal(docId)}
              onRequestAuditHistory={handleRequestAuditHistory}
            />
          </section>
        </main>
      </div>

      <ExportAndSyncModal
        open={exportModalOpen}
        batch={activeBatch}
        batchRevision={batchRevision}
        api={api}
        syncingBaseline={syncingBaseline}
        lastSyncResult={lastSyncResult}
        onClose={() => setExportModalOpen(false)}
        onSyncBaseline={handleSyncBaseline}
        onOpenBundleExport={handleOpenExportBundleClick}
        onNotify={pushToast}
      />

      {/* Manual Billing Record Creation Modal */}
      <LumnoModal
        open={manualModalOpen}
        title="从来源单据补充漏提费用行"
        onClose={() => setManualModalOpen(false)}
        footer={
          <>
            <LumnoButton
              variant="secondary"
              onClick={() => setManualModalOpen(false)}
            >
              取消
            </LumnoButton>
            <LumnoButton
              variant="primary"
              icon="ri-add-line"
              busy={submittingManual}
              onClick={() => void handleSubmitManualRecord()}
            >
              确认新增费用行
            </LumnoButton>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
          {activeBatch ? (
            <label className="wb-form-field">
              <span className="wb-form-label">所属来源文档 (source_doc_id)</span>
              <select
                className="lumno-input"
                value={manualSourceDocId}
                onChange={(e) => setManualSourceDocId(e.target.value)}
              >
                {activeBatch.documents.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.sanitized_filename} ({doc.status})
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          <div className="wb-form-grid-2">
            <label className="wb-form-field">
              <span className="wb-form-label">标准费用名 (charge_item) *</span>
              <LumnoInput
                value={manualChargeItem}
                placeholder="如 Handling Charge"
                onChange={(e) => setManualChargeItem(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">原始费用名 (raw_charge_name)</span>
              <LumnoInput
                value={manualRawChargeName}
                placeholder="可选原文别名"
                onChange={(e) => setManualRawChargeName(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">金额 (amount) *</span>
              <LumnoInput
                className="wb-tabular"
                inputMode="decimal"
                value={manualAmount}
                placeholder="如 120.00"
                onChange={(e) => setManualAmount(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">币种 (currency) *</span>
              <LumnoInput
                className="wb-mono"
                maxLength={3}
                value={manualCurrency}
                placeholder="USD"
                onChange={(e) => setManualCurrency(e.target.value.toUpperCase())}
              />
            </label>
            <div className="wb-form-field">
              <span className="wb-form-label">付款条款 (payment_term)</span>
              <LumnoSegmentedControl
                value={manualPaymentTerm}
                ariaLabel="新增行付款条款"
                options={[
                  { value: "", label: "未定" },
                  { value: "PP", label: "PP" },
                  { value: "CC", label: "CC" },
                ]}
                onChange={setManualPaymentTerm}
              />
            </div>
            <div className="wb-form-field">
              <span className="wb-form-label">记账方向 (posting_type)</span>
              <LumnoSegmentedControl
                value={manualPostingType}
                ariaLabel="新增行记账方向"
                options={[
                  { value: "", label: "待定" },
                  { value: "AR", label: "AR" },
                  { value: "AP", label: "AP" },
                ]}
                onChange={setManualPostingType}
              />
            </div>
            <label className="wb-form-field">
              <span className="wb-form-label">单据编号 (reference_no)</span>
              <LumnoInput
                className="wb-mono"
                value={manualReferenceNo}
                placeholder="如 DC-108478"
                onChange={(e) => setManualReferenceNo(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">单据日期 (YYYY-MM-DD)</span>
              <LumnoInput
                className="wb-mono"
                value={manualDocumentDate}
                placeholder="2026-11-20"
                onChange={(e) => setManualDocumentDate(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">签发方 (issuer)</span>
              <LumnoInput
                value={manualIssuer}
                onChange={(e) => setManualIssuer(e.target.value)}
              />
            </label>
            <label className="wb-form-field">
              <span className="wb-form-label">接收方 (recipient)</span>
              <LumnoInput
                value={manualRecipient}
                onChange={(e) => setManualRecipient(e.target.value)}
              />
            </label>
          </div>

          <label className="wb-form-field">
            <span className="wb-form-label">补录原因说明 (reason)</span>
            <LumnoInput
              value={manualReason}
              onChange={(e) => setManualReason(e.target.value)}
            />
          </label>

          {manualFormError ? (
            <div className="wb-field-error">{manualFormError}</div>
          ) : null}
        </div>
      </LumnoModal>

      {/* Unsaved Draft Guard Modal */}
      <LumnoModal
        open={pendingDraftGuard !== null}
        title="存在未保存的本地草稿"
        onClose={() => setPendingDraftGuard(null)}
        footer={
          <>
            <LumnoButton
              variant="secondary"
              onClick={() => setPendingDraftGuard(null)}
            >
              返回先保存
            </LumnoButton>
            <LumnoButton
              variant="primary"
              onClick={() => {
                const action = pendingDraftGuard;
                setPendingDraftGuard(null);
                if (!action) return;
                if (action.kind === "switch_batch") {
                  void executeSelectBatch(action.batchId);
                } else if (action.kind === "open_import_bundle") {
                  setBundleModalMode("import");
                  setImportModalOpen(true);
                } else if (action.kind === "confirm_import_bundle") {
                  void executeImportBundle(action.bundle);
                }
              }}
            >
              继续切换（保留后台草稿缓存）
            </LumnoButton>
          </>
        }
      >
        <div className="wb-alert-item" data-severity="WARNING">
          当前批次尚有 <strong>{unsavedDraftCount}</strong>{" "}
          项未保存修订草稿。切换批次或导入新复核包时，当前未提交编辑不会写入后端（切回原批次仍在本地保留）。
        </div>
      </LumnoModal>

      {/* Bidirectional ReviewBundleV1 ("1.0") Interop Modal */}
      <LumnoModal
        open={importModalOpen}
        title="复核包互通（ReviewBundleV1 · bundle_version: 1.0）"
        onClose={() => setImportModalOpen(false)}
        footer={
          bundleModalMode === "import" ? (
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
          ) : (
            <>
              <LumnoButton
                variant="secondary"
                onClick={() => setImportModalOpen(false)}
              >
                关闭
              </LumnoButton>
              <LumnoButton
                variant="secondary"
                icon="ri-file-copy-line"
                disabled={!exportBundleJsonText || exportBundleLoading}
                onClick={() => void handleCopyExportBundle()}
              >
                复制复核包 JSON
              </LumnoButton>
              <LumnoButton
                variant="primary"
                icon="ri-download-2-line"
                disabled={!activeBatch || !exportBundleJsonText || exportBundleLoading}
                onClick={handleDownloadExportBundle}
              >
                下载复核包 (.json)
              </LumnoButton>
            </>
          )
        }
      >
        <LumnoSegmentedControl<"import" | "export">
          value={bundleModalMode}
          ariaLabel="复核包互通操作模式"
          options={[
            { value: "import", label: "导入接手复核包" },
            { value: "export", label: "导出当前批复核包" },
          ]}
          onChange={(mode) => {
            setBundleModalMode(mode);
            setBundleError(null);
          }}
        />

        {bundleModalMode === "import" ? (
          <>
            {unsavedDraftCount > 0 ? (
              <div className="wb-alert-item" data-severity="WARNING">
                提示：当前批次有 {unsavedDraftCount} 项未保存草稿，提交导入时将再次确认，切换后当前草稿保留在原批次本地缓存中。
              </div>
            ) : null}
            <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
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
                须包含 bundle_version: "1.0"、accounting_entity、documents 与 records
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
          </>
        ) : !activeBatch ? (
          <div className="wb-empty-state">
            <span>当前尚未选中批次，请先在工作台加载或选择一个批次后再导出 ReviewBundleV1。</span>
          </div>
        ) : (
          <>
            {unsavedDraftCount > 0 ? (
              <div className="wb-alert-item" data-severity="WARNING">
                当前批次尚有 {unsavedDraftCount} 项未保存本地草稿，导出的复核包基于服务端已保存快照与审计历史，建议先保存修订。
              </div>
            ) : null}
            <div className="wb-notice-banner">
              <span>
                导出批次 <strong>{activeBatch.short_code}</strong> 的完整 ReviewBundleV1（含 bundle_version: "1.0"、高精度数值、历史快照与人工复核历史），可供 CLI 或另一工作台实例无损接手。
              </span>
            </div>
            {exportBundleLoading ? (
              <div className="wb-helper-note">正在生成无损 ReviewBundleV1 快照...</div>
            ) : exportBundleError ? (
              <div className="wb-field-error">{exportBundleError}</div>
            ) : (
              <pre
                className="wb-evidence-pre wb-tabular"
                style={{ maxHeight: "240px", overflowY: "auto" }}
              >
                {exportBundleJsonText}
              </pre>
            )}
          </>
        )}
      </LumnoModal>

      <ManagementDrawer
        open={drawerOpen}
        initialTab={drawerTab}
        api={api}
        activeBatch={activeBatch}
        entities={entities}
        dictionaryList={dictionaryList}
        evalRuns={evalRuns}
        selectedEntity={selectedEntity}
        reapplyingDictionary={reapplyingDictionary}
        onClose={() => setDrawerOpen(false)}
        onEntitiesChanged={setEntities}
        onDictionaryChanged={setDictionaryList}
        onExistingBillsCountChanged={setExistingBillCount}
        onEvalRunsChanged={setEvalRuns}
        onReapplyDictionary={handleReapplyDictionary}
        onNotify={pushToast}
      />
    </>
  );
}
