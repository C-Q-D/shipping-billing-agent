import {
  isValidIso4217Currency,
  normalizeBillingRecord,
  normalizeExistingBill,
  normalizeSourceDocument,
  type AccountingEntity,
  type AccountingEntityUpsert,
  type BillingRecordDetail,
  type ChargeDictionaryCreate,
  type EvalRunSummary,
  type ExistingBill,
  type ExportMode,
  type ExportResponse,
  type ReviewBundleV1,
  type SourceDocumentSummary,
} from "./client.ts";
import {
  copyDecimalMeta,
  parseLosslessJson,
  rawDecimal,
  readDecimalString,
  toSchemaDecimal,
} from "./decimalJson.ts";
import { countCodePoints } from "./reviewState.ts";

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
  const len = countCodePoints(canonical);
  if (!canonical) {
    return { valid: false, error: "记账主体规范名称不能为空" };
  }
  if (len > 120) {
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
  if (countCodePoints(alias) > 120 || countCodePoints(std) > 120) {
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

function isPlainObject(val: unknown): val is Record<string, unknown> {
  return Boolean(val) && typeof val === "object" && !Array.isArray(val);
}

function flattenStandardBillSections(
  rawItem: Record<string, unknown>
): Record<string, unknown> {
  const values: Record<string, unknown> = copyDecimalMeta(rawItem, {
    ...rawItem,
  });
  for (const section of [
    "document_info",
    "transaction_party",
    "charge_details",
    "financial_attributes",
  ] as const) {
    const nested = rawItem[section];
    if (nested === undefined) continue;
    if (!isPlainObject(nested)) {
      throw new Error(`${section} 必须为 JSON 对象`);
    }
    for (const [k, v] of Object.entries(nested)) {
      if (!(k in values) || values[k] === undefined) {
        values[k] = v;
        if (typeof v === "number") {
          const decStr = readDecimalString(nested, k);
          if (decStr !== null) {
            values[k] = toSchemaDecimal(decStr);
          }
        }
      }
    }
  }
  return values;
}

function validateAndNormalizeBillRow(
  rawItem: Record<string, unknown>,
  rowNo: number,
  seenIds: Set<string>,
  defaultAccountingEntity?: string
): { bill?: Record<string, unknown>; errors: string[] } {
  const errors: string[] = [];
  let flat: Record<string, unknown>;
  try {
    flat = flattenStandardBillSections(rawItem);
  } catch (err) {
    return {
      errors: [`第 ${rowNo} 行：${err instanceof Error ? err.message : "结构无效"}`],
    };
  }

  const next: Record<string, unknown> = copyDecimalMeta(flat, { ...flat });

  if (next.id !== undefined && next.id !== null) {
    if (typeof next.id !== "string" || !next.id.trim()) {
      errors.push(`第 ${rowNo} 行：id 必须为非空字符串`);
    } else {
      const cleanId = next.id.trim();
      if (seenIds.has(cleanId)) {
        errors.push(`第 ${rowNo} 行：id "${cleanId}" 重复`);
      }
      seenIds.add(cleanId);
      next.id = cleanId;
    }
  }

  const entityVal =
    typeof next.accounting_entity === "string" && next.accounting_entity.trim()
      ? next.accounting_entity.trim()
      : (defaultAccountingEntity ?? "").trim();
  const entityLen = countCodePoints(entityVal);
  if (!entityVal || entityLen > 120) {
    errors.push(
      `第 ${rowNo} 行：缺少有效的记账主体 accounting_entity（1–120 字符）`
    );
  } else {
    next.accounting_entity = entityVal;
  }

  if (typeof next.charge_item !== "string" || !next.charge_item.trim()) {
    errors.push(`第 ${rowNo} 行：缺少必填费用名称 (charge_item)`);
  } else {
    next.charge_item = next.charge_item.trim();
  }

  const amtStr = readDecimalString(next, "amount");
  if (amtStr === null) {
    errors.push(`第 ${rowNo} 行：缺少合法十进制金额 (amount)`);
  } else {
    try {
      next.amount = toSchemaDecimal(amtStr);
    } catch {
      errors.push(`第 ${rowNo} 行：amount "${amtStr}" 不是合法十进制数值`);
    }
  }

  for (const optNum of ["quantity", "unit_price"] as const) {
    const rawVal = next[optNum];
    if (rawVal !== undefined && rawVal !== null && rawVal !== "") {
      const decStr = readDecimalString(next, optNum);
      if (decStr === null) {
        errors.push(`第 ${rowNo} 行：${optNum} 不是合法十进制数值`);
      } else {
        try {
          next[optNum] = toSchemaDecimal(decStr);
        } catch {
          errors.push(`第 ${rowNo} 行：${optNum} "${decStr}" 不是合法十进制数值`);
        }
      }
    }
  }

  // A10: NEVER silently default missing/invalid currency to USD
  const rawCurrency =
    typeof next.currency === "string" ? next.currency.trim().toUpperCase() : "";
  if (!rawCurrency) {
    errors.push(`第 ${rowNo} 行：缺少必填币种 (currency)`);
  } else if (!isValidIso4217Currency(rawCurrency)) {
    errors.push(
      `第 ${rowNo} 行：币种 "${rawCurrency}" 非有效 3 位 ISO 4217 代码`
    );
  } else {
    next.currency = rawCurrency;
  }

  // A10: NEVER silently default missing/invalid posting_type to AP
  const rawPosting =
    typeof next.posting_type === "string"
      ? next.posting_type.trim().toUpperCase()
      : "";
  if (rawPosting !== "AR" && rawPosting !== "AP") {
    errors.push(
      `第 ${rowNo} 行：记账方向 (posting_type) 必须严格为 "AR" 或 "AP"（当前为 "${
        typeof next.posting_type === "string" ? next.posting_type : "缺失"
      }"）`
    );
  } else {
    next.posting_type = rawPosting;
  }

  if (
    next.payment_term !== undefined &&
    next.payment_term !== null &&
    next.payment_term !== ""
  ) {
    const pt =
      typeof next.payment_term === "string"
        ? next.payment_term.trim().toUpperCase()
        : "";
    if (pt !== "PP" && pt !== "CC") {
      errors.push(`第 ${rowNo} 行：payment_term 仅支持 "PP"、"CC" 或留空`);
    } else {
      next.payment_term = pt;
    }
  } else if (next.payment_term === "") {
    next.payment_term = null;
  }

  if (errors.length > 0) {
    return { errors };
  }
  return { bill: next, errors: [] };
}

export function parseExistingBillsImportInput(
  rawText: string,
  options: { filename?: string; defaultAccountingEntity?: string } = {}
):
  | { valid: true; bills: Array<Record<string, unknown>> }
  | { valid: false; error: string; rowErrors: string[] } {
  const trimmed = rawText.replace(/^\uFEFF/, "").trim();
  if (!trimmed) {
    const msg = "请先选择 JSON/CSV 文件或粘贴历史账单内容";
    return { valid: false, error: msg, rowErrors: [msg] };
  }

  const isCsvByName = (options.filename ?? "").toLowerCase().endsWith(".csv");
  const looksLikeJson =
    trimmed.startsWith("[") || trimmed.startsWith("{");

  if (looksLikeJson && !isCsvByName) {
    try {
      const parsed = parseLosslessJson<unknown>(trimmed);
      const rawList = Array.isArray(parsed)
        ? parsed
        : isPlainObject(parsed) && Array.isArray(parsed.bills)
          ? (parsed.bills as unknown[])
          : null;
      if (!rawList) {
        const msg = "历史账单 JSON 必须为数组，或包含 bills 数组的对象";
        return { valid: false, error: msg, rowErrors: [msg] };
      }
      if (rawList.length === 0) {
        const msg = "待导入的历史账单列表不能为空";
        return { valid: false, error: msg, rowErrors: [msg] };
      }
      const seenIds = new Set<string>();
      const allErrors: string[] = [];
      const bills: Array<Record<string, unknown>> = [];
      for (let idx = 0; idx < rawList.length; idx++) {
        const item = rawList[idx];
        const rowNo = idx + 1;
        if (!isPlainObject(item)) {
          allErrors.push(`第 ${rowNo} 行：格式无效，须为 JSON 对象`);
          continue;
        }
        const checked = validateAndNormalizeBillRow(
          item,
          rowNo,
          seenIds,
          options.defaultAccountingEntity
        );
        if (checked.errors.length > 0) {
          allErrors.push(...checked.errors);
        } else if (checked.bill) {
          bills.push(checked.bill);
        }
      }
      if (allErrors.length > 0) {
        return {
          valid: false,
          error: allErrors.join("；"),
          rowErrors: allErrors,
        };
      }
      return { valid: true, bills };
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "历史账单 JSON 解析失败，请检查语法";
      return { valid: false, error: msg, rowErrors: [msg] };
    }
  }

  try {
    const rows = parseCsvRows(trimmed);
    if (rows.length < 2) {
      const msg = "CSV 至少需包含 1 行表头与 1 行账单数据";
      return { valid: false, error: msg, rowErrors: [msg] };
    }
    const headers = rows[0].map((h) => h.trim());
    const missingCols = (
      ["charge_item", "amount", "currency", "posting_type"] as const
    ).filter((col) => !headers.includes(col));
    if (missingCols.length > 0) {
      const msg = `CSV 表头缺少必填列：${missingCols.join(", ")}`;
      return { valid: false, error: msg, rowErrors: [msg] };
    }

    const seenIds = new Set<string>();
    const allErrors: string[] = [];
    const bills: Array<Record<string, unknown>> = [];
    for (let r = 1; r < rows.length; r++) {
      const rowNo = r;
      const cells = rows[r];
      const record: Record<string, unknown> = {};
      let rowParseError: string | null = null;
      for (let c = 0; c < headers.length; c++) {
        const key = headers[c];
        const cell = (cells[c] ?? "").trim();
        if (!key || cell === "") continue;
        if (key === "amount" || key === "quantity" || key === "unit_price") {
          try {
            record[key] = rawDecimal(cell).raw;
          } catch {
            rowParseError = `第 ${rowNo} 行：${key} "${cell}" 不是合法十进制数值`;
            break;
          }
        } else if (key === "container_nos") {
          record[key] = cell
            .split(/[;|]+/)
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean);
        } else {
          record[key] = cell;
        }
      }
      if (rowParseError) {
        allErrors.push(rowParseError);
        continue;
      }
      const checked = validateAndNormalizeBillRow(
        record,
        rowNo,
        seenIds,
        options.defaultAccountingEntity
      );
      if (checked.errors.length > 0) {
        allErrors.push(...checked.errors);
      } else if (checked.bill) {
        bills.push(checked.bill);
      }
    }
    if (allErrors.length > 0) {
      return {
        valid: false,
        error: allErrors.join("；"),
        rowErrors: allErrors,
      };
    }
    return { valid: true, bills };
  } catch (err) {
    const msg = err instanceof Error ? err.message : "CSV 解析失败";
    return { valid: false, error: msg, rowErrors: [msg] };
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

export interface EvalFailingCaseView {
  trial: number | null;
  groupId: string | null;
  caseId: string | null;
  reason: string;
  expected: string | null;
  actual: string | null;
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
  avgLatencyMs: number | null;
  avgLatencyText: string;
  executedCases: number | null;
  llmCalls: number | null;
  ocrCalls: number | null;
  selfRepairEvidence: string | null;
  failingCases: EvalFailingCaseView[];
}

interface ParsedReportMetric {
  rate: number | null;
  fraction: EvalMetricFraction | null;
}

function readMetricFromReport(
  report: Record<string, unknown> | undefined,
  metricKey: string
): ParsedReportMetric {
  if (!report) return { rate: null, fraction: null };

  // B05 runner.py real shape: report[metric] = { rate: float | None, numerator: int, denominator: int }
  const direct = report[metricKey];
  if (typeof direct === "number" && Number.isFinite(direct)) {
    const legacy = readLegacyFractionFromReport(report, metricKey);
    return { rate: direct, fraction: legacy };
  }
  if (isPlainObject(direct)) {
    const rate =
      typeof direct.rate === "number" && Number.isFinite(direct.rate)
        ? direct.rate
        : null;
    const passed =
      typeof direct.numerator === "number" && Number.isFinite(direct.numerator)
        ? direct.numerator
        : typeof direct.passed === "number" && Number.isFinite(direct.passed)
          ? direct.passed
          : null;
    const total =
      typeof direct.denominator === "number" &&
      Number.isFinite(direct.denominator)
        ? direct.denominator
        : typeof direct.total === "number" && Number.isFinite(direct.total)
          ? direct.total
          : null;
    return {
      rate,
      fraction: passed !== null && total !== null ? { passed, total } : null,
    };
  }

  return {
    rate: null,
    fraction: readLegacyFractionFromReport(report, metricKey),
  };
}

function readLegacyFractionFromReport(
  report: Record<string, unknown>,
  metricKey: string
): EvalMetricFraction | null {
  const containers = [report.metric_counts, report.details, report.counts];
  for (const container of containers) {
    if (!isPlainObject(container)) continue;
    const entry = container[metricKey];
    if (!isPlainObject(entry)) continue;
    const passed =
      typeof entry.passed === "number"
        ? entry.passed
        : typeof entry.numerator === "number"
          ? entry.numerator
          : null;
    const total =
      typeof entry.total === "number"
        ? entry.total
        : typeof entry.denominator === "number"
          ? entry.denominator
          : null;
    if (passed !== null && total !== null) {
      return { passed, total };
    }
  }
  return null;
}

function readReportOptionalInt(
  report: Record<string, unknown> | undefined,
  key: string
): number | null {
  if (!report) return null;
  const val = report[key];
  return typeof val === "number" && Number.isFinite(val) ? Math.round(val) : null;
}

function formatEvalDetailValue(val: unknown): string | null {
  if (val === undefined || val === null) return null;
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    return JSON.stringify(val);
  } catch {
    return String(val);
  }
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
  const report = isPlainObject(run.report) ? run.report : undefined;

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
    summaryVal?: number | null
  ): EvalMetricRowView => {
    const parsed = readMetricFromReport(report, key);
    const effectiveRate =
      summaryVal !== undefined && summaryVal !== null
        ? summaryVal
        : parsed.rate;
    const formatted = formatEvalMetricValue(
      run,
      effectiveRate,
      parsed.fraction
    );
    return {
      key,
      label,
      description,
      ...formatted,
    };
  };

  // A12: Always render all 9 truth metrics & 2 stability metrics even when COMPLETED has null rate ("无结果")
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
    makeRow(
      "amount_accuracy",
      "金额无损一致率",
      "高精十进制金额相对真值零舍入误差一致率"
    ),
    makeRow(
      "currency_accuracy",
      "币种识别一致率",
      "ISO 4217 三位币种代码准确匹配率"
    ),
    makeRow(
      "false_positive_rate",
      "非计费条款误报率",
      "违约金/免费豁免条款误生成账单比例（越低越好）"
    ),
    makeRow(
      "alert_precision",
      "告警精确率",
      "命中真实校验告警占全部触发告警比例"
    ),
  ];

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

  const rawLatency =
    report &&
    typeof report.avg_latency_ms === "number" &&
    Number.isFinite(report.avg_latency_ms)
      ? report.avg_latency_ms
      : null;
  const avgLatencyText =
    run.status === "COMPLETED" && rawLatency !== null
      ? `${rawLatency.toFixed(1)} ms`
      : NO_EVAL_RESULT_LABEL;

  const executedCases = readReportOptionalInt(report, "executed_cases");
  const llmCalls = readReportOptionalInt(report, "llm_calls");
  const ocrCalls = readReportOptionalInt(report, "ocr_calls");
  const selfRepairEvidence =
    report && typeof report.self_repair_evidence === "string"
      ? report.self_repair_evidence
      : null;

  const rawFailingCases =
    report && Array.isArray(report.failing_cases) ? report.failing_cases : [];
  const failingCases: EvalFailingCaseView[] = rawFailingCases
    .filter(isPlainObject)
    .map((item) => ({
      trial:
        typeof item.trial === "number" && Number.isFinite(item.trial)
          ? item.trial
          : null,
      groupId: typeof item.group_id === "string" ? item.group_id : null,
      caseId: typeof item.case_id === "string" ? item.case_id : null,
      reason: typeof item.reason === "string" ? item.reason : "未达预期",
      expected: formatEvalDetailValue(item.expected),
      actual: formatEvalDetailValue(item.actual),
    }));

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
    avgLatencyMs: rawLatency,
    avgLatencyText,
    executedCases,
    llmCalls,
    ocrCalls,
    selfRepairEvidence,
    failingCases,
  };
}

// A8: Strict validation against openapi.yaml ReviewBundleV1 contract
const VALID_OCR_MODES = new Set(["REMOTE_ONLY", "REMOTE_WITH_LOCAL_FALLBACK"]);
const VALID_DOC_STATUSES = new Set([
  "PARSED",
  "SKIPPED_NOISE",
  "METADATA_ONLY",
  "FAILED",
]);
const VALID_OPERATIONS = new Set([
  "CREATE",
  "UPDATE",
  "NO_CHANGE",
  "PENDING_REVIEW",
]);
const VALID_REVIEW_STATUSES = new Set([
  "AUTO_READY",
  "NEEDS_REVIEW",
  "APPROVED",
  "REJECTED",
]);

function validateDecimalOrNull(
  holder: Record<string, unknown>,
  key: string
): boolean {
  const val = holder[key];
  if (val === undefined || val === null) return true;
  if (typeof val === "number") return Number.isFinite(val);
  if (val instanceof Object && "toJsonLiteral" in val) return true;
  return false;
}

export function validateReviewBundleInput(
  rawText: string
):
  | { valid: true; bundle: ReviewBundleV1 }
  | { valid: false; error: string } {
  if (!rawText.trim()) {
    return { valid: false, error: "请先粘贴或选择 CLI 生成的 ReviewBundleV1 JSON" };
  }
  let parsed: unknown;
  try {
    parsed = parseLosslessJson<unknown>(rawText);
  } catch {
    return { valid: false, error: "复核包 JSON 语法无效" };
  }
  if (!isPlainObject(parsed)) {
    return { valid: false, error: "复核包顶层必须为 JSON 对象" };
  }
  if (parsed.bundle_version !== "1.0") {
    return {
      valid: false,
      error: '不符合 ReviewBundleV1 契约：bundle_version 必须严格为 "1.0"',
    };
  }
  if (
    typeof parsed.accounting_entity !== "string" ||
    countCodePoints(parsed.accounting_entity) < 1 ||
    countCodePoints(parsed.accounting_entity) > 120
  ) {
    return {
      valid: false,
      error: "不符合 ReviewBundleV1 契约：accounting_entity 须为 1–120 字符非空字符串",
    };
  }
  const ocrMode =
    parsed.ocr_mode === undefined ? "REMOTE_ONLY" : parsed.ocr_mode;
  if (typeof ocrMode !== "string" || !VALID_OCR_MODES.has(ocrMode)) {
    return {
      valid: false,
      error:
        '不符合 ReviewBundleV1 契约：ocr_mode 仅允许 "REMOTE_ONLY" 或 "REMOTE_WITH_LOCAL_FALLBACK"',
    };
  }
  if (!Array.isArray(parsed.documents) || !Array.isArray(parsed.records)) {
    return {
      valid: false,
      error: "不符合 ReviewBundleV1 契约：documents 与 records 必须为数组",
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

  const docIds = new Set<string>();
  const normalizedDocs: SourceDocumentSummary[] = [];
  for (let i = 0; i < parsed.documents.length; i++) {
    const rawDoc = parsed.documents[i];
    const rowNo = i + 1;
    if (!isPlainObject(rawDoc)) {
      return {
        valid: false,
        error: `documents[${i}] 必须为对象`,
      };
    }
    const id = typeof rawDoc.id === "string" ? rawDoc.id.trim() : "";
    if (!id) {
      return {
        valid: false,
        error: `第 ${rowNo} 份来源单据缺少必填非空字符串 id`,
      };
    }
    if (docIds.has(id)) {
      return { valid: false, error: `documents 中存在重复 id "${id}"` };
    }
    docIds.add(id);

    for (const strKey of [
      "original_filename",
      "sanitized_filename",
      "source_kind",
      "extraction_channel",
    ] as const) {
      if (typeof rawDoc[strKey] !== "string" || !(rawDoc[strKey] as string).trim()) {
        return {
          valid: false,
          error: `第 ${rowNo} 份来源单据 (${id}) 缺少必填字段 ${strKey}`,
        };
      }
    }
    if (
      typeof rawDoc.status !== "string" ||
      !VALID_DOC_STATUSES.has(rawDoc.status)
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 份来源单据 (${id}) status 无效，须为 PARSED / SKIPPED_NOISE / METADATA_ONLY / FAILED`,
      };
    }
    if (
      rawDoc.parent_doc_id !== undefined &&
      rawDoc.parent_doc_id !== null &&
      typeof rawDoc.parent_doc_id !== "string"
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 份来源单据 (${id}) parent_doc_id 须为字符串或 null`,
      };
    }
    if (rawDoc.parent_doc_id === id) {
      return {
        valid: false,
        error: `第 ${rowNo} 份来源单据 (${id}) parent_doc_id 不能指向自身`,
      };
    }
    if (
      !validateDecimalOrNull(rawDoc, "subtotal_amount") ||
      !validateDecimalOrNull(rawDoc, "tax_amount") ||
      !validateDecimalOrNull(rawDoc, "total_amount")
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 份来源单据 (${id}) 汇总金额字段须为 JSON 数值或 null`,
      };
    }
    normalizedDocs.push(
      normalizeSourceDocument(rawDoc as unknown as SourceDocumentSummary)
    );
  }

  for (const doc of normalizedDocs) {
    if (doc.parent_doc_id && !docIds.has(doc.parent_doc_id)) {
      return {
        valid: false,
        error: `来源单据 "${doc.id}" 引用了不存在的 parent_doc_id "${doc.parent_doc_id}"`,
      };
    }
  }

  const recordIds = new Set<string>();
  const normalizedRecords: BillingRecordDetail[] = [];
  for (let i = 0; i < parsed.records.length; i++) {
    const rawRec = parsed.records[i];
    const rowNo = i + 1;
    if (!isPlainObject(rawRec)) {
      return { valid: false, error: `records[${i}] 必须为对象` };
    }
    const id = typeof rawRec.id === "string" ? rawRec.id.trim() : "";
    if (!id) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录缺少必填非空字符串 id`,
      };
    }
    if (recordIds.has(id)) {
      return { valid: false, error: `records 中存在重复 id "${id}"` };
    }
    recordIds.add(id);

    const sourceDocId =
      typeof rawRec.source_doc_id === "string"
        ? rawRec.source_doc_id.trim()
        : "";
    if (!sourceDocId) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) 缺少必填 source_doc_id`,
      };
    }
    if (!docIds.has(sourceDocId)) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) 的 source_doc_id "${sourceDocId}" 不在 documents 中`,
      };
    }
    if (
      typeof rawRec.source_line_index !== "number" ||
      !Number.isInteger(rawRec.source_line_index) ||
      rawRec.source_line_index < 0
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) source_line_index 须为非负整数`,
      };
    }
    if (
      typeof rawRec.operation !== "string" ||
      !VALID_OPERATIONS.has(rawRec.operation)
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) operation 无效`,
      };
    }
    if (
      typeof rawRec.review_status !== "string" ||
      !VALID_REVIEW_STATUSES.has(rawRec.review_status)
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) review_status 无效`,
      };
    }
    if (typeof rawRec.match_level !== "string" || !rawRec.match_level.trim()) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) 缺少必填 match_level`,
      };
    }
    if (
      !Array.isArray(rawRec.ai_reasoning) ||
      rawRec.ai_reasoning.some((line) => typeof line !== "string")
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) ai_reasoning 须为字符串数组`,
      };
    }

    const docInfo = rawRec.document_info;
    if (
      !isPlainObject(docInfo) ||
      !("document_type" in docInfo) ||
      !("reference_no" in docInfo) ||
      !("document_date" in docInfo)
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) 缺少完整的 document_info`,
      };
    }

    const party = rawRec.transaction_party;
    if (!isPlainObject(party) || !("issuer" in party) || !("recipient" in party)) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) 缺少完整的 transaction_party`,
      };
    }

    const charge = rawRec.charge_details;
    if (
      !isPlainObject(charge) ||
      !("charge_item" in charge) ||
      !("amount" in charge) ||
      !("currency" in charge) ||
      !("payment_term" in charge) ||
      !validateDecimalOrNull(charge, "quantity") ||
      !validateDecimalOrNull(charge, "unit_price") ||
      !validateDecimalOrNull(charge, "amount")
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) charge_details 结构或数值不合法`,
      };
    }
    if (
      charge.payment_term !== null &&
      charge.payment_term !== undefined &&
      charge.payment_term !== "PP" &&
      charge.payment_term !== "CC"
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) payment_term 无效`,
      };
    }

    const financial = rawRec.financial_attributes;
    if (
      !isPlainObject(financial) ||
      typeof financial.accounting_entity !== "string" ||
      !financial.accounting_entity.trim() ||
      (financial.posting_type !== null &&
        financial.posting_type !== "AR" &&
        financial.posting_type !== "AP")
    ) {
      return {
        valid: false,
        error: `第 ${rowNo} 条账单记录 (${id}) financial_attributes 不合法`,
      };
    }

    normalizedRecords.push(
      normalizeBillingRecord(rawRec as unknown as BillingRecordDetail)
    );
  }

  const rawBills = Array.isArray(parsed.existing_bills_snapshot)
    ? parsed.existing_bills_snapshot
    : [];
  const billIds = new Set<string>();
  const normalizedBills: ExistingBill[] = [];
  for (let i = 0; i < rawBills.length; i++) {
    const rawBill = rawBills[i];
    const rowNo = i + 1;
    if (!isPlainObject(rawBill)) {
      return {
        valid: false,
        error: `existing_bills_snapshot[${i}] 必须为对象`,
      };
    }
    const id = typeof rawBill.id === "string" ? rawBill.id.trim() : "";
    if (!id || billIds.has(id)) {
      return {
        valid: false,
        error: `existing_bills_snapshot 第 ${rowNo} 条 id 缺失或重复`,
      };
    }
    billIds.add(id);
    if (
      typeof rawBill.accounting_entity !== "string" ||
      !rawBill.accounting_entity.trim() ||
      typeof rawBill.charge_item !== "string" ||
      !rawBill.charge_item.trim() ||
      typeof rawBill.amount !== "number" ||
      !Number.isFinite(rawBill.amount) ||
      !isValidIso4217Currency(rawBill.currency) ||
      (rawBill.posting_type !== "AR" && rawBill.posting_type !== "AP")
    ) {
      return {
        valid: false,
        error: `existing_bills_snapshot 第 ${rowNo} 条 (${id}) 缺少必填有效字段`,
      };
    }
    normalizedBills.push(
      normalizeExistingBill(rawBill as unknown as ExistingBill)
    );
  }

  const bundle: ReviewBundleV1 = {
    ...(parsed as Record<string, unknown>),
    bundle_version: "1.0",
    short_code:
      typeof parsed.short_code === "string" ? parsed.short_code : null,
    accounting_entity: parsed.accounting_entity.trim(),
    ocr_mode: ocrMode as ReviewBundleV1["ocr_mode"],
    documents: normalizedDocs,
    records: normalizedRecords,
    existing_bills_snapshot: normalizedBills,
  };
  return { valid: true, bundle };
}

// A3: Pure batch-scoped & failure-cleared export preview cache state machine
export interface ExportPreviewCacheEntry {
  batchId: string;
  mode: ExportMode;
  response: ExportResponse;
}

export interface ExportModalCacheState {
  batchId: string | null;
  entries: Partial<Record<ExportMode, ExportPreviewCacheEntry>>;
  loading: boolean;
  loadError: string | null;
}

export function createExportModalCacheState(
  batchId: string | null = null
): ExportModalCacheState {
  return {
    batchId,
    entries: {},
    loading: false,
    loadError: null,
  };
}

export function resetExportModalForBatch(
  state: ExportModalCacheState,
  nextBatchId: string | null
): ExportModalCacheState {
  if (state.batchId === nextBatchId && !state.loadError) {
    return state;
  }
  return {
    batchId: nextBatchId,
    entries: {},
    loading: false,
    loadError: null,
  };
}

export function beginExportModalLoad(
  _state: ExportModalCacheState,
  targetBatchId: string
): ExportModalCacheState {
  return {
    batchId: targetBatchId,
    entries: {},
    loading: true,
    loadError: null,
  };
}

export function completeExportModalLoadSuccess(
  targetBatchId: string,
  responses: Record<ExportMode, ExportResponse>
): ExportModalCacheState {
  return {
    batchId: targetBatchId,
    entries: {
      official: {
        batchId: targetBatchId,
        mode: "official",
        response: responses.official,
      },
      full_standard: {
        batchId: targetBatchId,
        mode: "full_standard",
        response: responses.full_standard,
      },
      audit: {
        batchId: targetBatchId,
        mode: "audit",
        response: responses.audit,
      },
    },
    loading: false,
    loadError: null,
  };
}

export function completeExportModalLoadFailure(
  targetBatchId: string,
  errorMessage: string
): ExportModalCacheState {
  return {
    batchId: targetBatchId,
    entries: {},
    loading: false,
    loadError: errorMessage,
  };
}

export function getExportResponseForBatchMode(
  state: ExportModalCacheState,
  activeBatchId: string | null,
  mode: ExportMode
): ExportResponse | null {
  if (!activeBatchId || state.batchId !== activeBatchId || state.loadError) {
    return null;
  }
  const entry = state.entries[mode];
  if (!entry || entry.batchId !== activeBatchId || entry.mode !== mode) {
    return null;
  }
  return entry.response;
}

export function getExportModalActionEligibility(
  state: ExportModalCacheState,
  activeBatchId: string | null,
  mode: ExportMode
): {
  activeExport: ExportResponse | null;
  canCopy: boolean;
  canDownload: boolean;
} {
  const activeExport = getExportResponseForBatchMode(state, activeBatchId, mode);
  const ready = Boolean(
    activeBatchId &&
      state.batchId === activeBatchId &&
      !state.loading &&
      !state.loadError &&
      activeExport !== null
  );
  return {
    activeExport,
    canCopy: ready,
    canDownload: ready,
  };
}
