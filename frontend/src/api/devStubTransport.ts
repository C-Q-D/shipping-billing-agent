import {
  parseLosslessJson,
  rawDecimal,
  stringifyLosslessJson,
  toSchemaDecimal,
} from "./decimalJson.ts";
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
type ReviewBundleV1 = components["schemas"]["ReviewBundleV1"];
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
      affected_record_ids: ["rec-img-wharf-admin", "rec-img-wharf"],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [],
        hbl_no: null,
        booking_no: "ORFR1618731",
        container_nos: [],
      },
      subtotal_amount: toSchemaDecimal("26.90"),
      tax_amount: toSchemaDecimal("0.00"),
      total_amount: toSchemaDecimal("26.90"),
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
      affected_record_ids: ["rec-pdf1-oncarriage"],
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
        container_nos: [],
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
      affected_record_ids: [
        "rec-pdf2-hc1",
        "rec-pdf2-det",
        "rec-pdf2-hc2",
        "rec-dn-update",
      ],
      excluded_clauses: [],
      shipment_refs: {
        mbl_nos: [{ value: "MAEU275752922", source: "PDF 示例账单2.pdf" }],
        hbl_no: null,
        booking_no: null,
        container_nos: [],
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

  const records: BillingRecordDetail[] = [
    {
      id: "rec-dn-update",
      source_doc_id: "doc-pdf-2",
      source_line_index: 0,
      operation: "UPDATE",
      review_status: "AUTO_READY",
      match_level: "L1",
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
        quantity: toSchemaDecimal("1"),
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
          code: "CONTAINER_CHECK_DIGIT",
          severity: "WARNING",
          field: "container_nos",
          message: "箱号建议核对第 11 位 ISO 6346 校验码",
          expected: "OOLU1234565",
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
        container_nos: ["OOLU1234560"],
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
      id: "rec-img-wharf",
      source_doc_id: "doc-img-sample",
      source_line_index: 0,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
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
        charge_item: "Wharfage",
        raw_charge_name: "WHARFAGE",
        quantity: null,
        unit_price: toSchemaDecimal("2.85"),
        amount: toSchemaDecimal("21.90"),
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
        snippet: "WHARFAGE | RATE 2.85 PTT | AMOUNT 21.90 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: [
        "原文仅印出费率 2.85 与金额 21.90，未印数量故 quantity 保持 null 不反除造数",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-img-wharf-admin",
      source_doc_id: "doc-img-sample",
      source_line_index: 1,
      operation: "NO_CHANGE",
      review_status: "AUTO_READY",
      match_level: "L1",
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
      id: "rec-pdf1-oncarriage",
      source_doc_id: "doc-pdf-1",
      source_line_index: 0,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
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
        container_nos: [],
      },
      source_evidence: {
        source_file: "PDF示例账单1.pdf",
        page: 4,
        snippet: "On Carriage Additional - US Em | Collect | USD 245.00",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: [
        "第 1-3 页条件罚金与费率条款已归入已排除条款区，仅提取第 4 页实际计费行",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-pdf2-hc1",
      source_doc_id: "doc-pdf-2",
      source_line_index: 2,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Debit Note",
        reference_no: "DC-108478",
        document_date: "2026-10-24",
      },
      transaction_party: {
        issuer: "NEPTUNE SHIPPING LIMITED (LAX)",
        recipient: "OVERSEAS PARTNER",
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "HANDLING CHARGE",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("65.00"),
        amount: toSchemaDecimal("65.00"),
        currency: "USD",
        payment_term: "CC",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AR" : null,
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
        snippet: "HANDLING CHARGE | CC | 65.00 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: [
        "同单 DC-108478 合法保留 65.00 与 35.00 两条同名 Handling Charge 明细",
      ],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-pdf2-det",
      source_doc_id: "doc-pdf-2",
      source_line_index: 3,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Debit Note",
        reference_no: "DC-108478",
        document_date: "2026-10-24",
      },
      transaction_party: {
        issuer: "NEPTUNE SHIPPING LIMITED (LAX)",
        recipient: "OVERSEAS PARTNER",
      },
      charge_details: {
        charge_item: "Detention Free Time Charge",
        raw_charge_name: "4 ADD'L DETENTION FREE TIME CHARGE",
        quantity: toSchemaDecimal("4"),
        unit_price: toSchemaDecimal("327.60"),
        amount: toSchemaDecimal("1310.40"),
        currency: "USD",
        payment_term: "CC",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AR" : null,
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
        snippet: "4 ADD'L DETENTION FREE TIME CHARGE | CC | 1310.40 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: ["单据总计 65.00 + 1310.40 + 35.00 = 1410.40 对账通过"],
      human_overrides: {},
      reject_reason: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: "rec-pdf2-hc2",
      source_doc_id: "doc-pdf-2",
      source_line_index: 4,
      operation: "CREATE",
      review_status: "AUTO_READY",
      match_level: "NONE",
      matched_existing_bill_id: null,
      matched_bill_ref: null,
      inherited_fields: [],
      candidate_bills: [],
      document_info: {
        document_type: "Debit Note",
        reference_no: "DC-108478",
        document_date: "2026-10-24",
      },
      transaction_party: {
        issuer: "NEPTUNE SHIPPING LIMITED (LAX)",
        recipient: "OVERSEAS PARTNER",
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "HANDLING CHARGE",
        quantity: toSchemaDecimal("1"),
        unit_price: toSchemaDecimal("35.00"),
        amount: toSchemaDecimal("35.00"),
        currency: "USD",
        payment_term: "CC",
      },
      financial_attributes: {
        accounting_entity: entity,
        posting_type: isNeptune ? "AR" : null,
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
        snippet: "HANDLING CHARGE (LINE 2) | CC | 35.00 USD",
      },
      field_diffs: [],
      validation_alerts: [],
      ai_reasoning: ["同单合法同名第二行 Handling Charge 35.00 独立成条"],
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

function buildExportRecords(
  batch: BatchDetail,
  mode: ExportResponse["mode"]
): ExportResponse {
  let excludedPending = 0;
  let excludedRejected = 0;
  const included: StandardOutputRecord[] = [];

  for (const rec of batch.records) {
    if (rec.review_status === "REJECTED") {
      excludedRejected++;
      if (mode !== "audit") {
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
      charge_details: rec.charge_details,
      financial_attributes: rec.financial_attributes,
      audit_trail: {
        ai_reasoning: rec.ai_reasoning,
        match_level: rec.match_level,
        matched_bill_ref: rec.matched_bill_ref ?? null,
        ...(mode === "audit"
          ? {
              record_id: rec.id,
              review_status: rec.review_status,
              reject_reason: rec.reject_reason ?? null,
              source_evidence: rec.source_evidence ?? {},
              validation_alerts: rec.validation_alerts ?? [],
              human_overrides: rec.human_overrides ?? {},
              retry_count: 0,
              human_review_history: [],
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
  const pollSteps = new Map<string, number>([[initialBatch.id, STAGE_SEQUENCE.length]]);

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
      const created: AccountingEntity = {
        id: `ent-${entitySeq++}`,
        canonical_name: payload.canonical_name,
        aliases: payload.aliases ?? [],
        is_default: Boolean(payload.is_default),
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
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
      target.canonical_name = payload.canonical_name;
      target.aliases = payload.aliases ?? [];
      target.is_default = Boolean(payload.is_default);
      target.updated_at = new Date().toISOString();
      return jsonResponse(target);
    }

    if (pathname === "/api/v1/dictionary" && method === "GET") {
      return jsonResponse(dictionary);
    }

    if (pathname === "/api/v1/dictionary" && method === "POST") {
      const payload = parseLosslessJson<ChargeDictionaryCreate>(
        String(init?.body ?? "{}")
      );
      const entry: ChargeDictionaryEntry = {
        id: `dict-${dictSeq++}`,
        raw_alias: payload.raw_alias,
        standard_name: payload.standard_name,
        category: payload.category ?? "general",
        is_builtin: false,
        created_at: new Date().toISOString(),
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
      if (payload.replace_existing) {
        existingBills = [];
      }
      let imported = 0;
      for (const item of payload.bills) {
        existingBills.push({
          id: `bill-imp-${existingBills.length + 1}`,
          accounting_entity:
            String(
              item.accounting_entity ??
                payload.default_accounting_entity ??
                "NEPTUNE SHIPPING LIMITED"
            ),
          charge_item: String(item.charge_item ?? "Handling Charge"),
          amount:
            typeof item.amount === "number"
              ? item.amount
              : toSchemaDecimal("0.00"),
          currency: String(item.currency ?? "USD"),
          posting_type: item.posting_type === "AR" ? "AR" : "AP",
          reference_no:
            typeof item.reference_no === "string" ? item.reference_no : null,
          version: 1,
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
      const bundle = parseLosslessJson<ReviewBundleV1>(
        String(init?.body ?? "{}")
      );
      if (bundle.bundle_version !== "1.0") {
        return errorJsonResponse(
          {
            code: "INVALID_BUNDLE_VERSION",
            message: "仅支持 bundle_version 为 1.0 的复核包",
          },
          400
        );
      }
      batchSeq++;
      const id = `batch-bundle-0${batchSeq}`;
      const detail: BatchDetail = {
        id,
        short_code: bundle.short_code ?? `CLI-1120-0${batchSeq}`,
        accounting_entity: bundle.accounting_entity,
        source_channel: "CLI",
        status: "COMPLETED",
        stage: "COMPLETED",
        stage_message: "已从 CLI 复核包接手还原工作现场",
        ocr_mode: bundle.ocr_mode ?? "REMOTE_ONLY",
        total_files: bundle.documents.length,
        succeeded_files: bundle.documents.filter((d) => d.status !== "FAILED")
          .length,
        failed_files: bundle.documents.filter((d) => d.status === "FAILED")
          .length,
        counts: computeCounts(bundle.records),
        documents: bundle.documents,
        failed_documents: bundle.documents
          .filter((d) => d.status === "FAILED")
          .map((d) => ({
            doc_id: d.id,
            filename: d.sanitized_filename,
            error_message: d.error_message ?? "文件解析失败",
            affected_record_ids: d.affected_record_ids ?? [],
          })),
        records: bundle.records,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      batches.set(id, detail);
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
      return jsonResponse(batch);
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
      targetDoc.status = "METADATA_ONLY";
      targetDoc.extraction_channel = "LOCAL_FALLBACK";
      targetDoc.error_message = null;
      next.failed_documents = next.failed_documents.filter(
        (f) => f.doc_id !== docId
      );
      next.failed_files = next.failed_documents.length;
      next.succeeded_files = next.total_files - next.failed_files;
      next.status = next.failed_files > 0 ? "PARTIAL_ERROR" : "COMPLETED";
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
      targetDoc.status = "METADATA_ONLY";
      targetDoc.extraction_channel = "REMOTE_OCR";
      targetDoc.error_message = "已人工强制提取：图片仅含公司徽标，无计费金额";
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
      next.records.push({
        id: `rec-manual-${next.records.length + 1}`,
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
          unit_price: payload.amount,
          amount: payload.amount,
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
      });
      next.counts = computeCounts(next.records);
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
      if (payload.charge_item !== undefined) {
        target.charge_details.charge_item = payload.charge_item;
      }
      if (payload.quantity !== undefined) {
        target.charge_details.quantity = payload.quantity;
      }
      if (payload.unit_price !== undefined) {
        target.charge_details.unit_price = payload.unit_price;
      }
      if (payload.amount !== undefined) {
        target.charge_details.amount = payload.amount;
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
      target.human_overrides = {
        ...(target.human_overrides ?? {}),
        ...payload,
      };
      next.counts = computeCounts(next.records);
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
            details: { blocker_codes: blockers.map((b) => b.code) },
            request_id: "stub-422-blocker",
          },
          422
        );
      }
      target.review_status = "APPROVED";
      target.operation = target.matched_existing_bill_id ? "UPDATE" : "CREATE";
      next.counts = computeCounts(next.records);
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
      const payload = parseLosslessJson<{ reason: string }>(
        String(init?.body ?? "{}")
      );
      const next = cloneBatchDetail(batch);
      const target = next.records.find((r) => r.id === rejectMatch[2]);
      if (!target) {
        return errorJsonResponse(
          { code: "RECORD_NOT_FOUND", message: "未找到对应账单记录" },
          404
        );
      }
      target.review_status = "REJECTED";
      target.reject_reason = payload.reason;
      next.counts = computeCounts(next.records);
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
      return jsonResponse(buildExportRecords(batch, mode));
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
      const exported = buildExportRecords(batch, mode);
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
      const res: SyncBaselineResult = {
        created_count: batch.counts.create,
        updated_count: batch.counts.update,
        skipped_count: batch.counts.no_change,
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
      const run: EvalRunSummary =
        payload.run_mode === "OFFLINE_DETERMINISTIC"
          ? {
              id: `eval-${evalRuns.length + 1}`,
              run_mode: "OFFLINE_DETERMINISTIC",
              status: "COMPLETED",
              error_message: null,
              model_name: "deterministic-stub",
              k_trials: 1,
              actual_trials: 1,
              total_cases: 10,
              schema_valid_rate: rawDecimal("1.0") as unknown as number,
              field_exact_match: rawDecimal("0.98") as unknown as number,
              ar_ap_accuracy: rawDecimal("1.0") as unknown as number,
              operation_accuracy: rawDecimal("1.0") as unknown as number,
              conflict_recall: rawDecimal("1.0") as unknown as number,
              pass_at_k: null,
              pass_pow_k: null,
              created_at: new Date().toISOString(),
            }
          : {
              id: `eval-${evalRuns.length + 1}`,
              run_mode: payload.run_mode,
              status: "UNVERIFIED",
              error_message:
                "未配置真实 LLM API 密钥（前端独立演示桩不伪造联机分数）",
              model_name: "deepseek-flash",
              k_trials: payload.k_trials ?? 3,
              actual_trials: 0,
              total_cases: 10,
              schema_valid_rate: null,
              field_exact_match: null,
              ar_ap_accuracy: null,
              operation_accuracy: null,
              conflict_recall: null,
              pass_at_k: null,
              pass_pow_k: null,
              created_at: new Date().toISOString(),
            };
      evalRuns.unshift(run);
      return jsonResponse(run, 201);
    }

    return errorJsonResponse(
      { code: "NOT_IMPLEMENTED", message: `未处理的演示接口：${method} ${pathname}` },
      404
    );
  };
}
