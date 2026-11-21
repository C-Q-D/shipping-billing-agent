import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  ApiClientError,
  BillingApiClient,
  type BatchDetail,
  type BillingRecordDetail,
  type DocumentContentView,
  type SourceDocumentSummary,
} from "../api/client.ts";
import { LumnoButton, LumnoSegmentedControl } from "./LumnoPrimitives.tsx";

export type DocumentPreviewMode = "docked" | "floating";
export type DocumentPreviewKind = "pdf" | "image" | "text" | "binary";

export interface EmlFamilyTab {
  doc: SourceDocumentSummary;
  label: string;
  isBody: boolean;
  isRoot: boolean;
}

export interface OrderedPreviewDocItem {
  doc: SourceDocumentSummary;
  depth: 0 | 1;
}

export interface FloatingPreviewRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface TextHighlightSegment {
  text: string;
  highlight: boolean;
}

const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"];
const TEXT_EXTENSIONS = [".eml", ".txt", ".csv", ".log", ".md"];
const BINARY_EXTENSIONS = [".xls", ".xlsx", ".doc", ".docx"];

function getFilenameLower(doc: SourceDocumentSummary): string {
  return (doc.sanitized_filename || doc.original_filename || "").trim().toLowerCase();
}

export function classifyDocumentPreviewKind(
  doc: SourceDocumentSummary
): DocumentPreviewKind {
  const kind = (doc.source_kind ?? "").toUpperCase();
  const name = getFilenameLower(doc);
  if (kind === "PDF" || name.endsWith(".pdf")) {
    return "pdf";
  }
  if (kind === "IMAGE" || IMAGE_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return "image";
  }
  if (
    kind === "EML" ||
    kind === "EML_BODY" ||
    TEXT_EXTENSIONS.some((ext) => name.endsWith(ext))
  ) {
    return "text";
  }
  if (
    kind === "EXCEL" ||
    kind === "WORD" ||
    kind === "URL" ||
    BINARY_EXTENSIONS.some((ext) => name.endsWith(ext))
  ) {
    return "binary";
  }
  return "binary";
}

export function getDefaultPreviewViewMode(
  doc: SourceDocumentSummary
): DocumentContentView {
  const kind = (doc.source_kind ?? "").toUpperCase();
  if (kind === "EML_BODY") {
    return "text";
  }
  const previewKind = classifyDocumentPreviewKind(doc);
  if (previewKind === "binary") {
    return "text";
  }
  return "original";
}

export function buildOrderedPreviewDocs(
  documents: ReadonlyArray<SourceDocumentSummary>
): OrderedPreviewDocItem[] {
  const roots = documents.filter((d) => !d.parent_doc_id);
  const childrenByParent = new Map<string, SourceDocumentSummary[]>();
  for (const d of documents) {
    if (d.parent_doc_id) {
      const list = childrenByParent.get(d.parent_doc_id) ?? [];
      list.push(d);
      childrenByParent.set(d.parent_doc_id, list);
    }
  }
  const result: OrderedPreviewDocItem[] = [];
  const seen = new Set<string>();
  for (const root of roots) {
    seen.add(root.id);
    result.push({ doc: root, depth: 0 });
    for (const child of childrenByParent.get(root.id) ?? []) {
      if (!seen.has(child.id)) {
        seen.add(child.id);
        result.push({ doc: child, depth: 1 });
      }
    }
  }
  for (const d of documents) {
    if (!seen.has(d.id)) {
      seen.add(d.id);
      result.push({ doc: d, depth: d.parent_doc_id ? 1 : 0 });
    }
  }
  return result;
}

export function findEmlFamilyRoot(
  documents: ReadonlyArray<SourceDocumentSummary>,
  docId: string
): SourceDocumentSummary | null {
  const byId = new Map<string, SourceDocumentSummary>();
  for (const d of documents) {
    byId.set(d.id, d);
  }
  let current = byId.get(docId) ?? null;
  if (!current) return null;
  const visited = new Set<string>([current.id]);
  while (current.parent_doc_id) {
    const parent = byId.get(current.parent_doc_id);
    if (!parent || visited.has(parent.id)) {
      break;
    }
    visited.add(parent.id);
    current = parent;
  }
  return current;
}

export function buildEmlFamilyTabs(
  documents: ReadonlyArray<SourceDocumentSummary>,
  activeDocId: string
): { rootDoc: SourceDocumentSummary | null; tabs: EmlFamilyTab[] } {
  const activeDoc = documents.find((d) => d.id === activeDocId);
  if (!activeDoc) {
    return { rootDoc: null, tabs: [] };
  }
  const rootDoc = findEmlFamilyRoot(documents, activeDocId) ?? activeDoc;
  const childrenByParent = new Map<string, SourceDocumentSummary[]>();
  for (const d of documents) {
    if (d.parent_doc_id) {
      const list = childrenByParent.get(d.parent_doc_id) ?? [];
      list.push(d);
      childrenByParent.set(d.parent_doc_id, list);
    }
  }
  const descendants: SourceDocumentSummary[] = [];
  const visited = new Set<string>([rootDoc.id]);
  const queue: SourceDocumentSummary[] = [
    ...(childrenByParent.get(rootDoc.id) ?? []),
  ];
  while (queue.length > 0) {
    const next = queue.shift()!;
    if (visited.has(next.id)) continue;
    visited.add(next.id);
    descendants.push(next);
    for (const nested of childrenByParent.get(next.id) ?? []) {
      if (!visited.has(nested.id)) {
        queue.push(nested);
      }
    }
  }

  const isEmlRoot = rootDoc.source_kind.toUpperCase() === "EML";
  if (!isEmlRoot && descendants.length === 0 && !activeDoc.parent_doc_id) {
    return { rootDoc: null, tabs: [] };
  }

  const bodyDocs = descendants.filter(
    (d) => d.source_kind.toUpperCase() === "EML_BODY"
  );
  const attachmentDocs = descendants.filter(
    (d) => d.source_kind.toUpperCase() !== "EML_BODY"
  );

  const tabs: EmlFamilyTab[] = [];
  bodyDocs.forEach((doc, idx) => {
    tabs.push({
      doc,
      label: bodyDocs.length === 1 ? "邮件正文" : `邮件正文 ${idx + 1}`,
      isBody: true,
      isRoot: false,
    });
  });
  for (const doc of attachmentDocs) {
    tabs.push({
      doc,
      label: doc.sanitized_filename,
      isBody: false,
      isRoot: false,
    });
  }
  tabs.push({
    doc: rootDoc,
    label:
      descendants.length > 0
        ? `原始邮件 (${rootDoc.sanitized_filename})`
        : rootDoc.sanitized_filename,
    isBody: false,
    isRoot: true,
  });

  return { rootDoc, tabs };
}

export function resolveInitialPreviewTarget(
  documents: ReadonlyArray<SourceDocumentSummary>,
  requestedDocId: string,
  requestedPage?: number | null
): {
  doc: SourceDocumentSummary;
  page: number | null;
  viewMode: DocumentContentView;
} | null {
  const requestedDoc = documents.find((d) => d.id === requestedDocId);
  if (!requestedDoc) return null;

  let targetDoc = requestedDoc;
  if (requestedDoc.source_kind.toUpperCase() === "EML") {
    const bodyChild = documents.find(
      (d) =>
        d.parent_doc_id === requestedDoc.id &&
        d.source_kind.toUpperCase() === "EML_BODY"
    );
    if (bodyChild) {
      targetDoc = bodyChild;
    }
  }

  const normalizedPage =
    classifyDocumentPreviewKind(targetDoc) === "pdf" &&
    typeof requestedPage === "number" &&
    Number.isFinite(requestedPage) &&
    requestedPage >= 1
      ? Math.floor(requestedPage)
      : null;

  return {
    doc: targetDoc,
    page: normalizedPage,
    viewMode: getDefaultPreviewViewMode(targetDoc),
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function buildFlexibleSnippetRegex(snippet: string): RegExp | null {
  const trimmed = snippet.trim();
  if (!trimmed || trimmed.length > 300) return null;
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return null;
  const pattern = parts.map(escapeRegExp).join("\\s+");
  try {
    return new RegExp(pattern, "gi");
  } catch {
    return null;
  }
}

export function splitTextForSnippetHighlight(
  text: string,
  snippet: string
): TextHighlightSegment[] {
  if (!text) return [];
  const trimmed = snippet.trim();
  if (!trimmed) {
    return [{ text, highlight: false }];
  }

  const candidateRegexes: RegExp[] = [];
  const fullRegex = buildFlexibleSnippetRegex(trimmed);
  if (fullRegex) {
    candidateRegexes.push(fullRegex);
  }
  const lines = trimmed
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length >= 6)
    .sort((a, b) => b.length - a.length);
  if (lines[0] && lines[0] !== trimmed) {
    const lineRegex = buildFlexibleSnippetRegex(lines[0]);
    if (lineRegex) {
      candidateRegexes.push(lineRegex);
    }
  }

  for (const regex of candidateRegexes) {
    const segments: TextHighlightSegment[] = [];
    let lastIndex = 0;
    let matchCount = 0;
    regex.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = regex.exec(text)) !== null && matchCount < 20) {
      if (!match[0]) {
        regex.lastIndex += 1;
        continue;
      }
      if (match.index > lastIndex) {
        segments.push({
          text: text.slice(lastIndex, match.index),
          highlight: false,
        });
      }
      segments.push({
        text: match[0],
        highlight: true,
      });
      lastIndex = match.index + match[0].length;
      matchCount += 1;
    }
    if (matchCount > 0) {
      if (lastIndex < text.length) {
        segments.push({
          text: text.slice(lastIndex),
          highlight: false,
        });
      }
      return segments;
    }
  }

  return [{ text, highlight: false }];
}

export function clampFloatingRect(
  rect: FloatingPreviewRect,
  viewportW: number,
  viewportH: number
): FloatingPreviewRect {
  const safeVw = Math.max(320, viewportW);
  const safeVh = Math.max(280, viewportH);
  const maxW = Math.max(300, safeVw - 16);
  const maxH = Math.max(240, safeVh - 24);
  const minW = Math.min(380, maxW);
  const minH = Math.min(300, maxH);
  const width = Math.max(minW, Math.min(rect.width, maxW));
  const height = Math.max(minH, Math.min(rect.height, maxH));
  const maxLeft = Math.max(8, safeVw - width - 8);
  const maxTop = Math.max(8, safeVh - height - 8);
  const left = Math.max(8, Math.min(rect.left, maxLeft));
  const top = Math.max(8, Math.min(rect.top, maxTop));
  return { left, top, width, height };
}

function getInitialFloatingRect(): FloatingPreviewRect {
  const vw = typeof window !== "undefined" ? window.innerWidth : 1280;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  return clampFloatingRect(
    {
      left: 16,
      top: 68,
      width: Math.min(680, Math.max(400, vw - 520)),
      height: Math.min(660, Math.max(360, vh - 96)),
    },
    vw,
    vh
  );
}

function hasOpenPortalOverlay(): boolean {
  if (typeof document === "undefined") return false;
  if (typeof document.querySelector === "function") {
    const found = document.querySelector(
      ".lumno-modal-backdrop, .wb-drawer-backdrop, .lumno-popconfirm, .lumno-select-menu"
    );
    if (found) return true;
  }
  const body = document.body as unknown as {
    childNodes?: Array<{
      attributes?: Record<string, string>;
      childNodes?: unknown[];
    }>;
  } | null;
  if (!body || !Array.isArray(body.childNodes)) return false;
  const stack: Array<{
    attributes?: Record<string, string>;
    childNodes?: unknown[];
  }> = [...body.childNodes];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) continue;
    const cls = node.attributes?.class ?? "";
    if (
      cls.includes("lumno-modal-backdrop") ||
      cls.includes("wb-drawer-backdrop") ||
      cls.includes("lumno-popconfirm") ||
      cls.includes("lumno-select-menu")
    ) {
      return true;
    }
    if (Array.isArray(node.childNodes)) {
      for (const child of node.childNodes) {
        if (child && typeof child === "object") {
          stack.push(
            child as {
              attributes?: Record<string, string>;
              childNodes?: unknown[];
            }
          );
        }
      }
    }
  }
  return false;
}

export interface DocumentPreviewPaneProps {
  api: BillingApiClient;
  batch: BatchDetail;
  activeDoc: SourceDocumentSummary;
  documents: ReadonlyArray<SourceDocumentSummary>;
  selectedRecord: BillingRecordDetail | null;
  previewMode: DocumentPreviewMode;
  viewMode: DocumentContentView;
  page: number | null;
  jumpSeq?: number;
  locked: boolean;
  canCloseOnEscape: boolean;
  isDraggingSplitter?: boolean;
  onSelectDoc: (docId: string) => void;
  onToggleLocked: () => void;
  onToggleMode: () => void;
  onChangeViewMode: (view: DocumentContentView) => void;
  onClose: () => void;
}

export function DocumentPreviewPane({
  api,
  batch,
  activeDoc,
  documents,
  selectedRecord,
  previewMode,
  viewMode,
  page,
  jumpSeq = 0,
  locked,
  canCloseOnEscape,
  isDraggingSplitter = false,
  onSelectDoc,
  onToggleLocked,
  onToggleMode,
  onChangeViewMode,
  onClose,
}: DocumentPreviewPaneProps) {
  const previewKind = classifyDocumentPreviewKind(activeDoc);
  const orderedDocs = useMemo(
    () => buildOrderedPreviewDocs(documents),
    [documents]
  );
  const emlFamily = useMemo(
    () => buildEmlFamilyTabs(documents, activeDoc.id),
    [documents, activeDoc.id]
  );

  // Image zoom & 90° rotation state
  const [fitMode, setFitMode] = useState(true);
  const [zoomPercent, setZoomPercent] = useState(100);
  const [rotationDeg, setRotationDeg] = useState(0);
  const [originalError, setOriginalError] = useState<string | null>(null);
  const [naturalSize, setNaturalSize] = useState<{ w: number; h: number } | null>(
    null
  );
  const [stageSize, setStageSize] = useState<{ w: number; h: number } | null>(
    null
  );
  const imageStageRef = useRef<HTMLDivElement | null>(null);

  // Text preview fetch state (AbortController guarded)
  const [textLoading, setTextLoading] = useState(false);
  const [textContent, setTextContent] = useState<string | null>(null);
  const [textError, setTextError] = useState<string | null>(null);

  // Floating window geometry & drag/resize state
  const [floatRect, setFloatRect] = useState<FloatingPreviewRect>(
    getInitialFloatingRect
  );
  const [floatingInteracting, setFloatingInteracting] = useState(false);
  const firstMarkRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setFitMode(true);
    setZoomPercent(100);
    setRotationDeg(0);
    setOriginalError(null);
    setNaturalSize(null);
  }, [batch.id, activeDoc.id, viewMode]);

  useEffect(() => {
    if (previewKind !== "image" || viewMode !== "original") {
      return;
    }
    const el = imageStageRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect?.();
      const w = (el as { clientWidth?: number }).clientWidth || rect?.width || 0;
      const h = (el as { clientHeight?: number }).clientHeight || rect?.height || 0;
      if (w > 0 && h > 0) {
        setStageSize((prev) =>
          prev && prev.w === w && prev.h === h ? prev : { w, h }
        );
      }
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [previewKind, viewMode, activeDoc.id, previewMode]);

  const imageLayout = useMemo(() => {
    const isQuarterTurn = Math.abs(rotationDeg) % 180 === 90;
    const scale = zoomPercent / 100;
    if (!naturalSize || naturalSize.w <= 0 || naturalSize.h <= 0) {
      return {
        isQuarterTurn,
        hasMeasured: false,
        unrotatedW: null as number | null,
        unrotatedH: null as number | null,
        frameW: null as number | null,
        frameH: null as number | null,
      };
    }
    const natW = naturalSize.w;
    const natH = naturalSize.h;
    const availW = Math.max(200, (stageSize?.w ?? 680) - 32);
    const availH = Math.max(200, (stageSize?.h ?? 560) - 32);
    const fitScale = fitMode
      ? Math.min(
          1,
          availW / (isQuarterTurn ? natH : natW),
          availH / (isQuarterTurn ? natW : natH)
        )
      : Math.min(1, availW / natW, availH / natH) * scale;
    const unrotatedW = Math.max(1, Math.round(natW * fitScale));
    const unrotatedH = Math.max(1, Math.round(natH * fitScale));
    const frameW = isQuarterTurn ? unrotatedH : unrotatedW;
    const frameH = isQuarterTurn ? unrotatedW : unrotatedH;
    return {
      isQuarterTurn,
      hasMeasured: true,
      unrotatedW,
      unrotatedH,
      frameW,
      frameH,
    };
  }, [naturalSize, stageSize, fitMode, zoomPercent, rotationDeg]);

  useEffect(() => {
    if (previewMode !== "floating") return;
    const handleResize = () => {
      setFloatRect((prev) =>
        clampFloatingRect(prev, window.innerWidth, window.innerHeight)
      );
    };
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [previewMode]);

  // Esc closes preview ONLY when no modal/drawer/popconfirm/menu is open on top
  useEffect(() => {
    const handleKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (!canCloseOnEscape || e.defaultPrevented) return;
      if (hasOpenPortalOverlay()) return;
      e.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [canCloseOnEscape, onClose]);

  const shouldFetchText =
    viewMode === "text" ||
    (previewKind === "text" && viewMode === "original");

  const textRequestView: DocumentContentView =
    viewMode === "text" ? "text" : "original";

  useEffect(() => {
    if (!shouldFetchText) {
      setTextLoading(false);
      setTextContent(null);
      setTextError(null);
      return;
    }

    const controller = new AbortController();
    setTextLoading(true);
    setTextContent(null);
    setTextError(null);

    void api
      .getDocumentTextPreview(
        batch.id,
        activeDoc.id,
        controller.signal,
        textRequestView
      )
      .then((text) => {
        if (controller.signal.aborted) return;
        setTextContent(text);
        setTextLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiClientError && err.code === "REQUEST_ABORTED") {
          return;
        }
        const message =
          err instanceof Error
            ? err.message
            : "无法读取当前单据文本内容，请尝试下载原件查看";
        setTextError(message);
        setTextLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [api, batch.id, activeDoc.id, shouldFetchText, textRequestView]);

  const activeSnippet = useMemo(() => {
    if (!selectedRecord) return "";
    const rawSnippet = selectedRecord.source_evidence?.snippet;
    if (typeof rawSnippet !== "string" || !rawSnippet.trim()) return "";
    if (selectedRecord.source_doc_id === activeDoc.id) {
      return rawSnippet;
    }
    if (emlFamily.rootDoc) {
      const familyIds = new Set(emlFamily.tabs.map((t) => t.doc.id));
      if (familyIds.has(selectedRecord.source_doc_id)) {
        return rawSnippet;
      }
    }
    return "";
  }, [selectedRecord, activeDoc.id, emlFamily]);

  const highlightedSegments = useMemo(
    () =>
      textContent !== null
        ? splitTextForSnippetHighlight(textContent, activeSnippet)
        : [],
    [textContent, activeSnippet]
  );

  const firstHighlightIdx = useMemo(
    () => highlightedSegments.findIndex((seg) => seg.highlight),
    [highlightedSegments]
  );

  useEffect(() => {
    if (firstHighlightIdx >= 0) {
      firstMarkRef.current?.scrollIntoView?.({
        block: "center",
        behavior: "smooth",
      });
    }
  }, [firstHighlightIdx, activeDoc.id, textContent]);

  const downloadView: DocumentContentView =
    activeDoc.source_kind.toUpperCase() === "EML_BODY" ? "text" : "original";
  const downloadUrl = api.getDocumentContentUrl(batch.id, activeDoc.id, {
    view: downloadView,
    download: true,
  });

  const externalView: DocumentContentView =
    viewMode === "text" || previewKind === "binary" ? "text" : "original";
  const externalUrl = api.getDocumentContentUrl(batch.id, activeDoc.id, {
    view: externalView,
    download: false,
    page: previewKind === "pdf" && externalView === "original" ? page : null,
  });

  const pdfIframeUrl = api.getDocumentContentUrl(batch.id, activeDoc.id, {
    view: "original",
    download: false,
    page,
  });
  const imageUrl = api.getDocumentContentUrl(batch.id, activeDoc.id, {
    view: "original",
    download: false,
  });

  const handleIframeLoad = useCallback((iframeEl: HTMLIFrameElement) => {
    try {
      const doc = iframeEl.contentDocument;
      if (!doc || !doc.contentType) return;
      if (doc.contentType.toLowerCase().includes("application/json")) {
        const raw = doc.body?.textContent?.trim() ?? "";
        if (raw.startsWith("{")) {
          const parsed = JSON.parse(raw) as { message?: unknown };
          setOriginalError(
            typeof parsed.message === "string" && parsed.message.trim()
              ? parsed.message
              : "当前单据未保留本地原件文件，请切换查看提取文本。"
          );
        } else {
          setOriginalError("当前单据原件暂不可用，请切换查看提取文本。");
        }
      }
    } catch {
      // Cross-origin or Chrome native PDF viewer plugin document
    }
  }, []);

  const handleFloatingHeaderPointerDown = (
    e: ReactPointerEvent<HTMLDivElement>
  ) => {
    if (previewMode !== "floating") return;
    const target = e.target as HTMLElement | null;
    if (
      target?.closest?.(
        'button, select, input, textarea, a, label, [role="tab"], [role="radio"]'
      )
    ) {
      return;
    }
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startRect = floatRect;
    setFloatingInteracting(true);
    e.currentTarget.setPointerCapture?.(e.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      setFloatRect(
        clampFloatingRect(
          {
            ...startRect,
            left: startRect.left + dx,
            top: startRect.top + dy,
          },
          window.innerWidth,
          window.innerHeight
        )
      );
    };

    const stopDrag = () => {
      setFloatingInteracting(false);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopDrag);
      window.removeEventListener("pointercancel", stopDrag);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopDrag);
    window.addEventListener("pointercancel", stopDrag);
  };

  const handleFloatingResizePointerDown = (
    e: ReactPointerEvent<HTMLDivElement>
  ) => {
    if (previewMode !== "floating") return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startY = e.clientY;
    const startRect = floatRect;
    setFloatingInteracting(true);
    e.currentTarget.setPointerCapture?.(e.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const maxWidth = Math.max(320, vw - startRect.left - 8);
      const maxHeight = Math.max(260, vh - startRect.top - 8);
      setFloatRect({
        ...startRect,
        width: Math.max(Math.min(380, maxWidth), Math.min(startRect.width + dx, maxWidth)),
        height: Math.max(Math.min(300, maxHeight), Math.min(startRect.height + dy, maxHeight)),
      });
    };

    const stopResize = () => {
      setFloatingInteracting(false);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
  };

  const disableIframePointerEvents = isDraggingSplitter || floatingInteracting;

  const renderFallbackActions = (showTextSwitch: boolean) => (
    <div className="wb-action-row" style={{ marginTop: "6px" }}>
      {showTextSwitch ? (
        <LumnoButton
          variant="primary"
          compact
          icon="ri-file-text-line"
          onClick={() => onChangeViewMode("text")}
        >
          查看提取文本
        </LumnoButton>
      ) : null}
      <a
        href={downloadUrl}
        download={activeDoc.sanitized_filename}
        className="lumno-btn lumno-pill lumno-btn--secondary lumno-btn--compact"
      >
        <span className="lumno-busy-label">
          <i className="ri-icon ri-download-2-line" aria-hidden="true" />
          <span>下载原件</span>
        </span>
      </a>
    </div>
  );

  const renderBody = () => {
    if (viewMode === "text" || previewKind === "text") {
      return (
        <div className="wb-preview-text-wrap">
          {previewKind === "binary" ? (
            <div className="wb-preview-binary-bar">
              <span className="wb-helper-note">
                Excel / Word 原件不支持浏览器直接内联渲染，当前展示提取文本内容
              </span>
              <a
                href={downloadUrl}
                download={activeDoc.sanitized_filename}
                className="lumno-btn lumno-pill lumno-btn--primary lumno-btn--compact"
              >
                <span className="lumno-busy-label">
                  <i className="ri-icon ri-download-2-line" aria-hidden="true" />
                  <span>下载原件</span>
                </span>
              </a>
            </div>
          ) : null}

          {textLoading ? (
            <div className="wb-empty-state lumno-paper-card" role="status">
              <span>正在加载单据文本内容…</span>
            </div>
          ) : textError ? (
            <div className="wb-alert-item" data-severity="WARNING" role="alert">
              <strong>
                {textRequestView === "original"
                  ? "原件文件暂不可用"
                  : "文本预览暂不可用"}
              </strong>
              <div>{textError}</div>
              {renderFallbackActions(
                textRequestView === "original" &&
                  activeDoc.source_kind.toUpperCase() !== "EML_BODY"
              )}
            </div>
          ) : (
            <>
              {activeSnippet ? (
                <div className="wb-preview-snippet-status">
                  <span
                    className={`wb-badge ${
                      firstHighlightIdx >= 0 ? "wb-badge--ok" : "wb-badge--muted"
                    }`}
                  >
                    {firstHighlightIdx >= 0
                      ? "已高亮当前费用摘录"
                      : "未在全文中匹配到完全一致片段"}
                  </span>
                </div>
              ) : null}
              <pre className="wb-preview-text-pre">
                {highlightedSegments.length > 0
                  ? highlightedSegments.map((seg, idx) =>
                      seg.highlight ? (
                        <mark
                          key={idx}
                          ref={idx === firstHighlightIdx ? firstMarkRef : undefined}
                          className="wb-preview-highlight"
                        >
                          {seg.text}
                        </mark>
                      ) : (
                        <Fragment key={idx}>{seg.text}</Fragment>
                      )
                    )
                  : textContent ?? ""}
              </pre>
            </>
          )}
        </div>
      );
    }

    if (previewKind === "binary") {
      return (
        <div className="wb-preview-fallback-wrap">
          <div className="wb-empty-state lumno-paper-card">
            <i
              className="ri-icon ri-file-excel-2-line"
              style={{ "--ri-size": "28px" } as React.CSSProperties}
              aria-hidden="true"
            />
            <strong>该文件格式不支持浏览器直接内联渲染</strong>
            <span className="wb-helper-note">
              可切换查看系统提取的文本内容，或直接下载原件在本地打开核对。
            </span>
            {renderFallbackActions(true)}
          </div>
        </div>
      );
    }

    if (previewKind === "pdf") {
      return (
        <div className="wb-preview-pdf-wrap">
          <div className="wb-preview-subbar">
            <span className="wb-helper-note">
              {page ? `当前定位：第 ${page} 页 · ` : ""}
              若浏览器未内置显示 PDF，可切换「文本」查看提取内容或点击「下载」
            </span>
            <LumnoButton
              variant="ghost"
              compact
              onClick={() => onChangeViewMode("text")}
            >
              切换提取文本
            </LumnoButton>
          </div>
          {originalError ? (
            <div className="wb-alert-item" data-severity="WARNING" role="alert">
              <strong>PDF 原件暂不可用</strong>
              <div>{originalError}</div>
              {renderFallbackActions(true)}
            </div>
          ) : (
            <iframe
              key={`${batch.id}::${activeDoc.id}::${page ?? 0}::${jumpSeq}`}
              src={pdfIframeUrl}
              title={`原件预览：${activeDoc.sanitized_filename}`}
              className="wb-preview-iframe"
              style={disableIframePointerEvents ? { pointerEvents: "none" } : undefined}
              onLoad={(e) => handleIframeLoad(e.currentTarget)}
              onError={() =>
                setOriginalError(
                  "无法内联加载 PDF 原件，请切换「查看提取文本」或点击「下载原件」。"
                )
              }
            />
          )}
        </div>
      );
    }

    // Image adaptive renderer (TIFF already converted to PNG by backend)
    return (
      <div className="wb-preview-image-wrap">
        <div className="wb-preview-image-toolbar" role="toolbar" aria-label="图片缩放与旋转控制">
          <div className="wb-action-row">
            <LumnoButton
              variant="ghost"
              compact
              aria-label="缩小图片"
              disabled={zoomPercent <= 25}
              onClick={() => {
                setFitMode(false);
                setZoomPercent((prev) => Math.max(25, prev - 25));
              }}
            >
              -
            </LumnoButton>
            <span className="wb-badge wb-badge--muted wb-tabular">
              {zoomPercent}%
            </span>
            <LumnoButton
              variant="ghost"
              compact
              aria-label="放大图片"
              disabled={zoomPercent >= 300}
              onClick={() => {
                setFitMode(false);
                setZoomPercent((prev) => Math.min(300, prev + 25));
              }}
            >
              +
            </LumnoButton>
            <LumnoButton
              variant={fitMode ? "secondary" : "ghost"}
              compact
              aria-pressed={fitMode}
              onClick={() => {
                setFitMode(true);
                setZoomPercent(100);
              }}
            >
              适应
            </LumnoButton>
          </div>

          <div className="wb-action-row">
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-anticlockwise-line"
              onClick={() => setRotationDeg((prev) => prev - 90)}
            >
              左转 90°
            </LumnoButton>
            <LumnoButton
              variant="ghost"
              compact
              icon="ri-clockwise-line"
              onClick={() => setRotationDeg((prev) => prev + 90)}
            >
              右转 90°
            </LumnoButton>
          </div>
        </div>

        {originalError ? (
          <div className="wb-alert-item" data-severity="WARNING" role="alert">
            <strong>图片原件暂不可用</strong>
            <div>{originalError}</div>
            {renderFallbackActions(true)}
          </div>
        ) : (
          <div ref={imageStageRef} className="wb-preview-image-stage">
            {imageLayout.hasMeasured &&
            imageLayout.frameW !== null &&
            imageLayout.frameH !== null &&
            imageLayout.unrotatedW !== null &&
            imageLayout.unrotatedH !== null ? (
              <div
                className="wb-preview-image-frame"
                data-rotated-quarter={
                  imageLayout.isQuarterTurn ? "true" : "false"
                }
                style={{
                  width: `${imageLayout.frameW}px`,
                  height: `${imageLayout.frameH}px`,
                }}
              >
                <img
                  src={imageUrl}
                  alt={activeDoc.sanitized_filename}
                  className="wb-preview-image wb-preview-image--framed"
                  data-fit={fitMode ? "true" : "false"}
                  style={{
                    width: `${imageLayout.unrotatedW}px`,
                    height: `${imageLayout.unrotatedH}px`,
                    transform: `translate(-50%, -50%) rotate(${rotationDeg}deg)`,
                  }}
                  onLoad={(e) => {
                    const img = e.currentTarget as HTMLImageElement;
                    if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                      setNaturalSize({
                        w: img.naturalWidth,
                        h: img.naturalHeight,
                      });
                    }
                  }}
                  onError={() =>
                    setOriginalError(
                      "当前图片原件暂不可用或未保留本地原件，可尝试切换提取文本或下载原件。"
                    )
                  }
                />
              </div>
            ) : (
              <div
                className="wb-preview-image-frame wb-preview-image-frame--unmeasured"
                data-rotated-quarter={
                  imageLayout.isQuarterTurn ? "true" : "false"
                }
              >
                <img
                  src={imageUrl}
                  alt={activeDoc.sanitized_filename}
                  className="wb-preview-image"
                  data-fit={fitMode ? "true" : "false"}
                  style={{
                    transform: `rotate(${rotationDeg}deg) scale(${zoomPercent / 100})`,
                  }}
                  onLoad={(e) => {
                    const img = e.currentTarget as HTMLImageElement;
                    if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                      setNaturalSize({
                        w: img.naturalWidth,
                        h: img.naturalHeight,
                      });
                    }
                  }}
                  onError={() =>
                    setOriginalError(
                      "当前图片原件暂不可用或未保留本地原件，可尝试切换提取文本或下载原件。"
                    )
                  }
                />
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  const innerContent = (
    <div className="wb-preview-container">
      <div
        className={`wb-preview-header ${
          previewMode === "floating" ? "wb-preview-header--draggable" : ""
        }`}
        onPointerDown={handleFloatingHeaderPointerDown}
      >
        <div className="wb-preview-header-main">
          <span className="wb-pane-title">
            <i className="ri-icon ri-file-paper-2-line" aria-hidden="true" />
            <span>原件对照</span>
          </span>
          <label htmlFor="wb-preview-doc-select" className="wb-sr-only">
            切换预览单据
          </label>
          <select
            id="wb-preview-doc-select"
            name="preview_doc_id"
            aria-label="切换预览单据"
            className="wb-preview-select"
            value={activeDoc.id}
            onChange={(e) => onSelectDoc(e.target.value)}
          >
            {orderedDocs.map(({ doc, depth }) => {
              const statusHint =
                doc.status === "FAILED"
                  ? " [失败]"
                  : doc.status === "SKIPPED_NOISE"
                    ? " [已忽略]"
                    : "";
              const kindPrefix =
                doc.source_kind.toUpperCase() === "EML_BODY"
                  ? "邮件正文 · "
                  : "";
              return (
                <option key={doc.id} value={doc.id}>
                  {depth > 0 ? "↳ " : ""}
                  {kindPrefix}
                  {doc.sanitized_filename}
                  {statusHint}
                </option>
              );
            })}
          </select>
        </div>

        <div className="wb-preview-toolbar-actions">
          <LumnoButton
            variant={locked ? "primary" : "ghost"}
            compact
            icon={locked ? "ri-lock-fill" : "ri-lock-unlock-line"}
            aria-pressed={locked}
            title={
              locked
                ? "已锁定原件：切换费用记录时不自动跟随（点击解锁）"
                : "锁定当前原件：切换费用记录时不自动跟随"
            }
            onClick={onToggleLocked}
          >
            锁定原件
          </LumnoButton>

          <LumnoSegmentedControl<DocumentContentView>
            value={viewMode}
            ariaLabel="切换原件与提取文本视图"
            options={[
              { value: "original", label: "原件" },
              { value: "text", label: "文本" },
            ]}
            onChange={onChangeViewMode}
          />

          <LumnoButton
            variant="ghost"
            compact
            icon={
              previewMode === "docked"
                ? "ri-picture-in-picture-2-line"
                : "ri-layout-left-2-line"
            }
            onClick={onToggleMode}
          >
            {previewMode === "docked" ? "浮窗" : "停靠"}
          </LumnoButton>

          <a
            href={externalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="lumno-btn lumno-pill lumno-btn--ghost lumno-btn--compact"
            title="在新窗口中打开"
          >
            <span className="lumno-busy-label">
              <i className="ri-icon ri-external-link-line" aria-hidden="true" />
              <span>新窗口</span>
            </span>
          </a>

          <a
            href={downloadUrl}
            download={activeDoc.sanitized_filename}
            className="lumno-btn lumno-pill lumno-btn--ghost lumno-btn--compact"
            title="下载原件"
          >
            <span className="lumno-busy-label">
              <i className="ri-icon ri-download-2-line" aria-hidden="true" />
              <span>下载</span>
            </span>
          </a>

          <LumnoButton
            variant="ghost"
            compact
            icon="ri-close-line"
            aria-label="关闭原件对照预览"
            onClick={onClose}
          >
            关闭
          </LumnoButton>
        </div>
      </div>

      {emlFamily.tabs.length > 0 ? (
        <div
          className="wb-preview-tabbar"
          role="tablist"
          aria-label="邮件正文与附件切换"
        >
          {emlFamily.tabs.map((tab) => {
            const active = tab.doc.id === activeDoc.id;
            const statusTag =
              tab.doc.status === "FAILED"
                ? " · 失败"
                : tab.doc.status === "SKIPPED_NOISE"
                  ? " · 已忽略"
                  : "";
            return (
              <button
                key={tab.doc.id}
                type="button"
                role="tab"
                aria-selected={active}
                data-active={active ? "true" : "false"}
                className="wb-preview-tab"
                onClick={() => onSelectDoc(tab.doc.id)}
              >
                <span>{tab.label}</span>
                {statusTag ? (
                  <span className="wb-helper-note">{statusTag}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}

      {activeDoc.status === "SKIPPED_NOISE" || activeDoc.status === "FAILED" ? (
        <div className="wb-preview-status-strip">
          <span
            className={`wb-badge ${
              activeDoc.status === "FAILED"
                ? "wb-badge--error"
                : "wb-badge--muted"
            }`}
          >
            {activeDoc.status === "FAILED"
              ? "该文件解析失败"
              : "已自动忽略的非计费图片（可在此确认后决定是否强制识别）"}
          </span>
          {activeDoc.error_message ? (
            <span className="wb-helper-note">{activeDoc.error_message}</span>
          ) : null}
        </div>
      ) : null}

      <div className="wb-preview-body">{renderBody()}</div>
    </div>
  );

  if (previewMode === "floating") {
    return (
      <aside
        className="wb-floating-preview"
        aria-label="原件对照预览"
        style={{
          left: `${floatRect.left}px`,
          top: `${floatRect.top}px`,
          width: `${floatRect.width}px`,
          height: `${floatRect.height}px`,
        }}
      >
        {innerContent}
        <div
          role="presentation"
          data-resize-handle="se"
          title="拖拽调整浮窗尺寸"
          className="wb-preview-resize-handle"
          onPointerDown={handleFloatingResizePointerDown}
        />
      </aside>
    );
  }

  return innerContent;
}
