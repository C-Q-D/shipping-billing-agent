import type { components } from "./schema";
import { rawDecimal, readDecimalString } from "./decimalJson.ts";
import type {
  RecordPatchInput,
  WarningWaiverItem,
} from "./client.ts";

type BatchDetail = components["schemas"]["BatchDetail"];
type BillingRecordDetail = components["schemas"]["BillingRecordDetail"];
type SourceDocumentSummary = components["schemas"]["SourceDocumentSummary"];
type ValidationAlert = components["schemas"]["ValidationAlert"];

export type DebitCreditOption = "DEBIT" | "CREDIT";

export interface RecordFormValues {
  charge_item: string;
  quantity: string;
  unit_price: string;
  amount: string;
  currency: string;
  payment_term: "PP" | "CC" | "";
  posting_type: "AR" | "AP" | "";
  debit_credit: DebitCreditOption;
  document_type: string;
  reference_no: string;
  document_date: string;
  issuer: string;
  recipient: string;
  mbl_no: string;
  hbl_no: string;
  booking_no: string;
  container_nos_text: string;
  matched_existing_bill_id: string | null;
  force_create_new: boolean;
  review_note: string;
}

export interface WarningWaiverDraft {
  code: string;
  field: string | null;
  actual: string | null;
  reason: string;
}

export interface RecordDraftEntry {
  fields: Partial<RecordFormValues>;
  waivedWarnings: Record<string, WarningWaiverDraft>;
}

export interface DocTotalsFormValues {
  subtotal_amount: string;
  tax_amount: string;
  total_amount: string;
  total_currency: string;
  review_note: string;
}

export interface DocTotalsDraftEntry {
  fields: Partial<DocTotalsFormValues>;
}

export interface DraftStoreState {
  recordDrafts: Record<string, RecordDraftEntry>;
  docDrafts: Record<string, DocTotalsDraftEntry>;
}

export interface RecordDiffPatchResult {
  valid: boolean;
  hasChanges: boolean;
  fieldErrors: Record<string, string>;
  patch: RecordPatchInput;
  resolvedWarnings: WarningWaiverItem[];
}

export interface DocTotalsDiffPatchPayload {
  subtotal_amount?: string | null;
  tax_amount?: string | null;
  total_amount?: string | null;
  total_currency?: string | null;
  review_note?: string | null;
}

export interface DocTotalsDiffPatchResult {
  valid: boolean;
  hasChanges: boolean;
  fieldErrors: Record<string, string>;
  patch: DocTotalsDiffPatchPayload;
}

export interface PollRaceTracker {
  activeBatchId: string | null;
  batchRevision: number;
  mutationEpoch: number;
  inFlightMutations: number;
  pollSeq: number;
  lastAppliedPollSeq: number;
  mutationSeq: number;
  lastAppliedMutationSeq: number;
}

export interface PollTicket {
  batchId: string;
  epochSnapshot: number;
  pollId: number;
}

export interface MutationTicket {
  batchId: string;
  mutationId: number;
}

const CREDIT_DOC_TYPE_PATTERN =
  /(?:\bcredit\s+(?:note|memo|invoice|advice|voucher|adjustment)\b|\bc\s*\/\s*n\b|\bcn\b|贷项(?:通知|凭证)?|貸項(?:通知|憑證)?|红字|紅字)/i;

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const ISO_CURRENCY_PATTERN = /^[A-Z]{3}$/;

export const HARD_BLOCKER_CODES: ReadonlySet<string> = new Set([
  "LINE_ARITHMETIC_MISMATCH",
  "NEGATIVE_NON_CREDIT_AMOUNT",
  "INVALID_OR_MISSING_CURRENCY",
  "INVALID_DOCUMENT_DATE",
  "ENTITY_NOT_MATCHED",
  "AMOUNT_SUBTOTAL_TAX_MISMATCH",
  "AMOUNT_TOTAL_MISMATCH",
  "MULTI_CURRENCY_TOTAL_AMBIGUITY",
  "OCR_NUMERIC_CONFUSION",
  "AMBIGUOUS_EXISTING_BILL",
  "DUPLICATE_LINE_EXTRACTED",
  "CROSS_DOCUMENT_CONFLICT",
  "LLM_EXTRACTION_FAILED",
]);

export function createEmptyDraftStore(): DraftStoreState {
  return { recordDrafts: {}, docDrafts: {} };
}

export function makeRecordDraftKey(batchId: string, recordId: string): string {
  return `${batchId}::${recordId}`;
}

export function makeDocDraftKey(batchId: string, docId: string): string {
  return `${batchId}::${docId}`;
}

export function makeWarningKey(alert: {
  code: string;
  field?: string | null;
  actual?: string | null;
}): string {
  return `${alert.code}::${alert.field ?? ""}::${alert.actual ?? ""}`;
}

export function canWaiveValidationAlert(alert: ValidationAlert): boolean {
  return alert.severity === "WARNING" && !HARD_BLOCKER_CODES.has(alert.code);
}

export function validateReviewReason(reason: string): string | null {
  const len = reason.trim().length;
  if (len < 2 || len > 500) {
    return "理由说明须为 2–500 个字符";
  }
  return null;
}

function isValidCalendarDate(dateStr: string): boolean {
  if (!ISO_DATE_PATTERN.test(dateStr)) return false;
  const [yStr, mStr, dStr] = dateStr.split("-");
  const y = Number.parseInt(yStr, 10);
  const m = Number.parseInt(mStr, 10);
  const d = Number.parseInt(dStr, 10);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === m - 1 &&
    dt.getUTCDate() === d
  );
}

export function parseContainerNosInput(raw: string): string[] {
  return raw
    .split(/[\s,;，；]+/)
    .map((item) => item.trim().toUpperCase())
    .filter((item) => item.length > 0);
}

export function isCreditDocumentType(docType: string | null | undefined): boolean {
  if (!docType) return false;
  return CREDIT_DOC_TYPE_PATTERN.test(docType.replace(/_/g, " "));
}

export function extractServerRecordFormValues(
  record: BillingRecordDetail
): RecordFormValues {
  const amountStr = readDecimalString(record.charge_details, "amount") ?? "";
  const docType = record.document_info.document_type ?? "";
  const isNegativeAmount =
    amountStr.startsWith("-") && amountStr !== "-0" && amountStr !== "-0.00";
  const overrides = record.human_overrides ?? {};
  const serverReviewNote =
    typeof overrides.review_note === "string" ? overrides.review_note : "";

  return {
    charge_item: record.charge_details.charge_item ?? "",
    quantity: readDecimalString(record.charge_details, "quantity") ?? "",
    unit_price: readDecimalString(record.charge_details, "unit_price") ?? "",
    amount: amountStr,
    currency: record.charge_details.currency ?? "",
    payment_term: record.charge_details.payment_term ?? "",
    posting_type: record.financial_attributes.posting_type ?? "",
    debit_credit:
      isCreditDocumentType(docType) || isNegativeAmount ? "CREDIT" : "DEBIT",
    document_type: docType,
    reference_no: record.document_info.reference_no ?? "",
    document_date: record.document_info.document_date ?? "",
    issuer: record.transaction_party.issuer ?? "",
    recipient: record.transaction_party.recipient ?? "",
    mbl_no: record.shipment_refs?.mbl_nos?.[0]?.value ?? "",
    hbl_no: record.shipment_refs?.hbl_no ?? "",
    booking_no: record.shipment_refs?.booking_no ?? "",
    container_nos_text: (record.shipment_refs?.container_nos ?? []).join(", "),
    matched_existing_bill_id: record.matched_existing_bill_id ?? null,
    force_create_new: Boolean(overrides.force_create_new),
    review_note: serverReviewNote,
  };
}

export function getEffectiveRecordFormValues(
  record: BillingRecordDetail,
  store: DraftStoreState,
  batchId: string
): RecordFormValues {
  const server = extractServerRecordFormValues(record);
  const key = makeRecordDraftKey(batchId, record.id);
  const draft = store.recordDrafts[key];
  if (!draft) {
    return server;
  }
  return { ...server, ...draft.fields };
}

export function getRecordWarningWaivers(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): Record<string, WarningWaiverDraft> {
  const key = makeRecordDraftKey(batchId, recordId);
  return store.recordDrafts[key]?.waivedWarnings ?? {};
}

function pruneRecordDraft(
  server: RecordFormValues,
  fields: Partial<RecordFormValues>,
  waivedWarnings: Record<string, WarningWaiverDraft>
): RecordDraftEntry | null {
  const nextFields: Partial<RecordFormValues> = {};
  (Object.keys(fields) as Array<keyof RecordFormValues>).forEach((k) => {
    const val = fields[k];
    if (val !== undefined && val !== server[k]) {
      (nextFields as Record<string, unknown>)[k] = val;
    }
  });
  if (
    Object.keys(nextFields).length === 0 &&
    Object.keys(waivedWarnings).length === 0
  ) {
    return null;
  }
  return { fields: nextFields, waivedWarnings };
}

export function setRecordDraftField<K extends keyof RecordFormValues>(
  store: DraftStoreState,
  batchId: string,
  record: BillingRecordDetail,
  field: K,
  value: RecordFormValues[K]
): DraftStoreState {
  const server = extractServerRecordFormValues(record);
  const key = makeRecordDraftKey(batchId, record.id);
  const existing = store.recordDrafts[key] ?? {
    fields: {},
    waivedWarnings: {},
  };
  const nextFields: Partial<RecordFormValues> = {
    ...existing.fields,
    [field]: value,
  };
  const current = { ...server, ...nextFields };

  if (field === "debit_credit") {
    if (value === "CREDIT") {
      if (!isCreditDocumentType(current.document_type)) {
        nextFields.document_type = "Credit Note";
      }
      const trimmedAmt = current.amount.trim();
      if (trimmedAmt && !trimmedAmt.startsWith("-")) {
        try {
          const parsed = rawDecimal(trimmedAmt).raw;
          if (parsed !== "0" && !/^0+\.?0*$/.test(parsed)) {
            nextFields.amount = `-${parsed.replace(/^\+/, "")}`;
          }
        } catch {
          // Leave invalid amount text untouched for explicit user edit
        }
      }
    } else {
      if (isCreditDocumentType(current.document_type)) {
        nextFields.document_type = isCreditDocumentType(server.document_type)
          ? "Invoice"
          : server.document_type;
      }
      const trimmedAmt = current.amount.trim();
      if (trimmedAmt.startsWith("-")) {
        nextFields.amount = trimmedAmt.slice(1);
      }
    }
  } else if (field === "force_create_new") {
    if (value === true) {
      nextFields.matched_existing_bill_id = null;
    }
  } else if (field === "matched_existing_bill_id") {
    if (value !== null) {
      nextFields.force_create_new = false;
    }
  }

  const pruned = pruneRecordDraft(server, nextFields, existing.waivedWarnings);
  const nextRecordDrafts = { ...store.recordDrafts };
  if (pruned) {
    nextRecordDrafts[key] = pruned;
  } else {
    delete nextRecordDrafts[key];
  }
  return { ...store, recordDrafts: nextRecordDrafts };
}

export function setRecordWarningWaiver(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  waiver: WarningWaiverDraft
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  const warningKey = makeWarningKey(waiver);
  const existing = store.recordDrafts[key] ?? {
    fields: {},
    waivedWarnings: {},
  };
  return {
    ...store,
    recordDrafts: {
      ...store.recordDrafts,
      [key]: {
        fields: existing.fields,
        waivedWarnings: {
          ...existing.waivedWarnings,
          [warningKey]: waiver,
        },
      },
    },
  };
}

export function removeRecordWarningWaiver(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  warningKey: string
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  const existing = store.recordDrafts[key];
  if (!existing || !(warningKey in existing.waivedWarnings)) {
    return store;
  }
  const nextWaived = { ...existing.waivedWarnings };
  delete nextWaived[warningKey];
  const nextRecordDrafts = { ...store.recordDrafts };
  if (
    Object.keys(existing.fields).length === 0 &&
    Object.keys(nextWaived).length === 0
  ) {
    delete nextRecordDrafts[key];
  } else {
    nextRecordDrafts[key] = {
      fields: existing.fields,
      waivedWarnings: nextWaived,
    };
  }
  return { ...store, recordDrafts: nextRecordDrafts };
}

export function clearRecordDraft(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  if (!(key in store.recordDrafts)) {
    return store;
  }
  const nextRecordDrafts = { ...store.recordDrafts };
  delete nextRecordDrafts[key];
  return { ...store, recordDrafts: nextRecordDrafts };
}

export function hasRecordDraft(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): boolean {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  if (!entry) return false;
  return (
    Object.keys(entry.fields).length > 0 ||
    Object.keys(entry.waivedWarnings).length > 0
  );
}

export function getDirtyRecordIds(
  store: DraftStoreState,
  batchId: string,
  records: ReadonlyArray<BillingRecordDetail>
): Set<string> {
  const dirty = new Set<string>();
  for (const rec of records) {
    if (hasRecordDraft(store, batchId, rec.id)) {
      dirty.add(rec.id);
    }
  }
  return dirty;
}

export function extractServerDocTotalsValues(
  doc: SourceDocumentSummary
): DocTotalsFormValues {
  return {
    subtotal_amount: readDecimalString(doc, "subtotal_amount") ?? "",
    tax_amount: readDecimalString(doc, "tax_amount") ?? "",
    total_amount: readDecimalString(doc, "total_amount") ?? "",
    total_currency: doc.total_currency ?? "",
    review_note: "",
  };
}

export function getEffectiveDocTotalsValues(
  doc: SourceDocumentSummary,
  store: DraftStoreState,
  batchId: string
): DocTotalsFormValues {
  const server = extractServerDocTotalsValues(doc);
  const draft = store.docDrafts[makeDocDraftKey(batchId, doc.id)];
  if (!draft) {
    return server;
  }
  return { ...server, ...draft.fields };
}

export function setDocTotalsDraftField<K extends keyof DocTotalsFormValues>(
  store: DraftStoreState,
  batchId: string,
  doc: SourceDocumentSummary,
  field: K,
  value: DocTotalsFormValues[K]
): DraftStoreState {
  const server = extractServerDocTotalsValues(doc);
  const key = makeDocDraftKey(batchId, doc.id);
  const existing = store.docDrafts[key] ?? { fields: {} };
  const nextFields: Partial<DocTotalsFormValues> = {
    ...existing.fields,
    [field]: value,
  };
  const prunedFields: Partial<DocTotalsFormValues> = {};
  (Object.keys(nextFields) as Array<keyof DocTotalsFormValues>).forEach((k) => {
    const val = nextFields[k];
    if (val !== undefined && val !== server[k]) {
      prunedFields[k] = val;
    }
  });
  const nextDocDrafts = { ...store.docDrafts };
  if (Object.keys(prunedFields).length === 0) {
    delete nextDocDrafts[key];
  } else {
    nextDocDrafts[key] = { fields: prunedFields };
  }
  return { ...store, docDrafts: nextDocDrafts };
}

export function clearDocTotalsDraft(
  store: DraftStoreState,
  batchId: string,
  docId: string
): DraftStoreState {
  const key = makeDocDraftKey(batchId, docId);
  if (!(key in store.docDrafts)) {
    return store;
  }
  const nextDocDrafts = { ...store.docDrafts };
  delete nextDocDrafts[key];
  return { ...store, docDrafts: nextDocDrafts };
}

export function hasDocTotalsDraft(
  store: DraftStoreState,
  batchId: string,
  docId: string
): boolean {
  const entry = store.docDrafts[makeDocDraftKey(batchId, docId)];
  return Boolean(entry && Object.keys(entry.fields).length > 0);
}

export function getDirtyDocIds(
  store: DraftStoreState,
  batchId: string,
  docs: ReadonlyArray<SourceDocumentSummary>
): Set<string> {
  const dirty = new Set<string>();
  for (const doc of docs) {
    if (hasDocTotalsDraft(store, batchId, doc.id)) {
      dirty.add(doc.id);
    }
  }
  return dirty;
}

export function countUnsavedDraftsInBatch(
  store: DraftStoreState,
  batch: BatchDetail | null
): number {
  if (!batch) return 0;
  const dirtyRecs = getDirtyRecordIds(store, batch.id, batch.records);
  const dirtyDocs = getDirtyDocIds(store, batch.id, batch.documents);
  return dirtyRecs.size + dirtyDocs.size;
}

export function buildRecordDiffPatch(
  record: BillingRecordDetail,
  store: DraftStoreState,
  batchId: string
): RecordDiffPatchResult {
  const server = extractServerRecordFormValues(record);
  const effective = getEffectiveRecordFormValues(record, store, batchId);
  const waivers = getRecordWarningWaivers(store, batchId, record.id);

  const fieldErrors: Record<string, string> = {};
  const patch: RecordPatchInput = {};
  const resolvedWarnings: WarningWaiverItem[] = [];

  if (effective.charge_item.trim() !== server.charge_item.trim()) {
    const trimmed = effective.charge_item.trim();
    patch.charge_item = trimmed || null;
  }

  for (const numField of ["quantity", "unit_price", "amount"] as const) {
    const nextRaw = effective[numField].trim();
    const prevRaw = server[numField].trim();
    if (nextRaw !== prevRaw) {
      if (nextRaw === "") {
        if (numField === "amount") {
          fieldErrors.amount = "金额不能为空（请输入有效十进制数字）";
        } else {
          patch[numField] = null;
        }
      } else {
        try {
          patch[numField] = rawDecimal(nextRaw);
        } catch (err) {
          fieldErrors[numField] =
            err instanceof Error ? err.message : "非法十进制数值";
        }
      }
    }
  }

  if (effective.currency.trim() !== server.currency.trim()) {
    const upper = effective.currency.trim().toUpperCase();
    if (upper === "") {
      patch.currency = null;
    } else if (!ISO_CURRENCY_PATTERN.test(upper)) {
      fieldErrors.currency = "币种须为 3 位大写 ISO 4217 代码（如 USD、CNY）";
    } else {
      patch.currency = upper;
    }
  }

  if (effective.payment_term !== server.payment_term) {
    patch.payment_term =
      effective.payment_term === "" ? null : effective.payment_term;
  }

  if (effective.posting_type !== server.posting_type) {
    patch.posting_type =
      effective.posting_type === "" ? null : effective.posting_type;
  }

  if (effective.document_type.trim() !== server.document_type.trim()) {
    patch.document_type = effective.document_type.trim() || null;
  }

  if (effective.reference_no.trim() !== server.reference_no.trim()) {
    patch.reference_no = effective.reference_no.trim() || null;
  }

  if (effective.document_date.trim() !== server.document_date.trim()) {
    const dateVal = effective.document_date.trim();
    if (dateVal === "") {
      patch.document_date = null;
    } else if (!isValidCalendarDate(dateVal)) {
      fieldErrors.document_date = "单据日期格式须为合法 YYYY-MM-DD";
    } else {
      patch.document_date = dateVal;
    }
  }

  if (effective.issuer.trim() !== server.issuer.trim()) {
    patch.issuer = effective.issuer.trim() || null;
  }

  if (effective.recipient.trim() !== server.recipient.trim()) {
    patch.recipient = effective.recipient.trim() || null;
  }

  if (effective.mbl_no.trim() !== server.mbl_no.trim()) {
    patch.mbl_no = effective.mbl_no.trim() || null;
  }

  if (effective.hbl_no.trim() !== server.hbl_no.trim()) {
    patch.hbl_no = effective.hbl_no.trim() || null;
  }

  if (effective.booking_no.trim() !== server.booking_no.trim()) {
    patch.booking_no = effective.booking_no.trim() || null;
  }

  const prevContainers = record.shipment_refs?.container_nos ?? [];
  const nextContainers = parseContainerNosInput(effective.container_nos_text);
  const containersChanged =
    prevContainers.length !== nextContainers.length ||
    prevContainers.some((c, idx) => c !== nextContainers[idx]);
  if (containersChanged) {
    patch.container_nos = nextContainers;
  }

  if (effective.matched_existing_bill_id !== server.matched_existing_bill_id) {
    patch.matched_existing_bill_id = effective.matched_existing_bill_id;
  }

  if (effective.force_create_new !== server.force_create_new) {
    patch.force_create_new = effective.force_create_new;
  }

  if (effective.review_note.trim() !== server.review_note.trim()) {
    patch.review_note = effective.review_note.trim() || null;
  }

  for (const [wKey, waiver] of Object.entries(waivers)) {
    const matchedAlert = (record.validation_alerts ?? []).find(
      (a) => makeWarningKey(a) === wKey
    );
    if (!matchedAlert || matchedAlert.severity === "RESOLVED") {
      continue;
    }
    if (!canWaiveValidationAlert(matchedAlert)) {
      fieldErrors[`waive:${wKey}`] =
        "硬性阻断或核心门禁告警严禁前端强行解除";
      continue;
    }
    const reasonErr = validateReviewReason(waiver.reason);
    if (reasonErr) {
      fieldErrors[`waive:${wKey}`] = reasonErr;
      continue;
    }
    resolvedWarnings.push({
      code: matchedAlert.code,
      field: matchedAlert.field ?? null,
      actual: matchedAlert.actual ?? null,
      reason: waiver.reason.trim(),
    });
  }

  const hasChanges =
    Object.keys(patch).length > 0 || resolvedWarnings.length > 0;
  return {
    valid: Object.keys(fieldErrors).length === 0,
    hasChanges,
    fieldErrors,
    patch,
    resolvedWarnings,
  };
}

export function buildDocTotalsDiffPatch(
  doc: SourceDocumentSummary,
  store: DraftStoreState,
  batchId: string
): DocTotalsDiffPatchResult {
  const server = extractServerDocTotalsValues(doc);
  const effective = getEffectiveDocTotalsValues(doc, store, batchId);

  const fieldErrors: Record<string, string> = {};
  const patch: DocTotalsDiffPatchPayload = {};

  for (const numField of [
    "subtotal_amount",
    "tax_amount",
    "total_amount",
  ] as const) {
    const nextRaw = effective[numField].trim();
    const prevRaw = server[numField].trim();
    if (nextRaw !== prevRaw) {
      if (nextRaw === "") {
        patch[numField] = null;
      } else {
        try {
          patch[numField] = rawDecimal(nextRaw).raw;
        } catch (err) {
          fieldErrors[numField] =
            err instanceof Error ? err.message : "非法十进制数值";
        }
      }
    }
  }

  if (effective.total_currency.trim() !== server.total_currency.trim()) {
    const upper = effective.total_currency.trim().toUpperCase();
    if (upper === "") {
      patch.total_currency = null;
    } else if (!ISO_CURRENCY_PATTERN.test(upper)) {
      fieldErrors.total_currency = "币种须为 3 位大写 ISO 4217 代码";
    } else {
      patch.total_currency = upper;
    }
  }

  if (effective.review_note.trim() !== server.review_note.trim()) {
    patch.review_note = effective.review_note.trim() || null;
  }

  const hasChanges = Object.keys(patch).length > 0;
  return {
    valid: Object.keys(fieldErrors).length === 0,
    hasChanges,
    fieldErrors,
    patch,
  };
}

export function createPollRaceTracker(
  activeBatchId: string | null = null
): PollRaceTracker {
  return {
    activeBatchId,
    batchRevision: 0,
    mutationEpoch: 0,
    inFlightMutations: 0,
    pollSeq: 0,
    lastAppliedPollSeq: 0,
    mutationSeq: 0,
    lastAppliedMutationSeq: 0,
  };
}

export function setTrackerActiveBatch(
  tracker: PollRaceTracker,
  batchId: string | null
): PollRaceTracker {
  if (tracker.activeBatchId === batchId) {
    return tracker;
  }
  return {
    ...tracker,
    activeBatchId: batchId,
    mutationEpoch: tracker.mutationEpoch + 1,
    batchRevision: tracker.batchRevision + 1,
  };
}

export function startPoll(
  tracker: PollRaceTracker,
  batchId: string
): { nextTracker: PollRaceTracker; ticket: PollTicket } {
  const nextPollSeq = tracker.pollSeq + 1;
  return {
    nextTracker: {
      ...tracker,
      pollSeq: nextPollSeq,
    },
    ticket: {
      batchId,
      epochSnapshot: tracker.mutationEpoch,
      pollId: nextPollSeq,
    },
  };
}

export function acceptPollResult(
  tracker: PollRaceTracker,
  ticket: PollTicket
): { accepted: boolean; nextTracker: PollRaceTracker } {
  const accepted =
    ticket.batchId === tracker.activeBatchId &&
    tracker.inFlightMutations === 0 &&
    ticket.epochSnapshot === tracker.mutationEpoch &&
    ticket.pollId > tracker.lastAppliedPollSeq;

  if (!accepted) {
    return { accepted: false, nextTracker: tracker };
  }

  return {
    accepted: true,
    nextTracker: {
      ...tracker,
      lastAppliedPollSeq: ticket.pollId,
    },
  };
}

export function startMutation(
  tracker: PollRaceTracker,
  batchId: string
): { nextTracker: PollRaceTracker; ticket: MutationTicket } {
  const nextMutationSeq = tracker.mutationSeq + 1;
  return {
    nextTracker: {
      ...tracker,
      mutationEpoch: tracker.mutationEpoch + 1,
      inFlightMutations: tracker.inFlightMutations + 1,
      mutationSeq: nextMutationSeq,
    },
    ticket: {
      batchId,
      mutationId: nextMutationSeq,
    },
  };
}

export function finishMutationSuccess(
  tracker: PollRaceTracker,
  ticket: MutationTicket
): { shouldApply: boolean; nextTracker: PollRaceTracker } {
  const shouldApply =
    ticket.batchId === tracker.activeBatchId &&
    ticket.mutationId > tracker.lastAppliedMutationSeq;

  return {
    shouldApply,
    nextTracker: {
      ...tracker,
      inFlightMutations: Math.max(0, tracker.inFlightMutations - 1),
      mutationEpoch: tracker.mutationEpoch + 1,
      batchRevision: tracker.batchRevision + 1,
      lastAppliedMutationSeq: shouldApply
        ? ticket.mutationId
        : tracker.lastAppliedMutationSeq,
    },
  };
}

export function finishMutationError(
  tracker: PollRaceTracker
): PollRaceTracker {
  return {
    ...tracker,
    inFlightMutations: Math.max(0, tracker.inFlightMutations - 1),
    mutationEpoch: tracker.mutationEpoch + 1,
  };
}
