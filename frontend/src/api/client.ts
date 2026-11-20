import {
  copyDecimalMeta,
  parseLosslessJson,
  rawDecimal,
  RawDecimalLiteral,
  stringifyLosslessJson,
  toSchemaDecimal,
} from "./decimalJson.ts";
import { createDevStubTransport, type FetchLike } from "./devStubTransport.ts";
import { ACTIVE_ISO4217_CURRENCY_SET } from "./iso4217.ts";
import type { components, operations } from "./schema";

export type ErrorResponse = components["schemas"]["ErrorResponse"];
export type HealthStatus = components["schemas"]["HealthStatus"];
export type AccountingEntity = components["schemas"]["AccountingEntity"];
export type AccountingEntityUpsert =
  components["schemas"]["AccountingEntityUpsert"];
export type ChargeDictionaryEntry =
  components["schemas"]["ChargeDictionaryEntry"];
export type ChargeDictionaryCreate =
  components["schemas"]["ChargeDictionaryCreate"];
export type ExistingBill = components["schemas"]["ExistingBill"];
export type ExistingBillImportRequest =
  components["schemas"]["ExistingBillImportRequest"];
export type ExistingBillImportResult =
  components["schemas"]["ExistingBillImportResult"];
export type BatchCounts = components["schemas"]["BatchCounts"];
export type BatchSummary = components["schemas"]["BatchSummary"];
export type ShipmentRefs = components["schemas"]["ShipmentRefs"];
export type SourceDocumentSummary =
  components["schemas"]["SourceDocumentSummary"];
export type FailedDocumentAlert = components["schemas"]["FailedDocumentAlert"];
export type ValidationAlert = components["schemas"]["ValidationAlert"];
export type FieldDiff = components["schemas"]["FieldDiff"];
export type CandidateBillOption = components["schemas"]["CandidateBillOption"];
export type BillingRecordDetail = components["schemas"]["BillingRecordDetail"];
export type BatchDetail = components["schemas"]["BatchDetail"];
export type SwitchEntityResponse =
  components["schemas"]["SwitchEntityResponse"];
export type ManualRecordCreate = components["schemas"]["ManualRecordCreate"];
export type DocumentTotalsPatchRequest =
  components["schemas"]["DocumentTotalsPatchRequest"];
export type ReviewBundleV1 = components["schemas"]["ReviewBundleV1"];
export type RecordPatchRequest = components["schemas"]["RecordPatchRequest"];
export type StandardOutputRecord =
  components["schemas"]["StandardOutputRecord"];
export type ExportResponse = components["schemas"]["ExportResponse"];
export type SyncBaselineResult = components["schemas"]["SyncBaselineResult"];
export type EvalRunSummary = components["schemas"]["EvalRunSummary"];

// C1: Directly derive inline operation request bodies from schema.d.ts operations[...]
export type ApproveRecordRequest =
  operations["approveBillingRecord"]["requestBody"]["content"]["application/json"];
export type RejectRecordRequest =
  operations["rejectBillingRecord"]["requestBody"]["content"]["application/json"];
export type TriggerEvalRunRequest =
  operations["triggerEvalRun"]["requestBody"]["content"]["application/json"];
export type SwitchBatchEntityRequest =
  operations["switchBatchAccountingEntity"]["requestBody"]["content"]["application/json"];
export type CreateDemoBatchRequest = NonNullable<
  operations["createDemoBatch"]["requestBody"]
>["content"]["application/json"];
export type SyncBaselineRequest = NonNullable<
  operations["syncBatchToBaseline"]["requestBody"]
>["content"]["application/json"];

export type OcrMode = BatchSummary["ocr_mode"];
export type ExportMode = ExportResponse["mode"];
export type DecimalInput = string | number | RawDecimalLiteral;

export type RecordPatchInput = Omit<
  RecordPatchRequest,
  "quantity" | "unit_price" | "amount" | "force_create_new"
> & {
  quantity?: DecimalInput | null;
  unit_price?: DecimalInput | null;
  amount?: DecimalInput | null;
  force_create_new?: boolean;
};

export const MAX_SINGLE_FILE_BYTES = 50 * 1024 * 1024;
export const MAX_BATCH_ITEMS_COUNT = 50;
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
export const UPLOAD_REQUEST_TIMEOUT_MS = 90_000;

export const ALLOWED_UPLOAD_EXTENSIONS = [
  ".eml",
  ".pdf",
  ".jpg",
  ".jpeg",
  ".png",
  ".tif",
  ".tiff",
  ".xls",
  ".xlsx",
  ".doc",
  ".docx",
] as const;

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;
  readonly requestId?: string | null;

  constructor(args: {
    status: number;
    code: string;
    message: string;
    details?: Record<string, unknown>;
    requestId?: string | null;
    cause?: unknown;
  }) {
    super(
      args.message,
      args.cause !== undefined ? { cause: args.cause } : undefined
    );
    this.name = "ApiClientError";
    this.status = args.status;
    this.code = args.code;
    this.details = args.details;
    this.requestId = args.requestId;
  }
}

export interface StagedFileLike {
  name: string;
  size: number;
}

export interface UrlLineError {
  index: number;
  line: string;
  message: string;
}

export interface BatchIngestionValidationResult {
  valid: boolean;
  fileErrors: Record<string, string>;
  urlErrors: UrlLineError[];
  parsedUrls: string[];
  existingBillsError: string | null;
  summaryErrors: string[];
}

export function getStagedFileKey(file: StagedFileLike, index: number): string {
  return `${file.name}::${file.size}::${index}`;
}

export function isAllowedUploadFilename(filename: string): boolean {
  const lower = filename.trim().toLowerCase();
  return ALLOWED_UPLOAD_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export function isValidPublicHttpUrl(raw: string): boolean {
  try {
    const url = new URL(raw.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function isValidIso4217Currency(code: unknown): boolean {
  return (
    typeof code === "string" && ACTIVE_ISO4217_CURRENCY_SET.has(code.trim())
  );
}

function isPlainObject(val: unknown): val is Record<string, unknown> {
  return Boolean(val) && typeof val === "object" && !Array.isArray(val);
}

export function hasValidDecimalNumber(val: unknown): boolean {
  if (val instanceof RawDecimalLiteral) return true;
  if (typeof val === "number") return Number.isFinite(val);
  if (typeof val === "string") {
    try {
      rawDecimal(val);
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

function countUnicodePoints(str: string): number {
  return Array.from(str.trim()).length;
}

export function validateExistingBillsJsonText(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = parseLosslessJson<unknown>(trimmed);
  } catch {
    return "随批已有账单 JSON 语法无效，请检查括号与逗号";
  }
  if (!Array.isArray(parsed)) {
    return "随批已有账单 JSON 必须为 JSON 数组（[...]），禁止使用对象包装";
  }
  const seenIds = new Set<string>();
  for (let i = 0; i < parsed.length; i++) {
    const item = parsed[i];
    const rowNo = i + 1;
    if (!isPlainObject(item)) {
      return `第 ${rowNo} 条随批账单必须是 JSON 对象`;
    }
    const id = typeof item.id === "string" ? item.id.trim() : "";
    if (!id) {
      return `第 ${rowNo} 条随批账单缺少非空字符串 id`;
    }
    if (seenIds.has(id)) {
      return `第 ${rowNo} 条随批账单 id "${id}" 重复`;
    }
    seenIds.add(id);

    const entity =
      typeof item.accounting_entity === "string"
        ? item.accounting_entity.trim()
        : "";
    const entityLen = countUnicodePoints(entity);
    if (entityLen < 1 || entityLen > 120) {
      return `第 ${rowNo} 条随批账单缺少 1–120 字符 accounting_entity`;
    }
    if (typeof item.charge_item !== "string" || !item.charge_item.trim()) {
      return `第 ${rowNo} 条随批账单缺少非空 charge_item`;
    }
    if (!hasValidDecimalNumber(item.amount)) {
      return `第 ${rowNo} 条随批账单缺少合法数值 amount`;
    }
    if (
      typeof item.currency !== "string" ||
      !isValidIso4217Currency(item.currency)
    ) {
      return `第 ${rowNo} 条随批账单币种须为有效 3 位大写 ISO 4217 代码`;
    }
    if (item.posting_type !== "AR" && item.posting_type !== "AP") {
      return `第 ${rowNo} 条随批账单 posting_type 须为 "AR" 或 "AP"`;
    }
  }
  return null;
}

export function validateBatchIngestionInput(args: {
  accountingEntity: string;
  files: ReadonlyArray<StagedFileLike>;
  publicUrlLinesText: string;
  existingBillsJsonText?: string;
}): BatchIngestionValidationResult {
  const fileErrors: Record<string, string> = {};
  const urlErrors: UrlLineError[] = [];
  const parsedUrls: string[] = [];
  const summaryErrors: string[] = [];

  const entityLen = countUnicodePoints(args.accountingEntity);
  if (entityLen < 1 || entityLen > 120) {
    summaryErrors.push("记账主体名称须为 1–120 个字符");
  }

  args.files.forEach((file, idx) => {
    const key = getStagedFileKey(file, idx);
    if (!isAllowedUploadFilename(file.name)) {
      fileErrors[key] =
        "不支持的文件扩展名（仅支持 .eml/.pdf/.jpg/.jpeg/.png/.tif/.tiff/.xls/.xlsx/.doc/.docx）";
      return;
    }
    if (file.size <= 0) {
      fileErrors[key] = "文件大小为 0 字节，无法解析，请移除或替换";
      return;
    }
    if (file.size > MAX_SINGLE_FILE_BYTES) {
      const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
      fileErrors[key] = `文件大小 ${sizeMb} MiB 超过 50 MiB 单文件上限`;
    }
  });

  const lines = args.publicUrlLinesText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  lines.forEach((line, index) => {
    if (!isValidPublicHttpUrl(line)) {
      urlErrors.push({
        index,
        line,
        message: `第 ${index + 1} 行 URL 非法（须以 http:// 或 https:// 开头）`,
      });
    } else {
      parsedUrls.push(line);
    }
  });

  const totalItems = args.files.length + lines.length;
  if (totalItems === 0) {
    summaryErrors.push("请至少添加一个待解析单据文件或公网 URL");
  } else if (totalItems > MAX_BATCH_ITEMS_COUNT) {
    summaryErrors.push(
      `当前共 ${totalItems} 项，超出单批最多 ${MAX_BATCH_ITEMS_COUNT} 项（文件 + URL）上限`
    );
  }

  const existingBillsError = args.existingBillsJsonText
    ? validateExistingBillsJsonText(args.existingBillsJsonText)
    : null;

  const valid =
    summaryErrors.length === 0 &&
    Object.keys(fileErrors).length === 0 &&
    urlErrors.length === 0 &&
    existingBillsError === null;

  return {
    valid,
    fileErrors,
    urlErrors,
    parsedUrls,
    existingBillsError,
    summaryErrors,
  };
}

export function isStubModeActive(): boolean {
  if (typeof window !== "undefined") {
    const params = new URLSearchParams(window.location.search);
    if (params.get("mode") === "stub") {
      return true;
    }
  }
  const envVal = (
    import.meta as unknown as { env?: Record<string, string | undefined> }
  ).env?.VITE_API_STUB;
  return envVal === "1";
}

function normalizeDecimalField(
  val: DecimalInput | null | undefined
): number | null | undefined {
  if (val === undefined) {
    return undefined;
  }
  if (val === null) {
    return null;
  }
  return toSchemaDecimal(val);
}

export function normalizeShipmentRefs(
  raw: ShipmentRefs | undefined
): ShipmentRefs {
  if (raw === undefined) {
    return {
      mbl_nos: [],
      hbl_no: null,
      booking_no: null,
      container_nos: [],
    };
  }
  if (!isPlainObject(raw)) {
    return raw as unknown as ShipmentRefs;
  }
  return {
    mbl_nos: raw.mbl_nos === undefined ? [] : raw.mbl_nos,
    hbl_no: raw.hbl_no ?? null,
    booking_no: raw.booking_no ?? null,
    container_nos: raw.container_nos === undefined ? [] : raw.container_nos,
  };
}

export function normalizeSourceEvidence(
  raw: Record<string, unknown> | undefined
): Record<string, unknown> {
  if (raw === undefined) {
    return { source_paths: [] };
  }
  if (!isPlainObject(raw)) {
    return raw as unknown as Record<string, unknown>;
  }
  return copyDecimalMeta(raw, {
    ...raw,
    source_paths: raw.source_paths === undefined ? [] : raw.source_paths,
  });
}

export function normalizeSourceDocument(
  raw: SourceDocumentSummary
): SourceDocumentSummary {
  const rawRecord = raw as unknown as Record<string, unknown>;
  const hasExtractionSnapshot =
    rawRecord.extraction_snapshot !== undefined &&
    rawRecord.extraction_snapshot !== null;
  const normalized: Record<string, unknown> = {
    ...raw,
    parent_doc_id: raw.parent_doc_id ?? null,
    error_message: raw.error_message ?? null,
    affected_record_ids:
      raw.affected_record_ids === undefined ? [] : raw.affected_record_ids,
  };
  if (raw.excluded_clauses !== undefined) {
    normalized.excluded_clauses = raw.excluded_clauses;
  } else if (!hasExtractionSnapshot) {
    normalized.excluded_clauses = [];
  }
  if (raw.shipment_refs !== undefined) {
    normalized.shipment_refs = normalizeShipmentRefs(raw.shipment_refs);
  } else if (!hasExtractionSnapshot) {
    normalized.shipment_refs = normalizeShipmentRefs(undefined);
  }
  if (!hasExtractionSnapshot || raw.subtotal_amount !== undefined) {
    normalized.subtotal_amount = raw.subtotal_amount ?? null;
  }
  if (!hasExtractionSnapshot || raw.tax_amount !== undefined) {
    normalized.tax_amount = raw.tax_amount ?? null;
  }
  if (!hasExtractionSnapshot || raw.total_amount !== undefined) {
    normalized.total_amount = raw.total_amount ?? null;
  }
  if (!hasExtractionSnapshot || raw.total_currency !== undefined) {
    normalized.total_currency = raw.total_currency ?? null;
  }
  return copyDecimalMeta(raw, normalized as unknown as SourceDocumentSummary);
}

export function normalizeBillingRecord(
  raw: BillingRecordDetail
): BillingRecordDetail {
  const safeDocInfo =
    raw.document_info && typeof raw.document_info === "object"
      ? raw.document_info
      : { document_type: null, reference_no: null, document_date: null };
  const safeParty =
    raw.transaction_party && typeof raw.transaction_party === "object"
      ? raw.transaction_party
      : { issuer: null, recipient: null };
  const safeCharge = copyDecimalMeta(
    raw.charge_details,
    raw.charge_details && typeof raw.charge_details === "object"
      ? {
          ...raw.charge_details,
          charge_item: raw.charge_details.charge_item ?? null,
          raw_charge_name: raw.charge_details.raw_charge_name ?? null,
          quantity: raw.charge_details.quantity ?? null,
          unit_price: raw.charge_details.unit_price ?? null,
          amount: raw.charge_details.amount ?? null,
          currency: raw.charge_details.currency ?? null,
          payment_term: raw.charge_details.payment_term ?? null,
        }
      : {
          charge_item: null,
          raw_charge_name: null,
          quantity: null,
          unit_price: null,
          amount: null,
          currency: null,
          payment_term: null,
        }
  );
  const safeFinancial =
    raw.financial_attributes && typeof raw.financial_attributes === "object"
      ? {
          accounting_entity: raw.financial_attributes.accounting_entity ?? "",
          posting_type: raw.financial_attributes.posting_type ?? null,
        }
      : { accounting_entity: "", posting_type: null };

  return copyDecimalMeta(raw, {
    ...raw,
    matched_existing_bill_id: raw.matched_existing_bill_id ?? null,
    matched_bill_ref: raw.matched_bill_ref ?? null,
    inherited_fields:
      raw.inherited_fields === undefined ? [] : raw.inherited_fields,
    candidate_bills:
      raw.candidate_bills === undefined
        ? []
        : Array.isArray(raw.candidate_bills)
          ? raw.candidate_bills.map((cand) => copyDecimalMeta(cand, { ...cand }))
          : raw.candidate_bills,
    document_info: {
      document_type: safeDocInfo.document_type ?? null,
      reference_no: safeDocInfo.reference_no ?? null,
      document_date: safeDocInfo.document_date ?? null,
    },
    transaction_party: {
      issuer: safeParty.issuer ?? null,
      recipient: safeParty.recipient ?? null,
    },
    charge_details: safeCharge,
    financial_attributes: safeFinancial,
    shipment_refs: normalizeShipmentRefs(raw.shipment_refs),
    source_evidence: normalizeSourceEvidence(raw.source_evidence),
    field_diffs:
      raw.field_diffs === undefined
        ? []
        : Array.isArray(raw.field_diffs)
          ? raw.field_diffs.map((diff) => copyDecimalMeta(diff, { ...diff }))
          : raw.field_diffs,
    validation_alerts:
      raw.validation_alerts === undefined ? [] : raw.validation_alerts,
    ai_reasoning: raw.ai_reasoning === undefined ? [] : raw.ai_reasoning,
    human_overrides:
      raw.human_overrides === undefined
        ? {}
        : isPlainObject(raw.human_overrides)
          ? copyDecimalMeta(raw.human_overrides, { ...raw.human_overrides })
          : raw.human_overrides,
    reject_reason: raw.reject_reason ?? null,
  });
}

export function normalizeBatchSummary(raw: BatchSummary): BatchSummary {
  const counts =
    raw.counts && typeof raw.counts === "object"
      ? raw.counts
      : {
          create: 0,
          update: 0,
          no_change: 0,
          pending_review: 0,
          rejected: 0,
        };
  return {
    ...raw,
    stage_message: raw.stage_message ?? null,
    counts: {
      create: counts.create ?? 0,
      update: counts.update ?? 0,
      no_change: counts.no_change ?? 0,
      pending_review: counts.pending_review ?? 0,
      rejected: counts.rejected ?? 0,
    },
  };
}

export function normalizeBatchDetail(raw: BatchDetail): BatchDetail {
  const summary = normalizeBatchSummary(raw);
  return {
    ...summary,
    documents: Array.isArray(raw.documents)
      ? raw.documents.map(normalizeSourceDocument)
      : [],
    failed_documents: Array.isArray(raw.failed_documents)
      ? raw.failed_documents.map((f) => ({
          ...f,
          affected_record_ids: Array.isArray(f.affected_record_ids)
            ? f.affected_record_ids
            : [],
        }))
      : [],
    records: Array.isArray(raw.records)
      ? raw.records.map(normalizeBillingRecord)
      : [],
  };
}

export function normalizeAccountingEntity(
  raw: AccountingEntity
): AccountingEntity {
  return {
    ...raw,
    aliases: Array.isArray(raw.aliases) ? raw.aliases : [],
    is_default: Boolean(raw.is_default),
  };
}

export function normalizeExistingBill(raw: ExistingBill): ExistingBill {
  return copyDecimalMeta(raw, {
    ...raw,
    container_nos: Array.isArray(raw.container_nos) ? raw.container_nos : [],
  });
}

export function normalizeExportResponse(raw: ExportResponse): ExportResponse {
  return {
    ...raw,
    records: Array.isArray(raw.records)
      ? raw.records.map((rec) =>
          copyDecimalMeta(rec, {
            ...rec,
            charge_details: copyDecimalMeta(rec.charge_details, {
              ...rec.charge_details,
            }),
            audit_trail: {
              ...rec.audit_trail,
              ai_reasoning: Array.isArray(rec.audit_trail?.ai_reasoning)
                ? rec.audit_trail.ai_reasoning
                : [],
            },
          })
        )
      : [],
  };
}

function buildRecordPatchPayload(
  patch: RecordPatchInput
): Partial<RecordPatchRequest> {
  const payload: Partial<RecordPatchRequest> = {};
  if (patch.force_create_new !== undefined) {
    payload.force_create_new = patch.force_create_new;
  }
  if (patch.charge_item !== undefined) payload.charge_item = patch.charge_item;
  if (patch.quantity !== undefined) {
    payload.quantity = normalizeDecimalField(patch.quantity);
  }
  if (patch.unit_price !== undefined) {
    payload.unit_price = normalizeDecimalField(patch.unit_price);
  }
  if (patch.amount !== undefined) {
    payload.amount = normalizeDecimalField(patch.amount);
  }
  if (patch.currency !== undefined) payload.currency = patch.currency;
  if (patch.payment_term !== undefined) {
    payload.payment_term = patch.payment_term;
  }
  if (patch.posting_type !== undefined) {
    payload.posting_type = patch.posting_type;
  }
  if (patch.document_type !== undefined) {
    payload.document_type = patch.document_type;
  }
  if (patch.reference_no !== undefined) {
    payload.reference_no = patch.reference_no;
  }
  if (patch.document_date !== undefined) {
    payload.document_date = patch.document_date;
  }
  if (patch.issuer !== undefined) payload.issuer = patch.issuer;
  if (patch.recipient !== undefined) payload.recipient = patch.recipient;
  if (patch.mbl_no !== undefined) payload.mbl_no = patch.mbl_no;
  if (patch.hbl_no !== undefined) payload.hbl_no = patch.hbl_no;
  if (patch.booking_no !== undefined) payload.booking_no = patch.booking_no;
  if (patch.container_nos !== undefined) {
    payload.container_nos = patch.container_nos;
  }
  if (patch.matched_existing_bill_id !== undefined) {
    payload.matched_existing_bill_id = patch.matched_existing_bill_id;
  }
  if (patch.review_note !== undefined) payload.review_note = patch.review_note;
  return payload;
}

export interface CreateBatchInput {
  accounting_entity: string;
  ocr_mode?: OcrMode;
  files?: ReadonlyArray<File | Blob>;
  public_urls?: ReadonlyArray<string>;
  existing_bills_json?: string;
  wait?: boolean;
}

export interface RetryDocumentInput {
  ocr_mode?: OcrMode;
  public_url?: string;
  file?: File | Blob;
}

export interface BillingApiClientOptions {
  baseUrl?: string;
  transport?: FetchLike;
}

/**
 * 统一账单工作台 HTTP 客户端：
 * - 所有 JSON 出入站严格走 decimalJson 无损编解码；
 * - 写请求超时后不自动重试，避免非幂等写重放；读取类失败由调用方按需刷新。
 */
export class BillingApiClient {
  private readonly baseUrl: string;
  private readonly transport: FetchLike;
  readonly isStubMode: boolean;

  constructor(options: BillingApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    if (options.transport) {
      this.transport = options.transport;
      this.isStubMode = false;
    } else if (isStubModeActive()) {
      this.transport = createDevStubTransport();
      this.isStubMode = true;
    } else {
      this.transport = (input, init) => fetch(input, init);
      this.isStubMode = false;
    }
  }

  private async requestRaw(
    path: string,
    options: {
      method?: string;
      jsonBody?: unknown;
      formData?: FormData;
      headers?: Record<string, string>;
      timeoutMs?: number;
      signal?: AbortSignal;
    } = {}
  ): Promise<{ status: number; text: string }> {
    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    const onExternalAbort = () => controller.abort();
    if (options.signal) {
      if (options.signal.aborted) {
        clearTimeout(timer);
        throw new ApiClientError({
          status: 0,
          code: "REQUEST_ABORTED",
          message: "请求已取消",
        });
      }
      options.signal.addEventListener("abort", onExternalAbort, { once: true });
    }

    const headers: Record<string, string> = { ...(options.headers ?? {}) };
    let body: BodyInit | undefined;
    if (options.formData) {
      body = options.formData;
    } else if (options.jsonBody !== undefined) {
      headers["Content-Type"] = "application/json";
      body = stringifyLosslessJson(options.jsonBody);
    }

    try {
      const response = await this.transport(`${this.baseUrl}${path}`, {
        method: options.method ?? "GET",
        headers,
        body,
        signal: controller.signal,
      });

      const text = await response.text();
      if (!response.ok) {
        let errPayload: ErrorResponse | null = null;
        if (text.trim()) {
          try {
            errPayload = parseLosslessJson<ErrorResponse>(text);
          } catch {
            errPayload = null;
          }
        }
        throw new ApiClientError({
          status: response.status,
          code: errPayload?.code ?? `HTTP_${response.status}`,
          message:
            errPayload?.message ??
            `服务返回异常状态（${response.status}），请稍后重试`,
          details: errPayload?.details,
          requestId: errPayload?.request_id ?? null,
        });
      }

      return { status: response.status, text };
    } catch (err) {
      if (err instanceof ApiClientError) {
        throw err;
      }
      if (timedOut) {
        throw new ApiClientError({
          status: 408,
          code: "REQUEST_TIMEOUT",
          message: "请求超时，若刚才执行了保存或提交，请先刷新确认最新状态",
          cause: err,
        });
      }
      if (
        options.signal?.aborted ||
        (err instanceof DOMException && err.name === "AbortError")
      ) {
        throw new ApiClientError({
          status: 0,
          code: "REQUEST_ABORTED",
          message: "请求已取消",
          cause: err,
        });
      }
      throw new ApiClientError({
        status: 0,
        code: "NETWORK_UNREACHABLE",
        message: "无法连接到账单解析后端服务，请检查本地 API 服务状态或网络配置",
        cause: err,
      });
    } finally {
      clearTimeout(timer);
      if (options.signal) {
        options.signal.removeEventListener("abort", onExternalAbort);
      }
    }
  }

  private async requestJson<T>(
    path: string,
    options: {
      method?: string;
      jsonBody?: unknown;
      formData?: FormData;
      headers?: Record<string, string>;
      timeoutMs?: number;
      signal?: AbortSignal;
    } = {}
  ): Promise<T> {
    const { text } = await this.requestRaw(path, options);
    if (!text.trim()) {
      return undefined as T;
    }
    return parseLosslessJson<T>(text);
  }

  getHealth(signal?: AbortSignal): Promise<HealthStatus> {
    return this.requestJson<HealthStatus>("/api/v1/health", { signal });
  }

  async listAccountingEntities(
    signal?: AbortSignal
  ): Promise<AccountingEntity[]> {
    const res = await this.requestJson<AccountingEntity[]>("/api/v1/entities", {
      signal,
    });
    return Array.isArray(res) ? res.map(normalizeAccountingEntity) : [];
  }

  async createAccountingEntity(
    payload: AccountingEntityUpsert
  ): Promise<AccountingEntity> {
    const res = await this.requestJson<AccountingEntity>("/api/v1/entities", {
      method: "POST",
      jsonBody: payload,
    });
    return normalizeAccountingEntity(res);
  }

  async updateAccountingEntity(
    entityId: string,
    payload: AccountingEntityUpsert
  ): Promise<AccountingEntity> {
    const res = await this.requestJson<AccountingEntity>(
      `/api/v1/entities/${encodeURIComponent(entityId)}`,
      {
        method: "PUT",
        jsonBody: payload,
      }
    );
    return normalizeAccountingEntity(res);
  }

  listChargeDictionary(
    signal?: AbortSignal
  ): Promise<ChargeDictionaryEntry[]> {
    return this.requestJson<ChargeDictionaryEntry[]>("/api/v1/dictionary", {
      signal,
    });
  }

  upsertChargeDictionaryEntry(
    payload: ChargeDictionaryCreate
  ): Promise<ChargeDictionaryEntry> {
    return this.requestJson<ChargeDictionaryEntry>("/api/v1/dictionary", {
      method: "POST",
      jsonBody: payload,
    });
  }

  async deleteChargeDictionaryEntry(entryId: string): Promise<void> {
    await this.requestRaw(
      `/api/v1/dictionary/${encodeURIComponent(entryId)}`,
      { method: "DELETE" }
    );
  }

  async listExistingBills(
    accountingEntity?: string,
    signal?: AbortSignal
  ): Promise<ExistingBill[]> {
    const query = accountingEntity
      ? `?accounting_entity=${encodeURIComponent(accountingEntity)}`
      : "";
    const res = await this.requestJson<ExistingBill[]>(
      `/api/v1/existing-bills${query}`,
      { signal }
    );
    return Array.isArray(res) ? res.map(normalizeExistingBill) : [];
  }

  async clearExistingBills(accountingEntity?: string): Promise<void> {
    const query = accountingEntity
      ? `?accounting_entity=${encodeURIComponent(accountingEntity)}`
      : "";
    await this.requestRaw(`/api/v1/existing-bills${query}`, {
      method: "DELETE",
    });
  }

  importExistingBills(
    payload: ExistingBillImportRequest
  ): Promise<ExistingBillImportResult> {
    return this.requestJson<ExistingBillImportResult>(
      "/api/v1/existing-bills/import",
      {
        method: "POST",
        jsonBody: payload,
      }
    );
  }

  async listBatches(limit = 20, signal?: AbortSignal): Promise<BatchSummary[]> {
    const res = await this.requestJson<BatchSummary[]>(
      `/api/v1/batches?limit=${encodeURIComponent(String(limit))}`,
      { signal }
    );
    return Array.isArray(res) ? res.map(normalizeBatchSummary) : [];
  }

  async createBatch(
    input: CreateBatchInput
  ): Promise<BatchSummary | BatchDetail> {
    const wait = input.wait ?? false;
    const ocrMode = input.ocr_mode ?? "REMOTE_ONLY";
    const formData = new FormData();
    formData.append("accounting_entity", input.accounting_entity.trim());
    formData.append("ocr_mode", ocrMode);

    if (input.public_urls && input.public_urls.length > 0) {
      formData.append(
        "public_urls_json",
        stringifyLosslessJson([...input.public_urls])
      );
    }
    if (input.existing_bills_json && input.existing_bills_json.trim()) {
      formData.append("existing_bills_json", input.existing_bills_json.trim());
    }
    if (input.files) {
      for (const file of input.files) {
        const filename = file instanceof File ? file.name : "upload.bin";
        formData.append("files", file, filename);
      }
    }

    const res = await this.requestJson<BatchSummary | BatchDetail>(
      `/api/v1/batches?wait=${wait ? "true" : "false"}`,
      {
        method: "POST",
        formData,
        timeoutMs: UPLOAD_REQUEST_TIMEOUT_MS,
      }
    );
    return "records" in res
      ? normalizeBatchDetail(res)
      : normalizeBatchSummary(res);
  }

  async createDemoBatch(args: {
    accounting_entity: string;
    ocr_mode?: OcrMode;
    wait?: boolean;
  }): Promise<BatchSummary | BatchDetail> {
    const wait = args.wait ?? false;
    const body: CreateDemoBatchRequest = {
      accounting_entity: args.accounting_entity.trim(),
      ocr_mode: args.ocr_mode ?? "REMOTE_ONLY",
    };
    const res = await this.requestJson<BatchSummary | BatchDetail>(
      `/api/v1/batches/demo?wait=${wait ? "true" : "false"}`,
      {
        method: "POST",
        jsonBody: body,
      }
    );
    return "records" in res
      ? normalizeBatchDetail(res)
      : normalizeBatchSummary(res);
  }

  async importReviewBundle(bundle: ReviewBundleV1): Promise<BatchDetail> {
    const res = await this.requestJson<BatchDetail>(
      "/api/v1/batches/import-review-bundle",
      {
        method: "POST",
        jsonBody: bundle,
      }
    );
    return normalizeBatchDetail(res);
  }

  async getBatchDetail(
    batchId: string,
    signal?: AbortSignal
  ): Promise<BatchDetail> {
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}`,
      { signal }
    );
    return normalizeBatchDetail(res);
  }

  async switchBatchAccountingEntity(
    batchId: string,
    accountingEntity: string
  ): Promise<SwitchEntityResponse> {
    const body: SwitchBatchEntityRequest = {
      accounting_entity: accountingEntity.trim(),
    };
    const res = await this.requestJson<SwitchEntityResponse>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/switch-entity`,
      {
        method: "POST",
        jsonBody: body,
      }
    );
    return {
      ...res,
      batch: normalizeBatchDetail(res.batch),
    };
  }

  async reapplyBatchChargeDictionary(batchId: string): Promise<BatchDetail> {
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/reapply-dictionary`,
      { method: "POST" }
    );
    return normalizeBatchDetail(res);
  }

  async retryOrReplaceDocument(
    batchId: string,
    docId: string,
    input: RetryDocumentInput = {}
  ): Promise<BatchDetail> {
    const trimmedUrl = input.public_url?.trim() || undefined;
    if (input.file && trimmedUrl) {
      throw new ApiClientError({
        status: 400,
        code: "CONFLICTING_SOURCE_INPUTS",
        message: "替换文件与公网 URL 互斥，请仅提供一种重试来源",
      });
    }
    const formData = new FormData();
    if (input.ocr_mode) {
      formData.append("ocr_mode", input.ocr_mode);
    }
    if (trimmedUrl) {
      formData.append("public_url", trimmedUrl);
    }
    if (input.file) {
      const name = input.file instanceof File ? input.file.name : "replace.bin";
      formData.append("file", input.file, name);
    }
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}/retry`,
      {
        method: "POST",
        formData,
        timeoutMs: UPLOAD_REQUEST_TIMEOUT_MS,
      }
    );
    return normalizeBatchDetail(res);
  }

  async patchDocumentTotals(
    batchId: string,
    docId: string,
    patch: {
      subtotal_amount?: DecimalInput | null;
      tax_amount?: DecimalInput | null;
      total_amount?: DecimalInput | null;
      total_currency?: string | null;
      review_note?: string | null;
    }
  ): Promise<BatchDetail> {
    const payload: DocumentTotalsPatchRequest = {};
    if (patch.subtotal_amount !== undefined) {
      payload.subtotal_amount = normalizeDecimalField(patch.subtotal_amount);
    }
    if (patch.tax_amount !== undefined) {
      payload.tax_amount = normalizeDecimalField(patch.tax_amount);
    }
    if (patch.total_amount !== undefined) {
      payload.total_amount = normalizeDecimalField(patch.total_amount);
    }
    if (patch.total_currency !== undefined) {
      payload.total_currency = patch.total_currency;
    }
    if (patch.review_note !== undefined) {
      payload.review_note = patch.review_note;
    }
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}`,
      {
        method: "PATCH",
        jsonBody: payload,
      }
    );
    return normalizeBatchDetail(res);
  }

  async forceParseSkippedDocument(
    batchId: string,
    docId: string
  ): Promise<BatchDetail> {
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}/force-parse`,
      { method: "POST" }
    );
    return normalizeBatchDetail(res);
  }

  async addManualBillingRecord(
    batchId: string,
    record: Omit<ManualRecordCreate, "amount"> & { amount: DecimalInput }
  ): Promise<BatchDetail> {
    const payload: ManualRecordCreate = {
      ...record,
      amount: toSchemaDecimal(record.amount),
    };
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records`,
      {
        method: "POST",
        jsonBody: payload,
      }
    );
    return normalizeBatchDetail(res);
  }

  async patchBillingRecord(
    batchId: string,
    recordId: string,
    patch: RecordPatchInput
  ): Promise<BatchDetail> {
    const payload = buildRecordPatchPayload(patch);
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}`,
      {
        method: "PATCH",
        jsonBody: payload,
      }
    );
    return normalizeBatchDetail(res);
  }

  async approveBillingRecord(
    batchId: string,
    recordId: string,
    reason: string
  ): Promise<BatchDetail> {
    const body: ApproveRecordRequest = { reason };
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}/approve`,
      {
        method: "POST",
        jsonBody: body,
      }
    );
    return normalizeBatchDetail(res);
  }

  async rejectBillingRecord(
    batchId: string,
    recordId: string,
    reason: string
  ): Promise<BatchDetail> {
    const body: RejectRecordRequest = { reason };
    const res = await this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}/reject`,
      {
        method: "POST",
        jsonBody: body,
      }
    );
    return normalizeBatchDetail(res);
  }

  async exportBatchJson(
    batchId: string,
    mode: ExportMode = "official",
    signal?: AbortSignal
  ): Promise<ExportResponse> {
    const res = await this.requestJson<ExportResponse>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/export?mode=${encodeURIComponent(mode)}`,
      { signal }
    );
    return normalizeExportResponse(res);
  }

  async downloadBatchJsonArray(
    batchId: string,
    mode: ExportMode = "official"
  ): Promise<{ rawJsonText: string; records: StandardOutputRecord[] }> {
    const { text } = await this.requestRaw(
      `/api/v1/batches/${encodeURIComponent(batchId)}/export/download?mode=${encodeURIComponent(mode)}`
    );
    const parsed = parseLosslessJson<StandardOutputRecord[]>(text);
    const records = Array.isArray(parsed) ? parsed : [];
    return { rawJsonText: text, records };
  }

  syncBatchToBaseline(
    batchId: string,
    idempotencyKey?: string
  ): Promise<SyncBaselineResult> {
    const body: SyncBaselineRequest = idempotencyKey
      ? { idempotency_key: idempotencyKey }
      : {};
    return this.requestJson<SyncBaselineResult>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/sync-baseline`,
      {
        method: "POST",
        jsonBody: body,
      }
    );
  }

  listEvalRuns(signal?: AbortSignal): Promise<EvalRunSummary[]> {
    return this.requestJson<EvalRunSummary[]>("/api/v1/eval-runs", { signal });
  }

  triggerEvalRun(args: TriggerEvalRunRequest): Promise<EvalRunSummary> {
    return this.requestJson<EvalRunSummary>("/api/v1/eval-runs", {
      method: "POST",
      jsonBody: args,
    });
  }
}

export { rawDecimal, RawDecimalLiteral };
