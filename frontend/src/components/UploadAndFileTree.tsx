import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import {
  getStagedFileKey,
  validateBatchIngestionInput,
  type AccountingEntity,
  type BatchDetail,
  type OcrMode,
  type SourceDocumentSummary,
} from "../api/client.ts";
import { formatLosslessAmount } from "../api/decimalJson.ts";
import {
  formatOcrChannelLabel,
  type PendingBatchDetailRecovery,
} from "../api/reviewState.ts";
import {
  LumnoButton,
  LumnoInfoButton,
  LumnoInput,
  LumnoSegmentedControl,
  LumnoTextarea,
} from "./LumnoPrimitives.tsx";

interface WebkitFileEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  file?: (cb: (file: File) => void, errCb?: (err: unknown) => void) => void;
  createReader?: () => {
    readEntries: (
      cb: (entries: WebkitFileEntryLike[]) => void,
      errCb?: (err: unknown) => void
    ) => void;
  };
}

interface DataTransferItemWithEntry {
  webkitGetAsEntry?: () => WebkitFileEntryLike | null;
}

async function collectFilesFromEntry(
  entry: WebkitFileEntryLike
): Promise<File[]> {
  if (entry.isFile && entry.file) {
    return new Promise<File[]>((resolve) => {
      entry.file!(
        (file) => resolve([file]),
        () => resolve([])
      );
    });
  }
  if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader();
    const collected: File[] = [];
    const readBatch = (): Promise<WebkitFileEntryLike[]> =>
      new Promise((resolve) => {
        reader.readEntries(
          (entries) => resolve(entries),
          () => resolve([])
        );
      });

    while (true) {
      const entries = await readBatch();
      if (entries.length === 0) break;
      for (const child of entries) {
        const subFiles = await collectFilesFromEntry(child);
        collected.push(...subFiles);
      }
    }
    return collected;
  }
  return [];
}

function formatFileSize(bytes: number): string {
  if (bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

function getDocIcon(sourceKind: string): string {
  const kind = sourceKind.toUpperCase();
  if (kind.includes("EML") || kind.includes("EMAIL")) return "ri-mail-line";
  if (kind.includes("PDF")) return "ri-file-pdf-2-line";
  if (kind.includes("EXCEL") || kind.includes("XLS")) {
    return "ri-file-excel-2-line";
  }
  if (kind.includes("WORD") || kind.includes("DOC")) {
    return "ri-file-word-2-line";
  }
  if (kind.includes("IMAGE") || kind.includes("IMG")) return "ri-image-line";
  return "ri-file-text-line";
}

function getDocStatusMeta(status: SourceDocumentSummary["status"]): {
  label: string;
  tone: "ok" | "info" | "warn" | "error" | "muted";
} {
  switch (status) {
    case "PARSED":
      return { label: "已解析", tone: "ok" };
    case "METADATA_ONLY":
      return { label: "辅助凭据", tone: "info" };
    case "SKIPPED_NOISE":
      return { label: "已忽略", tone: "muted" };
    case "FAILED":
      return { label: "解析失败", tone: "error" };
  }
}

function getDocSemanticTitle(
  doc: SourceDocumentSummary,
  isChild: boolean
): string {
  const count = doc.affected_record_ids?.length ?? 0;
  const kind = doc.source_kind.toUpperCase();
  const isEmail = kind.includes("EML") || kind.includes("EMAIL");

  if (doc.status === "FAILED") {
    return isChild ? "附件解析失败" : "单据解析失败";
  }
  if (doc.status === "SKIPPED_NOISE") {
    return "非计费图片（已自动忽略）";
  }
  if (isChild) {
    if (isEmail) {
      return count > 0 ? `邮件正文 · ${count} 条费用` : "邮件正文 · 无费用";
    }
    return count > 0 ? `邮件附件 · ${count} 条费用` : "邮件附件 · 无费用";
  }
  if (isEmail) {
    return count > 0 ? `邮件主件 · ${count} 条费用` : "邮件容器 · 见下方正文与附件";
  }
  return count > 0 ? `单据 · ${count} 条费用` : "单据 · 仅辅助凭据";
}

export interface UploadAndFileTreeProps {
  defaultEntity: string;
  entities: ReadonlyArray<AccountingEntity>;
  activeBatch: BatchDetail | null;
  activeDocFilterId: string | null;
  dirtyDocIds?: ReadonlySet<string>;
  previewOpen?: boolean;
  previewDocId?: string | null;
  submittingBatch: boolean;
  submittingDemo: boolean;
  retryingDocId: string | null;
  forcingDocId: string | null;
  pendingBatchRecovery?: PendingBatchDetailRecovery | null;
  onRetryPendingBatch?: () => void;
  onSubmitBatch: (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
    files: File[];
    publicUrls: string[];
    existingBillsJsonText: string;
    onAccepted?: () => void;
  }) => Promise<boolean>;
  onSubmitDemo: (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
    onAccepted?: () => void;
  }) => Promise<boolean>;
  onSelectDocFilter: (docId: string | null) => void;
  onInspectDoc: (docId: string) => void;
  onPreviewDoc?: (docId: string) => void;
  onForceParseDoc: (docId: string) => void;
  onRetryOrReplaceDoc: (docId: string, file?: File) => void;
}

export function UploadAndFileTree({
  defaultEntity,
  entities,
  activeBatch,
  activeDocFilterId,
  dirtyDocIds,
  previewOpen = false,
  previewDocId = null,
  submittingBatch,
  submittingDemo,
  retryingDocId,
  forcingDocId,
  pendingBatchRecovery = null,
  onRetryPendingBatch,
  onSubmitBatch,
  onSubmitDemo,
  onSelectDocFilter,
  onInspectDoc,
  onPreviewDoc,
  onForceParseDoc,
  onRetryOrReplaceDoc,
}: UploadAndFileTreeProps) {
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [entityOverrideOpen, setEntityOverrideOpen] = useState(false);
  const [customEntityInput, setCustomEntityInput] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [demoOpen, setDemoOpen] = useState(false);
  const [publicUrlLinesText, setPublicUrlLinesText] = useState("");
  const [ocrMode, setOcrMode] = useState<OcrMode>("REMOTE_WITH_LOCAL_FALLBACK");
  const [batchBills, setBatchBills] = useState<{
    name: string | null;
    text: string;
  }>({ name: null, text: "" });
  const [hasAttemptedSubmit, setHasAttemptedSubmit] = useState(false);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dirInputRef = useRef<HTMLInputElement | null>(null);
  const billsFileInputRef = useRef<HTMLInputElement | null>(null);
  const replaceDocInputRef = useRef<HTMLInputElement | null>(null);
  const replaceTargetDocIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (dirInputRef.current) {
      dirInputRef.current.setAttribute("webkitdirectory", "");
      dirInputRef.current.setAttribute("directory", "");
    }
  }, []);

  const trimmedOverride = customEntityInput.trim();
  const isEntityOverridden =
    trimmedOverride.length > 0 && trimmedOverride !== defaultEntity;
  const effectiveEntity = (trimmedOverride || defaultEntity).trim();

  const validation = useMemo(
    () =>
      validateBatchIngestionInput({
        accountingEntity: effectiveEntity,
        files: stagedFiles,
        publicUrlLinesText,
        existingBillsJsonText: batchBills.text,
      }),
    [effectiveEntity, stagedFiles, publicUrlLinesText, batchBills.text]
  );

  const totalStagedCount = stagedFiles.length + validation.parsedUrls.length;
  const hasActiveAdvancedOptions =
    validation.parsedUrls.length > 0 ||
    publicUrlLinesText.trim().length > 0 ||
    Boolean(batchBills.name) ||
    ocrMode !== "REMOTE_WITH_LOCAL_FALLBACK";

  const appendFiles = (incoming: File[]) => {
    if (incoming.length === 0) return;
    setStagedFiles((prev) => [...prev, ...incoming]);
  };

  const handleFileInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files ? Array.from(e.target.files) : [];
    appendFiles(picked);
    e.target.value = "";
  };

  const handleDrop = async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    const items = e.dataTransfer?.items;
    const entries: WebkitFileEntryLike[] = [];
    if (items && items.length > 0) {
      for (let i = 0; i < items.length; i++) {
        const item = items[i] as unknown as DataTransferItemWithEntry;
        const entry = item.webkitGetAsEntry?.();
        if (entry) {
          entries.push(entry);
        }
      }
    }
    const fallbackFiles = e.dataTransfer?.files
      ? Array.from(e.dataTransfer.files)
      : [];

    if (entries.length > 0) {
      const nestedGroups = await Promise.all(entries.map(collectFilesFromEntry));
      appendFiles(nestedGroups.flat());
      return;
    }
    appendFiles(fallbackFiles);
  };

  const handleBillsFilePick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await file.text();
    setBatchBills({ name: file.name, text });
  };

  const resetEntityOverride = () => {
    setCustomEntityInput("");
    setEntityOverrideOpen(false);
  };

  const handleSubmit = async () => {
    if (submittingBatch || submittingDemo) {
      return;
    }
    setHasAttemptedSubmit(true);
    if (!validation.valid) {
      return;
    }
    const submittedFiles = [...stagedFiles];
    const submittedSet = new Set(submittedFiles);
    const submittedUrlText = publicUrlLinesText;
    const submittedBills = batchBills;
    const parsedUrls = [...validation.parsedUrls];
    let accepted = false;

    const handleAccepted = () => {
      if (accepted) return;
      accepted = true;
      setStagedFiles((prev) => prev.filter((file) => !submittedSet.has(file)));
      setPublicUrlLinesText((prev) => (prev === submittedUrlText ? "" : prev));
      setBatchBills((prev) =>
        prev.text === submittedBills.text && prev.name === submittedBills.name
          ? { name: null, text: "" }
          : prev
      );
      setCustomEntityInput("");
      setEntityOverrideOpen(false);
      setHasAttemptedSubmit(false);
    };

    const ok = await onSubmitBatch({
      accountingEntity: effectiveEntity,
      ocrMode,
      files: submittedFiles,
      publicUrls: parsedUrls,
      existingBillsJsonText: submittedBills.text,
      onAccepted: handleAccepted,
    });
    if (ok && !accepted) {
      handleAccepted();
    }
  };

  const handleReplaceDocInput = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    const docId = replaceTargetDocIdRef.current;
    replaceTargetDocIdRef.current = null;
    if (docId && file) {
      onRetryOrReplaceDoc(docId, file);
    }
  };

  const orderedDocs = useMemo(() => {
    if (!activeBatch) return [];
    const docs = activeBatch.documents ?? [];
    const roots = docs.filter((d) => !d.parent_doc_id);
    const childrenByParent = new Map<string, SourceDocumentSummary[]>();
    for (const d of docs) {
      if (d.parent_doc_id) {
        const list = childrenByParent.get(d.parent_doc_id) ?? [];
        list.push(d);
        childrenByParent.set(d.parent_doc_id, list);
      }
    }
    const result: Array<{ doc: SourceDocumentSummary; isChild: boolean }> = [];
    const seen = new Set<string>();
    for (const root of roots) {
      seen.add(root.id);
      result.push({ doc: root, isChild: false });
      for (const child of childrenByParent.get(root.id) ?? []) {
        seen.add(child.id);
        result.push({ doc: child, isChild: true });
      }
    }
    for (const d of docs) {
      if (!seen.has(d.id)) {
        result.push({ doc: d, isChild: Boolean(d.parent_doc_id) });
      }
    }
    return result;
  }, [activeBatch]);

  const primarySubmitLabel =
    totalStagedCount > 0
      ? `解析 ${totalStagedCount} 份单据`
      : "选择单据后开始解析";

  return (
    <>
      <div className="wb-pane-header">
        <h2 className="wb-pane-title">
          <i className="ri-icon ri-inbox-archive-line" aria-hidden="true" />
          <span>单据上传与文件列表</span>
        </h2>
        {activeBatch ? (
          <span className="wb-badge wb-badge--muted wb-tabular">
            {activeBatch.succeeded_files}/{activeBatch.total_files} 份
          </span>
        ) : null}
      </div>

      <div className="wb-pane-body">
        <section className="wb-section-box" aria-label="单据投递配置">
          {/* P0-3: Distinct Next-Batch Entity Indicator & Clear Override */}
          <div className="wb-next-entity-bar">
            <div style={{ minWidth: 0 }}>
              <div className="wb-kv-label">下一批新建将使用公司</div>
              <div
                style={{
                  fontSize: "12px",
                  fontWeight: 600,
                  overflowWrap: "anywhere",
                }}
              >
                {effectiveEntity}
              </div>
            </div>
            <div style={{ display: "inline-flex", gap: "4px", flexShrink: 0 }}>
              {isEntityOverridden ? (
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-arrow-go-back-line"
                  onClick={resetEntityOverride}
                >
                  恢复默认
                </LumnoButton>
              ) : null}
              <LumnoButton
                variant="ghost"
                compact
                icon="ri-edit-line"
                aria-expanded={entityOverrideOpen}
                onClick={() => setEntityOverrideOpen((v) => !v)}
              >
                {entityOverrideOpen ? "收起" : "临时更换"}
              </LumnoButton>
            </div>
          </div>

          {isEntityOverridden ? (
            <div className="wb-alert-item" data-severity="WARNING" role="status">
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "6px",
                  flexWrap: "wrap",
                }}
              >
                <span>
                  已临时覆盖：下一批将按 <strong>{effectiveEntity}</strong>{" "}
                  记账（不影响上方当前正在复核的批次），提交后自动恢复默认。
                </span>
              </div>
            </div>
          ) : null}

          {entityOverrideOpen ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
              <label
                htmlFor="wb-upload-entity-override"
                className="wb-form-label"
              >
                临时指定下一批记账公司（默认：{defaultEntity}）
              </label>
              <LumnoInput
                id="wb-upload-entity-override"
                name="upload_accounting_entity"
                list="wb-upload-entity-options"
                value={customEntityInput}
                placeholder={`留空沿用默认：${defaultEntity}`}
                aria-label="临时指定下一批记账公司"
                onChange={(e) => setCustomEntityInput(e.target.value)}
              />
              <datalist id="wb-upload-entity-options">
                {entities.map((ent) => (
                  <option key={ent.id} value={ent.canonical_name} />
                ))}
              </datalist>
            </div>
          ) : null}

          {/* Primary Dropzone */}
          <div
            className="wb-dropzone"
            data-dragging={dragging ? "true" : "false"}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => void handleDrop(e)}
          >
            <i
              className="ri-icon ri-upload-cloud-2-line"
              style={{ "--ri-size": "22px" } as React.CSSProperties}
              aria-hidden="true"
            />
            <div style={{ fontWeight: 600, fontSize: "12px" }}>
              拖拽多格式账单或文件夹至此
            </div>
            <div className="wb-helper-note">
              支持邮件 .eml / PDF / 图片 / Excel / Word（单文件 ≤ 50 MiB）
            </div>
            <div className="wb-dropzone-actions">
              <LumnoButton
                variant="secondary"
                compact
                icon="ri-file-add-line"
                onClick={() => fileInputRef.current?.click()}
              >
                选择文件
              </LumnoButton>
              <LumnoButton
                variant="secondary"
                compact
                icon="ri-folder-open-line"
                onClick={() => dirInputRef.current?.click()}
              >
                选择目录
              </LumnoButton>
            </div>
            <input
              id="wb-upload-files"
              name="upload_files"
              ref={fileInputRef}
              type="file"
              multiple
              accept=".eml,.pdf,.jpg,.jpeg,.png,.tif,.tiff,.xls,.xlsx,.doc,.docx"
              style={{ display: "none" }}
              onChange={handleFileInputChange}
            />
            <input
              id="wb-upload-dir"
              name="upload_directory"
              ref={dirInputRef}
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={handleFileInputChange}
            />
          </div>

          {stagedFiles.length > 0 ? (
            <div className="wb-staged-list" aria-label="待提交文件列表">
              {stagedFiles.map((file, idx) => {
                const key = getStagedFileKey(file, idx);
                const fileErr = validation.fileErrors[key];
                return (
                  <div
                    key={key}
                    className="wb-staged-item"
                    data-invalid={fileErr ? "true" : "false"}
                  >
                    <div className="wb-staged-row">
                      <span className="wb-staged-name" title={file.name}>
                        {file.name}
                      </span>
                      <div
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "4px",
                          flexShrink: 0,
                        }}
                      >
                        <span className="wb-helper-note wb-tabular">
                          {formatFileSize(file.size)}
                        </span>
                        <LumnoButton
                          variant="ghost"
                          compact
                          icon="ri-close-line"
                          aria-label={`移除文件 ${file.name}`}
                          onClick={() =>
                            setStagedFiles((prev) =>
                              prev.filter((_, i) => i !== idx)
                            )
                          }
                        />
                      </div>
                    </div>
                    {fileErr ? (
                      <div className="wb-field-error">{fileErr}</div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}

          {(hasAttemptedSubmit ||
            stagedFiles.length > 0 ||
            publicUrlLinesText.trim().length > 0) &&
          validation.summaryErrors.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
              {validation.summaryErrors.map((msg) => (
                <div key={msg} className="wb-field-error">
                  {msg}
                </div>
              ))}
            </div>
          ) : null}

          <LumnoButton
            variant="primary"
            icon="ri-sparkling-line"
            busy={submittingBatch}
            disabled={!validation.valid || submittingDemo}
            onClick={() => void handleSubmit()}
          >
            {primarySubmitLabel}
          </LumnoButton>

          {/* Collapsible Advanced Upload Options */}
          <div className="wb-collapse-block">
            <button
              type="button"
              className="wb-collapse-trigger"
              aria-expanded={advancedOpen}
              onClick={() => setAdvancedOpen((v) => !v)}
            >
              <span>
                高级投递选项
                {hasActiveAdvancedOptions ? "（已设置）" : ""}
              </span>
              <i
                className={`ri-icon ri-icon--compact ${
                  advancedOpen ? "ri-arrow-up-s-line" : "ri-arrow-down-s-line"
                }`}
                aria-hidden="true"
              />
            </button>

            {advancedOpen ? (
              <div className="wb-collapse-body">
                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  <label htmlFor="wb-upload-public-urls" className="wb-field-label">
                    <span>从链接添加（每行一个 http/https 链接）</span>
                    <span className="wb-tabular">
                      {validation.parsedUrls.length} 条有效
                    </span>
                  </label>
                  <LumnoTextarea
                    id="wb-upload-public-urls"
                    name="public_urls"
                    value={publicUrlLinesText}
                    placeholder="https://example.com/invoice_1.pdf"
                    aria-label="公网文件 URL 列表"
                    onChange={(e) => setPublicUrlLinesText(e.target.value)}
                  />
                  {validation.urlErrors.map((err) => (
                    <div key={err.index} className="wb-field-error">
                      {err.message}
                    </div>
                  ))}
                </div>

                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  <div className="wb-field-label">
                    <span>仅本批使用的历史账单（JSON）</span>
                    <LumnoInfoButton
                      ariaLabel="本批临时历史账单说明"
                      tooltip="仅用于本批核对是否重复入账或更新，不会改写全局历史账单库。"
                    />
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                      flexWrap: "wrap",
                    }}
                  >
                    <LumnoButton
                      variant="secondary"
                      compact
                      icon="ri-file-code-line"
                      onClick={() => billsFileInputRef.current?.click()}
                    >
                      {batchBills.name
                        ? `已选：${batchBills.name}`
                        : "选择本批历史账单 JSON"}
                    </LumnoButton>
                    {batchBills.name ? (
                      <LumnoButton
                        variant="ghost"
                        compact
                        icon="ri-close-line"
                        onClick={() => setBatchBills({ name: null, text: "" })}
                      >
                        移除
                      </LumnoButton>
                    ) : null}
                    <input
                      id="wb-upload-batch-bills"
                      name="existing_bills_file"
                      ref={billsFileInputRef}
                      type="file"
                      accept=".json,application/json"
                      style={{ display: "none" }}
                      onChange={(e) => void handleBillsFilePick(e)}
                    />
                  </div>
                  {validation.existingBillsError ? (
                    <div className="wb-field-error">
                      {validation.existingBillsError}
                    </div>
                  ) : null}
                </div>

                <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
                  <div className="wb-field-label">
                    <span>单据识别方式</span>
                    <LumnoInfoButton
                      ariaLabel="识别方式说明"
                      tooltip="默认优先远端识别并在不可用时自动切换本地识别；如仅允许远端网关，可切换为「仅使用远端服务」。"
                    />
                  </div>
                  <LumnoSegmentedControl
                    value={ocrMode}
                    ariaLabel="选择单据识别方式"
                    options={[
                      {
                        value: "REMOTE_WITH_LOCAL_FALLBACK",
                        label: "自动识别（含本地回退）",
                      },
                      { value: "REMOTE_ONLY", label: "仅远端识别" },
                    ]}
                    onChange={setOcrMode}
                  />
                </div>
              </div>
            ) : null}
          </div>

          {/* Collapsible Sample Demo Drawer */}
          <div className="wb-collapse-block">
            <button
              type="button"
              className="wb-collapse-trigger"
              aria-expanded={demoOpen}
              onClick={() => setDemoOpen((v) => !v)}
            >
              <span>示例演练（内置 4 份真实样本）</span>
              <i
                className={`ri-icon ri-icon--compact ${
                  demoOpen ? "ri-arrow-up-s-line" : "ri-arrow-down-s-line"
                }`}
                aria-hidden="true"
              />
            </button>

            {demoOpen ? (
              <div className="wb-collapse-body">
                <div className="wb-helper-note">
                  会按当前「下一批公司：{effectiveEntity}」新建一个独立演示批次，不影响已选文件。
                </div>
                <LumnoButton
                  variant="secondary"
                  compact
                  icon="ri-play-circle-line"
                  busy={submittingDemo}
                  disabled={submittingBatch}
                  onClick={() =>
                    void onSubmitDemo({
                      accountingEntity: effectiveEntity,
                      ocrMode,
                      onAccepted: resetEntityOverride,
                    })
                  }
                >
                  一键载入 4 份真样本
                </LumnoButton>
              </div>
            ) : null}
          </div>
        </section>

        {/* Document Tree */}
        <section
          aria-label="当前批次文档与附件树"
          style={{ display: "flex", flexDirection: "column", gap: "6px" }}
        >
          <div className="wb-field-label">
            <span>本批文件列表</span>
            {activeDocFilterId ? (
              <LumnoButton
                variant="ghost"
                compact
                onClick={() => onSelectDocFilter(null)}
              >
                显示全部文件
              </LumnoButton>
            ) : null}
          </div>

          {!activeBatch ? (
            pendingBatchRecovery ? (
              <div className="wb-empty-state lumno-paper-card" role="status">
                <strong>
                  {pendingBatchRecovery.errorMessage
                    ? `批次 ${pendingBatchRecovery.shortCode} 详情待读取`
                    : `正在加载批次 ${pendingBatchRecovery.shortCode}…`}
                </strong>
                <span className="wb-mono wb-helper-note">
                  {pendingBatchRecovery.id}
                </span>
                {pendingBatchRecovery.errorMessage && onRetryPendingBatch ? (
                  <LumnoButton
                    variant="secondary"
                    compact
                    icon="ri-refresh-line"
                    busy={pendingBatchRecovery.loading}
                    onClick={onRetryPendingBatch}
                  >
                    重试读取此批次
                  </LumnoButton>
                ) : null}
              </div>
            ) : (
              <div className="wb-empty-state lumno-paper-card">
                <i
                  className="ri-icon ri-folder-info-line"
                  style={{ "--ri-size": "24px" } as React.CSSProperties}
                  aria-hidden="true"
                />
                <span>上传单据后，这里会列出邮件正文、附件及识别结果</span>
              </div>
            )
          ) : orderedDocs.length === 0 ? (
            <div className="wb-empty-state lumno-paper-card">
              <span>正在拆包整理文件列表，请稍候...</span>
            </div>
          ) : (
            <div className="wb-doc-tree">
              {orderedDocs.map(({ doc, isChild }) => {
                const statusMeta = getDocStatusMeta(doc.status);
                const isSelected = activeDocFilterId === doc.id;
                const isDocDirty = dirtyDocIds?.has(doc.id) ?? false;
                const totalStr = formatLosslessAmount(doc, "total_amount", "");
                const semanticTitle = getDocSemanticTitle(doc, isChild);
                return (
                  <div
                    key={doc.id}
                    role="button"
                    tabIndex={0}
                    className="wb-doc-node"
                    data-child={isChild ? "true" : "false"}
                    data-failed={doc.status === "FAILED" ? "true" : "false"}
                    data-active={isSelected ? "true" : "false"}
                    onClick={() => {
                      onSelectDocFilter(isSelected ? null : doc.id);
                      if (
                        doc.status === "FAILED" ||
                        doc.status === "SKIPPED_NOISE"
                      ) {
                        onInspectDoc(doc.id);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelectDocFilter(isSelected ? null : doc.id);
                        if (
                          doc.status === "FAILED" ||
                          doc.status === "SKIPPED_NOISE"
                        ) {
                          onInspectDoc(doc.id);
                        }
                      }
                    }}
                  >
                    <div className="wb-doc-title-row">
                      <span className="wb-doc-semantic">
                        <i
                          className={`ri-icon ri-icon--compact ${getDocIcon(doc.source_kind)}`}
                          aria-hidden="true"
                        />
                        <span>{semanticTitle}</span>
                      </span>
                      <div
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "4px",
                          flexShrink: 0,
                        }}
                      >
                        {isDocDirty ? (
                          <span className="wb-badge wb-badge--warn">未保存</span>
                        ) : null}
                        <span className={`wb-badge wb-badge--${statusMeta.tone}`}>
                          {statusMeta.label}
                        </span>
                      </div>
                    </div>

                    <div
                      className="wb-doc-subfilename"
                      title={doc.sanitized_filename}
                    >
                      {doc.sanitized_filename}
                    </div>

                    <div className="wb-doc-meta-row">
                      <span className="wb-helper-note">
                        {formatOcrChannelLabel(doc.extraction_channel)}
                        {totalStr
                          ? ` · 合计 ${totalStr} ${doc.total_currency ?? ""}`
                          : ""}
                      </span>

                      <div
                        style={{
                          display: "inline-flex",
                          gap: "4px",
                          flexWrap: "wrap",
                        }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        {doc.status === "SKIPPED_NOISE" ? (
                          <LumnoButton
                            variant="ghost"
                            compact
                            icon="ri-scan-2-line"
                            busy={forcingDocId === doc.id}
                            onClick={() => onForceParseDoc(doc.id)}
                          >
                            强制识别
                          </LumnoButton>
                        ) : null}

                        {doc.status === "FAILED" ? (
                          <>
                            <LumnoButton
                              variant="secondary"
                              compact
                              icon="ri-refresh-line"
                              busy={retryingDocId === doc.id}
                              onClick={() => onRetryOrReplaceDoc(doc.id)}
                            >
                              重试
                            </LumnoButton>
                            <LumnoButton
                              variant="ghost"
                              compact
                              icon="ri-file-upload-line"
                              onClick={() => {
                                replaceTargetDocIdRef.current = doc.id;
                                replaceDocInputRef.current?.click();
                              }}
                            >
                              替换
                            </LumnoButton>
                          </>
                        ) : null}

                        <LumnoButton
                          variant="ghost"
                          compact
                          icon="ri-file-edit-line"
                          onClick={() => onInspectDoc(doc.id)}
                        >
                          {doc.status === "PARSED" ? "核对合计" : "查看"}
                        </LumnoButton>

                        {onPreviewDoc ? (
                          <LumnoButton
                            variant={
                              previewOpen && previewDocId === doc.id
                                ? "secondary"
                                : "ghost"
                            }
                            compact
                            icon="ri-eye-line"
                            aria-pressed={previewOpen && previewDocId === doc.id}
                            onClick={() => onPreviewDoc(doc.id)}
                          >
                            查看原件
                          </LumnoButton>
                        ) : null}
                      </div>
                    </div>

                    {doc.error_message ? (
                      <div
                        className={
                          doc.status === "FAILED"
                            ? "wb-field-error"
                            : "wb-helper-note"
                        }
                      >
                        {doc.error_message}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}

          <input
            id="wb-upload-replace-doc"
            name="upload_replace_doc_file"
            ref={replaceDocInputRef}
            type="file"
            style={{ display: "none" }}
            onChange={handleReplaceDocInput}
          />
        </section>
      </div>
    </>
  );
}
