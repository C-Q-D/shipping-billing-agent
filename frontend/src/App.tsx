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
  isValidIso4217Currency,
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
import { rawDecimal } from "./api/decimalJson.ts";
import { validateReviewBundleInput } from "./api/managementHelpers.ts";
import {
  acceptPollResult,
  beginDocTotalsSave,
  beginRecordSave,
  buildDocTotalsDiffPatch,
  buildRecordDiffPatch,
  clearBatchDrafts,
  clearBatchScopedMap,
  clearDocTotalsDraft,
  clearRecordDraft,
  countUnsavedDraftsInBatch,
  createEmptyDraftStore,
  createPollRaceTracker,
  extractMutationErrorBanner,
  finishDocTotalsSaveError,
  finishDocTotalsSaveSuccess,
  finishMutationError,
  finishMutationSuccess,
  finishRecordSaveError,
  finishRecordSaveSuccess,
  getDirtyDocIds,
  getDirtyRecordIds,
  getDocTotalsDraftVersion,
  getRecordDraftVersion,
  hasRecordDraft,
  isActiveBatchStage,
  makeDocDraftKey,
  makeRecordDraftKey,
  setDocTotalsDraftField,
  setRecordDraftField,
  setTrackerActiveBatch,
  shouldContinuePollingAfterDiscard,
  startMutation,
  startPoll,
  validateReviewReason,
  type DraftStoreState,
  type MutationErrorBanner,
  type PendingBatchDetailRecovery,
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

type MobileViewTab = "upload" | "queue" | "detail";

type PendingDraftGuardAction =
  | { kind: "switch_batch"; batchId: string }
  | { kind: "open_import_bundle" }
  | { kind: "confirm_import_bundle"; bundle: ReviewBundleV1 };

export interface AppProps {
  initialBatch?: BatchDetail | null;
  initialBatches?: BatchSummary[];
  initialPendingBatchRecovery?: PendingBatchDetailRecovery | null;
}

function toBatchSummary(item: BatchSummary | BatchDetail): BatchSummary {
  return {
    id: item.id,
    short_code: item.short_code,
    accounting_entity: item.accounting_entity,
    source_channel: item.source_channel,
    status: item.status,
    stage: item.stage,
    stage_message: item.stage_message,
    ocr_mode: item.ocr_mode,
    total_files: item.total_files,
    succeeded_files: item.succeeded_files,
    failed_files: item.failed_files,
    counts: item.counts,
    created_at: item.created_at,
    updated_at: item.updated_at,
  };
}

function syncPersistedBatchId(batchId: string): void {
  if (typeof window === "undefined" || !batchId) return;
  try {
    window.localStorage.setItem(LAST_BATCH_STORAGE_KEY, batchId);
  } catch {
    // ignore storage quota or privacy restrictions
  }
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get("batch") !== batchId) {
      url.searchParams.set("batch", batchId);
      window.history.replaceState(null, "", url.toString());
    }
  } catch {
    // ignore history restrictions in sandboxed environments
  }
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

export default function App({
  initialBatch = null,
  initialBatches,
  initialPendingBatchRecovery = null,
}: AppProps = {}) {
  const api = useMemo(() => new BillingApiClient(), []);
  const stubMode = useMemo(() => isStubModeActive(), []);

  const [theme, setTheme] = useState<LumnoTheme>("light");
  const [mobileTab, setMobileTab] = useState<MobileViewTab>(() =>
    initialBatch || initialPendingBatchRecovery ? "queue" : "upload"
  );

  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [entities, setEntities] = useState<AccountingEntity[]>([]);
  const [selectedEntity, setSelectedEntity] = useState<string>(
    () =>
      initialBatch?.accounting_entity ??
      initialPendingBatchRecovery?.accountingEntity ??
      "NEPTUNE SHIPPING LIMITED"
  );
  const [existingBillCount, setExistingBillCount] = useState<number>(0);

  const [batches, setBatches] = useState<BatchSummary[]>(() => {
    if (initialBatches) return initialBatches;
    if (initialBatch) return [toBatchSummary(initialBatch)];
    return [];
  });
  const [activeBatchId, setActiveBatchId] = useState<string | null>(
    () => initialBatch?.id ?? initialPendingBatchRecovery?.id ?? null
  );
  const [activeBatch, setActiveBatch] = useState<BatchDetail | null>(
    () => initialBatch ?? null
  );
  const [pendingBatchDetail, setPendingBatchDetail] =
    useState<PendingBatchDetailRecovery | null>(
      () => initialPendingBatchRecovery ?? null
    );
  const [switchingBatchId, setSwitchingBatchId] = useState<string | null>(null);
  const switchBatchSeqRef = useRef(0);
  const activeBatchStageRef = useRef<BatchSummary["stage"] | null>(
    initialBatch?.stage ?? null
  );
  activeBatchStageRef.current = activeBatch?.stage ?? null;

  const [filterTab, setFilterTab] = useState<RecordFilterTab>("all");
  const [activeDocFilterId, setActiveDocFilterId] = useState<string | null>(
    null
  );
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(
    () => initialBatch?.records?.[0]?.id ?? null
  );
  const [inspectedDocId, setInspectedDocId] = useState<string | null>(null);

  // T02: Isolated Draft Store keyed by (batch_id, record_id) and (batch_id, doc_id)
  const [draftStore, setDraftStore] = useState<DraftStoreState>(
    createEmptyDraftStore
  );
  const draftStoreRef = useRef<DraftStoreState>(draftStore);
  const updateDraftStore = useCallback(
    (updater: (prev: DraftStoreState) => DraftStoreState) => {
      const next = updater(draftStoreRef.current);
      draftStoreRef.current = next;
      setDraftStore(next);
    },
    []
  );
  const [mutationError, setMutationError] =
    useState<MutationErrorBanner | null>(null);
  const [auditHistoryByKey, setAuditHistoryByKey] = useState<
    Record<string, RecordAuditHistoryState>
  >({});

  // T02: Monotonic revision, poll-race guard & polling transient resilience
  const pollTrackerRef = useRef(
    createPollRaceTracker(
      initialBatch?.id ?? initialPendingBatchRecovery?.id ?? null
    )
  );
  const [batchRevision, setBatchRevision] = useState<number>(0);
  const [pollRetryAttempt, setPollRetryAttempt] = useState<number>(0);
  const [pollCycle, setPollCycle] = useState<number>(0);
  const [pollTransientError, setPollTransientError] = useState<string | null>(
    null
  );

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
  const [bundleJsonText, setBundleJsonText] = useState("");
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [importingBundle, setImportingBundle] = useState(false);

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
      syncPersistedBatchId(activeBatchId);
    }
  }, [activeBatchId]);

  const upsertBatchSummary = useCallback((summary: BatchSummary) => {
    setBatches((prev) => {
      const idx = prev.findIndex((item) => item.id === summary.id);
      if (idx === -1) {
        return [summary, ...prev];
      }
      const next = [...prev];
      next[idx] = summary;
      return next;
    });
  }, []);

  const applyFreshBatchDetail = useCallback(
    (detail: BatchDetail) => {
      activeBatchStageRef.current = detail.stage;
      pollTrackerRef.current = {
        ...pollTrackerRef.current,
        activeBatchId: detail.id,
      };
      setPendingBatchDetail(null);
      setActiveBatch(detail);
      setActiveBatchId(detail.id);
      setSelectedEntity(detail.accounting_entity);
      setPollRetryAttempt(0);
      setPollTransientError(null);
      upsertBatchSummary(toBatchSummary(detail));
      const safeRecords = detail.records ?? [];
      const safeDocs = detail.documents ?? [];
      setSelectedRecordId((prevSelected) => {
        if (prevSelected && safeRecords.some((r) => r.id === prevSelected)) {
          return prevSelected;
        }
        return safeRecords[0]?.id ?? null;
      });
      setInspectedDocId((prevDoc) => {
        if (prevDoc && safeDocs.some((d) => d.id === prevDoc)) {
          return prevDoc;
        }
        return null;
      });
    },
    [upsertBatchSummary]
  );

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
    if (initialBatch || initialPendingBatchRecovery) {
      return;
    }
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

        // A11: Restore preferred batch directly by id even if outside recent-20 list
        const preferredId = getInitialBatchIdFromUrlOrStorage();
        let loadedDetail: BatchDetail | null = null;

        if (preferredId) {
          try {
            pollTrackerRef.current = setTrackerActiveBatch(
              pollTrackerRef.current,
              preferredId
            );
            const started = startPoll(pollTrackerRef.current, preferredId);
            pollTrackerRef.current = started.nextTracker;
            const detail = await api.getBatchDetail(
              preferredId,
              controller.signal
            );
            if (controller.signal.aborted) return;
            const check = acceptPollResult(
              pollTrackerRef.current,
              started.ticket
            );
            pollTrackerRef.current = check.nextTracker;
            if (check.accepted) {
              loadedDetail = detail;
            }
          } catch (err) {
            if (
              controller.signal.aborted ||
              (err instanceof ApiClientError && err.code === "REQUEST_ABORTED")
            ) {
              return;
            }
            if (err instanceof ApiClientError && err.status === 404) {
              try {
                window.localStorage.removeItem(LAST_BATCH_STORAGE_KEY);
              } catch {
                // ignore storage failures
              }
            } else {
              pushToast(
                err instanceof Error
                  ? `恢复批次 ${preferredId} 失败：${err.message}`
                  : `恢复批次 ${preferredId} 失败`,
                "error"
              );
            }
          }
        }

        if (!loadedDetail && batchList[0]?.id) {
          const fallbackId = batchList[0].id;
          pollTrackerRef.current = setTrackerActiveBatch(
            pollTrackerRef.current,
            fallbackId
          );
          const started = startPoll(pollTrackerRef.current, fallbackId);
          pollTrackerRef.current = started.nextTracker;
          const detail = await api.getBatchDetail(fallbackId, controller.signal);
          if (controller.signal.aborted) return;
          const check = acceptPollResult(pollTrackerRef.current, started.ticket);
          pollTrackerRef.current = check.nextTracker;
          if (check.accepted) {
            loadedDetail = detail;
          }
        }

        if (loadedDetail) {
          setBatchRevision(pollTrackerRef.current.batchRevision);
          applyFreshBatchDetail(loadedDetail);
          setMobileTab("queue");
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

  // Real-time polling while batch stage is active (guarded by PollRaceTracker + backoff retry)
  useEffect(() => {
    if (!activeBatch || !isActiveBatchStage(activeBatch.stage)) {
      return;
    }
    const targetBatchId = activeBatch.id;
    const controller = new AbortController();
    const delayMs =
      pollRetryAttempt === 0
        ? POLL_INTERVAL_MS
        : Math.min(4000, 1000 * 2 ** Math.min(pollRetryAttempt - 1, 2));

    const timer = window.setTimeout(async () => {
      const started = startPoll(pollTrackerRef.current, targetBatchId);
      pollTrackerRef.current = started.nextTracker;
      try {
        const detail = await api.getBatchDetail(targetBatchId, controller.signal);
        if (controller.signal.aborted) return;
        const check = acceptPollResult(pollTrackerRef.current, started.ticket);
        pollTrackerRef.current = check.nextTracker;
        if (!check.accepted) {
          // F4: Even if stale poll response is discarded after concurrent 409/mutation,
          // keep scheduling the next poll while this batch remains active.
          if (
            shouldContinuePollingAfterDiscard(
              pollTrackerRef.current,
              targetBatchId,
              activeBatchStageRef.current
            )
          ) {
            setPollCycle((c) => c + 1);
          }
          return;
        }
        applyFreshBatchDetail(detail);
        if (isActiveBatchStage(detail.stage)) {
          setPollCycle((c) => c + 1);
        }
      } catch (err) {
        if (
          controller.signal.aborted ||
          (err instanceof ApiClientError && err.code === "REQUEST_ABORTED")
        ) {
          return;
        }
        const msg = err instanceof Error ? err.message : "轮询批次状态失败";
        setPollRetryAttempt((n) => n + 1);
        setPollCycle((c) => c + 1);
        setPollTransientError(
          `状态轮询临时中断（${msg}），正在自动退避重试…`
        );
      }
    }, delayMs);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [
    api,
    activeBatch?.id,
    activeBatch?.stage,
    pollCycle,
    pollRetryAttempt,
    applyFreshBatchDetail,
  ]);

  const dirtyRecordIds = useMemo(() => {
    if (!activeBatch) return new Set<string>();
    return getDirtyRecordIds(
      draftStore,
      activeBatch.id,
      activeBatch.records ?? []
    );
  }, [activeBatch, draftStore]);

  const dirtyDocIds = useMemo(() => {
    if (!activeBatch) return new Set<string>();
    return getDirtyDocIds(
      draftStore,
      activeBatch.id,
      activeBatch.documents ?? []
    );
  }, [activeBatch, draftStore]);

  const unsavedDraftCount = useMemo(
    () => countUnsavedDraftsInBatch(draftStore, activeBatch),
    [draftStore, activeBatch]
  );

  const executeSelectBatch = async (batchId: string) => {
    if (!batchId) return;
    const switchSeq = ++switchBatchSeqRef.current;
    setSwitchingBatchId(batchId);
    try {
      const detail = await api.getBatchDetail(batchId);
      if (switchBatchSeqRef.current !== switchSeq) {
        return;
      }
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
      applyFreshBatchDetail(detail);
      setMobileTab("queue");
    } catch (err) {
      if (switchBatchSeqRef.current === switchSeq) {
        pushToast(
          err instanceof Error
            ? `切换批次失败，已保留原批次：${err.message}`
            : "加载批次详情失败，已保留原批次",
          "error"
        );
      }
    } finally {
      if (switchBatchSeqRef.current === switchSeq) {
        setSwitchingBatchId(null);
      }
    }
  };

  const handleSelectBatch = (batchId: string) => {
    if (!batchId) return;
    if (batchId === activeBatchId) {
      if (pendingBatchDetail?.id === batchId && !pendingBatchDetail.loading) {
        void handleRetryPendingBatchDetail();
      }
      return;
    }
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

  const activateNewlyCreatedBatch = async (
    created: BatchSummary | BatchDetail,
    isDemo: boolean
  ): Promise<boolean> => {
    const summary = toBatchSummary(created);
    ++switchBatchSeqRef.current;
    pollTrackerRef.current = setTrackerActiveBatch(
      pollTrackerRef.current,
      summary.id
    );
    setBatchRevision(pollTrackerRef.current.batchRevision);
    setActiveBatchId(summary.id);
    setSelectedEntity(summary.accounting_entity);
    syncPersistedBatchId(summary.id);
    upsertBatchSummary(summary);

    activeBatchStageRef.current = null;
    setActiveBatch(null);
    setSelectedRecordId(null);
    setActiveDocFilterId(null);
    setInspectedDocId(null);
    setFilterTab("all");
    setEntityNotice(null);
    setMutationError(null);
    setPollRetryAttempt(0);
    setPollTransientError(null);
    setPendingBatchDetail({
      id: summary.id,
      shortCode: summary.short_code,
      accountingEntity: summary.accounting_entity,
      isDemo,
      loading: true,
      errorMessage: null,
    });
    setMobileTab("queue");

    try {
      const detail = await api.getBatchDetail(summary.id);
      if (pollTrackerRef.current.activeBatchId !== summary.id) {
        return true;
      }
      setPendingBatchDetail(null);
      applyFreshBatchDetail(detail);
      void refreshBatchesList().catch(() => undefined);
      pushToast(
        isDemo
          ? `已载入 4 份真样本批次 ${summary.short_code}`
          : `批次 ${summary.short_code} 已入队启动解析`
      );
      return true;
    } catch (err) {
      const detailMsg =
        err instanceof Error ? err.message : "读取新建批次详情失败";
      if (pollTrackerRef.current.activeBatchId === summary.id) {
        setPendingBatchDetail({
          id: summary.id,
          shortCode: summary.short_code,
          accountingEntity: summary.accounting_entity,
          isDemo,
          loading: false,
          errorMessage: detailMsg,
        });
      }
      void refreshBatchesList().catch(() => undefined);
      pushToast(
        `批次 ${summary.short_code} 已创建入队，但首次读取详情失败，请点击“重试读取此批次”`,
        "error"
      );
      return true;
    }
  };

  const handleRetryPendingBatchDetail = async () => {
    const target = pendingBatchDetail;
    if (!target) return;
    setPendingBatchDetail((prev) =>
      prev && prev.id === target.id
        ? { ...prev, loading: true, errorMessage: null }
        : prev
    );
    const started = startPoll(pollTrackerRef.current, target.id);
    pollTrackerRef.current = started.nextTracker;
    try {
      const detail = await api.getBatchDetail(target.id);
      const check = acceptPollResult(pollTrackerRef.current, started.ticket);
      pollTrackerRef.current = check.nextTracker;
      if (!check.accepted || pollTrackerRef.current.activeBatchId !== target.id) {
        return;
      }
      setPendingBatchDetail(null);
      applyFreshBatchDetail(detail);
      void refreshBatchesList().catch(() => undefined);
      pushToast(`已加载批次 ${detail.short_code} 详情`);
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "重试读取批次详情失败";
      setPendingBatchDetail((prev) =>
        prev && prev.id === target.id
          ? { ...prev, loading: false, errorMessage: msg }
          : prev
      );
      pushToast(msg, "error");
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
      return await activateNewlyCreatedBatch(res, false);
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
      await activateNewlyCreatedBatch(res, true);
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
    const targetBatchId = activeBatch.id;
    setSwitchingEntity(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      targetBatchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const res = await api.switchBatchAccountingEntity(
        targetBatchId,
        nextEntity
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      // F2: Only clear drafts and audit cache belonging to targetBatchId
      updateDraftStore((prev) => clearBatchDrafts(prev, targetBatchId));
      setAuditHistoryByKey((prev) => clearBatchScopedMap(prev, targetBatchId));
      if (finished.shouldApply) {
        applyFreshBatchDetail(res.batch);
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
      const banner = extractMutationErrorBanner("强制 OCR 提取失败", err);
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
    const targetBatchId = activeBatch.id;
    setRetryingDocId(docId);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      targetBatchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.retryOrReplaceDocument(targetBatchId, docId, {
        ocr_mode: options.ocrMode ?? activeBatch.ocr_mode,
        public_url: options.publicUrl,
        file: options.file,
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      setAuditHistoryByKey((prev) => clearBatchScopedMap(prev, targetBatchId));
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
      }
      pushToast(
        options.file ? "已上传替换文件并重新计算" : "已触发单文件重试"
      );
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = extractMutationErrorBanner("重试或替换文件失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setRetryingDocId(null);
    }
  };

  const handleSaveDocTotals = async (doc: SourceDocumentSummary) => {
    if (!activeBatch) return;
    const batchId = activeBatch.id;
    const currentStore = draftStoreRef.current;
    const diff = buildDocTotalsDiffPatch(doc, currentStore, batchId);
    if (!diff.valid || !diff.hasChanges) return;

    const submittedVersion = getDocTotalsDraftVersion(
      currentStore,
      batchId,
      doc.id
    );
    const submittedFields =
      currentStore.docDrafts[makeDocDraftKey(batchId, doc.id)]?.fields;
    updateDraftStore((prev) =>
      beginDocTotalsSave(
        prev,
        batchId,
        doc.id,
        submittedVersion,
        submittedFields
      )
    );
    setSavingDocTotals(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      batchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.patchDocumentTotals(batchId, doc.id, {
        subtotal_amount: diff.patch.subtotal_amount,
        tax_amount: diff.patch.tax_amount,
        total_amount: diff.patch.total_amount,
        total_currency: diff.patch.total_currency,
        review_note: diff.patch.review_note,
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      const updatedDoc = (updated.documents ?? []).find((d) => d.id === doc.id);
      updateDraftStore((prev) =>
        finishDocTotalsSaveSuccess(
          prev,
          batchId,
          doc.id,
          updatedDoc,
          submittedVersion ?? undefined
        )
      );
      setAuditHistoryByKey((prev) => clearBatchScopedMap(prev, batchId));
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
      }
      pushToast(`已保存 ${doc.sanitized_filename} 汇总并重算同单所有行`);
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      updateDraftStore((prev) =>
        finishDocTotalsSaveError(prev, batchId, doc.id, doc)
      );
      const banner = extractMutationErrorBanner("保存单据汇总失败", err);
      setMutationError(banner);
      pushToast(banner.message, "error");
    } finally {
      setSavingDocTotals(false);
    }
  };

  const handleSaveRecordPatch = async (record: BillingRecordDetail) => {
    if (!activeBatch) return;
    const batchId = activeBatch.id;
    const currentStore = draftStoreRef.current;
    const diff = buildRecordDiffPatch(record, currentStore, batchId);
    if (!diff.valid || !diff.hasChanges) return;

    const submittedVersion = getRecordDraftVersion(
      currentStore,
      batchId,
      record.id
    );
    const submittedFields =
      currentStore.recordDrafts[makeRecordDraftKey(batchId, record.id)]?.fields;
    updateDraftStore((prev) =>
      beginRecordSave(
        prev,
        batchId,
        record.id,
        submittedVersion,
        submittedFields
      )
    );
    setSavingRecord(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      batchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.patchBillingRecord(
        batchId,
        record.id,
        diff.patch
      );
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      const updatedRecord = (updated.records ?? []).find(
        (r) => r.id === record.id
      );
      updateDraftStore((prev) =>
        finishRecordSaveSuccess(
          prev,
          batchId,
          record.id,
          updatedRecord,
          submittedVersion ?? undefined
        )
      );
      setAuditHistoryByKey((prev) => {
        const next = { ...prev };
        delete next[`${batchId}::${record.id}`];
        return next;
      });
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
      }
      pushToast("已保存修订并完成门禁重验");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      updateDraftStore((prev) =>
        finishRecordSaveError(prev, batchId, record.id, record)
      );
      const banner = extractMutationErrorBanner(
        "保存修订未通过",
        err,
        record.validation_alerts
      );
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
    const batchId = activeBatch.id;
    if (hasRecordDraft(draftStoreRef.current, batchId, record.id)) {
      const msg = "请先保存当前未保存修改，或放弃草稿后再执行批准/驳回";
      setMutationError({ title: "存在未保存修改", message: msg });
      pushToast(msg, "error");
      return;
    }

    setApprovingOrRejecting(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      batchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.approveBillingRecord(batchId, record.id, reason);
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
        setAuditHistoryByKey((prev) => {
          const next = { ...prev };
          delete next[`${batchId}::${record.id}`];
          return next;
        });
      }
      pushToast("已批准该条费用记录入账");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = extractMutationErrorBanner(
        "批准入账被拦截",
        err,
        record.validation_alerts
      );
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
    const batchId = activeBatch.id;
    if (hasRecordDraft(draftStoreRef.current, batchId, record.id)) {
      const msg = "请先保存当前未保存修改，或放弃草稿后再执行批准/驳回";
      setMutationError({ title: "存在未保存修改", message: msg });
      pushToast(msg, "error");
      return;
    }

    setApprovingOrRejecting(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      batchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.rejectBillingRecord(batchId, record.id, reason);
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        setMutationError(null);
        setAuditHistoryByKey((prev) => {
          const next = { ...prev };
          delete next[`${batchId}::${record.id}`];
          return next;
        });
      }
      pushToast("已将该费用记录驳回并从 official 口径排除");
    } catch (err) {
      pollTrackerRef.current = finishMutationError(pollTrackerRef.current);
      const banner = extractMutationErrorBanner(
        "驳回操作失败",
        err,
        record.validation_alerts
      );
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
          retryCount: prev[key]?.retryCount ?? null,
          extractionChannel: prev[key]?.extractionChannel ?? null,
          history: prev[key]?.history ?? [],
        },
      }));
      try {
        const res = await api.exportBatchJson(batchId, "audit");
        const matched = (res.records ?? []).find(
          (item) => item.audit_trail.record_id === recordId
        );
        const trail: Record<string, unknown> = matched?.audit_trail ?? {};
        const extractionChannel =
          typeof trail.extraction_channel === "string"
            ? trail.extraction_channel
            : null;
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
            retryCount,
            extractionChannel,
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
            retryCount: null,
            extractionChannel: null,
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

  const handleOpenManagementDrawer = (tab: ManagementDrawerTab) => {
    setDrawerTab(tab);
    setDrawerOpen(true);
  };

  const handleReapplyDictionary = async () => {
    if (!activeBatch) return;
    const targetBatchId = activeBatch.id;
    setReapplyingDictionary(true);
    const { nextTracker, ticket } = startMutation(
      pollTrackerRef.current,
      targetBatchId
    );
    pollTrackerRef.current = nextTracker;
    try {
      const updated = await api.reapplyBatchChargeDictionary(targetBatchId);
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      setAuditHistoryByKey((prev) => clearBatchScopedMap(prev, targetBatchId));
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
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
    setBundleError(null);
    if (unsavedDraftCount > 0) {
      setPendingDraftGuard({ kind: "open_import_bundle" });
      return;
    }
    setImportModalOpen(true);
  };

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
      setAuditHistoryByKey((prev) => clearBatchScopedMap(prev, imported.id));
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

  const handleOpenManualModal = (defaultDocId?: string) => {
    if (!activeBatch) return;
    const safeDocs = activeBatch.documents ?? [];
    const fallbackDocId =
      defaultDocId ??
      inspectedDocId ??
      selectedRecord?.source_doc_id ??
      safeDocs.find((d) => d.status !== "FAILED")?.id ??
      safeDocs[0]?.id ??
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
    if (!isValidIso4217Currency(currUpper)) {
      setManualFormError("币种须为有效的 3 位大写 ISO 4217 代码（如 USD）");
      return;
    }
    if (
      manualDocumentDate.trim() &&
      !/^\d{4}-\d{2}-\d{2}$/.test(manualDocumentDate.trim())
    ) {
      setManualFormError("单据日期格式须为 YYYY-MM-DD");
      return;
    }
    const manualReasonErr = validateReviewReason(manualReason);
    if (manualReasonErr) {
      setManualFormError(`补录原因：${manualReasonErr}`);
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
        reason: manualReason.trim(),
      });
      const finished = finishMutationSuccess(pollTrackerRef.current, ticket);
      pollTrackerRef.current = finished.nextTracker;
      setBatchRevision(finished.nextTracker.batchRevision);
      if (finished.shouldApply) {
        applyFreshBatchDetail(updated);
        const safeRecords = updated.records ?? [];
        const newest = safeRecords[safeRecords.length - 1];
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
      (activeBatch.records ?? []).find((r) => r.id === selectedRecordId) ?? null
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
          switchingBatchId={switchingBatchId}
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
          onOpenMasterData={() => handleOpenManagementDrawer("entities")}
          onOpenEvalDashboard={() => handleOpenManagementDrawer("eval_runs")}
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
              pendingBatchRecovery={pendingBatchDetail}
              onRetryPendingBatch={() => void handleRetryPendingBatchDetail()}
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
              pollTransientError={pollTransientError}
              pendingBatchRecovery={pendingBatchDetail}
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
              onRefreshBatch={() => void handleRefreshActiveBatch()}
              onRetryPendingBatch={() => void handleRetryPendingBatchDetail()}
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
                updateDraftStore((prev) =>
                  setRecordDraftField(
                    prev,
                    activeBatch.id,
                    record,
                    field,
                    value
                  )
                );
              }}
              onResetRecordDraft={(recordId) => {
                if (!activeBatch) return;
                updateDraftStore((prev) =>
                  clearRecordDraft(prev, activeBatch.id, recordId)
                );
                setMutationError(null);
              }}
              onSaveRecordPatch={handleSaveRecordPatch}
              onApproveRecord={handleApproveRecord}
              onRejectRecord={handleRejectRecord}
              onUpdateDocField={(doc, field, value) => {
                if (!activeBatch) return;
                updateDraftStore((prev) =>
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
                updateDraftStore((prev) =>
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
                {(activeBatch.documents ?? []).map((doc) => (
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

      {/* CLI ReviewBundleV1 ("1.0") Import Modal */}
      <LumnoModal
        open={importModalOpen}
        title="导入 CLI 复核包（ReviewBundleV1 · bundle_version: 1.0）"
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
        <div className="wb-notice-banner">
          <span>
            支持导入 CLI 端导出的标准 ReviewBundleV1（含 bundle_version: "1.0"、无损高精度数值、随批私有基准快照与审计记录）。标准口径结果请使用「导出标准 JSON」。
          </span>
        </div>
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
            须包含 bundle_version: "1.0"、非空 accounting_entity、合法 ocr_mode 与有效 documents / records
          </span>
        </div>
        <LumnoTextarea
          value={bundleJsonText}
          placeholder='{"bundle_version":"1.0","accounting_entity":"NEPTUNE SHIPPING LIMITED","ocr_mode":"REMOTE_ONLY","documents":[...],"records":[...]}'
          aria-label="ReviewBundleV1 JSON 内容"
          style={{ minHeight: "160px", fontFamily: "var(--lumno-font-mono)" }}
          onChange={(e) => {
            setBundleJsonText(e.target.value);
            setBundleError(null);
          }}
        />
        {bundleError ? <div className="wb-field-error">{bundleError}</div> : null}
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
