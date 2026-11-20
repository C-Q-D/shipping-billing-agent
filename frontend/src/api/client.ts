import {
  parseLosslessJson,
  rawDecimal,
  RawDecimalLiteral,
  stringifyLosslessJson,
  toSchemaDecimal,
} from "./decimalJson.ts";
import { createDevStubTransport, type FetchLike } from "./devStubTransport.ts";
import type { components } from "./schema";

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

export type OcrMode = BatchSummary["ocr_mode"];
export type ExportMode = ExportResponse["mode"];
export type DecimalInput = string | number | RawDecimalLiteral;

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
    super(args.message, args.cause !== undefined ? { cause: args.cause } : undefined);
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

export function validateExistingBillsJsonText(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = parseLosslessJson<unknown>(trimmed);
    if (Array.isArray(parsed)) {
      return null;
    }
    if (
      parsed &&
      typeof parsed === "object" &&
      Array.isArray((parsed as { bills?: unknown }).bills)
    ) {
      return null;
    }
    return "随批已有账单 JSON 必须为账单数组，或包含 bills 数组的对象";
  } catch {
    return "随批已有账单 JSON 语法无效，请检查括号与逗号";
  }
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

  const entityTrimmed = args.accountingEntity.trim();
  if (!entityTrimmed || entityTrimmed.length > 120) {
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

function buildRecordPatchPayload(
  patch: Omit<RecordPatchRequest, "quantity" | "unit_price" | "amount"> & {
    quantity?: DecimalInput | null;
    unit_price?: DecimalInput | null;
    amount?: DecimalInput | null;
  }
): RecordPatchRequest {
  const payload: RecordPatchRequest = {
    force_create_new: patch.force_create_new,
  };
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

    const headers: Record<string, string> = {};
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

  listAccountingEntities(signal?: AbortSignal): Promise<AccountingEntity[]> {
    return this.requestJson<AccountingEntity[]>("/api/v1/entities", {
      signal,
    });
  }

  createAccountingEntity(
    payload: AccountingEntityUpsert
  ): Promise<AccountingEntity> {
    return this.requestJson<AccountingEntity>("/api/v1/entities", {
      method: "POST",
      jsonBody: payload,
    });
  }

  updateAccountingEntity(
    entityId: string,
    payload: AccountingEntityUpsert
  ): Promise<AccountingEntity> {
    return this.requestJson<AccountingEntity>(
      `/api/v1/entities/${encodeURIComponent(entityId)}`,
      {
        method: "PUT",
        jsonBody: payload,
      }
    );
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

  listExistingBills(
    accountingEntity?: string,
    signal?: AbortSignal
  ): Promise<ExistingBill[]> {
    const query = accountingEntity
      ? `?accounting_entity=${encodeURIComponent(accountingEntity)}`
      : "";
    return this.requestJson<ExistingBill[]>(`/api/v1/existing-bills${query}`, {
      signal,
    });
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

  listBatches(limit = 20, signal?: AbortSignal): Promise<BatchSummary[]> {
    return this.requestJson<BatchSummary[]>(
      `/api/v1/batches?limit=${encodeURIComponent(String(limit))}`,
      { signal }
    );
  }

  createBatch(
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

    return this.requestJson<BatchSummary | BatchDetail>(
      `/api/v1/batches?wait=${wait ? "true" : "false"}`,
      {
        method: "POST",
        formData,
        timeoutMs: UPLOAD_REQUEST_TIMEOUT_MS,
      }
    );
  }

  createDemoBatch(args: {
    accounting_entity: string;
    ocr_mode?: OcrMode;
    wait?: boolean;
  }): Promise<BatchSummary | BatchDetail> {
    const wait = args.wait ?? false;
    return this.requestJson<BatchSummary | BatchDetail>(
      `/api/v1/batches/demo?wait=${wait ? "true" : "false"}`,
      {
        method: "POST",
        jsonBody: {
          accounting_entity: args.accounting_entity.trim(),
          ocr_mode: args.ocr_mode ?? "REMOTE_ONLY",
        },
      }
    );
  }

  importReviewBundle(bundle: ReviewBundleV1): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      "/api/v1/batches/import-review-bundle",
      {
        method: "POST",
        jsonBody: bundle,
      }
    );
  }

  getBatchDetail(batchId: string, signal?: AbortSignal): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}`,
      { signal }
    );
  }

  switchBatchAccountingEntity(
    batchId: string,
    accountingEntity: string
  ): Promise<SwitchEntityResponse> {
    return this.requestJson<SwitchEntityResponse>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/switch-entity`,
      {
        method: "POST",
        jsonBody: { accounting_entity: accountingEntity.trim() },
      }
    );
  }

  reapplyBatchChargeDictionary(batchId: string): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/reapply-dictionary`,
      { method: "POST" }
    );
  }

  retryOrReplaceDocument(
    batchId: string,
    docId: string,
    input: RetryDocumentInput = {}
  ): Promise<BatchDetail> {
    const formData = new FormData();
    if (input.ocr_mode) {
      formData.append("ocr_mode", input.ocr_mode);
    }
    if (input.public_url) {
      formData.append("public_url", input.public_url);
    }
    if (input.file) {
      const name = input.file instanceof File ? input.file.name : "replace.bin";
      formData.append("file", input.file, name);
    }
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}/retry`,
      {
        method: "POST",
        formData,
        timeoutMs: UPLOAD_REQUEST_TIMEOUT_MS,
      }
    );
  }

  patchDocumentTotals(
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
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}`,
      {
        method: "PATCH",
        jsonBody: payload,
      }
    );
  }

  forceParseSkippedDocument(
    batchId: string,
    docId: string
  ): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/documents/${encodeURIComponent(docId)}/force-parse`,
      { method: "POST" }
    );
  }

  addManualBillingRecord(
    batchId: string,
    record: Omit<ManualRecordCreate, "amount"> & { amount: DecimalInput }
  ): Promise<BatchDetail> {
    const payload: ManualRecordCreate = {
      ...record,
      amount: toSchemaDecimal(record.amount),
    };
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records`,
      {
        method: "POST",
        jsonBody: payload,
      }
    );
  }

  patchBillingRecord(
    batchId: string,
    recordId: string,
    patch: Omit<RecordPatchRequest, "quantity" | "unit_price" | "amount"> & {
      quantity?: DecimalInput | null;
      unit_price?: DecimalInput | null;
      amount?: DecimalInput | null;
    }
  ): Promise<BatchDetail> {
    const payload = buildRecordPatchPayload(patch);
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}`,
      {
        method: "PATCH",
        jsonBody: payload,
      }
    );
  }

  approveBillingRecord(
    batchId: string,
    recordId: string,
    reason: string
  ): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}/approve`,
      {
        method: "POST",
        jsonBody: { reason },
      }
    );
  }

  rejectBillingRecord(
    batchId: string,
    recordId: string,
    reason: string
  ): Promise<BatchDetail> {
    return this.requestJson<BatchDetail>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/records/${encodeURIComponent(recordId)}/reject`,
      {
        method: "POST",
        jsonBody: { reason },
      }
    );
  }

  exportBatchJson(
    batchId: string,
    mode: ExportMode = "official",
    signal?: AbortSignal
  ): Promise<ExportResponse> {
    return this.requestJson<ExportResponse>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/export?mode=${encodeURIComponent(mode)}`,
      { signal }
    );
  }

  async downloadBatchJsonArray(
    batchId: string,
    mode: ExportMode = "official"
  ): Promise<{ rawJsonText: string; records: StandardOutputRecord[] }> {
    const { text } = await this.requestRaw(
      `/api/v1/batches/${encodeURIComponent(batchId)}/export/download?mode=${encodeURIComponent(mode)}`
    );
    const records = parseLosslessJson<StandardOutputRecord[]>(text);
    return { rawJsonText: text, records };
  }

  syncBatchToBaseline(
    batchId: string,
    idempotencyKey?: string
  ): Promise<SyncBaselineResult> {
    return this.requestJson<SyncBaselineResult>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/sync-baseline`,
      {
        method: "POST",
        jsonBody: idempotencyKey ? { idempotency_key: idempotencyKey } : {},
      }
    );
  }

  listEvalRuns(signal?: AbortSignal): Promise<EvalRunSummary[]> {
    return this.requestJson<EvalRunSummary[]>("/api/v1/eval-runs", { signal });
  }

  triggerEvalRun(args: {
    run_mode: EvalRunSummary["run_mode"];
    k_trials?: number;
  }): Promise<EvalRunSummary> {
    return this.requestJson<EvalRunSummary>("/api/v1/eval-runs", {
      method: "POST",
      jsonBody: args,
    });
  }
}

export { rawDecimal, RawDecimalLiteral };
