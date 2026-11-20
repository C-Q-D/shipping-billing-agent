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

export type ConfirmableRecordPatchField =
  | "charge_item"
  | "quantity"
  | "unit_price"
  | "amount"
  | "currency"
  | "payment_term"
  | "posting_type"
  | "document_type"
  | "reference_no"
  | "document_date"
  | "issuer"
  | "recipient"
  | "mbl_no"
  | "hbl_no"
  | "booking_no"
  | "container_nos";

export interface RecordDraftEntry {
  draftVersion: number;
  fields: Partial<RecordFormValues>;
  confirmedFields?: Record<string, true>;
}

export type RecordDraft = RecordDraftEntry;

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
  submittedConfirmedFields?: Record<string, true>;
  postSubmitTouched: Partial<Record<keyof RecordFormValues, true>>;
  postSubmitConfirmed?: Record<string, true>;
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

const CONFIRMABLE_PATCH_FIELDS: ReadonlySet<string> = new Set([
  "charge_item",
  "quantity",
  "unit_price",
  "amount",
  "currency",
  "payment_term",
  "posting_type",
  "document_type",
  "reference_no",
  "document_date",
  "issuer",
  "recipient",
  "mbl_no",
  "hbl_no",
  "booking_no",
  "container_nos",
]);

export function normalizeConfirmedFieldName(
  raw: string
): ConfirmableRecordPatchField | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (trimmed === "container_nos_text") return "container_nos";
  if (trimmed === "mbl_nos") return "mbl_no";
  if (CONFIRMABLE_PATCH_FIELDS.has(trimmed)) {
    return trimmed as ConfirmableRecordPatchField;
  }
  return null;
}

function hasNonEmptyConfirmedMap(confirmed?: Record<string, true>): boolean {
  return Boolean(confirmed && Object.keys(confirmed).length > 0);
}

let pendingRecordConfirmation: {
  key: string;
  fields: ReadonlyArray<string>;
} | null = null;

/**
 * Synchronously stages record field confirmations around an `onUpdateRecordField` call
 * so parent containers managing `DraftStoreState` via `setRecordDraftField` record
 * `confirmedFields` atomically even when the form value already matches the server value.
 */
export function withPendingRecordConfirmation(
  batchId: string,
  recordId: string,
  fields: ReadonlyArray<string>,
  trigger: () => void
): void {
  pendingRecordConfirmation = {
    key: makeRecordDraftKey(batchId, recordId),
    fields,
  };
  try {
    trigger();
  } finally {
    pendingRecordConfirmation = null;
  }
}

interface DraftEntryShape<TFields extends object> {
  draftVersion: number;
  fields: Partial<TFields>;
  confirmedFields?: Record<string, true>;
}

interface InFlightSaveShape<TFields extends object> {
  submittedVersion: number;
  submittedFields: Partial<TFields>;
  submittedConfirmedFields?: Record<string, true>;
  postSubmitTouched: Partial<Record<keyof TFields, true>>;
  postSubmitConfirmed?: Record<string, true>;
}

function beginDraftSave<TFields extends object>(
  drafts: Record<string, DraftEntryShape<TFields>>,
  inFlightMap: Record<string, InFlightSaveShape<TFields>>,
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
  const confirmedSnapshot = hasNonEmptyConfirmedMap(
    existingDraft?.confirmedFields
  )
    ? { ...existingDraft!.confirmedFields }
    : undefined;
  return {
    ...inFlightMap,
    [key]: {
      submittedVersion,
      submittedFields: snapshot,
      ...(confirmedSnapshot
        ? { submittedConfirmedFields: confirmedSnapshot }
        : {}),
      postSubmitTouched: {} as Partial<Record<keyof TFields, true>>,
      postSubmitConfirmed: {},
    },
  };
}

function finishDraftSaveSuccess<TFields extends object, TServer>(
  drafts: Record<string, DraftEntryShape<TFields>>,
  inFlightMap: Record<string, InFlightSaveShape<TFields>>,
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
    const remainingConfirmed = hasNonEmptyConfirmedMap(current.confirmedFields)
      ? { ...current.confirmedFields }
      : undefined;
    const nextDrafts = { ...drafts };
    if (!remaining && !remainingConfirmed) {
      delete nextDrafts[key];
    } else {
      nextDrafts[key] = {
        draftVersion: current.draftVersion,
        fields: remaining ?? {},
        ...(remainingConfirmed ? { confirmedFields: remainingConfirmed } : {}),
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

  const postConfirmedFields: Record<string, true> = {};
  if (inFlight.postSubmitConfirmed && current.confirmedFields) {
    for (const k of Object.keys(current.confirmedFields)) {
      if (inFlight.postSubmitConfirmed[k] === true) {
        postConfirmedFields[k] = true;
      }
    }
  }
  const hasPostConfirmed = Object.keys(postConfirmedFields).length > 0;

  const nextDrafts = { ...drafts };
  if (Object.keys(postTouchedFields).length === 0 && !hasPostConfirmed) {
    delete nextDrafts[key];
    return { nextDrafts, nextInFlight };
  }

  const remaining =
    Object.keys(postTouchedFields).length > 0
      ? updatedServer
        ? pruneFields(extractServer(updatedServer), postTouchedFields)
        : postTouchedFields
      : null;

  if (!remaining && !hasPostConfirmed) {
    delete nextDrafts[key];
  } else {
    nextDrafts[key] = {
      draftVersion: current.draftVersion,
      fields: remaining ?? {},
      ...(hasPostConfirmed ? { confirmedFields: postConfirmedFields } : {}),
    };
  }
  return { nextDrafts, nextInFlight };
}

function finishDraftSaveError<TFields extends object, TServer>(
  drafts: Record<string, DraftEntryShape<TFields>>,
  inFlightMap: Record<string, InFlightSaveShape<TFields>>,
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
  const remainingConfirmed = hasNonEmptyConfirmedMap(current.confirmedFields)
    ? { ...current.confirmedFields }
    : undefined;
  const nextDrafts = { ...drafts };
  if (!remaining && !remainingConfirmed) {
    delete nextDrafts[key];
  } else {
    nextDrafts[key] = {
      draftVersion: current.draftVersion,
      fields: remaining ?? {},
      ...(remainingConfirmed ? { confirmedFields: remainingConfirmed } : {}),
    };
  }
  return { nextDrafts, nextInFlight };
}

export function getConfirmedRecordFields(
  store: DraftStoreState,
  batchId: string,
  recordId: string
): Record<string, true> {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  return entry?.confirmedFields ? { ...entry.confirmedFields } : {};
}

export function markRecordFieldsConfirmed(
  store: DraftStoreState,
  record: BillingRecordDetail,
  batchId: string,
  fields: ReadonlyArray<string>
): DraftStoreState {
  const normalized: ConfirmableRecordPatchField[] = [];
  for (const raw of fields) {
    const mapped = normalizeConfirmedFieldName(raw);
    if (mapped && !normalized.includes(mapped)) {
      normalized.push(mapped);
    }
  }
  if (normalized.length === 0) {
    return store;
  }

  const key = makeRecordDraftKey(batchId, record.id);
  const existing = store.recordDrafts[key];
  const nextConfirmed: Record<string, true> = {
    ...(existing?.confirmedFields ?? {}),
  };
  for (const f of normalized) {
    nextConfirmed[f] = true;
  }

  const nextVersion = store.nextDraftVersion + 1;
  const nextRecordDrafts: Record<string, RecordDraftEntry> = {
    ...store.recordDrafts,
    [key]: {
      draftVersion: nextVersion,
      fields: existing ? { ...existing.fields } : {},
      confirmedFields: nextConfirmed,
    },
  };

  const inFlightMap = store.inFlightRecordSaves ?? {};
  const inFlight = inFlightMap[key];
  if (inFlight) {
    const nextPostConfirmed: Record<string, true> = {
      ...(inFlight.postSubmitConfirmed ?? {}),
    };
    for (const f of normalized) {
      nextPostConfirmed[f] = true;
    }
    return {
      ...store,
      nextDraftVersion: nextVersion,
      recordDrafts: nextRecordDrafts,
      inFlightRecordSaves: {
        ...inFlightMap,
        [key]: {
          ...inFlight,
          postSubmitConfirmed: nextPostConfirmed,
        },
      },
    };
  }

  return {
    ...store,
    nextDraftVersion: nextVersion,
    recordDrafts: nextRecordDrafts,
  };
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
  const stagedConfirmed =
    pendingRecordConfirmation && pendingRecordConfirmation.key === key
      ? pendingRecordConfirmation.fields
      : null;
  if (stagedConfirmed) {
    pendingRecordConfirmation = null;
  }

  const existing = store.recordDrafts[key] ?? {
    draftVersion: store.nextDraftVersion,
    fields: {},
  };
  const nextConfirmed: Record<string, true> = {
    ...(existing.confirmedFields ?? {}),
  };
  const newlyConfirmed: ConfirmableRecordPatchField[] = [];
  if (stagedConfirmed) {
    for (const raw of stagedConfirmed) {
      const mapped = normalizeConfirmedFieldName(raw);
      if (mapped) {
        nextConfirmed[mapped] = true;
        if (!newlyConfirmed.includes(mapped)) {
          newlyConfirmed.push(mapped);
        }
      }
    }
  }
  const hasConfirmed = Object.keys(nextConfirmed).length > 0;

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
      ...(hasConfirmed ? { confirmedFields: nextConfirmed } : {}),
    };
    const nextTouched = { ...inFlight.postSubmitTouched };
    for (const tf of touchedFields) {
      nextTouched[tf] = true;
    }
    const nextPostConfirmed = { ...(inFlight.postSubmitConfirmed ?? {}) };
    for (const cf of newlyConfirmed) {
      nextPostConfirmed[cf] = true;
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
          postSubmitConfirmed: nextPostConfirmed,
        },
      },
    };
  }

  const prunedFields = pruneRecordFields(server, nextFields);
  if (prunedFields || hasConfirmed) {
    nextRecordDrafts[key] = {
      draftVersion: nextVersion,
      fields: prunedFields ?? {},
      ...(hasConfirmed ? { confirmedFields: nextConfirmed } : {}),
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
  if (!entry) return false;
  return (
    Object.keys(entry.fields).length > 0 ||
    hasNonEmptyConfirmedMap(entry.confirmedFields)
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

  // Wave 13: Explicitly serialize validated current values for `confirmedFields`
  // even when `effective === server`, and block invalid/empty confirmed values via `fieldErrors`.
  const confirmedMap =
    store.recordDrafts[makeRecordDraftKey(batchId, record.id)]?.confirmedFields;
  if (confirmedMap) {
    for (const rawKey of Object.keys(confirmedMap)) {
      const confirmedField = normalizeConfirmedFieldName(rawKey);
      if (!confirmedField || confirmedField in fieldErrors) {
        continue;
      }

      switch (confirmedField) {
        case "charge_item": {
          const trimmed = effective.charge_item.trim();
          if (!trimmed) {
            delete patch.charge_item;
            fieldErrors.charge_item = "请先填写非空标准费用名称后再确认保存";
          } else if (patch.charge_item === undefined) {
            patch.charge_item = trimmed;
          }
          break;
        }
        case "quantity":
        case "unit_price":
        case "amount": {
          const raw = effective[confirmedField].trim();
          if (!raw) {
            delete patch[confirmedField];
            fieldErrors[confirmedField] =
              confirmedField === "amount"
                ? "请先填写合法金额数值后再确认保存"
                : "请先填写合法数值后再确认保存";
          } else if (patch[confirmedField] === undefined) {
            try {
              patch[confirmedField] = rawDecimal(raw);
            } catch (err) {
              fieldErrors[confirmedField] =
                err instanceof Error ? err.message : "非法十进制数值";
            }
          }
          break;
        }
        case "currency": {
          const upper = effective.currency.trim().toUpperCase();
          if (!upper || !isValidIso4217Currency(upper)) {
            delete patch.currency;
            fieldErrors.currency =
              "币种须为有效 3 位大写 ISO 4217 代码（如 USD、CNY）";
          } else if (patch.currency === undefined) {
            patch.currency = upper;
          }
          break;
        }
        case "payment_term": {
          if (
            effective.payment_term !== "PP" &&
            effective.payment_term !== "CC"
          ) {
            delete patch.payment_term;
            fieldErrors.payment_term = "确认付款条款时须选择 PP 或 CC";
          } else if (patch.payment_term === undefined) {
            patch.payment_term = effective.payment_term;
          }
          break;
        }
        case "posting_type": {
          if (
            effective.posting_type !== "AR" &&
            effective.posting_type !== "AP"
          ) {
            delete patch.posting_type;
            fieldErrors.posting_type = "确认记账方向时须选择 AR 或 AP";
          } else if (patch.posting_type === undefined) {
            patch.posting_type = effective.posting_type;
          }
          break;
        }
        case "document_type": {
          const trimmed = effective.document_type.trim();
          if (!trimmed) {
            delete patch.document_type;
            fieldErrors.document_type = "请先填写非空单据类型后再确认保存";
          } else if (patch.document_type === undefined) {
            patch.document_type = trimmed;
          }
          break;
        }
        case "reference_no": {
          const trimmed = effective.reference_no.trim();
          if (!trimmed) {
            delete patch.reference_no;
            fieldErrors.reference_no = "请先填写非空单据编号后再确认保存";
          } else if (patch.reference_no === undefined) {
            patch.reference_no = trimmed;
          }
          break;
        }
        case "document_date": {
          const dateVal = effective.document_date.trim();
          if (!dateVal || !isValidCalendarDate(dateVal)) {
            delete patch.document_date;
            fieldErrors.document_date = "单据日期格式须为合法 YYYY-MM-DD";
          } else if (patch.document_date === undefined) {
            patch.document_date = dateVal;
          }
          break;
        }
        case "issuer": {
          const trimmed = effective.issuer.trim();
          if (!trimmed) {
            delete patch.issuer;
            fieldErrors.issuer = "请先填写非空签发方名称后再确认保存";
          } else if (patch.issuer === undefined) {
            patch.issuer = trimmed;
          }
          break;
        }
        case "recipient": {
          const trimmed = effective.recipient.trim();
          if (!trimmed) {
            delete patch.recipient;
            fieldErrors.recipient = "请先填写非空接收方名称后再确认保存";
          } else if (patch.recipient === undefined) {
            patch.recipient = trimmed;
          }
          break;
        }
        case "mbl_no":
        case "hbl_no":
        case "booking_no": {
          const trimmed = effective[confirmedField].trim();
          if (!trimmed) {
            delete patch[confirmedField];
            fieldErrors[confirmedField] = "请先填写非空运输单号后再确认保存";
          } else if (patch[confirmedField] === undefined) {
            patch[confirmedField] = trimmed;
          }
          break;
        }
        case "container_nos": {
          const nextContainers = parseContainerNosInput(
            effective.container_nos_text
          );
          if (nextContainers.length === 0) {
            delete patch.container_nos;
            fieldErrors.container_nos =
              "请先填写至少一个有效箱号后再确认保存";
          } else if (patch.container_nos === undefined) {
            patch.container_nos = nextContainers;
          }
          break;
        }
      }
    }
  }

  const hasChanges =
    Object.keys(patch).length > 0 || Object.keys(fieldErrors).length > 0;
  return {
    valid: Object.keys(fieldErrors).length === 0,
    hasChanges,
    fieldErrors,
    patch,
  };
}

const NON_FILLABLE_PLACEHOLDER_VALUES: ReadonlySet<string> = new Set([
  "—",
  "-",
  "缺失",
  "未提供",
  "未定",
  "待定",
  "无",
  "null",
  "none",
  "同一源单中的费用名称和金额",
]);

function parseFillableTextValue(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (
    NON_FILLABLE_PLACEHOLDER_VALUES.has(trimmed) ||
    NON_FILLABLE_PLACEHOLDER_VALUES.has(trimmed.toLowerCase()) ||
    trimmed.includes("同一源单中的")
  ) {
    return null;
  }
  return trimmed;
}

function parseFillableDecimalValue(
  raw: string | null | undefined
): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  try {
    rawDecimal(trimmed);
    return trimmed;
  } catch {
    return null;
  }
}

function parseFillableContainersValue(
  raw: string | null | undefined
): string | null {
  if (typeof raw !== "string") return null;
  const tokens = parseContainerNosInput(raw);
  if (
    tokens.length === 0 ||
    tokens.some((tok) => !/^[A-Z0-9]{4,15}$/.test(tok))
  ) {
    return null;
  }
  return tokens.join(", ");
}

interface DirectAlertFieldMeta {
  formKey: keyof RecordFormValues;
  patchKey: ConfirmableRecordPatchField;
  label: string;
  parseExpected: (raw: string | null | undefined) => string | null;
  isCurrentValid: (values: RecordFormValues) => boolean;
}

const DIRECT_ALERT_FIELD_TABLE: Readonly<Record<string, DirectAlertFieldMeta>> =
  {
    charge_item: {
      formKey: "charge_item",
      patchKey: "charge_item",
      label: "标准费用名称 (charge_item)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.charge_item.trim().length > 0,
    },
    quantity: {
      formKey: "quantity",
      patchKey: "quantity",
      label: "数量 (quantity)",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.quantity) !== null,
    },
    unit_price: {
      formKey: "unit_price",
      patchKey: "unit_price",
      label: "单价 (unit_price)",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.unit_price) !== null,
    },
    amount: {
      formKey: "amount",
      patchKey: "amount",
      label: "金额 (amount)",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.amount) !== null,
    },
    currency: {
      formKey: "currency",
      patchKey: "currency",
      label: "币种 (currency)",
      parseExpected: (raw) => {
        if (typeof raw !== "string") return null;
        const upper = raw.trim().toUpperCase();
        return isValidIso4217Currency(upper) ? upper : null;
      },
      isCurrentValid: (v) =>
        isValidIso4217Currency(v.currency.trim().toUpperCase()),
    },
    payment_term: {
      formKey: "payment_term",
      patchKey: "payment_term",
      label: "付款条款 (payment_term)",
      parseExpected: (raw) => {
        if (typeof raw !== "string") return null;
        const upper = raw.trim().toUpperCase();
        return upper === "PP" || upper === "CC" ? upper : null;
      },
      isCurrentValid: (v) => v.payment_term === "PP" || v.payment_term === "CC",
    },
    posting_type: {
      formKey: "posting_type",
      patchKey: "posting_type",
      label: "记账方向 (posting_type)",
      parseExpected: (raw) => {
        if (typeof raw !== "string") return null;
        const upper = raw.trim().toUpperCase();
        return upper === "AR" || upper === "AP" ? upper : null;
      },
      isCurrentValid: (v) => v.posting_type === "AR" || v.posting_type === "AP",
    },
    document_type: {
      formKey: "document_type",
      patchKey: "document_type",
      label: "单据类型 (document_type)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.document_type.trim().length > 0,
    },
    reference_no: {
      formKey: "reference_no",
      patchKey: "reference_no",
      label: "单据编号 (reference_no)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.reference_no.trim().length > 0,
    },
    document_date: {
      formKey: "document_date",
      patchKey: "document_date",
      label: "单据日期 (document_date)",
      parseExpected: (raw) => {
        if (typeof raw !== "string") return null;
        const trimmed = raw.trim();
        return isValidCalendarDate(trimmed) ? trimmed : null;
      },
      isCurrentValid: (v) => isValidCalendarDate(v.document_date.trim()),
    },
    issuer: {
      formKey: "issuer",
      patchKey: "issuer",
      label: "签发方 (issuer)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.issuer.trim().length > 0,
    },
    recipient: {
      formKey: "recipient",
      patchKey: "recipient",
      label: "接收方 (recipient)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.recipient.trim().length > 0,
    },
    mbl_no: {
      formKey: "mbl_no",
      patchKey: "mbl_no",
      label: "主提单号 (mbl_no)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.mbl_no.trim().length > 0,
    },
    mbl_nos: {
      formKey: "mbl_no",
      patchKey: "mbl_no",
      label: "主提单号 (mbl_no)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.mbl_no.trim().length > 0,
    },
    hbl_no: {
      formKey: "hbl_no",
      patchKey: "hbl_no",
      label: "分提单号 (hbl_no)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.hbl_no.trim().length > 0,
    },
    booking_no: {
      formKey: "booking_no",
      patchKey: "booking_no",
      label: "订舱号 (booking_no)",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.booking_no.trim().length > 0,
    },
    container_nos: {
      formKey: "container_nos_text",
      patchKey: "container_nos",
      label: "箱号列表 (container_nos)",
      parseExpected: parseFillableContainersValue,
      isCurrentValid: (v) =>
        parseContainerNosInput(v.container_nos_text).length > 0,
    },
  };

export interface AlertActionButtonSpec {
  kind: "apply_suggestion" | "confirm_current";
  label: string;
  title: string;
  focusFieldKey: string;
  updateField?: {
    formKey: keyof RecordFormValues;
    value: string;
  };
  confirmFields: ConfirmableRecordPatchField[];
  missingRequiredCheck?: {
    focusKey: string;
    fieldKey: string;
    errorMessage: string;
    feedbackMessage: string;
  };
  feedbackMessage: string;
}

export interface ResolvedRecordAlertActions {
  locateButton: {
    label: "定位字段" | "定位关联字段" | "定位单据汇总";
    focusFieldKey: string;
    emptyFieldHint?: string;
  } | null;
  helperNote: string | null;
  actionButtons: AlertActionButtonSpec[];
}

export function resolveAlertFocusTargetKey(
  rawField: string | null | undefined,
  hasSourceDoc = false
): string | null {
  if (!rawField) return null;
  const trimmed = rawField.trim();
  if (!trimmed) return null;
  if (trimmed in DIRECT_ALERT_FIELD_TABLE) {
    return DIRECT_ALERT_FIELD_TABLE[trimmed].formKey;
  }
  if (trimmed === "payee_hint") return "issuer";
  if (trimmed === "payer_hint") return "recipient";
  if (trimmed === "reported_charge_amount") return "amount";
  if (hasSourceDoc) {
    if (
      trimmed === "total_amount" ||
      trimmed.startsWith("totals_by_currency") ||
      trimmed.startsWith("reported_total_amount")
    ) {
      return "doc_total_amount";
    }
    if (
      trimmed === "subtotal_amount" ||
      trimmed.startsWith("subtotals_by_currency") ||
      trimmed.startsWith("reported_subtotal_amount")
    ) {
      return "doc_subtotal_amount";
    }
    if (
      trimmed === "tax_amount" ||
      trimmed.startsWith("taxes_by_currency") ||
      trimmed.startsWith("reported_tax_amount")
    ) {
      return "doc_tax_amount";
    }
    if (trimmed === "total_currency") {
      return "doc_total_currency";
    }
  }
  return null;
}

export function resolveRecordAlertActions(
  alert: ValidationAlert,
  formValues: RecordFormValues,
  options: { hasSourceDoc?: boolean } = {}
): ResolvedRecordAlertActions {
  const rawField = alert.field?.trim() ?? "";
  if (!rawField) {
    return { locateButton: null, helperNote: null, actionButtons: [] };
  }

  // 1. Direct editable record fields
  const directMeta = DIRECT_ALERT_FIELD_TABLE[rawField];
  if (directMeta) {
    const locateButton: ResolvedRecordAlertActions["locateButton"] = {
      label: "定位字段",
      focusFieldKey: directMeta.formKey,
    };
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED") {
      const parsedExpected = directMeta.parseExpected(alert.expected);
      if (parsedExpected !== null) {
        let nextFormVal = parsedExpected;
        let isSameAsCurrent = false;

        if (directMeta.patchKey === "container_nos") {
          const actualToken = alert.actual?.trim() ?? "";
          const currText = formValues.container_nos_text;
          if (
            actualToken &&
            currText.toUpperCase().includes(actualToken.toUpperCase())
          ) {
            const escaped = actualToken.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            nextFormVal = currText.replace(
              new RegExp(escaped, "i"),
              parsedExpected
            );
          }
          const currNorm = parseContainerNosInput(currText).join(", ");
          const nextNorm = parseContainerNosInput(nextFormVal).join(", ");
          isSameAsCurrent = currNorm.length > 0 && currNorm === nextNorm;
        } else if (directMeta.patchKey === "currency") {
          isSameAsCurrent =
            formValues.currency.trim().toUpperCase() === parsedExpected;
        } else {
          isSameAsCurrent =
            String(formValues[directMeta.formKey]).trim() === parsedExpected;
        }

        if (!isSameAsCurrent) {
          actionButtons.push({
            kind: "apply_suggestion",
            label: "采用建议值",
            title: `将填入建议值: ${parsedExpected}`,
            focusFieldKey: directMeta.formKey,
            updateField: {
              formKey: directMeta.formKey,
              value: nextFormVal,
            },
            confirmFields: [directMeta.patchKey],
            feedbackMessage: `已将 ${directMeta.label} 填入建议值（${parsedExpected}），请点击「保存修订并重验」`,
          });
        } else {
          actionButtons.push({
            kind: "confirm_current",
            label: "确认采用原文值",
            title: `确认保留当前原文值: ${parsedExpected}`,
            focusFieldKey: directMeta.formKey,
            confirmFields: [directMeta.patchKey],
            feedbackMessage: `已标记确认当前原文值（${parsedExpected}），请点击下方「保存修订并重验」解除告警`,
          });
        }
      } else if (
        alert.code === "CROSS_DOCUMENT_CONFLICT" &&
        directMeta.isCurrentValid(formValues)
      ) {
        const currentText = String(formValues[directMeta.formKey]).trim();
        actionButtons.push({
          kind: "confirm_current",
          label: "确认采用原文值",
          title: `确认保留当前原文值: ${currentText}`,
          focusFieldKey: directMeta.formKey,
          confirmFields: [directMeta.patchKey],
          feedbackMessage: `已标记确认当前原文值（${currentText}），请点击下方「保存修订并重验」解除告警`,
        });
      }
    }

    return { locateButton, helperNote: null, actionButtons };
  }

  // 2. Indirect party evidence fields: payee_hint / payer_hint
  if (rawField === "payee_hint" || rawField === "payer_hint") {
    const targetField: "issuer" | "recipient" =
      rawField === "payee_hint" ? "issuer" : "recipient";
    const targetLabel =
      rawField === "payee_hint" ? "签发方 (issuer)" : "接收方 (recipient)";
    const helperNote =
      rawField === "payee_hint"
        ? "关联字段说明：payee_hint 映射至「签发方 (issuer)」或「记账方向 (posting_type)」，确认当前签发方或台账方向并保存重验即可解除告警。"
        : "关联字段说明：payer_hint 映射至「接收方 (recipient)」或「记账方向 (posting_type)」，确认当前接收方或台账方向并保存重验即可解除告警。";

    const currentParty = formValues[targetField].trim();
    const locateButton: ResolvedRecordAlertActions["locateButton"] = {
      label: "定位关联字段",
      focusFieldKey: targetField,
      emptyFieldHint: currentParty
        ? undefined
        : `请先在输入框填写${targetLabel}（或核对记账方向），再点击「保存修订并重验」`,
    };
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED") {
      const expectedParty = parseFillableTextValue(alert.expected);
      if (expectedParty !== null && currentParty !== expectedParty) {
        actionButtons.push({
          kind: "apply_suggestion",
          label: "采用建议主体",
          title: `将填入建议主体: ${expectedParty}`,
          focusFieldKey: targetField,
          updateField: {
            formKey: targetField,
            value: expectedParty,
          },
          confirmFields: [targetField],
          feedbackMessage: `已将 ${targetLabel} 填入建议主体（${expectedParty}），请点击下方「保存修订并重验」`,
        });
      } else {
        actionButtons.push({
          kind: "confirm_current",
          label: "确认关联主体",
          title: currentParty
            ? `确认保留当前${targetLabel}: ${currentParty}`
            : `请先填写${targetLabel}后再确认`,
          focusFieldKey: targetField,
          confirmFields: [targetField],
          missingRequiredCheck: currentParty
            ? undefined
            : {
                focusKey: targetField,
                fieldKey: targetField,
                errorMessage: `请先填写非空${targetLabel}后再确认保存`,
                feedbackMessage: `当前${targetLabel}尚未填写，已定位输入框，请补齐主体名称（或修订台账方向）后保存重验`,
              },
          feedbackMessage: `已标记确认当前${targetLabel}（${currentParty}），请点击下方「保存修订并重验」解除告警`,
        });
      }
    }

    return { locateButton, helperNote, actionButtons };
  }

  // 3. Indirect charge evidence field: reported_charge_amount
  if (rawField === "reported_charge_amount") {
    const helperNote =
      alert.code === "AMOUNT_TOTAL_MISMATCH"
        ? "关联字段说明：此项为抽取阶段行与原文明细对比提示，请核对下方「金额 (amount)」与「标准费用名称 (charge_item)」后确认保存。"
        : "关联字段说明：对应可编辑字段「金额 (amount)」与「标准费用名称 (charge_item)」。";

    const currAmt = formValues.amount.trim();
    const currCharge = formValues.charge_item.trim();
    const hasValidAmt = parseFillableDecimalValue(currAmt) !== null;
    const hasValidCharge = currCharge.length > 0;

    const locateButton: ResolvedRecordAlertActions["locateButton"] = {
      label: "定位关联字段",
      focusFieldKey: !hasValidCharge ? "charge_item" : "amount",
      emptyFieldHint:
        !hasValidCharge || !hasValidAmt
          ? "请先补齐标准费用名称与合法金额，再点击「保存修订并重验」"
          : undefined,
    };
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED") {
      const parsedExpectedAmt = parseFillableDecimalValue(alert.expected);
      if (parsedExpectedAmt !== null && currAmt !== parsedExpectedAmt) {
        actionButtons.push({
          kind: "apply_suggestion",
          label: "采用建议值",
          title: `将填入建议值: ${parsedExpectedAmt}`,
          focusFieldKey: "amount",
          updateField: {
            formKey: "amount",
            value: parsedExpectedAmt,
          },
          confirmFields: hasValidCharge ? ["amount", "charge_item"] : ["amount"],
          feedbackMessage: `已将金额 (amount) 填入建议值（${parsedExpectedAmt}），请核对费用项后点击「保存修订并重验」`,
        });
      }

      actionButtons.push({
        kind: "confirm_current",
        label: "确认当前金额与费用项",
        title:
          hasValidAmt && hasValidCharge
            ? `确认保留当前金额 ${currAmt} 与费用项 ${currCharge}`
            : "请先补齐有效的费用名称与金额后再确认",
        focusFieldKey: !hasValidCharge ? "charge_item" : "amount",
        confirmFields: ["amount", "charge_item"],
        missingRequiredCheck:
          hasValidAmt && hasValidCharge
            ? undefined
            : !hasValidCharge
              ? {
                  focusKey: "charge_item",
                  fieldKey: "charge_item",
                  errorMessage: "请先填写非空标准费用名称后再确认保存",
                  feedbackMessage:
                    "当前标准费用名称为空，已定位输入框，请填写后再确认保存",
                }
              : {
                  focusKey: "amount",
                  fieldKey: "amount",
                  errorMessage: "请先填写合法金额数值后再确认保存",
                  feedbackMessage:
                    "当前金额为空或格式不合法，已定位输入框，请修正后再确认保存",
                },
        feedbackMessage: `已标记确认当前金额（${currAmt}）与费用项（${currCharge}），请点击下方「保存修订并重验」解除告警`,
      });
    }

    return { locateButton, helperNote, actionButtons };
  }

  // 4. Document-level summary total fields
  const docFocusTarget = resolveAlertFocusTargetKey(
    rawField,
    Boolean(options.hasSourceDoc)
  );
  if (docFocusTarget && docFocusTarget.startsWith("doc_")) {
    return {
      locateButton: {
        label: "定位单据汇总",
        focusFieldKey: docFocusTarget,
      },
      helperNote:
        "关联字段说明：此项涉及所属单据汇总金额，可在下方「单据汇总金额修订」区核对小计、税额与总计。",
      actionButtons: [],
    };
  }

  return { locateButton: null, helperNote: null, actionButtons: [] };
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
