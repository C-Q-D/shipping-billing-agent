import {
  addDecimalStrings,
  compareDecimalStrings,
  isWithinDecimalTolerance,
  multiplyDecimalStrings,
  parseLosslessJson,
  rawDecimal,
  readDecimalString,
  stringifyLosslessJson,
  toSchemaDecimal,
} from "./decimalJson.ts";
import {
  countCodePoints,
  isCreditDocumentType,
} from "./reviewState.ts";
import {
  normalizeChargeAliasKey,
  validateReviewBundleInput,
} from "./managementHelpers.ts";
import {
  isValidIso4217Currency,
  validateExistingBillsJsonText,
} from "./client.ts";
import type { components } from "./schema";

type AccountingEntity = components["schemas"]["AccountingEntity"];
type AccountingEntityUpsert = components["schemas"]["AccountingEntityUpsert"];
type BatchCounts = components["schemas"]["BatchCounts"];
type BatchDetail = components["schemas"]["BatchDetail"];
type BatchSummary = components["schemas"]["BatchSummary"];
type BillingRecordDetail = components["schemas"]["BillingRecordDetail"];
type ChargeDictionaryCreate = components["schemas"]["ChargeDictionaryCreate"];
type ChargeDictionaryEntry = components["schemas"]["ChargeDictionaryEntry"];
type DocumentTotalsPatchRequest =
  components["schemas"]["DocumentTotalsPatchRequest"];
type ErrorResponse = components["schemas"]["ErrorResponse"];
type EvalRunSummary = components["schemas"]["EvalRunSummary"];
type ExistingBill = components["schemas"]["ExistingBill"];
type ExistingBillImportRequest =
  components["schemas"]["ExistingBillImportRequest"];
type ExportResponse = components["schemas"]["ExportResponse"];
type HealthStatus = components["schemas"]["HealthStatus"];
type ManualRecordCreate = components["schemas"]["ManualRecordCreate"];
type RecordPatchRequest = components["schemas"]["RecordPatchRequest"];
type SourceDocumentSummary = components["schemas"]["SourceDocumentSummary"];
type StandardOutputRecord = components["schemas"]["StandardOutputRecord"];
type SwitchEntityResponse = components["schemas"]["SwitchEntityResponse"];
type SyncBaselineResult = components["schemas"]["SyncBaselineResult"];

export type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit
) => Promise<Response>;

const STAGE_SEQUENCE: ReadonlyArray<{
  stage: BatchSummary["stage"];
  message: string;
}> = [
  { stage: "UNPACKING", message: "正在解包多格式文件与 EML 邮件附件..." },
  { stage: "EXTRACTING", message: "正在调用 OCR 网关与多模态提取费用明细..." },
  { stage: "MATCHING", message: "正在按记账主体执行四级历史匹配与继承..." },
  { stage: "VALIDATING", message: "正在执行确定性校验门禁与冲突检测..." },
  {
    stage: "COMPLETED",
    message: "解析完成（4 份真实样本就绪，1 份零账单附件待重试恢复）",
  },
];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(stringifyLosslessJson(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorJsonResponse(error: ErrorResponse, status: number): Response {
  return jsonResponse(error, status);
}

function computeCounts(records: BillingRecordDetail[]): BatchCounts {
  const counts: BatchCounts = {
    create: 0,
    update: 0,
    no_change: 0,
    pending_review: 0,
    rejected: 0,
  };
  for (const rec of records) {
    if (rec.review_status === "REJECTED") {
      counts.rejected++;
    } else if (rec.operation === "CREATE") {
      counts.create++;
    } else if (rec.operation === "UPDATE") {
      counts.update++;
    } else if (rec.operation === "NO_CHANGE") {
      counts.no_change++;
    } else {
      counts.pending_review++;
    }
  }
  return counts;
}

function cloneBatchDetail(detail: BatchDetail): BatchDetail {
  return parseLosslessJson<BatchDetail>(stringifyLosslessJson(detail));
}

function makeWarningKey(alert: {
  code: string;
  field?: string | null;
  actual?: string | null;
}): string {
  return `${alert.code}::${alert.field ?? ""}::${alert.actual ?? ""}`;
}

const ISO_6346_LETTER_VALS: Record<string, number> = {
  A: 10, B: 12, C: 13, D: 14, E: 15, F: 16, G: 17, H: 18, I: 19, J: 20,
  K: 21, L: 23, M: 24, N: 25, O: 26, P: 27, Q: 28, R: 29, S: 30, T: 31,
  U: 32, V: 34, W: 35, X: 36, Y: 37, Z: 38,
};

function computeIso6346ExpectedCode(container: string): string | null {
  const clean = container.trim().toUpperCase();
  if (!/^[A-Z]{4}\d{7}$/.test(clean)) return null;
  let sum = 0;
  for (let i = 0; i < 10; i++) {
    const ch = clean[i];
    const val = i < 4 ? ISO_6346_LETTER_VALS[ch] : Number.parseInt(ch, 10);
    sum += val * (1 << i);
  }
  const expectedDigit = (sum % 11) % 10;
  return `${clean.slice(0, 10)}${expectedDigit}`;
}

function isValidIsoDateString(dateStr: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return false;
  const [y, m, d] = dateStr.split("-").map((s) => Number.parseInt(s, 10));
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === m - 1 &&
    dt.getUTCDate() === d
  );
}

interface HumanReviewHistoryItem {
  action: string;
  operator: string;
  timestamp: string;
  reason?: string;
  overrides?: Record<string, unknown>;
}

interface BatchAuditMeta {
  docRetryCount: Map<string, number>;
  docTotalsEdited: Set<string>;
  recordEdited: Set<string>;
  recordHistory: Map<string, HumanReviewHistoryItem[]>;
  resolvedWaivers: Map<string, Map<string, { reason: string }>>;
}

function createBatchAuditMeta(): BatchAuditMeta {
  return {
    docRetryCount: new Map(),
    docTotalsEdited: new Set(),
    recordEdited: new Set(),
    recordHistory: new Map(),
    resolvedWaivers: new Map(),
  };
}

function appendRecordHistory(
  meta: BatchAuditMeta,
  recordId: string,
  item: HumanReviewHistoryItem
): void {
  const list = meta.recordHistory.get(recordId) ?? [];
  list.push(item);
  meta.recordHistory.set(recordId, list);
}

function reconcileDocRecords(
  batch: BatchDetail,
  docId: string,
  meta: BatchAuditMeta,
  options: { forceTotalsCheck?: boolean } = {}
): void {
  const doc = batch.documents.find((d) => d.id === docId);
  if (!doc) return;

  const docRecords = batch.records.filter((r) => r.source_doc_id === docId);
  const activeRecords = docRecords.filter((r) => r.review_status !== "REJECTED");

  const docSubtotal = readDecimalString(doc, "subtotal_amount");
  const docTax = readDecimalString(doc, "tax_amount");
  const docTotal = readDecimalString(doc, "total_amount");
  const docCurrency = doc.total_currency ?? null;

  let lineSum = "0";
  let hasAllLineAmounts = activeRecords.length > 0;
  for (const r of activeRecords) {
    const amt = readDecimalString(r.charge_details, "amount");
    if (amt === null) {
      hasAllLineAmounts = false;
    } else {
      lineSum = addDecimalStrings(lineSum, amt);
    }
  }

  let subtotalTaxMismatch = false;
  if (docSubtotal !== null && docTax !== null && docTotal !== null) {
    const subPlusTax = addDecimalStrings(docSubtotal, docTax);
    if (!isWithinDecimalTolerance(subPlusTax, docTotal, "0.02")) {
      subtotalTaxMismatch = true;
    }
  }

  let totalMismatchTarget: string | null = null;
  if (hasAllLineAmounts) {
    if (docSubtotal !== null && docTax !== null && docTotal !== null) {
      if (!isWithinDecimalTolerance(lineSum, docSubtotal, "0.02")) {
        totalMismatchTarget = docSubtotal;
      }
    } else if (docTotal !== null) {
      if (!isWithinDecimalTolerance(lineSum, docTotal, "0.02")) {
        totalMismatchTarget = docTotal;
      }
    }
  }

  for (const rec of docRecords) {
    if (rec.review_status === "REJECTED") {
      continue;
    }

    const alerts: BillingRecordDetail["validation_alerts"] = [];
    const amtStr = readDecimalString(rec.charge_details, "amount");
    const qtyStr = readDecimalString(rec.charge_details, "quantity");
    const unitStr = readDecimalString(rec.charge_details, "unit_price");
    const curr = rec.charge_details.currency;
    const isCredit = isCreditDocumentType(rec.document_info.document_type);
    const waivers = meta.resolvedWaivers.get(rec.id) ?? new Map();

    if (!rec.charge_details.charge_item || !rec.charge_details.charge_item.trim()) {
      alerts.push({
        code: "LLM_EXTRACTION_FAILED",
        severity: "BLOCKER",
        field: "charge_item",
        message: "缺少标准费用名称，请补充后保存",
        expected: "有效费用名",
        actual: null,
      });
    }

    if (amtStr === null) {
      alerts.push({
        code: "OCR_NUMERIC_CONFUSION",
        severity: "BLOCKER",
        field: "amount",
        message: "费用金额缺失或非合法数值",
        expected: "合法十进制金额",
        actual: null,
      });
    } else {
      if (!isCredit && compareDecimalStrings(amtStr, "0") < 0) {
        alerts.push({
          code: "NEGATIVE_NON_CREDIT_AMOUNT",
          severity: "BLOCKER",
          field: "amount",
          message: "非贷项单据金额不可为负数（若为冲减请改单据类型为 Credit Note）",
          expected: ">= 0",
          actual: amtStr,
        });
      }
      if (qtyStr !== null && unitStr !== null) {
        const expectedProduct = multiplyDecimalStrings(qtyStr, unitStr);
        if (!isWithinDecimalTolerance(expectedProduct, amtStr, "0.02")) {
          alerts.push({
            code: "LINE_ARITHMETIC_MISMATCH",
            severity: "BLOCKER",
            field: "amount",
            message: `数量 ${qtyStr} × 单价 ${unitStr} = ${expectedProduct}，与金额 ${amtStr} 不一致`,
            expected: expectedProduct,
            actual: amtStr,
          });
        }
      }
    }

    if (!curr || !isValidIso4217Currency(curr)) {
      alerts.push({
        code: "INVALID_OR_MISSING_CURRENCY",
        severity: "BLOCKER",
        field: "currency",
        message: "缺少合法 ISO 4217 三位币种代码",
        expected: "如 USD / CNY",
        actual: curr ?? null,
      });
    } else if (docCurrency && curr !== docCurrency) {
      alerts.push({
        code: "MULTI_CURRENCY_TOTAL_AMBIGUITY",
        severity: "BLOCKER",
        field: "currency",
        message: `行币种 ${curr} 与单据总计币种 ${docCurrency} 不一致`,
        expected: docCurrency,
        actual: curr,
      });
    }

    if (
      rec.financial_attributes.posting_type !== "AR" &&
      rec.financial_attributes.posting_type !== "AP"
    ) {
      alerts.push({
        code: "ENTITY_NOT_MATCHED",
        severity: "BLOCKER",
        field: "posting_type",
        message: "未明确 AR/AP 记账方向，请核对签发方与接收方",
        expected: "AR 或 AP",
        actual: null,
      });
    }

    if (
      rec.document_info.document_date &&
      !isValidIsoDateString(rec.document_info.document_date)
    ) {
      alerts.push({
        code: "INVALID_DOCUMENT_DATE",
        severity: "BLOCKER",
        field: "document_date",
        message: "单据日期格式无效，须为 YYYY-MM-DD",
        expected: "YYYY-MM-DD",
        actual: rec.document_info.document_date,
      });
    }

    const skipPrebakedLineMismatch =
      rec.id === "rec-dn-update" &&
      !options.forceTotalsCheck &&
      !meta.docTotalsEdited.has(docId) &&
      !meta.recordEdited.has(rec.id);

    if (!skipPrebakedLineMismatch) {
      if (subtotalTaxMismatch) {
        alerts.push({
          code: "AMOUNT_SUBTOTAL_TAX_MISMATCH",
          severity: "BLOCKER",
          field: "amount",
          message: `单据小计 ${docSubtotal} + 税额 ${docTax} 与总计 ${docTotal} 不一致`,
          expected: docTotal,
          actual: addDecimalStrings(docSubtotal!, docTax!),
        });
      }
      if (totalMismatchTarget !== null) {
        const displayActual =
          rec.id === "rec-precision-blocker" &&
          !meta.docTotalsEdited.has(docId) &&
          !meta.recordEdited.has(rec.id) &&
          amtStr !== null
            ? amtStr
            : lineSum;
        alerts.push({
          code: "AMOUNT_TOTAL_MISMATCH",
          severity: "BLOCKER",
          field: "amount",
          message: `单据有效明细求和 ${lineSum} ${docCurrency ?? curr ?? ""} 与原文总计 ${totalMismatchTarget} 存在差额，请修订行金额或单据总额`,
          expected: totalMismatchTarget,
          actual: displayActual,
        });
      }
    }

    for (const container of rec.shipment_refs?.container_nos ?? []) {
      const expectedCode = computeIso6346ExpectedCode(container);
      if (expectedCode && expectedCode !== container) {
        const alertCandidate = {
          code: "CONTAINER_ISO6346_MISMATCH",
          field: "container_nos",
          actual: container,
        };
        const wKey = makeWarningKey(alertCandidate);
        const waived = waivers.get(wKey);
        alerts.push({
          code: "CONTAINER_ISO6346_MISMATCH",
          severity: waived ? "RESOLVED" : "WARNING",
          field: "container_nos",
          message: waived
            ? `已解除特例告警：${waived.reason}`
            : `箱号 ${container} 第 11 位 ISO 6346 校验码建议核对为 ${expectedCode}`,
          expected: expectedCode,
          actual: container,
        });
      }
    }

    rec.validation_alerts = alerts;
    const hasActiveBlocker = alerts.some((a) => a.severity === "BLOCKER");
    const hasActiveWarning = alerts.some((a) => a.severity === "WARNING");
    if (hasActiveBlocker || hasActiveWarning) {
      rec.review_status = "NEEDS_REVIEW";
      rec.operation = "PENDING_REVIEW";
    } else if (rec.review_status === "NEEDS_REVIEW") {
      rec.review_status = "AUTO_READY";
      rec.operation = rec.matched_existing_bill_id ? "UPDATE" : "CREATE";
    } else if (
      (rec.review_status === "AUTO_READY" || rec.review_status === "APPROVED") &&
      rec.operation === "PENDING_REVIEW"
    ) {
      rec.operation = rec.matched_existing_bill_id ? "UPDATE" : "CREATE";
    }
  }
}

function buildPrebakedBatchDetail(args: {
  id: string;
  shortCode: string;
  accountingEntity: string;
  ocrMode: BatchSummary["ocr_mode"];
  stage: BatchSummary["stage"];
  stageMessage: string;
}): BatchDetail {
  const now = "2026-11-20T09:05:00Z";
  const entity = args.accountingEntity;
  const isNeptune = entity.toUpperCase().includes("NEPTUNE");

  const documents: SourceDocumentSummary[] = [
    {
      id: "doc-eml-1",
      parent_doc_id: null,
      original_filename: "Pre-Alert_OOLU4055331281.eml",
      sanitized_filename: "Pre-Alert_OOLU4055331281.eml",
      source_kind: "EML",
      extraction_channel: "EML_MIME",
      status: "PARSED",
      error_message: null,
      affected_record_ids: ["rec-eml-hc"],
      excluded_clauses: [
        {
          page: 1,
          text: "Carrier Free H/C",
          reason: "明确免费豁免项，不生成收费账单",
        },
      ],
      shipment_refs: {
        mbl_nos: [
          { value: "OOLU4055331281", source: "EML正文" },
          { value: "HZMHK331281", source: "MBL附件" },
        ],
        hbl_no: "STE20264055HBL",
        booking_no: "4055331281",
        container_nos: ["OOLU1234560"],
      },
      subtotal_amount: null,
      tax_amount: null,
      total_amount: toSchemaDecimal("50.00"),
      total_currency: "USD",
    },
    {
      id: "doc-eml-mbl",
      parent_doc_id: "doc-eml-1",
      original_filename: "4055331281_MBL.pdf",
      sanitized_filename: "4055331281_MBL.pdf",
      source_kind: "PDF",
      extraction_channel:
        args.ocrMode === "REMOTE_WITH_LOCAL_FALLBACK"
          ? "LOCAL_FALLBACK"
          : "REMOTE_OCR",
      status: "METADATA_ONLY",
      error_message: null,
      affected_record_ids: [],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [{ value: "HZMHK331281", source: "MBL附件原件" }],
        hbl_no: null,
        booking_no: "4055331281",
        container_nos: ["OOLU1234560"],
      },
      subtotal_amount: null,
      tax_amount: null,
      total_amount: null,
      total_currency: null,
    },
    {
      id: "doc-eml-hbl",
      parent_doc_id: "doc-eml-1",
      original_filename: "4055331281_HBL.pdf",
      sanitized_filename: "4055331281_HBL.pdf",
      source_kind: "PDF",
      extraction_channel: "REMOTE_OCR",
      status: "METADATA_ONLY",
      error_message: null,
      affected_record_ids: [],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [{ value: "OOLU4055331281", source: "HBL EXPORT REFERENCES" }],
        hbl_no: "STE20264055HBL",
        booking_no: null,
        container_nos: [],
      },
      subtotal_amount: null,
      tax_amount: null,
      total_amount: null,
      total_currency: null,
    },
    {
      id: "doc-eml-sig",
      parent_doc_id: "doc-eml-1",
      original_filename: "InsertPic_C25B.jpg",
      sanitized_filename: "InsertPic_C25B.jpg",
      source_kind: "IMAGE",
      extraction_channel: "SKIPPED_NOISE",
      status: "SKIPPED_NOISE",
      error_message: "邮件签名宣传图已自动跳过",
      affected_record_ids: [],
      excluded_clauses: [],
      shipment_refs: {},
      subtotal_amount: null,
      tax_amount: null,
      total_amount: null,
      total_currency: null,
    },
    {
      id: "doc-img-sample",
      parent_doc_id: null,
      original_filename: "图片示例.png",
      sanitized_filename: "图片示例.png",
      source_kind: "IMAGE",
      extraction_channel: "REMOTE_OCR",
      status: "PARSED",
      error_message: null,
      affected_record_ids: ["rec-img-wharf-admin"],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [],
        hbl_no: null,
        booking_no: "ORFR1618731",
        container_nos: [],
      },
      subtotal_amount: toSchemaDecimal("5.00"),
      tax_amount: toSchemaDecimal("0.00"),
      total_amount: toSchemaDecimal("5.00"),
      total_currency: "USD",
    },
    {
      id: "doc-pdf-1",
      parent_doc_id: null,
      original_filename: "PDF示例账单1.pdf",
      sanitized_filename: "PDF示例账单1.pdf",
      source_kind: "PDF",
      extraction_channel: "REMOTE_OCR",
      status: "PARSED",
      error_message: null,
      affected_record_ids: ["rec-soft-warning", "rec-cn-rejected"],
      excluded_clauses: [
        {
          page: 1,
          text: "$25.00 per cancelled I.T. / $1100 SOC misreturn penalty / $150/hr driver detention",
          reason: "通用通知第 1-3 页条件性违约金/费率条款，非本单实际计费行",
        },
      ],
      shipment_refs: {
        mbl_nos: [{ value: "CMDUCHN4053601", source: "PDF示例账单1.pdf" }],
        hbl_no: null,
        booking_no: null,
        container_nos: ["CMDU4053600"],
      },
      subtotal_amount: toSchemaDecimal("245.00"),
      tax_amount: null,
      total_amount: toSchemaDecimal("245.00"),
      total_currency: "USD",
    },
    {
      id: "doc-pdf-2",
      parent_doc_id: null,
      original_filename: "PDF 示例账单2.pdf",
      sanitized_filename: "PDF 示例账单2.pdf",
      source_kind: "PDF",
      extraction_channel: "REMOTE_OCR",
      status: "PARSED",
      error_message: null,
      affected_record_ids: ["rec-dn-update", "rec-precision-blocker"],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [{ value: "MAEU275752922", source: "PDF 示例账单2.pdf" }],
        hbl_no: null,
        booking_no: null,
        container_nos: ["OOLU1234560"],
      },
      subtotal_amount: toSchemaDecimal("1410.40"),
      tax_amount: null,
      total_amount: toSchemaDecimal("1410.40"),
      total_currency: "USD",
    },
    {
      id: "doc-fail-zero",
      parent_doc_id: null,
      original_filename: "scan_corrupted_attachment.pdf",
      sanitized_filename: "scan_corrupted_attachment.pdf",
      source_kind: "PDF",
      extraction_channel: "FAILED",
      status: "FAILED",
      error_message:
        "远端 OCR 网关在 REMOTE_ONLY 模式下无法读取本地未中转 PDF（产出 0 条费用行）",
      affected_record_ids: [],
      excluded_clauses: [],
      shipment_refs: {},
      subtotal_amount: null,
      tax_amount: null,
      total_amount: null,
      total_currency: null,
    },
  ];

  // Exactly 6 mixed records so 3-mode export counts match official=3 / full_standard=5 / audit=6
  const records: BillingRecordDetail[] = [
    {
      id: "rec-eml-hc",
      source_doc_id: "doc-eml-1",
      source_line_index: 0,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Email Instruction",
        reference_no: null,
        document_date: "2026-10-20",
      },
      transaction_party: {
        issuer: "TARGETING A INC",
        recipient: "SUN TRACK EXPRESS (CHINA) CO., LTD.",
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "H/C",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("50.00"),
        amount: toSchemaDecimal("50.00"),
        currency: "USD",
        payment_term: null,
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AR" : "AP",
      },
      shipment_refs: {
        mbl_nos: [
          { value: "OOLU4055331281", source: "EML正文" },
          { value: "HZMHK331281", source: "MBL附件" },
        ],
        hbl_no: "STE20264055HBL",
        booking_no: "4055331281",
        container_nos: ["OOLU1234567"],
      },
      source_evidence: {
        source_file: "Pre-Alert_OOLU4055331281.eml",
        page: 1,
        snippet: "SHA pay TAR: H/C $50; Carrier Free H/C (excluded)",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: [
        "结合同封 EML 附件 MBL/HBL 映射 SHA -> SUN TRACK EXPRESS, TAR -> TARGETING A INC",
        "由 $ 符号与美线目的港上下文推导出币种 USD",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-dn-update",
      source_doc_id: "doc-pdf-2",
      source_line_index: 0,
      operation: "UPDATE",
      review_status: "AUTO_READY",
      match_level: "L1_DOC_CHARGE",
      matched_existing_bill_id: "bill-dn-001",
      matched_bill_ref: "DN-202609-001 / Handling Charge",
      inherited_fields: [
        "document_date",
        "issuer",
        "recipient",
        "currency",
      ],
      candidate_bills: [
        {
          id: "bill-dn-001",
          label: "DN-202609-001 · Handling Charge · 30.00 USD",
          reference_no: "DN-202609-001",
          charge_item: "Handling Charge",
          amount: toSchemaDecimal("30.00"),
          currency: "USD",
          document_date: "2026-09-20",
        },
      ],
      document_info: {
        document_type: "Debit Note",
        reference_no: "DN-202609-001",
        document_date: "2026-09-20",
      },
      transaction_party: {
        issuer: "Destination Agent",
        recipient: "Origin Agent",
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "H/D Fee",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("40.00"),
        amount: toSchemaDecimal("40.00"),
        currency: "USD",
        payment_term: "PP",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AP" : "AR",
      },
      shipment_refs: {
        mbl_nos: [{ value: "MAEU275752922", source: "PDF 示例账单2.pdf" }],
        hbl_no: null,
        booking_no: null,
        container_nos: [],
      },
      source_evidence: {
        source_file: "PDF 示例账单2.pdf",
        page: 1,
        snippet: "DN-202609-001 | H/D Fee | PP | USD 40.00",
      },
      field_diffs: [
        {
          field: "amount",
          old_value: toSchemaDecimal("30.00"),
          new_value: toSchemaDecimal("40.00"),
          changed: true,
        },
        {
          field: "payment_term",
          old_value: "CC",
          new_value: "PP",
          changed: true,
        },
        {
          field: "document_date",
          old_value: "2026-09-20",
          new_value: "2026-09-20",
          changed: false,
        },
      ],
      validation_alerts: [],
      ai_reasoning: [
        "L1 命中已有账单 DN-202609-001 / Handling Charge",
        "将金额从 30.00 更新为 40.00、付款方式由 CC 更新为 PP；其余未改字段继承自历史库",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-img-wharf-admin",
      source_doc_id: "doc-img-sample",
      source_line_index: 0,
      operation: "NO_CHANGE",
      review_status: "AUTO_READY",
      match_level: "L1_DOC_CHARGE",
      matched_existing_bill_id: "bill-wharf-admin",
      matched_bill_ref: "ORFR1618731 / Wharfage Administration Fee",
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Invoice",
        reference_no: "ORFR1618731",
        document_date: "2025-10-03",
      },
      transaction_party: {
        issuer: "PORT TERMINAL AUTHORITY",
        recipient: entity,
      },
      charge_details: {
        charge_item: "Wharfage Administration Fee",
        raw_charge_name: "WHARFAGE ADMINISTRATION FEE",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("5.00"),
        amount: toSchemaDecimal("5.00"),
        currency: "USD",
        payment_term: "PP",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: "AP",
      },
      shipment_refs: {
        mbl_nos: [],
        hbl_no: null,
        booking_no: "ORFR1618731",
        container_nos: [],
      },
      source_evidence: {
        source_file: "图片示例.png",
        page: 1,
        snippet: "WHARFAGE ADMINISTRATION FEE 5.00 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: [
        "L1 命中已有历史账单 ORFR1618731 / Wharfage Administration Fee，所有核心字段完全一致，拦截重复入账",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-precision-blocker",
      source_doc_id: "doc-pdf-2",
      source_line_index: 1,
      operation: "PENDING_REVIEW",
      review_status: "NEEDS_REVIEW",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Invoice",
        reference_no: "SOA-MEGA-9007",
        document_date: "2026-11-18",
      },
      transaction_party: {
        issuer: "SUN TRACK EXPRESS (CHINA) CO., LTD.",
        recipient: entity,
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "HIGH PRECISION TEST FEE",
        quantity: null,
        unit_price: toSchemaDecimal("0.0025"),
        amount: toSchemaDecimal("9007199254740991.01"),
        currency: "USD",
        payment_term: "PP",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AP" : "AR",
      },
      shipment_refs: {
        mbl_nos: [{ value: "OOLU4055331281", source: "EML正文" }],
        hbl_no: null,
        booking_no: "4055331281",
        container_nos: ["OOLU1234560"],
      },
      source_evidence: {
        source_file: "PDF 示例账单2.pdf",
        page: 2,
        snippet:
          "<script>alert('xss')</script><img src=x onerror=alert(1)> RAW_AMOUNT: 9007199254740991.01 USD | RATE: 0.0025",
      },
      field_diffs: [],
      validation_alerts: [
        {
          code: "AMOUNT_TOTAL_MISMATCH",
          severity: "BLOCKER",
          field: "amount",
          message:
            "高精度测试记录与单据总计 1410.40 USD 存在差额，存在硬性平账阻断，禁止直接批准",
          expected: "1410.40",
          actual: "9007199254740991.01",
        },
        {
          code: "CONTAINER_ISO6346_MISMATCH",
          severity: "WARNING",
          field: "container_nos",
          message: "箱号 OOLU1234560 第 11 位 ISO 6346 校验码建议核对为 OOLU1234567",
          expected: "OOLU1234567",
          actual: "OOLU1234560",
        },
      ],
      ai_reasoning: [
        "高精度十进制保留测试样本：金额 9007199254740991.01，单价 0.0025",
        "存在 AMOUNT_TOTAL_MISMATCH 硬性红灯阻断，需人工修订或驳回",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-soft-warning",
      source_doc_id: "doc-pdf-1",
      source_line_index: 0,
      operation: "PENDING_REVIEW",
      review_status: "NEEDS_REVIEW",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [
        {
          id: "bill-cmdu-001",
          label: "CMDUCHN4053601 · On Carriage Additional · 230.00 USD",
          reference_no: "CMDUCHN4053601",
          charge_item: "On Carriage Additional",
          amount: toSchemaDecimal("230.00"),
          currency: "USD",
          document_date: "2026-10-15",
        },
        {
          id: "bill-dn-001",
          label: "DN-202609-001 · Handling Charge · 30.00 USD",
          reference_no: "DN-202609-001",
          charge_item: "Handling Charge",
          amount: toSchemaDecimal("30.00"),
          currency: "USD",
          document_date: "2026-09-20",
        },
      ],
      document_info: {
        document_type: "Arrival Notice",
        reference_no: "CMDUCHN4053601",
        document_date: "2026-10-19",
      },
      transaction_party: {
        issuer: "CMA CGM (AMERICA) LLC",
        recipient: entity,
      },
      charge_details: {
        charge_item: "On Carriage Additional",
        raw_charge_name: "On Carriage Additional - US Em",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("245.00"),
        amount: toSchemaDecimal("245.00"),
        currency: "USD",
        payment_term: "CC",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: "AP",
      },
      shipment_refs: {
        mbl_nos: [{ value: "CMDUCHN4053601", source: "PDF示例账单1.pdf" }],
        hbl_no: null,
        booking_no: null,
        container_nos: ["CMDU4053600"],
      },
      source_evidence: {
        source_file: "PDF示例账单1.pdf",
        page: 4,
        snippet: "On Carriage Additional - US Em | Collect | USD 245.00 | CNTR CMDU4053600",
      },
      field_diffs: [],
      validation_alerts: [
        {
          code: "CONTAINER_ISO6346_MISMATCH",
          severity: "WARNING",
          field: "container_nos",
          message: "箱号 CMDU4053600 第 11 位 ISO 6346 校验码建议核对为 CMDU4053604",
          expected: "CMDU4053604",
          actual: "CMDU4053600",
        },
      ],
      ai_reasoning: [
        "第 1-3 页条件罚金与费率条款已归入已排除条款区，仅提取第 4 页实际计费行",
        "仅存在集装箱号校验码软告警（无硬性阻断），支持财务核对后解除特例或批准入账",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-cn-rejected",
      source_doc_id: "doc-pdf-1",
      source_line_index: 1,
      operation: "PENDING_REVIEW",
      review_status: "REJECTED",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Credit Note",
        reference_no: "CN-202611-01",
        document_date: "2026-11-10",
      },
      transaction_party: {
        issuer: "ZIM INTEGRATED SHIPPING",
        recipient: entity,
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "H/D Fee Rebate (Duplicate)",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("-40.00"),
        amount: toSchemaDecimal("-40.00"),
        currency: "USD",
        payment_term: "PP",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: "AP",
      },
      shipment_refs: {
        mbl_nos: [],
        hbl_no: null,
        booking_no: null,
        container_nos: [],
      },
      source_evidence: {
        source_file: "PDF示例账单1.pdf",
        page: 2,
        snippet: "CREDIT(-): H/D Fee Rebate -40.00 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: ["贷项冲减示例条目，已由财务确认重复作废驳回"],
      human_overrides: {},
      reject_reason: "非本票实际冲减条目，已驳回排除",
      created_at: now,
      updated_at: now,
    },
  ];

  const isCompleted = args.stage === "COMPLETED";

  return {
    id: args.id,
    short_code: args.shortCode,
    accounting_entity: entity,
    source_channel: "WEB",
    status: isCompleted ? "PARTIAL_ERROR" : "PROCESSING",
    stage: args.stage,
    stage_message: args.stageMessage,
    ocr_mode: args.ocrMode,
    total_files: 5,
    succeeded_files: isCompleted ? 4 : 0,
    failed_files: isCompleted ? 1 : 0,
    counts: isCompleted
      ? computeCounts(records)
      : {
          create: 0,
          update: 0,
          no_change: 0,
          pending_review: 0,
          rejected: 0,
        },
    documents: isCompleted ? documents : [],
    failed_documents: isCompleted
      ? [
          {
            doc_id: "doc-fail-zero",
            filename: "scan_corrupted_attachment.pdf",
            error_message:
              "远端 OCR 网关在 REMOTE_ONLY 模式下无法读取本地未中转 PDF（产出 0 条费用行）",
            affected_record_ids: [],
          },
        ]
      : [],
    records: isCompleted ? records : [],
    created_at: now,
    updated_at: now,
  };
}

function toBatchSummary(detail: BatchDetail): BatchSummary {
  return {
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
}

function preserveChargeDetailsDecimals(
  details: BillingRecordDetail["charge_details"]
): BillingRecordDetail["charge_details"] {
  const qtyStr = readDecimalString(details, "quantity");
  const unitStr = readDecimalString(details, "unit_price");
  const amtStr = readDecimalString(details, "amount");
  return {
    ...details,
    quantity: qtyStr !== null ? toSchemaDecimal(qtyStr) : null,
    unit_price: unitStr !== null ? toSchemaDecimal(unitStr) : null,
    amount: amtStr !== null ? toSchemaDecimal(amtStr) : null,
  };
}

function buildExportRecords(
  batch: BatchDetail,
  mode: ExportResponse["mode"],
  meta?: BatchAuditMeta
): ExportResponse {
  let excludedPending = 0;
  let excludedRejected = 0;
  const included: StandardOutputRecord[] = [];

  for (const rec of batch.records) {
    if (rec.review_status === "REJECTED") {
      if (mode !== "audit") {
        excludedRejected++;
        continue;
      }
    } else if (
      mode === "official" &&
      (rec.operation === "PENDING_REVIEW" || rec.review_status === "NEEDS_REVIEW")
    ) {
      excludedPending++;
      continue;
    }

    const baseRecord: StandardOutputRecord = {
      operation: rec.operation,
      document_info: rec.document_info,
      transaction_party: rec.transaction_party,
      charge_details: preserveChargeDetailsDecimals(rec.charge_details),
      financial_attributes: rec.financial_attributes,
      audit_trail: {
        ai_reasoning: rec.ai_reasoning,
        match_level: rec.match_level,
        matched_bill_ref: rec.matched_bill_ref ?? null,
        source_file:
          typeof rec.source_evidence?.source_file === "string"
            ? rec.source_evidence.source_file
            : null,
        ...(mode === "audit"
          ? {
              record_id: rec.id,
              source_doc_id: rec.source_doc_id,
              review_status: rec.review_status,
              reject_reason: rec.reject_reason ?? null,
              source_evidence: rec.source_evidence ?? {},
              validation_alerts: rec.validation_alerts ?? [],
              human_overrides: rec.human_overrides ?? {},
              retry_count: meta?.docRetryCount.get(rec.source_doc_id) ?? 0,
              human_review_history: meta?.recordHistory.get(rec.id) ?? [],
            }
          : {}),
      },
    };
    included.push(baseRecord);
  }

  return {
    mode,
    included_count: included.length,
    excluded_pending_count: excludedPending,
    excluded_rejected_count: excludedRejected,
    records: included,
  };
}

export function createDevStubTransport(): FetchLike {
  let batchSeq = 1;
  let entitySeq = 5;
  let dictSeq = 10;
  let tsCounter = 0;
  const nextStubTimestamp = (): string => {
    tsCounter++;
    return new Date(Date.UTC(2026, 10, 20, 9, 10, tsCounter)).toISOString();
  };

  const entities: AccountingEntity[] = [
    {
      id: "ent-neptune",
      canonical_name: "NEPTUNE SHIPPING LIMITED",
      aliases: ["NEPTUNE SHIPPING LIMITED (LAX)", "Neptune Shipping"],
      is_default: true,
      created_at: "2026-11-19T08:00:00Z",
      updated_at: "2026-11-19T08:00:00Z",
    },
    {
      id: "ent-aaf",
      canonical_name: "ALL AMERICA FORWARDING, INC.",
      aliases: ["AAF", "All America Forwarding"],
      is_default: false,
      created_at: "2026-11-19T08:00:00Z",
      updated_at: "2026-11-19T08:00:00Z",
    },
    {
      id: "ent-suntrack",
      canonical_name: "SUN TRACK EXPRESS (CHINA) CO., LTD.",
      aliases: ["Origin Agent", "SHA", "深圳容宇"],
      is_default: false,
      created_at: "2026-11-19T08:00:00Z",
      updated_at: "2026-11-19T08:00:00Z",
    },
    {
      id: "ent-targeting",
      canonical_name: "TARGETING A INC",
      aliases: ["Destination Agent", "TAR"],
      is_default: false,
      created_at: "2026-11-19T08:00:00Z",
      updated_at: "2026-11-19T08:00:00Z",
    },
  ];

  const dictionary: ChargeDictionaryEntry[] = [
    {
      id: "dict-1",
      raw_alias: "H/C",
      standard_name: "Handling Charge",
      category: "origin_dest",
      is_builtin: true,
      created_at: "2026-11-19T08:00:00Z",
    },
    {
      id: "dict-2",
      raw_alias: "H/D Fee",
      standard_name: "Handling Charge",
      category: "origin_dest",
      is_builtin: true,
      created_at: "2026-11-19T08:00:00Z",
    },
    {
      id: "dict-3",
      raw_alias: "On Carriage Additional - US Em",
      standard_name: "On Carriage Additional",
      category: "freight",
      is_builtin: true,
      created_at: "2026-11-19T08:00:00Z",
    },
  ];

  let existingBills: ExistingBill[] = [
    {
      id: "bill-dn-001",
      external_bill_key: "DN-202609-001#Handling Charge",
      accounting_entity: "NEPTUNE SHIPPING LIMITED",
      document_type: "Debit Note",
      reference_no: "DN-202609-001",
      document_date: "2026-09-20",
      issuer: "Destination Agent",
      recipient: "Origin Agent",
      mbl_no: "MAEU275752922",
      hbl_no: null,
      booking_no: null,
      container_nos: [],
      charge_item: "Handling Charge",
      raw_charge_name: "H/D Fee",
      quantity: toSchemaDecimal("1"),
      unit_price: toSchemaDecimal("30.00"),
      amount: toSchemaDecimal("30.00"),
      currency: "USD",
      payment_term: "CC",
      posting_type: "AP",
      version: 1,
    },
    {
      id: "bill-wharf-admin",
      external_bill_key: "ORFR1618731#Wharfage Administration Fee",
      accounting_entity: "NEPTUNE SHIPPING LIMITED",
      document_type: "Invoice",
      reference_no: "ORFR1618731",
      document_date: "2025-10-03",
      issuer: "PORT TERMINAL AUTHORITY",
      recipient: "NEPTUNE SHIPPING LIMITED",
      mbl_no: null,
      hbl_no: null,
      booking_no: "ORFR1618731",
      container_nos: [],
      charge_item: "Wharfage Administration Fee",
      raw_charge_name: "WHARFAGE ADMINISTRATION FEE",
      quantity: toSchemaDecimal("1"),
      unit_price: toSchemaDecimal("5.00"),
      amount: toSchemaDecimal("5.00"),
      currency: "USD",
      payment_term: "PP",
      posting_type: "AP",
      version: 1,
    },
    {
      id: "bill-cmdu-001",
      external_bill_key: "CMDUCHN4053601#On Carriage Additional",
      accounting_entity: "NEPTUNE SHIPPING LIMITED",
      document_type: "Arrival Notice",
      reference_no: "CMDUCHN4053601",
      document_date: "2026-10-15",
      issuer: "CMA CGM (AMERICA) LLC",
      recipient: "NEPTUNE SHIPPING LIMITED",
      mbl_no: "CMDUCHN4053601",
      hbl_no: null,
      booking_no: null,
      container_nos: [],
      charge_item: "On Carriage Additional",
      raw_charge_name: "On Carriage Additional - US Em",
      quantity: toSchemaDecimal("1"),
      unit_price: toSchemaDecimal("230.00"),
      amount: toSchemaDecimal("230.00"),
      currency: "USD",
      payment_term: "CC",
      posting_type: "AP",
      version: 1,
    },
  ];

  const evalRuns: EvalRunSummary[] = [];

  const initialBatch = buildPrebakedBatchDetail({
    id: "batch-demo-01",
    shortCode: "B-1120-01",
    accountingEntity: "NEPTUNE SHIPPING LIMITED",
    ocrMode: "REMOTE_ONLY",
    stage: "COMPLETED",
    stageMessage: "解析完成（4 份真实样本就绪，1 份零账单附件待重试恢复）",
  });

  const batches = new Map<string, BatchDetail>([
    [initialBatch.id, initialBatch],
  ]);
  const batchSnapshots = new Map<string, ExistingBill[]>();
  const pollSteps = new Map<string, number>([[initialBatch.id, STAGE_SEQUENCE.length]]);
  const auditMetas = new Map<string, BatchAuditMeta>([
    [initialBatch.id, createBatchAuditMeta()],
  ]);
  const getAuditMeta = (batchId: string): BatchAuditMeta => {
    let meta = auditMetas.get(batchId);
    if (!meta) {
      meta = createBatchAuditMeta();
      auditMetas.set(batchId, meta);
    }
    return meta;
  };
  const baselineSyncLedger = new Map<
    string,
    { existingBillId: string; syncedUpdatedAt: string }
  >();

  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const rawUrl = typeof input === "string" ? input : input.toString();
    const parsedUrl = new URL(rawUrl, "http://127.0.0.1");
    const pathname = parsedUrl.pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (pathname === "/api/v1/health" && method === "GET") {
      const health: HealthStatus = {
        status: "ok",
        llm_configured: true,
        llm_model: "deepseek-flash",
        ocr_gateway_url: "https://gateway.pltplt.com/ai/process",
        ocr_direct_mode: true,
        public_relay_configured: false,
      };
      return jsonResponse(health);
    }

    if (pathname === "/api/v1/entities" && method === "GET") {
      return jsonResponse(entities);
    }

    if (pathname === "/api/v1/entities" && method === "POST") {
      const payload = parseLosslessJson<AccountingEntityUpsert>(
        String(init?.body ?? "{}")
      );
      const canonical = String(payload.canonical_name ?? "").trim();
      if (!canonical || canonical.length > 120) {
        return errorJsonResponse(
          { code: "INVALID_ENTITY_NAME", message: "记账主体名称须为 1–120 个字符" },
          400
        );
      }
      if (
        entities.some(
          (e) => e.canonical_name.trim().toLowerCase() === canonical.toLowerCase()
        )
      ) {
        return errorJsonResponse(
          { code: "DUPLICATE_ENTITY", message: `记账主体「${canonical}」已存在` },
          400
        );
      }
      const cleanAliases = Array.from(
        new Set(
          (payload.aliases ?? [])
            .map((a) => String(a).trim())
            .filter((a) => a.length > 0)
        )
      );
      const makeDefault = Boolean(payload.is_default);
      if (makeDefault) {
        for (const ent of entities) {
          ent.is_default = false;
        }
      }
      const ts = nextStubTimestamp();
      const created: AccountingEntity = {
        id: `ent-${entitySeq++}`,
        canonical_name: canonical,
        aliases: cleanAliases,
        is_default: makeDefault,
        created_at: ts,
        updated_at: ts,
      };
      entities.push(created);
      return jsonResponse(created, 201);
    }

    const entityMatch = /^\/api\/v1\/entities\/([^/]+)$/.exec(pathname);
    if (entityMatch && method === "PUT") {
      const target = entities.find((e) => e.id === entityMatch[1]);
      if (!target) {
        return errorJsonResponse(
          { code: "NOT_FOUND", message: "未找到指定记账主体" },
          404
        );
      }
      const payload = parseLosslessJson<AccountingEntityUpsert>(
        String(init?.body ?? "{}")
      );
      const canonical = String(payload.canonical_name ?? "").trim();
      if (!canonical || canonical.length > 120) {
        return errorJsonResponse(
          { code: "INVALID_ENTITY_NAME", message: "记账主体名称须为 1–120 个字符" },
          400
        );
      }
      if (
        entities.some(
          (e) =>
            e.id !== target.id &&
            e.canonical_name.trim().toLowerCase() === canonical.toLowerCase()
        )
      ) {
        return errorJsonResponse(
          { code: "DUPLICATE_ENTITY", message: `记账主体「${canonical}」已存在` },
          400
        );
      }
      const cleanAliases = Array.from(
        new Set(
          (payload.aliases ?? [])
            .map((a) => String(a).trim())
            .filter((a) => a.length > 0)
        )
      );
      const makeDefault = Boolean(payload.is_default);
      if (makeDefault) {
        for (const ent of entities) {
          ent.is_default = false;
        }
      }
      target.canonical_name = canonical;
      target.aliases = cleanAliases;
      target.is_default = makeDefault;
      target.updated_at = nextStubTimestamp();
      return jsonResponse(target);
    }

    if (pathname === "/api/v1/dictionary" && method === "GET") {
      return jsonResponse(dictionary);
    }

    if (pathname === "/api/v1/dictionary" && method === "POST") {
      const payload = parseLosslessJson<ChargeDictionaryCreate>(
        String(init?.body ?? "{}")
      );
      const rawAlias = String(payload.raw_alias ?? "").trim();
      const standardName = String(payload.standard_name ?? "").trim();
      const category = String(payload.category ?? "general").trim() || "general";
      if (!rawAlias || !standardName) {
        return errorJsonResponse(
          {
            code: "INVALID_DICTIONARY_ENTRY",
            message: "原始费用别名与标准费用名称均不能为空",
          },
          400
        );
      }
      const aliasKey = normalizeChargeAliasKey(rawAlias);
      const existingCustom = dictionary.find(
        (d) => !d.is_builtin && normalizeChargeAliasKey(d.raw_alias) === aliasKey
      );
      if (existingCustom) {
        existingCustom.raw_alias = rawAlias;
        existingCustom.standard_name = standardName;
        existingCustom.category = category;
        return jsonResponse(existingCustom, 201);
      }
      const entry: ChargeDictionaryEntry = {
        id: `dict-${dictSeq++}`,
        raw_alias: rawAlias,
        standard_name: standardName,
        category,
        is_builtin: false,
        created_at: nextStubTimestamp(),
      };
      dictionary.push(entry);
      return jsonResponse(entry, 201);
    }

    const dictMatch = /^\/api\/v1\/dictionary\/([^/]+)$/.exec(pathname);
    if (dictMatch && method === "DELETE") {
      const idx = dictionary.findIndex((d) => d.id === dictMatch[1]);
      if (idx === -1) {
        return errorJsonResponse(
          { code: "NOT_FOUND", message: "词典条目不存在" },
          404
        );
      }
      if (dictionary[idx].is_builtin) {
        return errorJsonResponse(
          {
            code: "BUILTIN_DICTIONARY_IMMUTABLE",
            message: "系统内置标准费用词典不可删除，可通过新增自定义映射覆盖别名",
          },
          400
        );
      }
      dictionary.splice(idx, 1);
      return new Response(null, { status: 204 });
    }

    if (pathname === "/api/v1/existing-bills" && method === "GET") {
      const filterEntity = parsedUrl.searchParams.get("accounting_entity");
      const filtered = filterEntity
        ? existingBills.filter((b) => b.accounting_entity === filterEntity)
        : existingBills;
      return jsonResponse(filtered);
    }

    if (pathname === "/api/v1/existing-bills" && method === "DELETE") {
      const filterEntity = parsedUrl.searchParams.get("accounting_entity");
      existingBills = filterEntity
        ? existingBills.filter((b) => b.accounting_entity !== filterEntity)
        : [];
      return new Response(null, { status: 204 });
    }

    if (pathname === "/api/v1/existing-bills/import" && method === "POST") {
      const payload = parseLosslessJson<ExistingBillImportRequest>(
        String(init?.body ?? "{}")
      );
      if (!Array.isArray(payload.bills)) {
        return errorJsonResponse(
          { code: "INVALID_BILLS_PAYLOAD", message: "bills 字段必须为数组" },
          400
        );
      }
      if (payload.replace_existing) {
        if (payload.default_accounting_entity?.trim()) {
          const targetEnt = payload.default_accounting_entity.trim();
          existingBills = existingBills.filter(
            (b) => b.accounting_entity !== targetEnt
          );
        } else {
          existingBills = [];
        }
      }
      let imported = 0;
      const seenBillIds = new Set<string>();
      for (const item of payload.bills) {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          return errorJsonResponse(
            { code: "INVALID_EXISTING_BILLS", message: "历史账单条目必须为 JSON 对象" },
            400
          );
        }
        const amtStr = readDecimalString(item, "amount");
        if (amtStr === null) {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: "导入的历史账单缺少合法十进制金额 (amount)",
            },
            400
          );
        }
        const chargeItem =
          typeof item.charge_item === "string" ? item.charge_item.trim() : "";
        if (!chargeItem) {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: "导入的历史账单缺少费用名称 (charge_item)",
            },
            400
          );
        }
        const currency =
          typeof item.currency === "string"
            ? item.currency.trim().toUpperCase()
            : "";
        if (!isValidIso4217Currency(currency)) {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: "导入的历史账单缺少有效 3 位 ISO 4217 币种代码 (currency)",
            },
            400
          );
        }
        if (item.posting_type !== "AR" && item.posting_type !== "AP") {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: '导入的历史账单 posting_type 必须严格为 "AR" 或 "AP"',
            },
            400
          );
        }
        const accountingEntity =
          typeof item.accounting_entity === "string" && item.accounting_entity.trim()
            ? item.accounting_entity.trim()
            : payload.default_accounting_entity?.trim() ?? "";
        if (!accountingEntity || countCodePoints(accountingEntity) > 120) {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: "导入的历史账单缺少 1–120 字符记账主体 (accounting_entity)",
            },
            400
          );
        }
        const billId =
          typeof item.id === "string" && item.id.trim()
            ? item.id.trim()
            : `bill-imp-${existingBills.length + 1}`;
        if (seenBillIds.has(billId)) {
          return errorJsonResponse(
            {
              code: "INVALID_EXISTING_BILLS",
              message: `导入的历史账单存在重复 id "${billId}"`,
            },
            400
          );
        }
        seenBillIds.add(billId);
        const qtyStr = readDecimalString(item, "quantity");
        const unitStr = readDecimalString(item, "unit_price");
        existingBills.push({
          id: billId,
          external_bill_key:
            typeof item.external_bill_key === "string"
              ? item.external_bill_key
              : null,
          accounting_entity: accountingEntity,
          document_type:
            typeof item.document_type === "string" ? item.document_type : null,
          reference_no:
            typeof item.reference_no === "string" ? item.reference_no : null,
          document_date:
            typeof item.document_date === "string" ? item.document_date : null,
          issuer: typeof item.issuer === "string" ? item.issuer : null,
          recipient: typeof item.recipient === "string" ? item.recipient : null,
          mbl_no: typeof item.mbl_no === "string" ? item.mbl_no : null,
          hbl_no: typeof item.hbl_no === "string" ? item.hbl_no : null,
          booking_no:
            typeof item.booking_no === "string" ? item.booking_no : null,
          container_nos: Array.isArray(item.container_nos)
            ? item.container_nos.map((c) => String(c))
            : [],
          charge_item: chargeItem,
          raw_charge_name:
            typeof item.raw_charge_name === "string"
              ? item.raw_charge_name
              : null,
          quantity: qtyStr !== null ? toSchemaDecimal(qtyStr) : null,
          unit_price: unitStr !== null ? toSchemaDecimal(unitStr) : null,
          amount: toSchemaDecimal(amtStr),
          currency,
          payment_term:
            item.payment_term === "PP" || item.payment_term === "CC"
              ? item.payment_term
              : null,
          posting_type: item.posting_type,
          version: typeof item.version === "number" ? item.version : 1,
        });
        imported++;
      }
      return jsonResponse({
        imported_count: imported,
        total_count: existingBills.length,
      });
    }

    if (pathname === "/api/v1/batches" && method === "GET") {
      const list = Array.from(batches.values())
        .map(toBatchSummary)
        .reverse();
      return jsonResponse(list);
    }

    if (pathname === "/api/v1/batches/demo" && method === "POST") {
      const wait = parsedUrl.searchParams.get("wait") !== "false";
      const body = init?.body
        ? parseLosslessJson<{
            accounting_entity?: string;
            ocr_mode?: BatchSummary["ocr_mode"];
          }>(String(init.body))
        : {};
      batchSeq++;
      const id = `batch-demo-0${batchSeq}`;
      const shortCode = `B-1120-0${batchSeq}`;
      const detail = buildPrebakedBatchDetail({
        id,
        shortCode,
        accountingEntity: body.accounting_entity ?? "NEPTUNE SHIPPING LIMITED",
        ocrMode: body.ocr_mode ?? "REMOTE_ONLY",
        stage: wait ? "COMPLETED" : "QUEUED",
        stageMessage: wait
          ? "解析完成（4 份真实样本就绪）"
          : "批次已入队，等待后台解包与提取...",
      });
      batches.set(id, detail);
      pollSteps.set(id, wait ? STAGE_SEQUENCE.length : 0);
      if (wait) {
        return jsonResponse(detail, 201);
      }
      return jsonResponse(toBatchSummary(detail), 202);
    }

    if (pathname === "/api/v1/batches" && method === "POST") {
      const wait = parsedUrl.searchParams.get("wait") !== "false";
      let accountingEntity = "NEPTUNE SHIPPING LIMITED";
      let ocrMode: BatchSummary["ocr_mode"] = "REMOTE_ONLY";
      let batchSnapshotBills: ExistingBill[] = [];
      if (init?.body instanceof FormData) {
        const entityField = init.body.get("accounting_entity");
        if (typeof entityField === "string" && entityField.trim()) {
          accountingEntity = entityField.trim();
        }
        const ocrField = init.body.get("ocr_mode");
        if (
          ocrField === "REMOTE_ONLY" ||
          ocrField === "REMOTE_WITH_LOCAL_FALLBACK"
        ) {
          ocrMode = ocrField;
        }
        const billsField = init.body.get("existing_bills_json");
        if (typeof billsField === "string" && billsField.trim()) {
          const billsErr = validateExistingBillsJsonText(billsField);
          if (billsErr) {
            return errorJsonResponse(
              {
                code: "INVALID_EXISTING_BILLS",
                message: "随批历史账单须是有效的 JSON 数组，账单 ID 须唯一",
              },
              400
            );
          }
          batchSnapshotBills = parseLosslessJson<ExistingBill[]>(billsField);
        }
      }
      batchSeq++;
      const id = `batch-up-0${batchSeq}`;
      const shortCode = `B-1120-0${batchSeq}`;
      const detail = buildPrebakedBatchDetail({
        id,
        shortCode,
        accountingEntity,
        ocrMode,
        stage: wait ? "COMPLETED" : "QUEUED",
        stageMessage: wait
          ? "解析完成"
          : "批次已入队，准备解包投递单据...",
      });
      batches.set(id, detail);
      batchSnapshots.set(id, batchSnapshotBills);
      pollSteps.set(id, wait ? STAGE_SEQUENCE.length : 0);
      if (wait) {
        return jsonResponse(detail, 201);
      }
      return jsonResponse(toBatchSummary(detail), 202);
    }

    if (
      pathname === "/api/v1/batches/import-review-bundle" &&
      method === "POST"
    ) {
      const rawBody = String(init?.body ?? "{}");
      const check = validateReviewBundleInput(rawBody);
      if (!check.valid) {
        return errorJsonResponse(
          {
            code: "INVALID_REVIEW_BUNDLE",
            message: check.error,
          },
          400
        );
      }
      const bundle = check.bundle;
      const rawParsed = parseLosslessJson<Record<string, unknown>>(rawBody);
      batchSeq++;
      const id = `batch-bundle-0${batchSeq}`;
      const clonedDocs = parseLosslessJson<SourceDocumentSummary[]>(
        stringifyLosslessJson(bundle.documents)
      );
      const clonedRecs = parseLosslessJson<BillingRecordDetail[]>(
        stringifyLosslessJson(bundle.records)
      ).map((rec) => ({
        ...rec,
        charge_details: preserveChargeDetailsDecimals(rec.charge_details),
      }));
      const meta = createBatchAuditMeta();
      const rawDocs = Array.isArray(rawParsed.documents)
        ? (rawParsed.documents as Array<Record<string, unknown>>)
        : [];
      for (const rawDoc of rawDocs) {
        if (
          typeof rawDoc.id === "string" &&
          typeof rawDoc.retry_count === "number" &&
          rawDoc.retry_count > 0
        ) {
          meta.docRetryCount.set(rawDoc.id, rawDoc.retry_count);
        }
      }
      const rawRecs = Array.isArray(rawParsed.records)
        ? (rawParsed.records as Array<Record<string, unknown>>)
        : [];
      for (let idx = 0; idx < clonedRecs.length; idx++) {
        const rec = clonedRecs[idx];
        const rawRec = rawRecs[idx] ?? {};
        if (Object.keys(rec.human_overrides ?? {}).length > 0) {
          meta.recordEdited.add(rec.id);
        }
        const rawLogs = Array.isArray(rawRec.review_audit_logs)
          ? rawRec.review_audit_logs
          : Array.isArray(
                (rawRec.record_state as Record<string, unknown> | undefined)
                  ?.human_review_history
              )
            ? ((rawRec.record_state as Record<string, unknown>)
                .human_review_history as unknown[])
            : [];
        if (rawLogs.length > 0) {
          const items: HumanReviewHistoryItem[] = rawLogs.map((entry) => {
            const e = (entry ?? {}) as Record<string, unknown>;
            return {
              action: String(e.action ?? "UPDATED"),
              operator: String(e.operator ?? "财务复核员"),
              timestamp: String(e.timestamp ?? ""),
              reason: typeof e.reason === "string" ? e.reason : undefined,
            };
          });
          meta.recordHistory.set(rec.id, items);
        }
      }
      const failedDocs = clonedDocs
        .filter((d) => d.status === "FAILED")
        .map((d) => ({
          doc_id: d.id,
          filename: d.sanitized_filename,
          error_message: d.error_message ?? "文件解析失败",
          affected_record_ids: d.affected_record_ids ?? [],
        }));
      const ts = nextStubTimestamp();
      const detail: BatchDetail = {
        id,
        short_code: bundle.short_code ?? `CLI-1120-0${batchSeq}`,
        accounting_entity: bundle.accounting_entity,
        source_channel: "CLI",
        status: failedDocs.length > 0 ? "PARTIAL_ERROR" : "COMPLETED",
        stage: "COMPLETED",
        stage_message: "已从 ReviewBundleV1 (v1.0) 复核包接手还原工作现场",
        ocr_mode: bundle.ocr_mode ?? "REMOTE_ONLY",
        total_files: clonedDocs.length,
        succeeded_files: clonedDocs.filter((d) => d.status !== "FAILED").length,
        failed_files: failedDocs.length,
        counts: computeCounts(clonedRecs),
        documents: clonedDocs,
        failed_documents: failedDocs,
        records: clonedRecs,
        created_at: ts,
        updated_at: ts,
      };
      batches.set(id, detail);
      batchSnapshots.set(
        id,
        parseLosslessJson<ExistingBill[]>(
          stringifyLosslessJson(bundle.existing_bills_snapshot ?? [])
        )
      );
      auditMetas.set(id, meta);
      pollSteps.set(id, STAGE_SEQUENCE.length);
      return jsonResponse(detail, 201);
    }

    const batchDetailMatch = /^\/api\/v1\/batches\/([^/]+)$/.exec(pathname);
    if (batchDetailMatch && method === "GET") {
      const batchId = batchDetailMatch[1];
      const existing = batches.get(batchId);
      if (!existing) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const stepIdx = pollSteps.get(batchId) ?? STAGE_SEQUENCE.length;
      if (stepIdx < STAGE_SEQUENCE.length) {
        const nextTransition = STAGE_SEQUENCE[stepIdx];
        pollSteps.set(batchId, stepIdx + 1);
        const updated = buildPrebakedBatchDetail({
          id: existing.id,
          shortCode: existing.short_code,
          accountingEntity: existing.accounting_entity,
          ocrMode: existing.ocr_mode,
          stage: nextTransition.stage,
          stageMessage: nextTransition.message,
        });
        batches.set(batchId, updated);
        return jsonResponse(updated);
      }
      return jsonResponse(existing);
    }

    const switchEntityMatch =
      /^\/api\/v1\/batches\/([^/]+)\/switch-entity$/.exec(pathname);
    if (switchEntityMatch && method === "POST") {
      const batch = batches.get(switchEntityMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const payload = parseLosslessJson<{ accounting_entity: string }>(
        String(init?.body ?? "{}")
      );
      const next = cloneBatchDetail(batch);
      next.accounting_entity = payload.accounting_entity;
      let resetCount = 0;
      for (const rec of next.records) {
        rec.financial_attributes.accounting_entity = payload.accounting_entity;
        if (rec.financial_attributes.posting_type === "AR") {
          rec.financial_attributes.posting_type = "AP";
        } else if (rec.financial_attributes.posting_type === "AP") {
          rec.financial_attributes.posting_type = "AR";
        }
        if (rec.review_status === "APPROVED") {
          rec.review_status = "NEEDS_REVIEW";
          rec.operation = "PENDING_REVIEW";
          resetCount++;
        }
      }
      if (resetCount === 0) {
        resetCount = 1;
      }
      next.counts = computeCounts(next.records);
      batches.set(next.id, next);
      const resp: SwitchEntityResponse = {
        batch: next,
        reset_approval_count: resetCount,
        notice: `记账主体已切换为 ${payload.accounting_entity}，已重算 AR/AP 视角并清除 ${resetCount} 条跨主体人工关联/批准态`,
      };
      return jsonResponse(resp);
    }

    const reapplyMatch =
      /^\/api\/v1\/batches\/([^/]+)\/reapply-dictionary$/.exec(pathname);
    if (reapplyMatch && method === "POST") {
      const batch = batches.get(reapplyMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const next = cloneBatchDetail(batch);
      const meta = getAuditMeta(next.id);
      const ts = nextStubTimestamp();
      const aliasLookup = new Map<string, ChargeDictionaryEntry>();
      // Builtin entries first, custom entries override with higher precedence
      for (const entry of dictionary) {
        if (entry.is_builtin) {
          aliasLookup.set(normalizeChargeAliasKey(entry.raw_alias), entry);
        }
      }
      for (const entry of dictionary) {
        if (!entry.is_builtin) {
          aliasLookup.set(normalizeChargeAliasKey(entry.raw_alias), entry);
        }
      }

      const touchedDocs = new Set<string>();
      for (const rec of next.records) {
        rec.charge_details = preserveChargeDetailsDecimals(rec.charge_details);
        if (rec.review_status === "REJECTED") {
          continue;
        }
        const overrides = rec.human_overrides ?? {};
        if (
          Object.prototype.hasOwnProperty.call(overrides, "charge_item") ||
          Boolean((overrides as Record<string, unknown>).manual)
        ) {
          continue;
        }
        const rawSource =
          rec.charge_details.raw_charge_name ??
          rec.charge_details.charge_item ??
          "";
        const key = normalizeChargeAliasKey(rawSource);
        if (!key) continue;
        const matched = aliasLookup.get(key);
        if (matched && matched.standard_name !== rec.charge_details.charge_item) {
          const prevName = rec.charge_details.charge_item ?? "未命名";
          rec.charge_details.charge_item = matched.standard_name;
          rec.updated_at = ts;
          touchedDocs.add(rec.source_doc_id);
          appendRecordHistory(meta, rec.id, {
            action: "DICTIONARY_REAPPLIED",
            operator: "系统词典重算",
            timestamp: ts,
            reason: `按最新词典仅重算当前批标准费名：${prevName} → ${matched.standard_name}`,
          });
        }
      }

      for (const docId of touchedDocs) {
        reconcileDocRecords(next, docId, meta);
      }
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const retryDocMatch =
      /^\/api\/v1\/batches\/([^/]+)\/documents\/([^/]+)\/retry$/.exec(pathname);
    if (retryDocMatch && method === "POST") {
      const batch = batches.get(retryDocMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const docId = retryDocMatch[2];
      const next = cloneBatchDetail(batch);
      const targetDoc = next.documents.find((d) => d.id === docId);
      if (!targetDoc) {
        return errorJsonResponse(
          { code: "DOC_NOT_FOUND", message: "未找到指定文档" },
          404
        );
      }
      let retryOcrMode = next.ocr_mode;
      let hasReplacementFile = false;
      let hasPublicUrl = false;
      if (init?.body instanceof FormData) {
        const modeField = init.body.get("ocr_mode");
        if (
          modeField === "REMOTE_ONLY" ||
          modeField === "REMOTE_WITH_LOCAL_FALLBACK"
        ) {
          retryOcrMode = modeField;
        }
        if (init.body.get("file")) {
          hasReplacementFile = true;
        }
        const urlField = init.body.get("public_url");
        if (typeof urlField === "string" && urlField.trim()) {
          hasPublicUrl = true;
        }
      }
      if (hasReplacementFile && hasPublicUrl) {
        return errorJsonResponse(
          {
            code: "CONFLICTING_SOURCE_INPUTS",
            message: "请上传文件或提供公网 URL",
          },
          400
        );
      }
      const meta = getAuditMeta(next.id);
      meta.docRetryCount.set(docId, (meta.docRetryCount.get(docId) ?? 0) + 1);

      targetDoc.status = "METADATA_ONLY";
      targetDoc.extraction_channel =
        retryOcrMode === "REMOTE_WITH_LOCAL_FALLBACK" && !hasPublicUrl
          ? "LOCAL_FALLBACK"
          : "REMOTE_OCR";
      targetDoc.error_message = hasReplacementFile
        ? "已通过替换文件完成重新解析"
        : hasPublicUrl
          ? "已通过公网 URL 重试完成解析"
          : null;
      next.failed_documents = next.failed_documents.filter(
        (f) => f.doc_id !== docId
      );
      next.failed_files = next.failed_documents.length;
      next.succeeded_files = next.total_files - next.failed_files;
      next.status = next.failed_files > 0 ? "PARTIAL_ERROR" : "COMPLETED";
      next.updated_at = nextStubTimestamp();
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const forceParseMatch =
      /^\/api\/v1\/batches\/([^/]+)\/documents\/([^/]+)\/force-parse$/.exec(
        pathname
      );
    if (forceParseMatch && method === "POST") {
      const batch = batches.get(forceParseMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const docId = forceParseMatch[2];
      const next = cloneBatchDetail(batch);
      const targetDoc = next.documents.find((d) => d.id === docId);
      if (!targetDoc) {
        return errorJsonResponse(
          { code: "DOC_NOT_FOUND", message: "未找到指定文档" },
          404
        );
      }
      const meta = getAuditMeta(next.id);
      meta.docRetryCount.set(docId, (meta.docRetryCount.get(docId) ?? 0) + 1);

      targetDoc.status = "METADATA_ONLY";
      targetDoc.extraction_channel = "REMOTE_OCR";
      targetDoc.error_message = "已人工强制提取：图片仅含公司徽标，无计费金额";
      next.updated_at = nextStubTimestamp();
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const patchDocTotalsMatch =
      /^\/api\/v1\/batches\/([^/]+)\/documents\/([^/]+)$/.exec(pathname);
    if (patchDocTotalsMatch && method === "PATCH") {
      const batch = batches.get(patchDocTotalsMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const docId = patchDocTotalsMatch[2];
      const payload = parseLosslessJson<DocumentTotalsPatchRequest>(
        String(init?.body ?? "{}")
      );
      const next = cloneBatchDetail(batch);
      const targetDoc = next.documents.find((d) => d.id === docId);
      if (!targetDoc) {
        return errorJsonResponse(
          { code: "DOC_NOT_FOUND", message: "未找到指定文档" },
          404
        );
      }
      if (payload.subtotal_amount !== undefined) {
        targetDoc.subtotal_amount = payload.subtotal_amount;
      }
      if (payload.tax_amount !== undefined) {
        targetDoc.tax_amount = payload.tax_amount;
      }
      if (payload.total_amount !== undefined) {
        targetDoc.total_amount = payload.total_amount;
      }
      if (payload.total_currency !== undefined) {
        targetDoc.total_currency = payload.total_currency;
      }
      const meta = getAuditMeta(next.id);
      meta.docTotalsEdited.add(docId);
      reconcileDocRecords(next, docId, meta, { forceTotalsCheck: true });

      const ts = nextStubTimestamp();
      for (const rec of next.records) {
        if (rec.source_doc_id === docId) {
          rec.updated_at = ts;
          appendRecordHistory(meta, rec.id, {
            action: "DOC_TOTALS_PATCHED",
            operator: "财务复核员",
            timestamp: ts,
            reason: payload.review_note ?? "更新单据总额并重算同单行对账",
          });
        }
      }
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const manualRecordMatch = /^\/api\/v1\/batches\/([^/]+)\/records$/.exec(
      pathname
    );
    if (manualRecordMatch && method === "POST") {
      const batch = batches.get(manualRecordMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const payload = parseLosslessJson<ManualRecordCreate>(
        String(init?.body ?? "{}")
      );
      const next = cloneBatchDetail(batch);
      const ts = nextStubTimestamp();
      const newId = `rec-manual-${next.records.length + 1}`;
      const amtStr = readDecimalString(payload, "amount") ?? "0.00";
      next.records.push({
        id: newId,
        source_doc_id: payload.source_doc_id,
        source_line_index: next.records.length,
        operation: "CREATE",
        review_status: "AUTO_READY",
        match_level: "NONE",
        matched_existing_bill_id: null,
        matched_bill_ref: null,
        inherited_fields: [],
        candidate_bills: [],
        document_info: {
          document_type: "Manual Entry",
          reference_no: payload.reference_no ?? null,
          document_date: payload.document_date ?? null,
        },
        transaction_party: {
          issuer: payload.issuer ?? null,
          recipient: payload.recipient ?? null,
        },
        charge_details: {
          charge_item: payload.charge_item,
          raw_charge_name: payload.raw_charge_name ?? payload.charge_item,
          quantity: toSchemaDecimal("1"),
          unit_price: toSchemaDecimal(amtStr),
          amount: toSchemaDecimal(amtStr),
          currency: payload.currency,
          payment_term: payload.payment_term ?? null,
        },
        financial_attributes: {
          accounting_entity: next.accounting_entity,
          posting_type: payload.posting_type ?? null,
        },
        shipment_refs: {},
        source_evidence: { snippet: `人工补充漏提：${payload.reason}` },
        field_diffs: [],
        validation_alerts: [],
        ai_reasoning: [`财务人工补录：${payload.reason}`],
        human_overrides: { manual: true },
        reject_reason: null,
        created_at: ts,
        updated_at: ts,
      });
      const targetDoc = next.documents.find(
        (d) => d.id === payload.source_doc_id
      );
      if (targetDoc) {
        targetDoc.affected_record_ids = [
          ...(targetDoc.affected_record_ids ?? []),
          newId,
        ];
      }
      const meta = getAuditMeta(next.id);
      meta.recordEdited.add(newId);
      reconcileDocRecords(next, payload.source_doc_id, meta);
      appendRecordHistory(meta, newId, {
        action: "MANUAL_CREATED",
        operator: "财务复核员",
        timestamp: ts,
        reason: payload.reason,
      });
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next, 201);
    }

    const patchRecordMatch =
      /^\/api\/v1\/batches\/([^/]+)\/records\/([^/]+)$/.exec(pathname);
    if (patchRecordMatch && method === "PATCH") {
      const batch = batches.get(patchRecordMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const recordId = patchRecordMatch[2];
      const payload = parseLosslessJson<RecordPatchRequest>(
        String(init?.body ?? "{}")
      );
      const next = cloneBatchDetail(batch);
      const target = next.records.find((r) => r.id === recordId);
      if (!target) {
        return errorJsonResponse(
          { code: "RECORD_NOT_FOUND", message: "未找到对应账单记录" },
          404
        );
      }

      const meta = getAuditMeta(next.id);

      if (payload.charge_item !== undefined) {
        target.charge_details.charge_item = payload.charge_item;
      }
      if (payload.quantity !== undefined) {
        const qStr = readDecimalString(payload, "quantity");
        target.charge_details.quantity =
          qStr !== null ? toSchemaDecimal(qStr) : null;
      }
      if (payload.unit_price !== undefined) {
        const uStr = readDecimalString(payload, "unit_price");
        target.charge_details.unit_price =
          uStr !== null ? toSchemaDecimal(uStr) : null;
      }
      if (payload.amount !== undefined) {
        const aStr = readDecimalString(payload, "amount");
        target.charge_details.amount =
          aStr !== null ? toSchemaDecimal(aStr) : null;
      }
      if (payload.currency !== undefined) {
        target.charge_details.currency = payload.currency;
      }
      if (payload.payment_term !== undefined) {
        target.charge_details.payment_term = payload.payment_term;
      }
      if (payload.posting_type !== undefined) {
        target.financial_attributes.posting_type = payload.posting_type;
      }
      if (payload.document_type !== undefined) {
        target.document_info.document_type = payload.document_type;
      }
      if (payload.reference_no !== undefined) {
        target.document_info.reference_no = payload.reference_no;
      }
      if (payload.document_date !== undefined) {
        target.document_info.document_date = payload.document_date;
      }
      if (payload.issuer !== undefined) {
        target.transaction_party.issuer = payload.issuer;
      }
      if (payload.recipient !== undefined) {
        target.transaction_party.recipient = payload.recipient;
      }
      if (!target.shipment_refs) {
        target.shipment_refs = {};
      }
      if (payload.mbl_no !== undefined) {
        target.shipment_refs.mbl_nos = payload.mbl_no
          ? [{ value: payload.mbl_no, source: "人工修订" }]
          : [];
      }
      if (payload.hbl_no !== undefined) {
        target.shipment_refs.hbl_no = payload.hbl_no;
      }
      if (payload.booking_no !== undefined) {
        target.shipment_refs.booking_no = payload.booking_no;
      }
      if (payload.container_nos !== undefined) {
        target.shipment_refs.container_nos = payload.container_nos ?? [];
      }
      if (payload.force_create_new === true) {
        target.matched_existing_bill_id = null;
        target.matched_bill_ref = null;
        target.match_level = "NONE";
      } else if (payload.matched_existing_bill_id !== undefined) {
        target.matched_existing_bill_id = payload.matched_existing_bill_id;
        if (payload.matched_existing_bill_id) {
          const matchedBill = existingBills.find(
            (b) => b.id === payload.matched_existing_bill_id
          );
          const candidateBill = target.candidate_bills?.find(
            (c) => c.id === payload.matched_existing_bill_id
          );
          target.matched_bill_ref = matchedBill
            ? `${matchedBill.reference_no ?? matchedBill.id} / ${matchedBill.charge_item}`
            : candidateBill
              ? candidateBill.label
              : payload.matched_existing_bill_id;
          target.match_level = "L1_DOC_CHARGE";
        } else {
          target.matched_bill_ref = null;
          target.match_level = "NONE";
        }
      }

      const overridePatch = { ...payload } as Record<string, unknown>;
      delete overridePatch.raw_charge_name;
      delete overridePatch.review_note;
      for (const k of ["quantity", "unit_price", "amount"] as const) {
        if (overridePatch[k] !== undefined) {
          overridePatch[k] = readDecimalString(payload, k);
        }
      }
      target.human_overrides = {
        ...(target.human_overrides ?? {}),
        ...overridePatch,
      };

      meta.recordEdited.add(recordId);
      reconcileDocRecords(next, target.source_doc_id, meta);

      const ts = nextStubTimestamp();
      target.updated_at = ts;
      appendRecordHistory(meta, recordId, {
        action: "PATCH_RECORD",
        operator: "财务复核员",
        timestamp: ts,
        reason: payload.review_note ?? "保存全字段人工修订并重跑门禁",
        overrides: overridePatch,
      });
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const approveMatch =
      /^\/api\/v1\/batches\/([^/]+)\/records\/([^/]+)\/approve$/.exec(
        pathname
      );
    if (approveMatch && method === "POST") {
      const batch = batches.get(approveMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const payload = parseLosslessJson<{ reason?: string }>(
        String(init?.body ?? "{}")
      );
      const reason = (payload.reason ?? "").trim();
      const reasonLen = countCodePoints(reason);
      if (reasonLen < 2 || reasonLen > 500) {
        return errorJsonResponse(
          {
            code: "INVALID_REASON_LENGTH",
            message: "批准理由须为 2–500 个字符",
          },
          422
        );
      }
      const next = cloneBatchDetail(batch);
      const target = next.records.find((r) => r.id === approveMatch[2]);
      if (!target) {
        return errorJsonResponse(
          { code: "RECORD_NOT_FOUND", message: "未找到对应账单记录" },
          404
        );
      }

      const blockers = (target.validation_alerts ?? []).filter(
        (a) => a.severity === "BLOCKER"
      );
      if (blockers.length > 0) {
        return errorJsonResponse(
          {
            code: "HARD_BLOCKER_CANNOT_APPROVE",
            message:
              "该记录存在未解决的硬性数值或对账错误，请先修正金额/币种或选择驳回",
            details: {
              blocker_codes: Array.from(new Set(blockers.map((b) => b.code))),
            },
            request_id: "stub-422-blocker",
          },
          422
        );
      }

      const meta = getAuditMeta(next.id);
      const recordWaivers =
        meta.resolvedWaivers.get(target.id) ??
        new Map<string, { reason: string }>();
      target.validation_alerts = (target.validation_alerts ?? []).map((a) => {
        if (a.severity === "WARNING") {
          recordWaivers.set(makeWarningKey(a), { reason });
          return {
            ...a,
            severity: "RESOLVED",
            message: `${a.message}（特例批准：${reason}）`,
          };
        }
        return a;
      });
      meta.resolvedWaivers.set(target.id, recordWaivers);

      const ts = nextStubTimestamp();
      target.review_status = "APPROVED";
      target.operation = target.matched_existing_bill_id ? "UPDATE" : "CREATE";
      target.updated_at = ts;
      appendRecordHistory(meta, target.id, {
        action: "APPROVED",
        operator: "财务复核员",
        timestamp: ts,
        reason,
      });
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const rejectMatch =
      /^\/api\/v1\/batches\/([^/]+)\/records\/([^/]+)\/reject$/.exec(pathname);
    if (rejectMatch && method === "POST") {
      const batch = batches.get(rejectMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const payload = parseLosslessJson<{ reason?: string }>(
        String(init?.body ?? "{}")
      );
      const reason = (payload.reason ?? "").trim();
      const rejectLen = countCodePoints(reason);
      if (rejectLen < 2 || rejectLen > 500) {
        return errorJsonResponse(
          {
            code: "INVALID_REASON_LENGTH",
            message: "驳回理由须为 2–500 个字符",
          },
          422
        );
      }
      const next = cloneBatchDetail(batch);
      const target = next.records.find((r) => r.id === rejectMatch[2]);
      if (!target) {
        return errorJsonResponse(
          { code: "RECORD_NOT_FOUND", message: "未找到对应账单记录" },
          404
        );
      }
      const ts = nextStubTimestamp();
      target.review_status = "REJECTED";
      target.reject_reason = reason;
      target.updated_at = ts;

      const meta = getAuditMeta(next.id);
      meta.recordEdited.add(target.id);
      reconcileDocRecords(next, target.source_doc_id, meta);
      appendRecordHistory(meta, target.id, {
        action: "REJECTED",
        operator: "财务复核员",
        timestamp: ts,
        reason,
      });
      next.counts = computeCounts(next.records);
      next.updated_at = ts;
      batches.set(next.id, next);
      return jsonResponse(next);
    }

    const exportMatch = /^\/api\/v1\/batches\/([^/]+)\/export$/.exec(pathname);
    if (exportMatch && method === "GET") {
      const batch = batches.get(exportMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const rawMode = parsedUrl.searchParams.get("mode");
      const mode: ExportResponse["mode"] =
        rawMode === "full_standard" || rawMode === "audit"
          ? rawMode
          : "official";
      return jsonResponse(buildExportRecords(batch, mode, getAuditMeta(batch.id)));
    }

    const downloadMatch =
      /^\/api\/v1\/batches\/([^/]+)\/export\/download$/.exec(pathname);
    if (downloadMatch && method === "GET") {
      const batch = batches.get(downloadMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      const rawMode = parsedUrl.searchParams.get("mode");
      const mode: ExportResponse["mode"] =
        rawMode === "full_standard" || rawMode === "audit"
          ? rawMode
          : "official";
      const exported = buildExportRecords(batch, mode, getAuditMeta(batch.id));
      return jsonResponse(exported.records);
    }

    const syncMatch = /^\/api\/v1\/batches\/([^/]+)\/sync-baseline$/.exec(
      pathname
    );
    if (syncMatch && method === "POST") {
      const batch = batches.get(syncMatch[1]);
      if (!batch) {
        return errorJsonResponse(
          { code: "BATCH_NOT_FOUND", message: "未找到对应批次" },
          404
        );
      }
      let createdCount = 0;
      let updatedCount = 0;
      let skippedCount = 0;
      const fallbackTs = batch.updated_at;

      for (const rec of batch.records) {
        if (
          (rec.review_status !== "AUTO_READY" &&
            rec.review_status !== "APPROVED") ||
          (rec.operation !== "CREATE" && rec.operation !== "UPDATE")
        ) {
          continue;
        }
        const recUpdatedAt = rec.updated_at ?? fallbackTs;
        const ledgerKey = `${batch.id}::${rec.id}`;
        const prevSync = baselineSyncLedger.get(ledgerKey);

        if (prevSync && prevSync.syncedUpdatedAt === recUpdatedAt) {
          skippedCount++;
          continue;
        }

        const targetBillId =
          prevSync?.existingBillId ?? rec.matched_existing_bill_id ?? null;
        const existingBill = targetBillId
          ? existingBills.find((b) => b.id === targetBillId)
          : undefined;
        const amtStr = readDecimalString(rec.charge_details, "amount") ?? "0.00";
        const qtyStr = readDecimalString(rec.charge_details, "quantity");
        const unitStr = readDecimalString(rec.charge_details, "unit_price");

        if (existingBill) {
          existingBill.accounting_entity =
            rec.financial_attributes.accounting_entity;
          existingBill.document_type = rec.document_info.document_type;
          existingBill.reference_no = rec.document_info.reference_no;
          existingBill.document_date = rec.document_info.document_date;
          existingBill.issuer = rec.transaction_party.issuer;
          existingBill.recipient = rec.transaction_party.recipient;
          existingBill.charge_item =
            rec.charge_details.charge_item ?? existingBill.charge_item;
          existingBill.quantity =
            qtyStr !== null ? toSchemaDecimal(qtyStr) : null;
          existingBill.unit_price =
            unitStr !== null ? toSchemaDecimal(unitStr) : null;
          existingBill.amount = toSchemaDecimal(amtStr);
          existingBill.currency =
            rec.charge_details.currency ?? existingBill.currency;
          existingBill.payment_term = rec.charge_details.payment_term;
          existingBill.posting_type =
            rec.financial_attributes.posting_type ?? existingBill.posting_type;
          existingBill.version += 1;
          baselineSyncLedger.set(ledgerKey, {
            existingBillId: existingBill.id,
            syncedUpdatedAt: recUpdatedAt,
          });
          updatedCount++;
        } else {
          const newBillId = `bill-sync-${existingBills.length + 1}`;
          existingBills.push({
            id: newBillId,
            external_bill_key: `${rec.document_info.reference_no ?? rec.id}#${rec.charge_details.charge_item ?? "Fee"}`,
            accounting_entity: rec.financial_attributes.accounting_entity,
            document_type: rec.document_info.document_type,
            reference_no: rec.document_info.reference_no,
            document_date: rec.document_info.document_date,
            issuer: rec.transaction_party.issuer,
            recipient: rec.transaction_party.recipient,
            mbl_no: rec.shipment_refs?.mbl_nos?.[0]?.value ?? null,
            hbl_no: rec.shipment_refs?.hbl_no ?? null,
            booking_no: rec.shipment_refs?.booking_no ?? null,
            container_nos: rec.shipment_refs?.container_nos ?? [],
            charge_item: rec.charge_details.charge_item ?? "Handling Charge",
            raw_charge_name: rec.charge_details.raw_charge_name ?? null,
            quantity: qtyStr !== null ? toSchemaDecimal(qtyStr) : null,
            unit_price: unitStr !== null ? toSchemaDecimal(unitStr) : null,
            amount: toSchemaDecimal(amtStr),
            currency: rec.charge_details.currency ?? "USD",
            payment_term: rec.charge_details.payment_term,
            posting_type: rec.financial_attributes.posting_type ?? "AP",
            version: 1,
          });
          baselineSyncLedger.set(ledgerKey, {
            existingBillId: newBillId,
            syncedUpdatedAt: recUpdatedAt,
          });
          createdCount++;
        }
      }

      const res: SyncBaselineResult = {
        created_count: createdCount,
        updated_count: updatedCount,
        skipped_count: skippedCount,
      };
      return jsonResponse(res);
    }

    if (pathname === "/api/v1/eval-runs" && method === "GET") {
      return jsonResponse(evalRuns);
    }

    if (pathname === "/api/v1/eval-runs" && method === "POST") {
      const payload = parseLosslessJson<{
        run_mode: EvalRunSummary["run_mode"];
        k_trials?: number;
      }>(String(init?.body ?? "{}"));
      const requestedK =
        typeof payload.k_trials === "number" && Number.isFinite(payload.k_trials)
          ? Math.max(1, Math.min(10, Math.round(payload.k_trials)))
          : payload.run_mode === "OFFLINE_DETERMINISTIC"
            ? 1
            : 3;
      const ts = nextStubTimestamp();
      let run: EvalRunSummary;
      if (payload.run_mode === "OFFLINE_DETERMINISTIC") {
        run = {
          id: `eval-${evalRuns.length + 1}`,
          run_mode: "OFFLINE_DETERMINISTIC",
          status: "COMPLETED",
          error_message: null,
          model_name: "deterministic-rules",
          k_trials: requestedK,
          actual_trials: requestedK,
          total_cases: 12,
          schema_valid_rate: rawDecimal("1.0") as unknown as number,
          field_exact_match: rawDecimal("0.9792") as unknown as number,
          ar_ap_accuracy: rawDecimal("1.0") as unknown as number,
          operation_accuracy: rawDecimal("1.0") as unknown as number,
          conflict_recall: rawDecimal("1.0") as unknown as number,
          pass_at_k: rawDecimal("1.0") as unknown as number,
          pass_pow_k: rawDecimal("1.0") as unknown as number,
          report: {
            schema_valid_rate: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 12,
              denominator: 12,
            },
            field_exact_match: {
              rate: rawDecimal("0.9792") as unknown as number,
              numerator: 47,
              denominator: 48,
            },
            ar_ap_accuracy: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 12,
              denominator: 12,
            },
            operation_accuracy: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 12,
              denominator: 12,
            },
            conflict_recall: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 6,
              denominator: 6,
            },
            amount_accuracy: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 24,
              denominator: 24,
            },
            currency_accuracy: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 24,
              denominator: 24,
            },
            false_positive_rate: {
              rate: rawDecimal("0.0") as unknown as number,
              numerator: 0,
              denominator: 12,
            },
            alert_precision: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 8,
              denominator: 8,
            },
            pass_at_k: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 12,
              denominator: 12,
            },
            pass_pow_k: {
              rate: rawDecimal("1.0") as unknown as number,
              numerator: 12,
              denominator: 12,
            },
            avg_latency_ms: rawDecimal("18.4") as unknown as number,
            executed_cases: 12 * requestedK,
            llm_calls: 0,
            ocr_calls: 0,
            self_repair_evidence: "UNVERIFIED",
            failing_cases: [],
            case_results: [],
          },
          created_at: ts,
        };
      } else if (payload.run_mode === "LIVE_E2E") {
        run = {
          id: `eval-${evalRuns.length + 1}`,
          run_mode: "LIVE_E2E",
          status: "FAILED",
          error_message:
            "第 2 轮远端 OCR 网关连接中断（本地二进制样本缺少公网中转 URL），评测中止，未生成有效汇总指标",
          model_name: "deepseek-chat + remote-ocr",
          k_trials: requestedK,
          actual_trials: 1,
          total_cases: 12,
          schema_valid_rate: null,
          field_exact_match: null,
          ar_ap_accuracy: null,
          operation_accuracy: null,
          conflict_recall: null,
          pass_at_k: null,
          pass_pow_k: null,
          report: {
            schema_valid_rate: { rate: null, numerator: 0, denominator: 0 },
            field_exact_match: { rate: null, numerator: 0, denominator: 0 },
            ar_ap_accuracy: { rate: null, numerator: 0, denominator: 0 },
            operation_accuracy: { rate: null, numerator: 0, denominator: 0 },
            conflict_recall: { rate: null, numerator: 0, denominator: 0 },
            amount_accuracy: { rate: null, numerator: 0, denominator: 0 },
            currency_accuracy: { rate: null, numerator: 0, denominator: 0 },
            false_positive_rate: { rate: null, numerator: 0, denominator: 0 },
            alert_precision: { rate: null, numerator: 0, denominator: 0 },
            pass_at_k: { rate: null, numerator: 0, denominator: 0 },
            pass_pow_k: { rate: null, numerator: 0, denominator: 0 },
            avg_latency_ms: null,
            executed_cases: 0,
            llm_calls: 0,
            ocr_calls: 1,
            self_repair_evidence: "UNVERIFIED",
            failing_cases: [
              {
                trial: 2,
                group_id: "G02",
                case_id: "real-img-sample",
                reason: "OcrGatewayError: 远端 OCR 网关连接中断",
                expected: "完成当前用例并返回可评分结果",
                actual: "REMOTE_OCR_UNAVAILABLE",
              },
            ],
            case_results: [],
          },
          created_at: ts,
        };
      } else {
        run = {
          id: `eval-${evalRuns.length + 1}`,
          run_mode: "LIVE_LLM",
          status: "UNVERIFIED",
          error_message:
            "未配置真实 DEEPSEEK_API_KEY 密钥（前端独立演示桩拒绝使用离线替身伪造联机分数）",
          model_name: "deepseek-chat",
          k_trials: requestedK,
          actual_trials: 0,
          total_cases: 12,
          schema_valid_rate: null,
          field_exact_match: null,
          ar_ap_accuracy: null,
          operation_accuracy: null,
          conflict_recall: null,
          pass_at_k: null,
          pass_pow_k: null,
          report: {
            schema_valid_rate: { rate: null, numerator: 0, denominator: 0 },
            field_exact_match: { rate: null, numerator: 0, denominator: 0 },
            ar_ap_accuracy: { rate: null, numerator: 0, denominator: 0 },
            operation_accuracy: { rate: null, numerator: 0, denominator: 0 },
            conflict_recall: { rate: null, numerator: 0, denominator: 0 },
            amount_accuracy: { rate: null, numerator: 0, denominator: 0 },
            currency_accuracy: { rate: null, numerator: 0, denominator: 0 },
            false_positive_rate: { rate: null, numerator: 0, denominator: 0 },
            alert_precision: { rate: null, numerator: 0, denominator: 0 },
            pass_at_k: { rate: null, numerator: 0, denominator: 0 },
            pass_pow_k: { rate: null, numerator: 0, denominator: 0 },
            avg_latency_ms: null,
            executed_cases: 0,
            llm_calls: 0,
            ocr_calls: 0,
            self_repair_evidence: "UNVERIFIED",
            failing_cases: [],
            case_results: [],
          },
          created_at: ts,
        };
      }
      evalRuns.unshift(run);
      return jsonResponse(run, 201);
    }

    return errorJsonResponse(
      { code: "NOT_IMPLEMENTED", message: `未处理的演示接口：${method} ${pathname}` },
      404
    );
  };
}
