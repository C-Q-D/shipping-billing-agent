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

export interface ConfirmedFieldSnapshot {
  value: string;
  recordUpdatedAt: string | null;
}

export type ConfirmedFieldMap = Record<string, ConfirmedFieldSnapshot>;

export interface RecordDraftEntry {
  draftVersion: number;
  fields: Partial<RecordFormValues>;
  confirmedFields?: ConfirmedFieldMap;
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
  submittedConfirmedFields?: ConfirmedFieldMap;
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

const observedRecordsByStore = new WeakMap<
  DraftStoreState,
  Map<string, BillingRecordDetail>
>();

function rememberObservedRecord(
  store: DraftStoreState,
  batchId: string,
  record: BillingRecordDetail
): void {
  let map = observedRecordsByStore.get(store);
  if (!map) {
    map = new Map<string, BillingRecordDetail>();
    observedRecordsByStore.set(store, map);
  }
  map.set(makeRecordDraftKey(batchId, record.id), record);
}

function inheritObservedRecords(
  prevStore: DraftStoreState,
  nextStore: DraftStoreState,
  batchId?: string,
  record?: BillingRecordDetail
): DraftStoreState {
  if (prevStore !== nextStore) {
    const prevMap = observedRecordsByStore.get(prevStore);
    if (prevMap && prevMap.size > 0) {
      observedRecordsByStore.set(nextStore, new Map(prevMap));
    }
  }
  if (batchId && record) {
    rememberObservedRecord(nextStore, batchId, record);
  }
  return nextStore;
}

function resolveObservedRecord(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  explicitRecord?: BillingRecordDetail | null
): BillingRecordDetail | null {
  if (explicitRecord) {
    rememberObservedRecord(store, batchId, explicitRecord);
    return explicitRecord;
  }
  return (
    observedRecordsByStore
      .get(store)
      ?.get(makeRecordDraftKey(batchId, recordId)) ?? null
  );
}

export function getEffectiveRecordFormValues(
  record: BillingRecordDetail,
  store: DraftStoreState,
  batchId: string
): RecordFormValues {
  rememberObservedRecord(store, batchId, record);
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

function patchFieldToFormKey(
  field: ConfirmableRecordPatchField
): keyof RecordFormValues {
  return field === "container_nos" ? "container_nos_text" : field;
}

export function getCanonicalConfirmFieldValue(
  values: RecordFormValues,
  field: ConfirmableRecordPatchField
): string {
  switch (field) {
    case "currency":
      return values.currency.trim().toUpperCase();
    case "payment_term":
      return values.payment_term;
    case "posting_type":
      return values.posting_type;
    case "container_nos":
      return parseContainerNosInput(values.container_nos_text).join(", ");
    case "charge_item":
    case "quantity":
    case "unit_price":
    case "amount":
    case "document_type":
    case "reference_no":
    case "document_date":
    case "issuer":
    case "recipient":
    case "mbl_no":
    case "hbl_no":
    case "booking_no":
      return values[field].trim();
  }
}

export function isConfirmedFieldEntryValid(
  field: ConfirmableRecordPatchField,
  entry: ConfirmedFieldSnapshot | undefined,
  record: BillingRecordDetail,
  draftFields?: Partial<RecordFormValues>
): boolean {
  if (!entry || typeof entry.value !== "string") {
    return false;
  }
  const serverValues = extractServerRecordFormValues(record);
  const formKey = patchFieldToFormKey(field);
  const effectiveValues = draftFields
    ? { ...serverValues, ...draftFields }
    : serverValues;
  const currentEffectiveVal = getCanonicalConfirmFieldValue(
    effectiveValues,
    field
  );
  if (currentEffectiveVal !== entry.value) {
    return false;
  }

  const hasExplicitFieldOverride = Boolean(
    draftFields &&
      Object.prototype.hasOwnProperty.call(draftFields, formKey) &&
      draftFields[formKey] !== undefined &&
      draftFields[formKey] !== serverValues[formKey]
  );
  if (!hasExplicitFieldOverride) {
    const currentServerVal = getCanonicalConfirmFieldValue(serverValues, field);
    if (currentServerVal !== entry.value) {
      return false;
    }
    const currentUpdatedAt = record.updated_at ?? null;
    if (entry.recordUpdatedAt !== currentUpdatedAt) {
      return false;
    }
  }
  return true;
}

function filterValidConfirmedFields(
  confirmed: ConfirmedFieldMap | undefined,
  record: BillingRecordDetail | null,
  draftFields?: Partial<RecordFormValues>
): ConfirmedFieldMap {
  if (!confirmed) {
    return {};
  }
  const valid: ConfirmedFieldMap = {};
  for (const [rawKey, snap] of Object.entries(confirmed)) {
    const mapped = normalizeConfirmedFieldName(rawKey);
    if (!mapped || !snap || typeof snap.value !== "string") {
      continue;
    }
    if (
      !record ||
      isConfirmedFieldEntryValid(mapped, snap, record, draftFields)
    ) {
      valid[mapped] = {
        value: snap.value,
        recordUpdatedAt: snap.recordUpdatedAt ?? null,
      };
    }
  }
  return valid;
}

function hasNonEmptyConfirmedMap(confirmed?: ConfirmedFieldMap): boolean {
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
  confirmedFields?: ConfirmedFieldMap;
}

interface InFlightSaveShape<TFields extends object> {
  submittedVersion: number;
  submittedFields: Partial<TFields>;
  submittedConfirmedFields?: ConfirmedFieldMap;
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
  expectedVersion?: number,
  pruneConfirmed?: (
    c: ConfirmedFieldMap | undefined,
    s: TServer | undefined,
    f: Partial<TFields>
  ) => ConfirmedFieldMap | undefined
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
    const remainingConfirmed = pruneConfirmed
      ? pruneConfirmed(current.confirmedFields, updatedServer, remaining ?? {})
      : hasNonEmptyConfirmedMap(current.confirmedFields)
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

  const rawPostConfirmed: ConfirmedFieldMap = {};
  if (inFlight.postSubmitConfirmed && current.confirmedFields) {
    for (const k of Object.keys(current.confirmedFields)) {
      if (inFlight.postSubmitConfirmed[k] === true) {
        rawPostConfirmed[k] = current.confirmedFields[k];
      }
    }
  }

  const remaining =
    Object.keys(postTouchedFields).length > 0
      ? updatedServer
        ? pruneFields(extractServer(updatedServer), postTouchedFields)
        : postTouchedFields
      : null;

  const validatedPostConfirmed = pruneConfirmed
    ? pruneConfirmed(rawPostConfirmed, updatedServer, remaining ?? {})
    : hasNonEmptyConfirmedMap(rawPostConfirmed)
      ? rawPostConfirmed
      : undefined;
  const hasPostConfirmed = hasNonEmptyConfirmedMap(validatedPostConfirmed);

  const nextDrafts = { ...drafts };
  if (!remaining && !hasPostConfirmed) {
    delete nextDrafts[key];
  } else {
    nextDrafts[key] = {
      draftVersion: current.draftVersion,
      fields: remaining ?? {},
      ...(hasPostConfirmed && validatedPostConfirmed
        ? { confirmedFields: validatedPostConfirmed }
        : {}),
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
  serverEntity?: TServer,
  pruneConfirmed?: (
    c: ConfirmedFieldMap | undefined,
    s: TServer | undefined,
    f: Partial<TFields>
  ) => ConfirmedFieldMap | undefined
) {
  const nextInFlight = { ...inFlightMap };
  delete nextInFlight[key];

  const current = drafts[key];
  if (!current || !serverEntity) {
    return { nextDrafts: drafts, nextInFlight };
  }
  const remaining = pruneFields(extractServer(serverEntity), current.fields);
  const remainingConfirmed = pruneConfirmed
    ? pruneConfirmed(current.confirmedFields, serverEntity, remaining ?? {})
    : hasNonEmptyConfirmedMap(current.confirmedFields)
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

function pruneConfirmedRecordMap(
  confirmed: ConfirmedFieldMap | undefined,
  record: BillingRecordDetail | undefined,
  fields: Partial<RecordFormValues>
): ConfirmedFieldMap | undefined {
  const filtered = filterValidConfirmedFields(confirmed, record ?? null, fields);
  return Object.keys(filtered).length > 0 ? filtered : undefined;
}

export function getConfirmedRecordFields(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  record?: BillingRecordDetail | null
): ConfirmedFieldMap {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  if (!entry?.confirmedFields) {
    if (record) {
      rememberObservedRecord(store, batchId, record);
    }
    return {};
  }
  const effectiveRecord = resolveObservedRecord(
    store,
    batchId,
    recordId,
    record
  );
  return filterValidConfirmedFields(
    entry.confirmedFields,
    effectiveRecord,
    entry.fields
  );
}

export function hasStaleConfirmedFields(
  store: DraftStoreState,
  batchId: string,
  record: BillingRecordDetail
): boolean {
  rememberObservedRecord(store, batchId, record);
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, record.id)];
  if (!entry?.confirmedFields) {
    return false;
  }
  const rawKeys = Object.keys(entry.confirmedFields);
  if (rawKeys.length === 0) {
    return false;
  }
  const validMap = filterValidConfirmedFields(
    entry.confirmedFields,
    record,
    entry.fields
  );
  return Object.keys(validMap).length < rawKeys.length;
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
    rememberObservedRecord(store, batchId, record);
    return store;
  }

  const key = makeRecordDraftKey(batchId, record.id);
  const existing = store.recordDrafts[key];
  const existingFields = existing ? { ...existing.fields } : {};
  const server = extractServerRecordFormValues(record);
  const effective = { ...server, ...existingFields };
  const recordUpdatedAt = record.updated_at ?? null;

  const nextConfirmed: ConfirmedFieldMap = filterValidConfirmedFields(
    existing?.confirmedFields,
    record,
    existingFields
  );
  for (const f of normalized) {
    nextConfirmed[f] = {
      value: getCanonicalConfirmFieldValue(effective, f),
      recordUpdatedAt,
    };
  }

  const nextVersion = store.nextDraftVersion + 1;
  const nextRecordDrafts: Record<string, RecordDraftEntry> = {
    ...store.recordDrafts,
    [key]: {
      draftVersion: nextVersion,
      fields: existingFields,
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
    return inheritObservedRecords(
      store,
      {
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
      },
      batchId,
      record
    );
  }

  return inheritObservedRecords(
    store,
    {
      ...store,
      nextDraftVersion: nextVersion,
      recordDrafts: nextRecordDrafts,
    },
    batchId,
    record
  );
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

  const finalEffective = { ...server, ...nextFields };
  const recordUpdatedAt = record.updated_at ?? null;
  const nextConfirmed: ConfirmedFieldMap = filterValidConfirmedFields(
    existing.confirmedFields,
    record,
    nextFields
  );
  const newlyConfirmed: ConfirmableRecordPatchField[] = [];
  if (stagedConfirmed) {
    for (const raw of stagedConfirmed) {
      const mapped = normalizeConfirmedFieldName(raw);
      if (mapped) {
        nextConfirmed[mapped] = {
          value: getCanonicalConfirmFieldValue(finalEffective, mapped),
          recordUpdatedAt,
        };
        if (!newlyConfirmed.includes(mapped)) {
          newlyConfirmed.push(mapped);
        }
      }
    }
  }
  const hasConfirmed = Object.keys(nextConfirmed).length > 0;

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
    return inheritObservedRecords(
      store,
      {
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
      },
      batchId,
      record
    );
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
  return inheritObservedRecords(
    store,
    {
      ...store,
      nextDraftVersion: nextVersion,
      recordDrafts: nextRecordDrafts,
    },
    batchId,
    record
  );
}

export function beginRecordSave(
  store: DraftStoreState,
  batchId: string,
  recordId: string,
  version?: number | null,
  submittedFields?: Partial<RecordFormValues>
): DraftStoreState {
  const key = makeRecordDraftKey(batchId, recordId);
  return inheritObservedRecords(store, {
    ...store,
    inFlightRecordSaves: beginDraftSave(
      store.recordDrafts,
      store.inFlightRecordSaves ?? {},
      key,
      store.nextDraftVersion,
      version,
      submittedFields
    ),
  });
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
    expectedVersion,
    pruneConfirmedRecordMap
  );
  return inheritObservedRecords(
    store,
    {
      ...store,
      recordDrafts: nextDrafts,
      inFlightRecordSaves: nextInFlight,
    },
    batchId,
    updatedRecord
  );
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
    serverRecord,
    pruneConfirmedRecordMap
  );
  return inheritObservedRecords(
    store,
    {
      ...store,
      recordDrafts: nextDrafts,
      inFlightRecordSaves: nextInFlight,
    },
    batchId,
    serverRecord
  );
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
    return inheritObservedRecords(
      store,
      {
        ...store,
        recordDrafts: nextRecordDrafts,
        inFlightRecordSaves: nextInFlight,
      },
      batchId,
      updatedRecord
    );
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
  recordId: string,
  record?: BillingRecordDetail | null
): boolean {
  const entry = store.recordDrafts[makeRecordDraftKey(batchId, recordId)];
  if (!entry) {
    if (record) {
      rememberObservedRecord(store, batchId, record);
    }
    return false;
  }
  if (Object.keys(entry.fields).length > 0) {
    if (record) {
      rememberObservedRecord(store, batchId, record);
    }
    return true;
  }
  const validConfirmed = getConfirmedRecordFields(
    store,
    batchId,
    recordId,
    record
  );
  return Object.keys(validConfirmed).length > 0;
}

export function getDirtyRecordIds(
  store: DraftStoreState,
  batchId: string,
  records: ReadonlyArray<BillingRecordDetail>
): Set<string> {
  const dirty = new Set<string>();
  for (const rec of records) {
    if (hasRecordDraft(store, batchId, rec.id, rec)) {
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

  // Wave 13 / 13B (F3): Explicitly serialize validated values for non-stale `confirmedFields`
  // bound to exact confirmation-time value + record `updated_at`, and block invalid/empty values via `fieldErrors`.
  const confirmedMap = getConfirmedRecordFields(
    store,
    batchId,
    record.id,
    record
  );
  for (const [rawKey, snap] of Object.entries(confirmedMap)) {
    const confirmedField = normalizeConfirmedFieldName(rawKey);
    if (!confirmedField || confirmedField in fieldErrors) {
      continue;
    }

    switch (confirmedField) {
      case "charge_item": {
        const trimmed = snap.value.trim();
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
        const raw = snap.value.trim();
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
        const upper = snap.value.trim().toUpperCase();
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
        if (snap.value !== "PP" && snap.value !== "CC") {
          delete patch.payment_term;
          fieldErrors.payment_term = "确认付款条款时须选择 PP 或 CC";
        } else if (patch.payment_term === undefined) {
          patch.payment_term = snap.value;
        }
        break;
      }
      case "posting_type": {
        if (snap.value !== "AR" && snap.value !== "AP") {
          delete patch.posting_type;
          fieldErrors.posting_type = "确认记账方向时须选择 AR 或 AP";
        } else if (patch.posting_type === undefined) {
          patch.posting_type = snap.value;
        }
        break;
      }
      case "document_type": {
        const trimmed = snap.value.trim();
        if (!trimmed) {
          delete patch.document_type;
          fieldErrors.document_type = "请先填写非空单据类型后再确认保存";
        } else if (patch.document_type === undefined) {
          patch.document_type = trimmed;
        }
        break;
      }
      case "reference_no": {
        const trimmed = snap.value.trim();
        if (!trimmed) {
          delete patch.reference_no;
          fieldErrors.reference_no = "请先填写非空单据编号后再确认保存";
        } else if (patch.reference_no === undefined) {
          patch.reference_no = trimmed;
        }
        break;
      }
      case "document_date": {
        const dateVal = snap.value.trim();
        if (!dateVal || !isValidCalendarDate(dateVal)) {
          delete patch.document_date;
          fieldErrors.document_date = "单据日期格式须为合法 YYYY-MM-DD";
        } else if (patch.document_date === undefined) {
          patch.document_date = dateVal;
        }
        break;
      }
      case "issuer": {
        const trimmed = snap.value.trim();
        if (!trimmed) {
          delete patch.issuer;
          fieldErrors.issuer = "请先填写非空签发方名称后再确认保存";
        } else if (patch.issuer === undefined) {
          patch.issuer = trimmed;
        }
        break;
      }
      case "recipient": {
        const trimmed = snap.value.trim();
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
        const trimmed = snap.value.trim();
        if (!trimmed) {
          delete patch[confirmedField];
          fieldErrors[confirmedField] = "请先填写非空运输单号后再确认保存";
        } else if (patch[confirmedField] === undefined) {
          patch[confirmedField] = trimmed;
        }
        break;
      }
      case "container_nos": {
        const nextContainers = parseContainerNosInput(snap.value);
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

export const ENTITY_NOT_MATCHED_GUIDANCE =
  "请核对签发方、接收方是否包含当前记账公司或别名，或确认应收/应付方向（若双方名称与方向存在明确矛盾，请先修正公司名称再保存）";

export const CONTEXT_INFERRED_WARNING_GUIDANCE =
  "此项为上下文自动补全供核对；金额与币种确认无误后，请直接填写批准理由放行（仅保存字段不会消除补全提示）";

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

function dedupeContainerTokens(tokens: ReadonlyArray<string>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const tok of tokens) {
    if (!seen.has(tok)) {
      seen.add(tok);
      out.push(tok);
    }
  }
  return out;
}

function parseFillableContainersValue(
  raw: string | null | undefined
): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (
    !trimmed ||
    NON_FILLABLE_PLACEHOLDER_VALUES.has(trimmed) ||
    NON_FILLABLE_PLACEHOLDER_VALUES.has(trimmed.toLowerCase()) ||
    trimmed.includes("同一源单中的")
  ) {
    return null;
  }
  const tokens = dedupeContainerTokens(parseContainerNosInput(trimmed));
  if (
    tokens.length === 0 ||
    tokens.some((tok) => !/^(?=.*[A-Z0-9])[A-Z0-9_-]{4,20}$/.test(tok))
  ) {
    return null;
  }
  return tokens.join(", ");
}

export interface ContainerSuggestionResult {
  nextValue: string;
  alreadyResolved: boolean;
  replacedActual: boolean;
  appendedExpected: boolean;
}

export function applyContainerSuggestion(
  currentText: string,
  actualRaw: string | null | undefined,
  expectedRaw: string | null | undefined
): ContainerSuggestionResult {
  const currentTokens = dedupeContainerTokens(
    parseContainerNosInput(currentText)
  );
  const expectedTokens = dedupeContainerTokens(
    parseContainerNosInput(expectedRaw ?? "")
  );
  if (expectedTokens.length === 0) {
    return {
      nextValue: currentTokens.join(", "),
      alreadyResolved: true,
      replacedActual: false,
      appendedExpected: false,
    };
  }
  if (currentTokens.length === 0) {
    return {
      nextValue: expectedTokens.join(", "),
      alreadyResolved: false,
      replacedActual: false,
      appendedExpected: true,
    };
  }

  const actualTokens = dedupeContainerTokens(
    parseContainerNosInput(actualRaw ?? "")
  );
  if (actualTokens.length > 0) {
    const actualSet = new Set(actualTokens);
    const hasAnyActual = currentTokens.some((tok) => actualSet.has(tok));
    if (!hasAnyActual) {
      return {
        nextValue: currentTokens.join(", "),
        alreadyResolved: true,
        replacedActual: false,
        appendedExpected: false,
      };
    }
    const replaced: string[] = [];
    for (const tok of currentTokens) {
      if (actualSet.has(tok)) {
        replaced.push(...expectedTokens);
      } else {
        replaced.push(tok);
      }
    }
    const dedupedNext = dedupeContainerTokens(replaced);
    const currNorm = currentTokens.join(", ");
    const nextNorm = dedupedNext.join(", ");
    return {
      nextValue: nextNorm,
      alreadyResolved: currNorm === nextNorm,
      replacedActual: currNorm !== nextNorm,
      appendedExpected: false,
    };
  }

  const currentSet = new Set(currentTokens);
  const missingExpected = expectedTokens.filter((tok) => !currentSet.has(tok));
  if (missingExpected.length === 0) {
    return {
      nextValue: currentTokens.join(", "),
      alreadyResolved: true,
      replacedActual: false,
      appendedExpected: false,
    };
  }
  return {
    nextValue: dedupeContainerTokens([
      ...currentTokens,
      ...missingExpected,
    ]).join(", "),
    alreadyResolved: false,
    replacedActual: false,
    appendedExpected: true,
  };
}

const PARTY_BRANCH_SUFFIX_RE = /(?:\s*\([^()]*\))+[^\p{L}\p{N}]*$/u;
const PARTY_WORDS_RE = /[\p{L}\p{N}]+/gu;

function extractPartyWords(text: string): string {
  const matches = text.replace(/\./g, "").match(PARTY_WORDS_RE);
  return matches ? matches.join(" ") : "";
}

function normalizePartyNameForComparison(raw: string): string {
  const text = raw.normalize("NFKC").toLowerCase();
  const branchMatch = PARTY_BRANCH_SUFFIX_RE.exec(text);
  if (branchMatch && branchMatch.index !== undefined) {
    const stem = extractPartyWords(text.slice(0, branchMatch.index));
    const parenMatches = Array.from(
      branchMatch[0].matchAll(/\(([^()]*)\)/g),
      (m) => `(${extractPartyWords(m[1])})`
    );
    return [stem, ...parenMatches].filter(Boolean).join(" ").trim();
  }
  return extractPartyWords(text).trim();
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
      label: "标准费用名称",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.charge_item.trim().length > 0,
    },
    quantity: {
      formKey: "quantity",
      patchKey: "quantity",
      label: "数量",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.quantity) !== null,
    },
    unit_price: {
      formKey: "unit_price",
      patchKey: "unit_price",
      label: "单价",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.unit_price) !== null,
    },
    amount: {
      formKey: "amount",
      patchKey: "amount",
      label: "金额",
      parseExpected: parseFillableDecimalValue,
      isCurrentValid: (v) => parseFillableDecimalValue(v.amount) !== null,
    },
    currency: {
      formKey: "currency",
      patchKey: "currency",
      label: "币种",
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
      label: "付款条款",
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
      label: "记账方向",
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
      label: "单据类型",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.document_type.trim().length > 0,
    },
    reference_no: {
      formKey: "reference_no",
      patchKey: "reference_no",
      label: "单据参考号",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.reference_no.trim().length > 0,
    },
    document_date: {
      formKey: "document_date",
      patchKey: "document_date",
      label: "单据日期",
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
      label: "签发方",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.issuer.trim().length > 0,
    },
    recipient: {
      formKey: "recipient",
      patchKey: "recipient",
      label: "接收方",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.recipient.trim().length > 0,
    },
    mbl_no: {
      formKey: "mbl_no",
      patchKey: "mbl_no",
      label: "主提单号",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.mbl_no.trim().length > 0,
    },
    mbl_nos: {
      formKey: "mbl_no",
      patchKey: "mbl_no",
      label: "主提单号",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.mbl_no.trim().length > 0,
    },
    hbl_no: {
      formKey: "hbl_no",
      patchKey: "hbl_no",
      label: "分提单号",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.hbl_no.trim().length > 0,
    },
    booking_no: {
      formKey: "booking_no",
      patchKey: "booking_no",
      label: "订舱号",
      parseExpected: parseFillableTextValue,
      isCurrentValid: (v) => v.booking_no.trim().length > 0,
    },
    container_nos: {
      formKey: "container_nos_text",
      patchKey: "container_nos",
      label: "箱号列表",
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
    label:
      | "定位字段"
      | "定位关联字段"
      | "定位金额字段"
      | "定位主体与方向"
      | "定位单据汇总";
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
  if (
    trimmed === "payee_hint" ||
    trimmed === "accounting_entity" ||
    trimmed === "transaction_party"
  ) {
    return "issuer";
  }
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
  const isEntityNotMatched = alert.code === "ENTITY_NOT_MATCHED";
  const isContextInferredSoft =
    alert.code === "CONTEXT_INFERRED_FIELD" && alert.severity === "WARNING";

  if (isEntityNotMatched && !(rawField in DIRECT_ALERT_FIELD_TABLE)) {
    const entityFocusTarget = !formValues.issuer.trim()
      ? "issuer"
      : !formValues.posting_type
        ? "posting_type"
        : "issuer";
    return {
      locateButton: {
        label: "定位主体与方向",
        focusFieldKey: entityFocusTarget,
        emptyFieldHint: ENTITY_NOT_MATCHED_GUIDANCE,
      },
      helperNote: ENTITY_NOT_MATCHED_GUIDANCE,
      actionButtons: [],
    };
  }

  if (!rawField) {
    return { locateButton: null, helperNote: null, actionButtons: [] };
  }

  // 1. Direct editable record fields
  const directMeta = DIRECT_ALERT_FIELD_TABLE[rawField];
  if (directMeta) {
    const locateButton: ResolvedRecordAlertActions["locateButton"] =
      isEntityNotMatched
        ? {
            label: "定位主体与方向",
            focusFieldKey: directMeta.formKey,
            emptyFieldHint: ENTITY_NOT_MATCHED_GUIDANCE,
          }
        : {
            label: "定位字段",
            focusFieldKey: directMeta.formKey,
          };
    const helperNote = isEntityNotMatched
      ? ENTITY_NOT_MATCHED_GUIDANCE
      : isContextInferredSoft
        ? CONTEXT_INFERRED_WARNING_GUIDANCE
        : null;
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED") {
      const parsedExpected = directMeta.parseExpected(alert.expected);
      if (parsedExpected !== null) {
        let nextFormVal = parsedExpected;
        let isSameAsCurrent = false;
        let containerAlreadyResolved = false;

        if (directMeta.patchKey === "container_nos") {
          const containerRes = applyContainerSuggestion(
            formValues.container_nos_text,
            alert.actual,
            parsedExpected
          );
          nextFormVal = containerRes.nextValue;
          containerAlreadyResolved = containerRes.alreadyResolved;
          isSameAsCurrent = containerRes.alreadyResolved;
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
            title: `将填入建议值: ${nextFormVal}`,
            focusFieldKey: directMeta.formKey,
            updateField: {
              formKey: directMeta.formKey,
              value: nextFormVal,
            },
            confirmFields: isContextInferredSoft ? [] : [directMeta.patchKey],
            feedbackMessage: isContextInferredSoft
              ? `已将 ${directMeta.label} 填入推断建议值（${nextFormVal}）；${CONTEXT_INFERRED_WARNING_GUIDANCE}`
              : `已将 ${directMeta.label} 填入建议值（${nextFormVal}），请点击「保存修订并重验」`,
          });
        } else if (isContextInferredSoft) {
          actionButtons.push({
            kind: "confirm_current",
            label: "查看批准理由",
            title: CONTEXT_INFERRED_WARNING_GUIDANCE,
            focusFieldKey: directMeta.formKey,
            confirmFields: [],
            feedbackMessage: CONTEXT_INFERRED_WARNING_GUIDANCE,
          });
        } else if (
          directMeta.patchKey === "container_nos" &&
          containerAlreadyResolved
        ) {
          const currTokens = dedupeContainerTokens(
            parseContainerNosInput(formValues.container_nos_text)
          );
          const currNorm = currTokens.join(", ");
          const exactSingleMatch =
            currTokens.length === 1 && currNorm === parsedExpected;
          actionButtons.push({
            kind: "confirm_current",
            label: exactSingleMatch ? "保留当前原文（待保存）" : "保留当前箱号（待保存）",
            title: exactSingleMatch
              ? `确认保留当前原文值: ${currNorm}`
              : `目标箱号已手工修正，保留现有列表: ${currNorm}`,
            focusFieldKey: directMeta.formKey,
            confirmFields: ["container_nos"],
            feedbackMessage: exactSingleMatch
              ? `已标记保留当前原文值（${currNorm}），请点击底部「保存修订并重验」完成校验`
              : `目标箱号已手工修正，保留现有列表（${currNorm}），请点击底部「保存修订并重验」完成校验`,
          });
        } else {
          actionButtons.push({
            kind: "confirm_current",
            label:
              directMeta.patchKey === "issuer" ||
              directMeta.patchKey === "recipient"
                ? "保留公司名（待保存）"
                : "保留当前原文（待保存）",
            title: `确认保留当前原文值: ${parsedExpected}`,
            focusFieldKey: directMeta.formKey,
            confirmFields: [directMeta.patchKey],
            feedbackMessage: `已标记保留当前原文值（${parsedExpected}），请点击底部「保存修订并重验」完成校验`,
          });
        }
      } else if (
        alert.code === "CROSS_DOCUMENT_CONFLICT" &&
        directMeta.isCurrentValid(formValues)
      ) {
        const currentText = String(formValues[directMeta.formKey]).trim();
        actionButtons.push({
          kind: "confirm_current",
          label:
            directMeta.patchKey === "issuer" ||
            directMeta.patchKey === "recipient"
              ? "保留公司名（待保存）"
              : "保留当前原文（待保存）",
          title: `确认保留当前原文值: ${currentText}`,
          focusFieldKey: directMeta.formKey,
          confirmFields: [directMeta.patchKey],
          feedbackMessage: `已标记保留当前原文值（${currentText}），请点击底部「保存修订并重验」完成校验`,
        });
      }
    }

    return { locateButton, helperNote, actionButtons };
  }

  // 2. Indirect party evidence fields: payee_hint / payer_hint (F4: NEVER overwrite issuer/recipient with payee/payer hint)
  if (rawField === "payee_hint" || rawField === "payer_hint") {
    const targetField: "issuer" | "recipient" =
      rawField === "payee_hint" ? "issuer" : "recipient";
    const targetLabel =
      rawField === "payee_hint" ? "签发方" : "接收方";
    const hintRoleLabel =
      rawField === "payee_hint" ? "收款方线索" : "付款方线索";
    const confirmButtonLabel =
      rawField === "payee_hint"
        ? "保留签发方公司名（待保存）"
        : "保留接收方公司名（待保存）";
    const helperNote =
      rawField === "payee_hint"
        ? "核对指引：收款方线索仅关联「签发方」与「记账方向」，不会自动改写公司名；请对照原件核对签发方、接收方或明确选择应收/应付方向后保存重验。"
        : "核对指引：付款方线索仅关联「接收方」与「记账方向」，不会自动改写公司名；请对照原件核对接收方、签发方或明确选择应收/应付方向后保存重验。";

    const currentParty = formValues[targetField].trim();
    const expectedParty = parseFillableTextValue(alert.expected);
    const partyAlreadyMatchesExpected =
      expectedParty !== null &&
      currentParty.length > 0 &&
      normalizePartyNameForComparison(currentParty) ===
        normalizePartyNameForComparison(expectedParty);

    const locateFocusKey =
      !currentParty || partyAlreadyMatchesExpected
        ? targetField
        : !formValues.posting_type
          ? "posting_type"
          : targetField;

    const locateButton: ResolvedRecordAlertActions["locateButton"] = {
      label: "定位主体与方向",
      focusFieldKey: locateFocusKey,
      emptyFieldHint: !currentParty
        ? `请对照单据原文填写${targetLabel}（切勿将收付款线索直接混填为开票/接收公司），并核对 AR/AP 方向后点击「保存修订并重验」`
        : !partyAlreadyMatchesExpected
          ? `当前${targetLabel}与${hintRoleLabel}不同，请对照单据原文分别核对签发方、接收方及 AR/AP 方向后点击「保存修订并重验」`
          : undefined,
    };
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED" && partyAlreadyMatchesExpected) {
      actionButtons.push({
        kind: "confirm_current",
        label: confirmButtonLabel,
        title: `确认保留当前${targetLabel}: ${currentParty}`,
        focusFieldKey: targetField,
        confirmFields: [targetField],
        feedbackMessage: `已标记保留当前${targetLabel}（${currentParty}），请点击底部「保存修订并重验」完成校验`,
      });
    }

    return { locateButton, helperNote, actionButtons };
  }

  // 3. Indirect charge evidence field: reported_charge_amount (F2: NEVER prefill row `amount` from document-level `expected`)
  if (rawField === "reported_charge_amount") {
    const helperNote =
      alert.code === "AMOUNT_TOTAL_MISMATCH"
        ? "关联字段说明：此项为单据级费用差异核对提示（不会自动改写当前行金额），请人工对照原件核对「金额」与「标准费用名称」后再确认或修改。"
        : "关联字段说明：对应可编辑字段「金额」与「标准费用名称」，请人工核对原件后再确认保存。";

    const currAmt = formValues.amount.trim();
    const currCharge = formValues.charge_item.trim();
    const hasValidAmt = parseFillableDecimalValue(currAmt) !== null;
    const hasValidCharge = currCharge.length > 0;

    const locateButton: ResolvedRecordAlertActions["locateButton"] = {
      label: "定位金额字段",
      focusFieldKey: "amount",
      emptyFieldHint:
        !hasValidCharge || !hasValidAmt
          ? "请先补齐标准费用名称与合法金额，再点击「保存修订并重验」"
          : undefined,
    };
    const actionButtons: AlertActionButtonSpec[] = [];

    if (alert.severity !== "RESOLVED") {
      actionButtons.push({
        kind: "confirm_current",
        label: "确认当前金额与费用",
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
        feedbackMessage: `已标记确认当前金额（${currAmt}）与费用项（${currCharge}），请点击底部「保存修订并重验」完成校验`,
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
        "关联字段说明：此项涉及所属单据汇总金额，可在下方「核对单据合计」区核对小计、税额与总计。",
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

export type RecordFilterTab =
  | "all"
  | "pending_review"
  | "create"
  | "update"
  | "no_change"
  | "rejected";

export interface ScopedRecordCounts {
  all: number;
  pending_review: number;
  create: number;
  update: number;
  no_change: number;
  rejected: number;
  exportable_official: number;
}

export interface RecordScopeFilterArgs {
  activeDocFilterId: string | null;
  filterTab?: RecordFilterTab;
  searchQuery?: string;
  docFilenameById?: ReadonlyMap<string, string>;
}

export function isRecordOfficialExportable(rec: BillingRecordDetail): boolean {
  return (
    (rec.review_status === "AUTO_READY" || rec.review_status === "APPROVED") &&
    rec.operation !== "PENDING_REVIEW"
  );
}

export function matchesRecordSearchQuery(
  rec: BillingRecordDetail,
  searchQuery: string,
  docFilenameById?: ReadonlyMap<string, string>
): boolean {
  const q = searchQuery.trim().toLowerCase();
  if (!q) return true;
  const haystacks: Array<string | null | undefined> = [
    rec.document_info?.reference_no,
    rec.document_info?.document_type,
    rec.charge_details?.charge_item,
    rec.charge_details?.raw_charge_name,
    rec.transaction_party?.issuer,
    rec.transaction_party?.recipient,
    rec.matched_bill_ref,
    rec.shipment_refs?.hbl_no,
    rec.shipment_refs?.booking_no,
    docFilenameById?.get(rec.source_doc_id),
    ...(rec.shipment_refs?.mbl_nos ?? []).map((m) => m.value),
    ...(rec.shipment_refs?.container_nos ?? []),
  ];
  return haystacks.some((val) =>
    typeof val === "string" && val.toLowerCase().includes(q)
  );
}

export function filterRecordsByDocAndSearch(
  records: ReadonlyArray<BillingRecordDetail>,
  args: Pick<
    RecordScopeFilterArgs,
    "activeDocFilterId" | "searchQuery" | "docFilenameById"
  >
): BillingRecordDetail[] {
  const q = args.searchQuery ?? "";
  return records.filter((rec) => {
    if (args.activeDocFilterId && rec.source_doc_id !== args.activeDocFilterId) {
      return false;
    }
    return matchesRecordSearchQuery(rec, q, args.docFilenameById);
  });
}

export function computeScopedRecordCounts(
  records: ReadonlyArray<BillingRecordDetail>,
  args: Pick<
    RecordScopeFilterArgs,
    "activeDocFilterId" | "searchQuery" | "docFilenameById"
  >
): ScopedRecordCounts {
  const scoped = filterRecordsByDocAndSearch(records, args);
  const counts: ScopedRecordCounts = {
    all: scoped.length,
    pending_review: 0,
    create: 0,
    update: 0,
    no_change: 0,
    rejected: 0,
    exportable_official: 0,
  };
  for (const rec of scoped) {
    if (isRecordOfficialExportable(rec)) {
      counts.exportable_official += 1;
    }
    if (rec.review_status === "REJECTED") {
      counts.rejected += 1;
      continue;
    }
    if (
      rec.operation === "PENDING_REVIEW" ||
      rec.review_status === "NEEDS_REVIEW"
    ) {
      counts.pending_review += 1;
    } else if (rec.operation === "CREATE") {
      counts.create += 1;
    } else if (rec.operation === "UPDATE") {
      counts.update += 1;
    } else if (rec.operation === "NO_CHANGE") {
      counts.no_change += 1;
    }
  }
  return counts;
}

export function filterBatchRecords(
  records: ReadonlyArray<BillingRecordDetail>,
  args: RecordScopeFilterArgs
): BillingRecordDetail[] {
  const scoped = filterRecordsByDocAndSearch(records, args);
  const tab = args.filterTab ?? "all";
  if (tab === "all") return scoped;
  return scoped.filter((rec) => {
    if (tab === "rejected") return rec.review_status === "REJECTED";
    if (rec.review_status === "REJECTED") return false;
    if (tab === "pending_review") {
      return (
        rec.operation === "PENDING_REVIEW" ||
        rec.review_status === "NEEDS_REVIEW"
      );
    }
    if (tab === "create") return rec.operation === "CREATE";
    if (tab === "update") return rec.operation === "UPDATE";
    if (tab === "no_change") return rec.operation === "NO_CHANGE";
    return true;
  });
}

export function syncSelectedRecordId(
  currentSelectedId: string | null,
  visibleRecords: ReadonlyArray<BillingRecordDetail>
): string | null {
  if (visibleRecords.length === 0) {
    return null;
  }
  if (
    currentSelectedId &&
    visibleRecords.some((rec) => rec.id === currentSelectedId)
  ) {
    return currentSelectedId;
  }
  return visibleRecords[0].id;
}

const ALERT_SEVERITY_ORDER: Record<ValidationAlert["severity"], number> = {
  BLOCKER: 0,
  WARNING: 1,
  RESOLVED: 2,
};

export function sortValidationAlerts(
  alerts: ReadonlyArray<ValidationAlert> | undefined
): ValidationAlert[] {
  if (!alerts || alerts.length === 0) return [];
  return [...alerts].sort((a, b) => {
    const rankA = ALERT_SEVERITY_ORDER[a.severity] ?? 3;
    const rankB = ALERT_SEVERITY_ORDER[b.severity] ?? 3;
    return rankA - rankB;
  });
}

export interface ExcludedClauseItemSummary {
  text: string;
  count: number;
  pages: number[];
}

export interface ExcludedClauseGroup {
  reason: string;
  totalCount: number;
  items: ExcludedClauseItemSummary[];
}

const STANDARD_EXCLUDED_CLAUSE_REASONS = new Set([
  "条件收费或违约条款",
  "银行汇款信息",
  "免收费说明",
]);

export function normalizeExcludedClauseReason(
  rawReason: string | null | undefined
): string {
  const trimmed = (rawReason ?? "").trim();
  if (!trimmed) return "其他非计费说明";
  const lower = trimmed.toLowerCase();
  if (
    /(?:conditional|penalty|third[-\s]?party|misuse|wait\s*time|chassis)|条件收费|违约/.test(
      lower
    )
  ) {
    return "条件收费或违约条款";
  }
  if (
    /(?:remit|bank|wire|\bach\b|routing|beneficiary|payment[\s_-]*instruction|\bcheck\b)|汇款|银行/.test(
      lower
    )
  ) {
    return "银行汇款信息";
  }
  if (/(?:free|waiv|exempt|zero|no[\s_-]*charge)|免收|免费/.test(lower)) {
    return "免收费说明";
  }
  if (/\bclause\b/.test(lower)) {
    return "条件收费或违约条款";
  }
  if (
    /(?:non[\s_-]*billing|non[\s_-]*billable|informational|disclaimer|notice|\bother\b|\bmisc\b)/.test(
      lower
    ) ||
    /^[a-z0-9\s/_.,:-]+$/i.test(trimmed)
  ) {
    return "其他非计费说明";
  }
  return trimmed;
}

const normClauseSubKey = (text: string): string =>
  text
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

export function groupExcludedClauses(
  clauses:
    | ReadonlyArray<{ page?: number | null; text: string; reason: string }>
    | undefined
): ExcludedClauseGroup[] {
  if (!clauses || clauses.length === 0) return [];
  interface ClauseDedupEntry {
    order: number;
    text: string;
    normSub: string;
    reason: string;
    hasChineseReason: boolean;
    pages: Set<number>;
  }
  const globalByNormKey = new Map<string, ClauseDedupEntry>();
  let nextOrder = 0;

  for (const clause of clauses) {
    const text = clause.text?.trim() ?? "";
    if (!text) continue;
    const normKey = text.replace(/\s+/g, " ").toLowerCase();
    const rawReasonTrimmed = (clause.reason ?? "").trim();
    const normalizedReason = normalizeExcludedClauseReason(rawReasonTrimmed);
    const isStandardOrChinese =
      STANDARD_EXCLUDED_CLAUSE_REASONS.has(normalizedReason) ||
      /[\u4e00-\u9fff]/.test(rawReasonTrimmed);

    let entry = globalByNormKey.get(normKey);
    if (!entry) {
      entry = {
        order: nextOrder++,
        text,
        normSub: normClauseSubKey(text),
        reason: normalizedReason,
        hasChineseReason: isStandardOrChinese,
        pages: new Set<number>(),
      };
      globalByNormKey.set(normKey, entry);
    } else if (!entry.hasChineseReason && isStandardOrChinese) {
      entry.reason = normalizedReason;
      entry.hasChineseReason = true;
    }

    if (typeof clause.page === "number" && Number.isFinite(clause.page)) {
      entry.pages.add(clause.page);
    }
  }

  const entriesByReason = new Map<string, ClauseDedupEntry[]>();
  for (const entry of globalByNormKey.values()) {
    let list = entriesByReason.get(entry.reason);
    if (!list) {
      list = [];
      entriesByReason.set(entry.reason, list);
    }
    list.push(entry);
  }

  const groups: ExcludedClauseGroup[] = [];
  for (const [reason, entries] of entriesByReason.entries()) {
    // Subsumption: fold redundant shorter snippet inside the same reason when a longer clause already covers it on the same pages.
    const byLengthDesc = [...entries].sort(
      (a, b) => b.normSub.length - a.normSub.length || a.order - b.order
    );
    const kept: ClauseDedupEntry[] = [];
    for (const candidate of byLengthDesc) {
      const candidatePages = Array.from(candidate.pages);
      const subsumer =
        candidate.normSub.length >= 12
          ? kept.find((longer) => {
              if (!longer.normSub.includes(candidate.normSub)) return false;
              return (
                candidatePages.length === 0 ||
                longer.pages.size === 0 ||
                candidatePages.every((p) => longer.pages.has(p))
              );
            })
          : undefined;
      if (subsumer) {
        if (subsumer.pages.size === 0 && candidate.pages.size > 0) {
          for (const p of candidate.pages) {
            subsumer.pages.add(p);
          }
        }
        continue;
      }
      kept.push(candidate);
    }

    kept.sort((a, b) => a.order - b.order);

    let totalCount = 0;
    const items: ExcludedClauseItemSummary[] = kept.map((entry) => {
      const count = entry.pages.size > 0 ? entry.pages.size : 1;
      const pages = Array.from(entry.pages).sort((a, b) => a - b);
      totalCount += count;
      return {
        text: entry.text,
        count,
        pages,
      };
    });

    groups.push({ reason, totalCount, items });
  }

  return groups;
}

export interface RecordPartyDisplayMeta {
  postingType: "AR" | "AP" | null;
  directionLabel: string;
  issuerLabel: string;
  recipientLabel: string;
  isPayerMissing: boolean;
  isPayeeMissing: boolean;
  missingPartyHint: string | null;
  flowSummary: string;
}

const normParty = (value: string): string =>
  value.trim().replace(/\s+/g, " ").toLowerCase();

export function getRecordPartyDisplayMeta(
  record: Pick<BillingRecordDetail, "transaction_party" | "financial_attributes">
): RecordPartyDisplayMeta {
  const postingType = record.financial_attributes?.posting_type ?? null;
  const accountingEntity = (
    record.financial_attributes?.accounting_entity ?? ""
  ).trim();
  const issuer = (record.transaction_party?.issuer ?? "").trim();
  const recipient = (record.transaction_party?.recipient ?? "").trim();
  const isPayerMissing = postingType === "AR" && recipient.length === 0;
  const isPayeeMissing = postingType === "AP" && issuer.length === 0;

  const directionLabel =
    postingType === "AR"
      ? "我司应收 (AR)"
      : postingType === "AP"
        ? "我司应付 (AP)"
        : "方向待定";

  const issuerLabel =
    issuer || (isPayeeMissing ? "收款方待确认" : "未识别签发方");
  const recipientLabel =
    recipient || (isPayerMissing ? "付款方待确认" : "未识别接收方");

  const missingPartyHint = isPayerMissing
    ? "付款方待确认"
    : isPayeeMissing
      ? "收款方待确认"
      : null;

  let flowSummary: string;
  if (postingType === "AR") {
    const payerSide = isPayerMissing ? "付款方待确认" : recipientLabel;
    const sameIssuer =
      Boolean(accountingEntity && issuer) &&
      normParty(issuer) === normParty(accountingEntity);
    if (accountingEntity && !sameIssuer) {
      flowSummary = `${payerSide} → ${accountingEntity}（款付给我方${issuer ? ` · 签发：${issuerLabel}` : ""}）`;
    } else {
      flowSummary = `${payerSide} → ${issuerLabel}（款付给我方）`;
    }
  } else if (postingType === "AP") {
    const payeeSide = isPayeeMissing ? "收款方待确认" : issuerLabel;
    const sameRecipient =
      Boolean(accountingEntity && recipient) &&
      normParty(recipient) === normParty(accountingEntity);
    if (accountingEntity && !sameRecipient) {
      flowSummary = `${accountingEntity} → ${payeeSide}（我方需付款${recipient ? ` · 原件抬头：${recipient}` : ""}）`;
    } else {
      flowSummary = `${recipientLabel} → ${payeeSide}（我方需付款）`;
    }
  } else {
    flowSummary = `${issuerLabel} → ${recipientLabel}`;
  }

  return {
    postingType,
    directionLabel,
    issuerLabel,
    recipientLabel,
    isPayerMissing,
    isPayeeMissing,
    missingPartyHint,
    flowSummary,
  };
}

const STALE_UNMATCHED_REASONING_PATTERN =
  /无法唯一匹配|当前记账主体无法匹配|记账主体无法唯一匹配|尚未确定.*(?:应收|应付|AR|AP)/;

export function splitRecordAiReasoning(
  record: Pick<
    BillingRecordDetail,
    "ai_reasoning" | "financial_attributes" | "validation_alerts"
  >
): { activeReasons: string[]; archivedReasons: string[] } {
  const raw = record.ai_reasoning ?? [];
  const posting = record.financial_attributes?.posting_type ?? null;
  const hasActiveEntityBlocker = (record.validation_alerts ?? []).some(
    (a) => a.code === "ENTITY_NOT_MATCHED" && a.severity !== "RESOLVED"
  );
  if ((posting !== "AR" && posting !== "AP") || hasActiveEntityBlocker) {
    return { activeReasons: raw, archivedReasons: [] };
  }
  const activeReasons: string[] = [];
  const archivedReasons: string[] = [];
  for (const line of raw) {
    if (STALE_UNMATCHED_REASONING_PATTERN.test(line)) {
      archivedReasons.push(line);
    } else {
      activeReasons.push(line);
    }
  }
  return { activeReasons, archivedReasons };
}

const FRIENDLY_FIELD_LABELS: Readonly<Record<string, string>> = {
  charge_item: "标准费用名称",
  raw_charge_name: "原件费用原文",
  quantity: "数量",
  unit_price: "单价",
  amount: "金额",
  currency: "币种",
  payment_term: "付款条款",
  posting_type: "记账方向",
  debit_credit: "借贷属性",
  document_type: "单据类型",
  reference_no: "单据参考号",
  document_date: "单据日期",
  issuer: "签发方",
  recipient: "接收方",
  mbl_no: "主提单号",
  mbl_nos: "主提单号",
  hbl_no: "分提单号",
  booking_no: "订舱号",
  container_nos: "箱号列表",
  payee_hint: "收款方线索",
  payer_hint: "付款方线索",
  reported_charge_amount: "源单费用与金额",
  accounting_entity: "记账公司",
  transaction_party: "交易双方",
  total_amount: "单据总计",
  subtotal_amount: "未税小计",
  tax_amount: "税额",
  total_currency: "单据汇总币种",
};

export function getFieldFriendlyLabel(rawField: string | null | undefined): string {
  if (!rawField) return "";
  const trimmed = rawField.trim();
  return FRIENDLY_FIELD_LABELS[trimmed] ?? trimmed;
}

const FRIENDLY_ALERT_TITLES: Readonly<Record<string, string>> = {
  ENTITY_NOT_MATCHED: "记账公司与收付方向待确认",
  CROSS_DOCUMENT_CONFLICT: "跨单据信息存在差异",
  AMOUNT_TOTAL_MISMATCH: "金额与单据合计不一致",
  AMOUNT_SUBTOTAL_TAX_MISMATCH: "小计与税额加总不平",
  MULTI_CURRENCY_TOTAL_AMBIGUITY: "多币种汇总需分币种核对",
  LINE_ARITHMETIC_MISMATCH: "数量 × 单价与金额不符",
  NEGATIVE_NON_CREDIT_AMOUNT: "非贷项凭证出现负数金额",
  INVALID_OR_MISSING_CURRENCY: "币种缺失或格式无效",
  INVALID_DOCUMENT_DATE: "单据日期缺失或无效",
  OCR_NUMERIC_CONFUSION: "识别数字疑有混淆请复核",
  AMBIGUOUS_EXISTING_BILL: "命中多笔历史账单待指定",
  DUPLICATE_LINE_EXTRACTED: "疑似重复费用行",
  LLM_EXTRACTION_FAILED: "模型解析未通过校验",
  CONTAINER_ISO6346_MISMATCH: "箱号校验位异常",
  OCR_SUSPICIOUS_FLAG: "识别字符存疑请核对",
  MULTI_SOURCE_BL_MISMATCH: "多附件提单号存在差异",
  CONTEXT_INFERRED_FIELD: "同批上下文自动补全",
};

export function getAlertFriendlyTitle(alert: Pick<ValidationAlert, "code" | "field">): string {
  const base = FRIENDLY_ALERT_TITLES[alert.code] ?? "账单核对提醒";
  const fieldLabel = getFieldFriendlyLabel(alert.field);
  return fieldLabel ? `${base} · ${fieldLabel}` : base;
}

export function formatOcrChannelLabel(channel: string | null | undefined): string {
  if (!channel) return "未记录识别方式";
  const upper = channel.trim().toUpperCase();
  switch (upper) {
    case "LOCAL_PDF_TEXT_FALLBACK":
      return "本地 PDF 文本提取（已自动回退）";
    case "LOCAL_TEXT":
      return "本地文本解析";
    case "LOCAL_RAPIDOCR_FALLBACK":
      return "本地图片识别（已自动回退）";
    case "EML_TEXT_PARSER":
      return "邮件正文解析";
    case "REMOTE_OCR":
    case "PLTPLT_OCR":
      return "远端 OCR 识别";
    case "SKIPPED_SIGNATURE":
      return "已跳过签名图片";
    case "EXCEL_PARSER":
      return "Excel 表格解析";
    case "WORD_PARSER":
      return "Word 文档解析";
    case "MANUAL":
      return "人工补录";
    default:
      return channel;
  }
}
