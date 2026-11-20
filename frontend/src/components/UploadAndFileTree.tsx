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
  type BatchDetail,
  type OcrMode,
  type SourceDocumentSummary,
} from "../api/client.ts";
import { formatLosslessAmount } from "../api/decimalJson.ts";
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
      return { label: "PARSED", tone: "ok" };
    case "METADATA_ONLY":
      return { label: "METADATA_ONLY", tone: "info" };
    case "SKIPPED_NOISE":
      return { label: "SKIPPED_NOISE", tone: "muted" };
    case "FAILED":
      return { label: "FAILED", tone: "error" };
  }
}

export interface UploadAndFileTreeProps {
  selectedEntity: string;
  activeBatch: BatchDetail | null;
  activeDocFilterId: string | null;
  submittingBatch: boolean;
  submittingDemo: boolean;
  retryingDocId: string | null;
  forcingDocId: string | null;
  onSubmitBatch: (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
    files: File[];
    publicUrls: string[];
    existingBillsJsonText: string;
  }) => Promise<boolean>;
  onSubmitDemo: (args: {
    accountingEntity: string;
    ocrMode: OcrMode;
  }) => Promise<void>;
  onSelectDocFilter: (docId: string | null) => void;
  onForceParseDoc: (docId: string) => void;
  onRetryOrReplaceDoc: (docId: string, file?: File) => void;
  onOpenDocTotalsHint: (doc: SourceDocumentSummary) => void;
}

export function UploadAndFileTree({
  selectedEntity,
  activeBatch,
  activeDocFilterId,
  submittingBatch,
  submittingDemo,
  retryingDocId,
  forcingDocId,
  onSubmitBatch,
  onSubmitDemo,
  onSelectDocFilter,
  onForceParseDoc,
  onRetryOrReplaceDoc,
  onOpenDocTotalsHint,
}: UploadAndFileTreeProps) {
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [showUrlsPanel, setShowUrlsPanel] = useState(false);
  const [publicUrlLinesText, setPublicUrlLinesText] = useState("");
  const [customEntityInput, setCustomEntityInput] = useState("");
  const [ocrMode, setOcrMode] = useState<OcrMode>("REMOTE_ONLY");
  const [batchBillsName, setBatchBillsName] = useState<string | null>(null);
  const [batchBillsText, setBatchBillsText] = useState("");
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

  const effectiveEntity = (
    customEntityInput.trim() || selectedEntity
  ).trim();

  const validation = useMemo(
    () =>
      validateBatchIngestionInput({
        accountingEntity: effectiveEntity,
        files: stagedFiles,
        publicUrlLinesText,
        existingBillsJsonText: batchBillsText,
      }),
    [effectiveEntity, stagedFiles, publicUrlLinesText, batchBillsText]
  );

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
    setBatchBillsName(file.name);
    setBatchBillsText(text);
  };

  const handleSubmit = async () => {
    setHasAttemptedSubmit(true);
    if (!validation.valid) {
      return;
    }
    const ok = await onSubmitBatch({
      accountingEntity: effectiveEntity,
      ocrMode,
      files: stagedFiles,
      publicUrls: validation.parsedUrls,
      existingBillsJsonText: batchBillsText,
    });
    if (ok) {
      setStagedFiles([]);
      setPublicUrlLinesText("");
      setBatchBillsName(null);
      setBatchBillsText("");
      setHasAttemptedSubmit(false);
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
    const docs = activeBatch.documents;
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

  return (
    <>
      <div className="wb-pane-header">
        <h2 className="wb-pane-title">
          <i className="ri-icon ri-inbox-archive-line" aria-hidden="true" />
          <span>单据投递与文件树</span>
        </h2>
        {activeBatch ? (
          <span className="wb-badge wb-badge--muted wb-tabular">
            {activeBatch.succeeded_files}/{activeBatch.total_files} 份
          </span>
        ) : null}
      </div>

      <div className="wb-pane-body">
        <section className="wb-section-box" aria-label="单据投递配置">
          <div className="wb-field-label">
            <span>投递记账主体（可选覆盖）</span>
            <span className="wb-helper-note">当前：{selectedEntity}</span>
          </div>
          <LumnoInput
            value={customEntityInput}
            placeholder={`留空沿用顶栏主体：${selectedEntity}`}
            aria-label="自定义投递记账主体"
            onChange={(e) => setCustomEntityInput(e.target.value)}
          />

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
              拖拽多格式单据或文件夹至此
            </div>
            <div className="wb-helper-note">
              支持 .eml / .pdf / 图片 / Excel / Word，单文件 ≤ 50 MiB，单批 ≤ 50 项
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
              <LumnoButton
                variant="ghost"
                compact
                icon="ri-links-line"
                onClick={() => setShowUrlsPanel((v) => !v)}
              >
                {showUrlsPanel ? "收起 URL" : "公网 URL"}
              </LumnoButton>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".eml,.pdf,.jpg,.jpeg,.png,.tif,.tiff,.xls,.xlsx,.doc,.docx"
              style={{ display: "none" }}
              onChange={handleFileInputChange}
            />
            <input
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
                      <div style={{ display: "inline-flex", alignItems: "center", gap: "4px", flexShrink: 0 }}>
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

          {showUrlsPanel ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
              <div className="wb-field-label">
                <span>公网 URL 列表（每行一个 http/https 链接）</span>
                <span className="wb-tabular">
                  {validation.parsedUrls.length} 条有效
                </span>
              </div>
              <LumnoTextarea
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
          ) : null}

          <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
            <div className="wb-field-label">
              <span>随批已有账单基准（existing_bills_json）</span>
              <LumnoInfoButton
                ariaLabel="随批基准与全局库区别说明"
                tooltip="随批导入的 existing_bills_json 仅用于当前批次四级匹配，不会覆盖或污染全局历史账单基准库。"
              />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
              <LumnoButton
                variant="secondary"
                compact
                icon="ri-file-code-line"
                onClick={() => billsFileInputRef.current?.click()}
              >
                {batchBillsName ? `已选：${batchBillsName}` : "挂载随批 JSON"}
              </LumnoButton>
              {batchBillsName ? (
                <LumnoButton
                  variant="ghost"
                  compact
                  icon="ri-close-line"
                  onClick={() => {
                    setBatchBillsName(null);
                    setBatchBillsText("");
                  }}
                >
                  移除
                </LumnoButton>
              ) : null}
              <input
                ref={billsFileInputRef}
                type="file"
                accept=".json,application/json"
                style={{ display: "none" }}
                onChange={(e) => void handleBillsFilePick(e)}
              />
            </div>
            <div className="wb-helper-note">
              仅作用于本批匹配，不改写全局库（全局导入请见顶栏主数据）
            </div>
            {validation.existingBillsError ? (
              <div className="wb-field-error">
                {validation.existingBillsError}
              </div>
            ) : null}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
            <div className="wb-field-label">
              <span>OCR 提取通道（OcrMode）</span>
              <LumnoInfoButton
                ariaLabel="OCR 模式前提说明"
                tooltip="默认优先 REMOTE_ONLY（远端 OCR 网关）；若本地二进制 PDF/图片未配置公网中转，系统会诚实阻断并提示，可按需显式切换本地离线兜底。"
              />
            </div>
            <LumnoSegmentedControl
              value={ocrMode}
              ariaLabel="选择 OCR 解析通道"
              options={[
                { value: "REMOTE_ONLY", label: "远端 OCR 优先" },
                { value: "REMOTE_WITH_LOCAL_FALLBACK", label: "本地回退兜底" },
              ]}
              onChange={setOcrMode}
            />
            <div className="wb-helper-note">
              {ocrMode === "REMOTE_ONLY"
                ? "REMOTE_ONLY：优先调用远端 OCR 网关；本地无公网中转的扫描件将阻断待重试。"
                : "REMOTE_WITH_LOCAL_FALLBACK：远端不可达时启用本地离线兜底并打标来源。"}
            </div>
          </div>

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

          <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "2px" }}>
            <LumnoButton
              variant="primary"
              icon="ri-sparkling-line"
              busy={submittingBatch}
              disabled={!validation.valid || submittingDemo}
              onClick={() => void handleSubmit()}
            >
              开始智能解析
            </LumnoButton>
            <LumnoButton
              variant="secondary"
              icon="ri-play-circle-line"
              busy={submittingDemo}
              disabled={submittingBatch}
              onClick={() =>
                void onSubmitDemo({
                  accountingEntity: effectiveEntity,
                  ocrMode,
                })
              }
            >
              一键载入 4 份真样本
            </LumnoButton>
          </div>
        </section>

        <section
          aria-label="当前批次文档与附件树"
          style={{ display: "flex", flexDirection: "column", gap: "6px" }}
        >
          <div className="wb-field-label">
            <span>批次文档 / EML 附件树</span>
            {activeDocFilterId ? (
              <LumnoButton
                variant="ghost"
                compact
                onClick={() => onSelectDocFilter(null)}
              >
                清除筛选
              </LumnoButton>
            ) : null}
          </div>

          {!activeBatch ? (
            <div className="wb-empty-state lumno-paper-card">
              <i
                className="ri-icon ri-folder-info-line"
                style={{ "--ri-size": "24px" } as React.CSSProperties}
                aria-hidden="true"
              />
              <span>上传单据或点击“一键载入 4 份真样本”后查看文件树</span>
            </div>
          ) : orderedDocs.length === 0 ? (
            <div className="wb-empty-state lumno-paper-card">
              <span>批次拆包中，稍候将显示文档与 EML 附件节点...</span>
            </div>
          ) : (
            <div className="wb-doc-tree">
              {orderedDocs.map(({ doc, isChild }) => {
                const statusMeta = getDocStatusMeta(doc.status);
                const isSelected = activeDocFilterId === doc.id;
                const totalStr = formatLosslessAmount(doc, "total_amount", "");
                return (
                  <div
                    key={doc.id}
                    role="button"
                    tabIndex={0}
                    className="wb-doc-node"
                    data-child={isChild ? "true" : "false"}
                    data-failed={doc.status === "FAILED" ? "true" : "false"}
                    data-active={isSelected ? "true" : "false"}
                    onClick={() =>
                      onSelectDocFilter(isSelected ? null : doc.id)
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelectDocFilter(isSelected ? null : doc.id);
                      }
                    }}
                  >
                    <div className="wb-doc-title-row">
                      <span
                        className="wb-doc-filename"
                        title={doc.sanitized_filename}
                      >
                        <i
                          className={`ri-icon ri-icon--compact ${getDocIcon(doc.source_kind)}`}
                          aria-hidden="true"
                        />
                        <span>{doc.sanitized_filename}</span>
                      </span>
                      <span className={`wb-badge wb-badge--${statusMeta.tone}`}>
                        {statusMeta.label}
                      </span>
                    </div>

                    <div className="wb-doc-meta-row">
                      <span className="wb-helper-note">
                        通道：{doc.extraction_channel}
                        {totalStr
                          ? ` · 总额 ${totalStr} ${doc.total_currency ?? ""}`
                          : ""}
                      </span>

                      <div
                        style={{ display: "inline-flex", gap: "4px", flexWrap: "wrap" }}
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
                            强制 OCR 提取
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

                        {doc.status === "PARSED" ? (
                          <LumnoButton
                            variant="ghost"
                            compact
                            icon="ri-calculator-line"
                            onClick={() => onOpenDocTotalsHint(doc)}
                          >
                            总额概览
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
