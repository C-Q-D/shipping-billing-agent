import type {
  AccountingEntity,
  AccountingEntityUpsert,
  BatchDetail,
  BillingRecordDetail,
  ChargeDictionaryCreate,
  EvalRunSummary,
  ExistingBill,
  ExportResponse,
  ReviewBundleV1,
  SourceDocumentSummary,
} from "./client.ts";
import {
  parseLosslessJson,
  rawDecimal,
  readDecimalString,
  stringifyLosslessJson,
  toSchemaDecimal,
} from "./decimalJson.ts";

export const NO_EVAL_RESULT_LABEL = "无结果";

export function parseEntityAliasesInput(raw: string): string[] {
  const tokens = raw
    .split(/[\r\n,;，；、]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  const seen = new Set<string>();
  const result: string[] = [];
  for (const token of tokens) {
    const lower = token.toLowerCase();
    if (!seen.has(lower)) {
      seen.add(lower);
      result.push(token);
    }
  }
  return result;
}

export function validateAccountingEntityInput(
  input: {
    canonicalName: string;
    aliasesText: string;
    isDefault: boolean;
  },
  existingEntities: ReadonlyArray<AccountingEntity>,
  editingId?: string | null
):
  | { valid: true; payload: AccountingEntityUpsert }
  | { valid: false; error: string } {
  const canonical = input.canonicalName.trim();
  if (!canonical) {
    return { valid: false, error: "记账主体规范名称不能为空" };
  }
  if (canonical.length > 120) {
    return { valid: false, error: "记账主体规范名称不得超过 120 个字符" };
  }
  const lower = canonical.toLowerCase();
  const duplicate = existingEntities.find(
    (e) =>
      e.id !== editingId && e.canonical_name.trim().toLowerCase() === lower
  );
  if (duplicate) {
    return {
      valid: false,
      error: `已存在同名记账主体「${duplicate.canonical_name}」`,
    };
  }
  return {
    valid: true,
    payload: {
      canonical_name: canonical,
      aliases: parseEntityAliasesInput(input.aliasesText),
      is_default: Boolean(input.isDefault),
    },
  };
}

export function normalizeChargeAliasKey(raw: string): string {
  const nfkc = raw
    .normalize("NFKC")
    .replace(/[’‘]/g, "'")
    .replace(/[–—]/g, "-")
    .toLowerCase();
  return nfkc
    .replace(/^[^\p{L}\p{N}_]+|[^\p{L}\p{N}_]+$/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function validateChargeDictionaryInput(
  rawAlias: string,
  standardName: string,
  category: string
):
  | { valid: true; payload: ChargeDictionaryCreate }
  | { valid: false; error: string } {
  const alias = rawAlias.trim();
  const std = standardName.trim();
  const cat = category.trim() || "general";
  if (!alias) {
    return { valid: false, error: "请输入原始费用别名（raw_alias）" };
  }
  if (!std) {
    return { valid: false, error: "请输入映射后的标准费用名（standard_name）" };
  }
  if (alias.length > 120 || std.length > 120) {
    return { valid: false, error: "费用名称长度不能超过 120 个字符" };
  }
  return {
    valid: true,
    payload: {
      raw_alias: alias,
      standard_name: std,
      category: cat,
    },
  };
}

function parseCsvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
      continue;
    }

    if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field.trim());
      field = "";
    } else if (ch === "\r" || ch === "\n") {
      if (ch === "\r" && text[i + 1] === "\n") {
        i++;
      }
      row.push(field.trim());
      if (row.some((cell) => cell.length > 0)) {
        rows.push(row);
      }
      row = [];
      field = "";
    } else {
      field += ch;
    }
  }

  if (inQuotes) {
    throw new Error("CSV 引号未闭合，请检查双引号配对");
  }

  row.push(field.trim());
  if (row.some((cell) => cell.length > 0)) {
    rows.push(row);
  }
  return rows;
}

function preserveBillObjectDecimals(
  rawItem: Record<string, unknown>,
  defaultAccountingEntity?: string
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...rawItem };
  for (const decField of ["amount", "quantity", "unit_price"] as const) {
    const str = readDecimalString(rawItem, decField);
    if (str !== null) {
      next[decField] = toSchemaDecimal(str);
    } else if (decField === "amount") {
      throw new Error("导入的历史账单缺少合法金额字段 (amount)");
    }
  }

  const entityVal =
    typeof next.accounting_entity === "string" && next.accounting_entity.trim()
      ? next.accounting_entity.trim()
      : (defaultAccountingEntity ?? "").trim();
  if (entityVal) {
    next.accounting_entity = entityVal;
  }

  if (typeof next.currency === "string" && next.currency.trim()) {
    next.currency = next.currency.trim().toUpperCase();
  } else {
    next.currency = "USD";
  }

  if (typeof next.charge_item !== "string" || !next.charge_item.trim()) {
    throw new Error("导入的历史账单缺少费用名称 (charge_item)");
  }
  next.charge_item = next.charge_item.trim();

  if (typeof next.posting_type === "string") {
    const upper = next.posting_type.trim().toUpperCase();
    next.posting_type = upper === "AR" || upper === "AP" ? upper : "AP";
  }

  return next;
}

export function parseExistingBillsImportInput(
  rawText: string,
  options: { filename?: string; defaultAccountingEntity?: string } = {}
):
  | { valid: true; bills: Array<Record<string, unknown>> }
  | { valid: false; error: string } {
  const trimmed = rawText.replace(/^\uFEFF/, "").trim();
  if (!trimmed) {
    return { valid: false, error: "请先选择 JSON/CSV 文件或粘贴历史账单内容" };
  }

  const isCsvByName = (options.filename ?? "").toLowerCase().endsWith(".csv");
  const looksLikeJson =
    trimmed.startsWith("[") || trimmed.startsWith("{");

  if (looksLikeJson && !isCsvByName) {
    try {
      const parsed = parseLosslessJson<unknown>(trimmed);
      const rawList = Array.isArray(parsed)
        ? parsed
        : parsed &&
            typeof parsed === "object" &&
            Array.isArray((parsed as { bills?: unknown }).bills)
          ? (parsed as { bills: unknown[] }).bills
          : null;
      if (!rawList) {
        return {
          valid: false,
          error: "历史账单 JSON 必须为数组，或包含 bills 数组的对象",
        };
      }
      if (rawList.length === 0) {
        return { valid: false, error: "待导入的历史账单列表不能为空" };
      }
      const normalized = rawList.map((item, idx) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          throw new Error(`第 ${idx + 1} 条账单格式无效，须为 JSON 对象`);
        }
        return preserveBillObjectDecimals(
          item as Record<string, unknown>,
          options.defaultAccountingEntity
        );
      });
      return { valid: true, bills: normalized };
    } catch (err) {
      return {
        valid: false,
        error:
          err instanceof Error
            ? err.message
            : "历史账单 JSON 解析失败，请检查语法",
      };
    }
  }

  try {
    const rows = parseCsvRows(trimmed);
    if (rows.length < 2) {
      return {
        valid: false,
        error: "CSV 至少需包含 1 行表头与 1 行账单数据",
      };
    }
    const headers = rows[0].map((h) => h.trim());
    if (!headers.includes("charge_item") || !headers.includes("amount")) {
      return {
        valid: false,
        error: "CSV 表头必须至少包含 charge_item 与 amount 列",
      };
    }
    const bills: Array<Record<string, unknown>> = [];
    for (let r = 1; r < rows.length; r++) {
      const cells = rows[r];
      const record: Record<string, unknown> = {};
      for (let c = 0; c < headers.length; c++) {
        const key = headers[c];
        const cell = (cells[c] ?? "").trim();
        if (!key || cell === "") continue;
        if (key === "amount" || key === "quantity" || key === "unit_price") {
          record[key] = rawDecimal(cell).raw;
        } else if (key === "container_nos") {
          record[key] = cell
            .split(/[;|]+/)
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean);
        } else if (key === "payment_term") {
          const upper = cell.toUpperCase();
          if (upper === "PP" || upper === "CC") {
            record[key] = upper;
          }
        } else {
          record[key] = cell;
        }
      }
      bills.push(
        preserveBillObjectDecimals(record, options.defaultAccountingEntity)
      );
    }
    return { valid: true, bills };
  } catch (err) {
    return {
      valid: false,
      error: err instanceof Error ? err.message : "CSV 解析失败",
    };
  }
}

export interface EvalMetricFraction {
  passed: number;
  total: number;
}

export interface FormattedEvalMetric {
  displayText: string;
  fractionText: string | null;
  isUnavailable: boolean;
}

export function formatEvalMetricValue(
  run: Pick<EvalRunSummary, "status">,
  value: number | null | undefined,
  fraction?: EvalMetricFraction | null
): FormattedEvalMetric {
  if (
    run.status === "UNVERIFIED" ||
    run.status === "FAILED" ||
    value === null ||
    value === undefined ||
    typeof value !== "number" ||
    !Number.isFinite(value)
  ) {
    return {
      displayText: NO_EVAL_RESULT_LABEL,
      fractionText: null,
      isUnavailable: true,
    };
  }

  const pct = value * 100;
  const rounded2 = Math.round(pct * 100) / 100;
  const rounded1 = Math.round(pct * 10) / 10;
  const displayText =
    Math.abs(rounded2 - rounded1) < 1e-6
      ? `${rounded2.toFixed(1)}%`
      : `${rounded2.toFixed(2)}%`;

  const fractionText =
    fraction &&
    typeof fraction.passed === "number" &&
    typeof fraction.total === "number" &&
    Number.isFinite(fraction.passed) &&
    Number.isFinite(fraction.total) &&
    fraction.total >= 0
      ? `${fraction.passed} / ${fraction.total}`
      : null;

  return {
    displayText,
    fractionText,
    isUnavailable: false,
  };
}

export interface EvalMetricRowView {
  key: string;
  label: string;
  description: string;
  displayText: string;
  fractionText: string | null;
  isUnavailable: boolean;
}

export interface EvalRunViewModel {
  id: string;
  runModeLabel: string;
  status: EvalRunSummary["status"];
  statusLabel: string;
  statusTone: "ok" | "warn" | "error";
  modelName: string;
  trialsText: string;
  totalCases: number;
  createdAt: string;
  hasFailureBanner: boolean;
  failureReason: string | null;
  truthMetrics: EvalMetricRowView[];
  stabilityMetrics: EvalMetricRowView[];
}

function readFractionFromReport(
  report: Record<string, unknown> | undefined,
  metricKey: string
): EvalMetricFraction | null {
  if (!report) return null;
  const containers = [report.metric_counts, report.details, report.counts];
  for (const container of containers) {
    if (!container || typeof container !== "object" || Array.isArray(container)) {
      continue;
    }
    const entry = (container as Record<string, unknown>)[metricKey];
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      continue;
    }
    const rec = entry as Record<string, unknown>;
    const passed =
      typeof rec.passed === "number"
        ? rec.passed
        : typeof rec.numerator === "number"
          ? rec.numerator
          : null;
    const total =
      typeof rec.total === "number"
        ? rec.total
        : typeof rec.denominator === "number"
          ? rec.denominator
          : null;
    if (passed !== null && total !== null) {
      return { passed, total };
    }
  }
  return null;
}

function readReportOptionalNumber(
  report: Record<string, unknown> | undefined,
  key: string
): number | null {
  if (!report) return null;
  const val = report[key];
  return typeof val === "number" && Number.isFinite(val) ? val : null;
}

export function getEvalRunModeLabel(
  mode: EvalRunSummary["run_mode"]
): string {
  switch (mode) {
    case "OFFLINE_DETERMINISTIC":
      return "离线确定性基准 (OFFLINE_DETERMINISTIC)";
    case "LIVE_LLM":
      return "联机 LLM 稳定性 (LIVE_LLM)";
    case "LIVE_E2E":
      return "联机全链路端到端 (LIVE_E2E)";
  }
}

export function buildEvalRunViewModel(run: EvalRunSummary): EvalRunViewModel {
  const report =
    run.report && typeof run.report === "object" ? run.report : undefined;

  const statusMeta: {
    label: string;
    tone: "ok" | "warn" | "error";
  } =
    run.status === "COMPLETED"
      ? { label: "已完成 (COMPLETED)", tone: "ok" }
      : run.status === "UNVERIFIED"
        ? { label: "未验证 (UNVERIFIED)", tone: "warn" }
        : { label: "执行失败 (FAILED)", tone: "error" };

  const makeRow = (
    key: string,
    label: string,
    description: string,
    rawVal: number | null | undefined
  ): EvalMetricRowView => {
    const formatted = formatEvalMetricValue(
      run,
      rawVal,
      readFractionFromReport(report, key)
    );
    return {
      key,
      label,
      description,
      ...formatted,
    };
  };

  const truthMetrics: EvalMetricRowView[] = [
    makeRow(
      "schema_valid_rate",
      "Schema 合规率",
      "输出 JSON 满足标准输出 Schema 比例",
      run.schema_valid_rate
    ),
    makeRow(
      "field_exact_match",
      "核心字段精确率",
      "相对标注真值（Decimal 等值）精确一致率",
      run.field_exact_match
    ),
    makeRow(
      "ar_ap_accuracy",
      "AR/AP 方向准确率",
      "按当前记账主体视角判断应收/应付准确度",
      run.ar_ap_accuracy
    ),
    makeRow(
      "operation_accuracy",
      "四态操作准确率",
      "CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW 判定",
      run.operation_accuracy
    ),
    makeRow(
      "conflict_recall",
      "异常冲突召回率",
      "平账差额、坏箱号与跨文档矛盾拦截召回",
      run.conflict_recall
    ),
  ];

  const amountAcc = readReportOptionalNumber(report, "amount_accuracy");
  if (amountAcc !== null || run.status !== "COMPLETED") {
    truthMetrics.push(
      makeRow(
        "amount_accuracy",
        "金额无损一致率",
        "高精十进制金额相对真值零舍入误差一致率",
        amountAcc
      )
    );
  }

  const currencyAcc = readReportOptionalNumber(report, "currency_accuracy");
  if (currencyAcc !== null || run.status !== "COMPLETED") {
    truthMetrics.push(
      makeRow(
        "currency_accuracy",
        "币种识别一致率",
        "ISO 4217 三位币种代码准确匹配率",
        currencyAcc
      )
    );
  }

  const falsePositiveRate = readReportOptionalNumber(
    report,
    "false_positive_rate"
  );
  if (falsePositiveRate !== null || run.status !== "COMPLETED") {
    truthMetrics.push(
      makeRow(
        "false_positive_rate",
        "非计费条款误报率",
        "违约金/免费豁免条款误生成账单比例（越低越好）",
        falsePositiveRate
      )
    );
  }

  const stabilityMetrics: EvalMetricRowView[] = [
    makeRow(
      "pass_at_k",
      `pass@${run.k_trials}`,
      `至少 1 轮在 ${run.k_trials} 次采样中全量通过率`,
      run.pass_at_k
    ),
    makeRow(
      "pass_pow_k",
      `pass^${run.k_trials}`,
      `连续 ${run.k_trials} 轮全部达标的一贯稳定性`,
      run.pass_pow_k
    ),
  ];

  const hasFailureBanner =
    run.status !== "COMPLETED" || Boolean(run.error_message);
  const failureReason =
    run.error_message ??
    (run.status === "UNVERIFIED"
      ? "外部模型或 OCR 前置条件未就绪，结果置为无结果（拒绝伪造分数）。"
      : run.status === "FAILED"
        ? "评测运行失败，未产生有效指标。"
        : null);

  return {
    id: run.id,
    runModeLabel: getEvalRunModeLabel(run.run_mode),
    status: run.status,
    statusLabel: statusMeta.label,
    statusTone: statusMeta.tone,
    modelName: run.model_name,
    trialsText: `${run.actual_trials} / ${run.k_trials}`,
    totalCases: run.total_cases,
    createdAt: run.created_at,
    hasFailureBanner,
    failureReason,
    truthMetrics,
    stabilityMetrics,
  };
}

function preserveDocumentDecimals(
  doc: SourceDocumentSummary
): SourceDocumentSummary {
  const next: SourceDocumentSummary = { ...doc };
  for (const field of [
    "subtotal_amount",
    "tax_amount",
    "total_amount",
  ] as const) {
    const str = readDecimalString(doc, field);
    if (str !== null) {
      next[field] = toSchemaDecimal(str);
    }
  }
  return next;
}

function preserveRecordDecimals(
  rec: BillingRecordDetail
): BillingRecordDetail {
  const qtyStr = readDecimalString(rec.charge_details, "quantity");
  const unitStr = readDecimalString(rec.charge_details, "unit_price");
  const amtStr = readDecimalString(rec.charge_details, "amount");
  const candidates = (rec.candidate_bills ?? []).map((cand) => {
    const cAmt = readDecimalString(cand, "amount");
    return {
      ...cand,
      amount: cAmt !== null ? toSchemaDecimal(cAmt) : cand.amount,
    };
  });
  return {
    ...rec,
    candidate_bills: candidates,
    charge_details: {
      ...rec.charge_details,
      quantity: qtyStr !== null ? toSchemaDecimal(qtyStr) : null,
      unit_price: unitStr !== null ? toSchemaDecimal(unitStr) : null,
      amount: amtStr !== null ? toSchemaDecimal(amtStr) : null,
    },
  };
}

function preserveExistingBillDecimals(bill: ExistingBill): ExistingBill {
  const qtyStr = readDecimalString(bill, "quantity");
  const unitStr = readDecimalString(bill, "unit_price");
  const amtStr = readDecimalString(bill, "amount");
  return {
    ...bill,
    quantity: qtyStr !== null ? toSchemaDecimal(qtyStr) : null,
    unit_price: unitStr !== null ? toSchemaDecimal(unitStr) : null,
    amount: amtStr !== null ? toSchemaDecimal(amtStr) : bill.amount,
  };
}

export function buildReviewBundleV1(
  batch: BatchDetail,
  existingBillsSnapshot: ReadonlyArray<ExistingBill> = [],
  auditExport?: ExportResponse | null
): ReviewBundleV1 {
  const clonedBatch = parseLosslessJson<BatchDetail>(
    stringifyLosslessJson(batch)
  );
  const clonedSnapshot = parseLosslessJson<ExistingBill[]>(
    stringifyLosslessJson(existingBillsSnapshot)
  );

  const auditMap = new Map<string, Record<string, unknown>>();
  if (auditExport) {
    for (const item of auditExport.records) {
      const recId = item.audit_trail?.record_id;
      if (typeof recId === "string" && recId) {
        auditMap.set(recId, item.audit_trail as Record<string, unknown>);
      }
    }
  }

  const documents = clonedBatch.documents.map(preserveDocumentDecimals);
  const records = clonedBatch.records.map((rec) => {
    const normalized = preserveRecordDecimals(rec);
    const trail = auditMap.get(rec.id);
    if (trail) {
      normalized.source_evidence = {
        ...(normalized.source_evidence ?? {}),
        audit_snapshot: {
          human_review_history: Array.isArray(trail.human_review_history)
            ? trail.human_review_history
            : [],
          retry_count:
            typeof trail.retry_count === "number" ? trail.retry_count : 0,
          llm_calls_used:
            typeof trail.llm_calls_used === "number"
              ? trail.llm_calls_used
              : null,
        },
      };
    }
    return normalized;
  });

  return {
    bundle_version: "1.0",
    short_code: clonedBatch.short_code,
    accounting_entity: clonedBatch.accounting_entity,
    ocr_mode: clonedBatch.ocr_mode,
    documents,
    records,
    existing_bills_snapshot: clonedSnapshot.map(preserveExistingBillDecimals),
  };
}

export function validateReviewBundleInput(
  rawText: string
):
  | { valid: true; bundle: ReviewBundleV1 }
  | { valid: false; error: string } {
  if (!rawText.trim()) {
    return { valid: false, error: "请先粘贴或选择 CLI 生成的 ReviewBundleV1 JSON" };
  }
  let parsed: ReviewBundleV1;
  try {
    parsed = parseLosslessJson<ReviewBundleV1>(rawText);
  } catch {
    return { valid: false, error: "复核包 JSON 语法无效" };
  }
  if (
    !parsed ||
    typeof parsed !== "object" ||
    parsed.bundle_version !== "1.0" ||
    typeof parsed.accounting_entity !== "string" ||
    !parsed.accounting_entity.trim() ||
    !Array.isArray(parsed.documents) ||
    !Array.isArray(parsed.records)
  ) {
    return {
      valid: false,
      error:
        '不符合 ReviewBundleV1 契约：必须包含 bundle_version: "1.0"、accounting_entity、documents 数组与 records 数组',
    };
  }
  if (
    parsed.existing_bills_snapshot !== undefined &&
    !Array.isArray(parsed.existing_bills_snapshot)
  ) {
    return {
      valid: false,
      error: "ReviewBundleV1 中的 existing_bills_snapshot 必须为数组",
    };
  }
  return { valid: true, bundle: parsed };
}
