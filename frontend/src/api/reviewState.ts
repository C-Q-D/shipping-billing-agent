import type { components } from "./schema";
import { rawDecimal, readDecimalString } from "./decimalJson.ts";
import {
  ApiClientError,
  isValidIso4217Currency,
  type RecordPatchInput,
} from "./client.ts";

type BatchDetail = components["schemas"]["BatchDetail"];
type BatchSummary = components["schemas"]["BatchSummary"];
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

export interface RecordDraftEntry {
  draftVersion: number;
  fields: Partial<RecordFormValues>;
}

export interface DocTotalsFormValues {
  subtotal_amount: string;
  tax_amount: string;
  total_amount: string;
  total_currency: string;
  review_note: string;
}

export interface DocTotalsDraftEntry {
  draftVersion: number;
  fields: Partial<DocTotalsFormValues>;
}

export interface InFlightRecordSaveEntry {
  submittedVersion: number;
  submittedFields: Partial<RecordFormValues>;
  postSubmitTouched: Partial<Record<keyof RecordFormValues, true>>;
}

export interface InFlightDocSaveEntry {
  submittedVersion: number;
  submittedFields: Partial<DocTotalsFormValues>;
  postSubmitTouched: Partial<Record<keyof DocTotalsFormValues, true>>;
}

export interface DraftStoreState {
  nextDraftVersion: number;
  recordDrafts: Record<string, RecordDraftEntry>;
  docDrafts: Record<string, DocTotalsDraftEntry>;
  inFlightRecordSaves: Record<string, InFlightRecordSaveEntry>;
  inFlightDocSaves: Record<string, InFlightDocSaveEntry>;
}

export interface RecordDiffPatchResult {
  valid: boolean;
  hasChanges: boolean;
  fieldErrors: Record<string, string>;
  patch: RecordPatchInput;
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

export interface MutationBlockerItem {
  code: string;
  field?: string | null;
  message: string;
  expected?: string | null;
  actual?: string | null;
}

export interface MutationErrorBanner {
  title: string;
  code?: string;
  message: string;
  blockers?: MutationBlockerItem[];
  isConflict?: boolean;
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
  return {
    nextDraftVersion: 0,
    recordDrafts: {},
    docDrafts: {},
    inFlightRecordSaves: {},
    inFlightDocSaves: {},
  };
}

export function clearBatchScopedMap<T>(
  entries: Record<string, T>,
  batchId: string
): Record<string, T> {
  const prefix = `${batchId}::`;
  let changed = false;
  const next: Record<string, T> = {};
  for (const [k, v] of Object.entries(entries)) {
    if (k.startsWith(prefix)) {
      changed = true;
    } else {
      next[k] = v;
    }
  }
  return changed ? next : entries;
}

export function clearBatchDrafts(
  store: DraftStoreState,
  batchId: string
): DraftStoreState {
  return {
    ...store,
    recordDrafts: clearBatchScopedMap(store.recordDrafts, batchId),
    docDrafts: clearBatchScopedMap(store.docDrafts, batchId),
    inFlightRecordSaves: clearBatchScopedMap(
      store.inFlightRecordSaves ?? {},
      batchId
    ),
    inFlightDocSaves: clearBatchScopedMap(store.inFlightDocSaves ?? {}, batchId),
  };
}

export function makeRecordDraftKey(batchId: string, recordId: string): string {
  return `${batchId}::${recordId}`;
}

export function makeDocDraftKey(batchId: string, docId: string): string {
  return `${batchId}::${docId}`;
}

export function countCodePoints(text: string): number {
  return Array.from(text.trim()).length;
}

export function validateReviewReason(reason: string): string | null {
  const len = countCodePoints(reason);
  if (len < 2 || len > 500) {
    return "理由说明须为 2–500 个字符";
  }
  return null;
}

export function extractMutationErrorBanner(
  title: string,
  err: unknown,
  recordAlerts: ReadonlyArray<ValidationAlert> = []
): MutationErrorBanner {
  if (err instanceof ApiClientError) {
    const rawCodes = Array.isArray(err.details?.blocker_codes)
      ? (err.details.blocker_codes as unknown[])
          .filter((code): code is string => typeof code === "string" && code.trim().length > 0)
      : [];

    const blockers: MutationBlockerItem[] = rawCodes.map((code) => {
      const matched =
        recordAlerts.find((a) => a.code === code && a.severity === "BLOCKER") ??
        recordAlerts.find((a) => a.code === code);
      if (matched) {
        return {
          code,
          field: matched.field ?? null,
          message: matched.message,
          expected: matched.expected ?? null,
          actual: matched.actual ?? null,
        };
      }
      return {
        code,
        field: null,
        message: "存在未解决的硬性门禁阻断，请修正对应字段、单据总额或选择驳回",
        expected: null,
        actual: null,
      };
    });

    return {
      title,
      code: err.code,
      message: err.message,
      blockers: blockers.length > 0 ? blockers : undefined,
      isConflict: err.status === 409,
    };
  }
  return {
    title,
    message: err instanceof Error ? err.message : "操作失败，请重试",
  };
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
  const chargeDetails = record.charge_details ?? {
    charge_item: null,
    amount: null,
    currency: null,
    payment_term: null,
  };
  const docInfo = record.document_info ?? {
    document_type: null,
    reference_no: null,
    document_date: null,
  };
  const party = record.transaction_party ?? {
    issuer: null,
    recipient: null,
  };
  const financial = record.financial_attributes ?? {
    accounting_entity: "",
    posting_type: null,
  };
  const amountStr = readDecimalString(chargeDetails, "amount") ?? "";
  const docType = docInfo.document_type ?? "";
  const isNegativeAmount =
    amountStr.startsWith("-") && amountStr !== "-0" && amountStr !== "-0.00";
  const overrides = record.human_overrides ?? {};

  return {
    charge_item: chargeDetails.charge_item ?? "",
    quantity: readDecimalString(chargeDetails, "quantity") ?? "",
    unit_price: readDecimalString(chargeDetails, "unit_price") ?? "",
    amount: amountStr,
    currency: chargeDetails.currency ?? "",
    payment_term: chargeDetails.payment_term ?? "",
    posting_type: financial.posting_type ?? "",
    debit_credit:
      isCreditDocumentType(docType) || isNegativeAmount ? "CREDIT" : "DEBIT",
    document_type: docType,
    reference_no: docInfo.reference_no ?? "",
    document_date: docInfo.document_date ?? "",
    issuer: party.issuer ?? "",
    recipient: party.recipient ?? "",
    mbl_no: record.shipment_refs?.mbl_nos?.[0]?.value ?? "",
    hbl_no: record.shipment_refs?.hbl_no ?? "",
    booking_no: record.shipment_refs?.booking_no ?? "",
    container_nos_text: (record.shipment_refs?.container_nos ?? []).join(", "),
    matched_existing_bill_id: record.matched_existing_bill_id ?? null,
    force_create_new: Boolean(overrides.force_create_new),
    review_note: "",
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

export function getRecordDraftVersion(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): number | null {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  return entry ? entry.draftVersion : null;
}

export function getDocTotalsDraftVersion(
  store: DraftStoreState,
  batchId: string,
  docId: string
): number | null {
  const entry = store.docDrafts[makeDocDraftKey(batchId, docId)];
  return entry ? entry.draftVersion : null;
}

function pruneRecordFields(
  server: RecordFormValues,
  fields: Partial<RecordFormValues>
): Partial<RecordFormValues> | null {
  const nextFields: Partial<RecordFormValues> = {};
  (Object.keys(fields) as Array<keyof RecordFormValues>).forEach((k) => {
    const val = fields[k];
    if (val !== undefined && val !== server[k]) {
      (nextFields as Record<string, unknown>)[k] = val;
    }
  });
  return Object.keys(nextFields).length > 0 ? nextFields : null;
}

function beginDraftSave<TFields extends object>(
  drafts: Record<string, { draftVersion: number; fields: Partial<TFields> }>,
  inFlightMap: Record<
    string,
    {
      submittedVersion: number;
      submittedFields: Partial<TFields>;
      postSubmitTouched: Partial<Record<keyof TFields, true>>;
    }
  >,
  key: string,
  fallbackVersion: number,
  version?: number | null,
  submittedFields?: Partial<TFields>
) {
  const existingDraft = drafts[key];
  const submittedVersion =
    version ?? existingDraft?.draftVersion ?? fallbackVersion;
  const snapshot: Partial<TFields> = {
    ...(submittedFields ?? existingDraft?.fields ?? {}),
  };
  return {
    ...inFlightMap,
    [key]: {
      submittedVersion,
      submittedFields: snapshot,
      postSubmitTouched: {} as Partial<Record<keyof TFields, true>>,
    },
  };
}

function finishDraftSaveSuccess<TFields extends object, TServer>(
  drafts: Record<string, { draftVersion: number; fields: Partial<TFields> }>,
  inFlightMap: Record<
    string,
    {
      submittedVersion: number;
      submittedFields: Partial<TFields>;
      postSubmitTouched: Partial<Record<keyof TFields, true>>;
    }
  >,
  key: string,
  extractServer: (s: TServer) => TFields,
  pruneFields: (s: TFields, f: Partial<TFields>) => Partial<TFields> | null,
  updatedServer?: TServer,
  expectedVersion?: number
) {
  const inFlight = inFlightMap[key];
  if (!inFlight) {
    const current = drafts[key];
    if (!current) {
      return { nextDrafts: drafts, nextInFlight: inFlightMap };
    }
    if (
      expectedVersion === undefined ||
      current.draftVersion === expectedVersion
    ) {
      const nextDrafts = { ...drafts };
      delete nextDrafts[key];
      return { nextDrafts, nextInFlight: inFlightMap };
    }
    if (!updatedServer) {
      return { nextDrafts: drafts, nextInFlight: inFlightMap };
    }
    const nextServer = extractServer(updatedServer);
    const remaining = pruneFields(nextServer, current.fields);
    const nextDrafts = { ...drafts };
    if (!remaining) {
      delete nextDrafts[key];
    } else {
      nextDrafts[key] = {
        draftVersion: current.draftVersion,
        fields: remaining,
      };
    }
    return { nextDrafts, nextInFlight: inFlightMap };
  }

  if (
    expectedVersion !== undefined &&
    inFlight.submittedVersion !== expectedVersion
  ) {
    return { nextDrafts: drafts, nextInFlight: inFlightMap };
  }

  const nextInFlight = { ...inFlightMap };
  delete nextInFlight[key];

  const current = drafts[key];
  if (!current) {
    return { nextDrafts: drafts, nextInFlight };
  }

  const postTouchedFields: Partial<TFields> = {};
  (Object.keys(current.fields) as Array<keyof TFields>).forEach((k) => {
    if (inFlight.postSubmitTouched[k] === true && current.fields[k] !== undefined) {
      postTouchedFields[k] = current.fields[k];
    }
  });

  const nextDrafts = { ...drafts };
  if (Object.keys(postTouchedFields).length === 0) {
    delete nextDrafts[key];
    return { nextDrafts, nextInFlight };
  }

  const remaining = updatedServer
    ? pruneFields(extractServer(updatedServer), postTouchedFields)
    : postTouchedFields;

  if (!remaining) {
    delete nextDrafts[key];
  } else {
    nextDrafts[key] = {
      draftVersion: current.draftVersion,
      fields: remaining,
    };
  }
  return { nextDrafts, nextInFlight };
}

function finishDraftSaveError<TFields extends object, TServer>(
  drafts: Record<string, { draftVersion: number; fields: Partial<TFields> }>,
  inFlightMap: Record<
    string,
    {
      submittedVersion: number;
      submittedFields: Partial<TFields>;
      postSubmitTouched: Partial<Record<keyof TFields, true>>;
    }
  >,
  key: string,
  extractServer: (s: TServer) => TFields,
  pruneFields: (s: TFields, f: Partial<TFields>) => Partial<TFields> | null,
  serverEntity?: TServer
) {
  const nextInFlight = { ...inFlightMap };
  delete nextInFlight[key];

  const current = drafts[key];
  if (!current || !serverEntity) {
    return { nextDrafts: drafts, nextInFlight };
  }
  const remaining = pruneFields(extractServer(serverEntity), current.fields);
  const nextDrafts = { ...drafts };
  if (!remaining) {
    delete nextDrafts[key];
  } else {
    nextDrafts[key] = {
      draftVersion: current.draftVersion,
      fields: remaining,
    };
  }
  return { nextDrafts, nextInFlight };
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
    draftVersion: store.nextDraftVersion,
    fields: {},
  };
  const nextFields: Partial<RecordFormValues> = {
    ...existing.fields,
    [field]: value,
  };
  const current = { ...server, ...nextFields };
  const touchedFields: Array<keyof RecordFormValues> = [field];

  if (field === "debit_credit") {
    if (value === "CREDIT") {
      if (!isCreditDocumentType(current.document_type)) {
        nextFields.document_type = "Credit Note";
        touchedFields.push("document_type");
      }
      const trimmedAmt = current.amount.trim();
      if (trimmedAmt && !trimmedAmt.startsWith("-")) {
        try {
          const parsed = rawDecimal(trimmedAmt).raw;
          if (parsed !== "0" && !/^0+\.?0*$/.test(parsed)) {
            nextFields.amount = `-${parsed.replace(/^\+/, "")}`;
            touchedFields.push("amount");
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
        touchedFields.push("document_type");
      }
      const trimmedAmt = current.amount.trim();
      if (trimmedAmt.startsWith("-")) {
        nextFields.amount = trimmedAmt.slice(1);
        touchedFields.push("amount");
      }
    }
  } else if (field === "force_create_new") {
    if (value === true) {
      nextFields.matched_existing_bill_id = null;
      touchedFields.push("matched_existing_bill_id");
    }
  } else if (field === "matched_existing_bill_id") {
    if (value !== null) {
      nextFields.force_create_new = false;
      touchedFields.push("force_create_new");
    }
  }

  const nextVersion = store.nextDraftVersion + 1;
  const nextRecordDrafts = { ...store.recordDrafts };
  const inFlightMap = store.inFlightRecordSaves ?? {};
  const inFlight = inFlightMap[key];

  if (inFlight) {
    // F1: Preserve edits made while a save is in-flight even if equal to old server value
    nextRecordDrafts[key] = {
      draftVersion: nextVersion,
      fields: nextFields,
    };
    const nextTouched = { ...inFlight.postSubmitTouched };
    for (const tf of touchedFields) {
      nextTouched[tf] = true;
    }
    return {
      ...store,
      nextDraftVersion: nextVersion,
      recordDrafts: nextRecordDrafts,
      inFlightRecordSaves: {
        ...inFlightMap,
        [key]: {
          ...inFlight,
          postSubmitTouched: nextTouched,
        },
      },
    };
  }

  const prunedFields = pruneRecordFields(server, nextFields);
  if (prunedFields) {
    nextRecordDrafts[key] = {
      draftVersion: nextVersion,
      fields: prunedFields,
    };
  } else {
    delete nextRecordDrafts[key];
  }
  return {
    ...store,
    nextDraftVersion: nextVersion,
    recordDrafts: nextRecordDrafts,
  };
}

export function beginRecordSave(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  version?: number | null,
  submittedFields?: Partial<RecordFormValues>
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  return {
    ...store,
    inFlightRecordSaves: beginDraftSave(
      store.recordDrafts,
      store.inFlightRecordSaves ?? {},
      key,
      store.nextDraftVersion,
      version,
      submittedFields
    ),
  };
}

export function finishRecordSaveSuccess(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  updatedRecord?: BillingRecordDetail,
  expectedVersion?: number
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  const { nextDrafts, nextInFlight } = finishDraftSaveSuccess(
    store.recordDrafts,
    store.inFlightRecordSaves ?? {},
    key,
    extractServerRecordFormValues,
    pruneRecordFields,
    updatedRecord,
    expectedVersion
  );
  return {
    ...store,
    recordDrafts: nextDrafts,
    inFlightRecordSaves: nextInFlight,
  };
}

export function finishRecordSaveError(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  serverRecord?: BillingRecordDetail
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  const { nextDrafts, nextInFlight } = finishDraftSaveError(
    store.recordDrafts,
    store.inFlightRecordSaves ?? {},
    key,
    extractServerRecordFormValues,
    pruneRecordFields,
    serverRecord
  );
  return {
    ...store,
    recordDrafts: nextDrafts,
    inFlightRecordSaves: nextInFlight,
  };
}

export function clearRecordDraft(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  expectedVersion?: number,
  updatedRecord?: BillingRecordDetail
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  const inFlightMap = store.inFlightRecordSaves ?? {};
  if (expectedVersion === undefined) {
    const nextRecordDrafts = { ...store.recordDrafts };
    delete nextRecordDrafts[key];
    const nextInFlight = { ...inFlightMap };
    delete nextInFlight[key];
    return {
      ...store,
      recordDrafts: nextRecordDrafts,
      inFlightRecordSaves: nextInFlight,
    };
  }
  return finishRecordSaveSuccess(
    store,
    batchId,
    recordId,
    updatedRecord,
    expectedVersion
  );
}

export function hasRecordDraft(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): boolean {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  return Boolean(entry && Object.keys(entry.fields).length > 0);
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

function pruneDocFields(
  server: DocTotalsFormValues,
  fields: Partial<DocTotalsFormValues>
): Partial<DocTotalsFormValues> | null {
  const prunedFields: Partial<DocTotalsFormValues> = {};
  (Object.keys(fields) as Array<keyof DocTotalsFormValues>).forEach((k) => {
    const val = fields[k];
    if (val !== undefined && val !== server[k]) {
      prunedFields[k] = val;
    }
  });
  return Object.keys(prunedFields).length > 0 ? prunedFields : null;
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
  const existing = store.docDrafts[key] ?? {
    draftVersion: store.nextDraftVersion,
    fields: {},
  };
  const nextFields: Partial<DocTotalsFormValues> = {
    ...existing.fields,
    [field]: value,
  };
  const nextVersion = store.nextDraftVersion + 1;
  const nextDocDrafts = { ...store.docDrafts };
  const inFlightMap = store.inFlightDocSaves ?? {};
  const inFlight = inFlightMap[key];

  if (inFlight) {
    // F1: Preserve edits made while a doc totals save is in-flight even if equal to old server value
    nextDocDrafts[key] = {
      draftVersion: nextVersion,
      fields: nextFields,
    };
    return {
      ...store,
      nextDraftVersion: nextVersion,
      docDrafts: nextDocDrafts,
      inFlightDocSaves: {
        ...inFlightMap,
        [key]: {
          ...inFlight,
          postSubmitTouched: {
            ...inFlight.postSubmitTouched,
            [field]: true,
          },
        },
      },
    };
  }

  const prunedFields = pruneDocFields(server, nextFields);
  if (!prunedFields) {
    delete nextDocDrafts[key];
  } else {
    nextDocDrafts[key] = {
      draftVersion: nextVersion,
      fields: prunedFields,
    };
  }
  return {
    ...store,
    nextDraftVersion: nextVersion,
    docDrafts: nextDocDrafts,
  };
}

export function beginDocTotalsSave(
  store: DraftStoreState,
  batchId: string,
  docId: string,
  version?: number | null,
  submittedFields?: Partial<DocTotalsFormValues>
): DraftStoreState {
  const key = makeDocDraftKey(batchId, docId);
  return {
    ...store,
    inFlightDocSaves: beginDraftSave(
      store.docDrafts,
      store.inFlightDocSaves ?? {},
      key,
      store.nextDraftVersion,
      version,
      submittedFields
    ),
  };
}

export function finishDocTotalsSaveSuccess(
  store: DraftStoreState,
  batchId: string,
  docId: string,
  updatedDoc?: SourceDocumentSummary,
  expectedVersion?: number
): DraftStoreState {
  const key = makeDocDraftKey(batchId, docId);
  const { nextDrafts, nextInFlight } = finishDraftSaveSuccess(
    store.docDrafts,
    store.inFlightDocSaves ?? {},
    key,
    extractServerDocTotalsValues,
    pruneDocFields,
    updatedDoc,
    expectedVersion
  );
  return {
    ...store,
    docDrafts: nextDrafts,
    inFlightDocSaves: nextInFlight,
  };
}

export function finishDocTotalsSaveError(
  store: DraftStoreState,
  batchId: string,
  docId: string,
  serverDoc?: SourceDocumentSummary
): DraftStoreState {
  const key = makeDocDraftKey(batchId, docId);
  const { nextDrafts, nextInFlight } = finishDraftSaveError(
    store.docDrafts,
    store.inFlightDocSaves ?? {},
    key,
    extractServerDocTotalsValues,
    pruneDocFields,
    serverDoc
  );
  return {
    ...store,
    docDrafts: nextDrafts,
    inFlightDocSaves: nextInFlight,
  };
}

export function clearDocTotalsDraft(
  store: DraftStoreState,
  batchId: string,
  docId: string,
  expectedVersion?: number,
  updatedDoc?: SourceDocumentSummary
): DraftStoreState {
  const key = makeDocDraftKey(batchId, docId);
  const inFlightMap = store.inFlightDocSaves ?? {};
  if (expectedVersion === undefined) {
    const nextDocDrafts = { ...store.docDrafts };
    delete nextDocDrafts[key];
    const nextInFlight = { ...inFlightMap };
    delete nextInFlight[key];
    return {
      ...store,
      docDrafts: nextDocDrafts,
      inFlightDocSaves: nextInFlight,
    };
  }
  return finishDocTotalsSaveSuccess(
    store,
    batchId,
    docId,
    updatedDoc,
    expectedVersion
  );
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
  const dirtyRecs = getDirtyRecordIds(store, batch.id, batch.records ?? []);
  const dirtyDocs = getDirtyDocIds(store, batch.id, batch.documents ?? []);
  return dirtyRecs.size + dirtyDocs.size;
}

export function buildRecordDiffPatch(
  record: BillingRecordDetail,
  store: DraftStoreState,
  batchId: string
): RecordDiffPatchResult {
  const server = extractServerRecordFormValues(record);
  const effective = getEffectiveRecordFormValues(record, store, batchId);

  const fieldErrors: Record<string, string> = {};
  const patch: RecordPatchInput = {};

  if (effective.charge_item.trim() !== server.charge_item.trim()) {
    const trimmed = effective.charge_item.trim();
    patch.charge_item = trimmed || null;
  }

  for (const numField of ["quantity", "unit_price", "amount"] as const) {
    const nextRaw = effective[numField].trim();
    const prevRaw = server[numField].trim();
    if (nextRaw !== prevRaw) {
      if (nextRaw === "") {
        patch[numField] = null;
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
    } else if (!isValidIso4217Currency(upper)) {
      fieldErrors.currency = "币种须为有效 3 位大写 ISO 4217 代码（如 USD、CNY）";
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

  // A9: Only emit container_nos diff when the user actually edited the field
  if (effective.container_nos_text !== server.container_nos_text) {
    const prevContainers = (record.shipment_refs?.container_nos ?? [])
      .map((c) => c.trim().toUpperCase())
      .filter((c) => c.length > 0);
    const nextContainers = parseContainerNosInput(effective.container_nos_text);
    const containersChanged =
      prevContainers.length !== nextContainers.length ||
      prevContainers.some((c, idx) => c !== nextContainers[idx]);
    if (containersChanged) {
      patch.container_nos = nextContainers;
    }
  }

  if (effective.matched_existing_bill_id !== server.matched_existing_bill_id) {
    patch.matched_existing_bill_id = effective.matched_existing_bill_id;
  }

  if (effective.force_create_new !== server.force_create_new) {
    patch.force_create_new = effective.force_create_new;
  }

  const trimmedNote = effective.review_note.trim();
  if (trimmedNote.length > 0) {
    patch.review_note = trimmedNote;
  }

  const hasChanges = Object.keys(patch).length > 0;
  return {
    valid: Object.keys(fieldErrors).length === 0,
    hasChanges,
    fieldErrors,
    patch,
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
    } else if (!isValidIso4217Currency(upper)) {
      fieldErrors.total_currency = "币种须为有效 3 位大写 ISO 4217 代码";
    } else {
      patch.total_currency = upper;
    }
  }

  const trimmedNote = effective.review_note.trim();
  if (trimmedNote.length > 0) {
    patch.review_note = trimmedNote;
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

export const ACTIVE_BATCH_STAGES: ReadonlySet<BatchSummary["stage"]> = new Set([
  "QUEUED",
  "UNPACKING",
  "EXTRACTING",
  "MATCHING",
  "VALIDATING",
]);

export function isActiveBatchStage(
  stage: BatchSummary["stage"] | null | undefined
): boolean {
  return Boolean(stage && ACTIVE_BATCH_STAGES.has(stage));
}

export function shouldContinuePollingAfterDiscard(
  tracker: PollRaceTracker,
  targetBatchId: string,
  currentStage: BatchSummary["stage"] | null | undefined
): boolean {
  return (
    tracker.activeBatchId === targetBatchId && isActiveBatchStage(currentStage)
  );
}

export interface PendingBatchDetailRecovery {
  id: string;
  shortCode: string;
  accountingEntity: string;
  isDemo: boolean;
  loading: boolean;
  errorMessage: string | null;
}

export interface AuditHistoryRequestTracker {
  nextSeq: number;
  latestSeqByKey: Record<string, number>;
  batchEpochs: Record<string, number>;
}

export interface AuditHistoryRequestTicket {
  key: string;
  batchId: string;
  recordId: string;
  reqSeq: number;
  batchEpoch: number;
}

export function makeAuditHistoryKey(batchId: string, recordId: string): string {
  return `${batchId}::${recordId}`;
}

export function createAuditHistoryTracker(): AuditHistoryRequestTracker {
  return {
    nextSeq: 0,
    latestSeqByKey: {},
    batchEpochs: {},
  };
}

export function beginAuditHistoryRequest(
  tracker: AuditHistoryRequestTracker,
  batchId: string,
  recordId: string
): {
  nextTracker: AuditHistoryRequestTracker;
  ticket: AuditHistoryRequestTicket;
} {
  const key = makeAuditHistoryKey(batchId, recordId);
  const nextSeq = tracker.nextSeq + 1;
  const batchEpoch = tracker.batchEpochs[batchId] ?? 0;
  return {
    nextTracker: {
      ...tracker,
      nextSeq,
      latestSeqByKey: {
        ...tracker.latestSeqByKey,
        [key]: nextSeq,
      },
    },
    ticket: {
      key,
      batchId,
      recordId,
      reqSeq: nextSeq,
      batchEpoch,
    },
  };
}

export function invalidateAuditHistoryForRecord(
  tracker: AuditHistoryRequestTracker,
  batchId: string,
  recordId: string
): AuditHistoryRequestTracker {
  const key = makeAuditHistoryKey(batchId, recordId);
  const nextSeq = tracker.nextSeq + 1;
  return {
    ...tracker,
    nextSeq,
    latestSeqByKey: {
      ...tracker.latestSeqByKey,
      [key]: nextSeq,
    },
  };
}

export function invalidateAuditHistoryForBatch(
  tracker: AuditHistoryRequestTracker,
  batchId: string
): AuditHistoryRequestTracker {
  const prefix = `${batchId}::`;
  const nextSeq = tracker.nextSeq + 1;
  const nextBatchEpoch = (tracker.batchEpochs[batchId] ?? 0) + 1;
  const nextLatestSeqByKey: Record<string, number> = {
    ...tracker.latestSeqByKey,
  };
  for (const key of Object.keys(nextLatestSeqByKey)) {
    if (key.startsWith(prefix)) {
      nextLatestSeqByKey[key] = nextSeq;
    }
  }
  return {
    nextSeq,
    latestSeqByKey: nextLatestSeqByKey,
    batchEpochs: {
      ...tracker.batchEpochs,
      [batchId]: nextBatchEpoch,
    },
  };
}

export function shouldAcceptAuditHistoryResult(
  tracker: AuditHistoryRequestTracker,
  ticket: AuditHistoryRequestTicket,
  activeBatchId: string | null
): boolean {
  if (activeBatchId !== ticket.batchId) {
    return false;
  }
  if ((tracker.batchEpochs[ticket.batchId] ?? 0) !== ticket.batchEpoch) {
    return false;
  }
  return tracker.latestSeqByKey[ticket.key] === ticket.reqSeq;
}
