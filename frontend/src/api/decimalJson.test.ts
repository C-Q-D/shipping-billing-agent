import {
  ApiClientError,
  BillingApiClient,
  MAX_SINGLE_FILE_BYTES,
  isValidIso4217Currency,
  normalizeBatchDetail,
  validateBatchIngestionInput,
} from "./client.ts";
import {
  DecimalSyntaxError,
  addDecimalStrings,
  compareDecimalStrings,
  formatLosslessAmount,
  isWithinDecimalTolerance,
  multiplyDecimalStrings,
  parseLosslessJson,
  rawDecimal,
  readDecimalString,
  stringifyLosslessJson,
  subtractDecimalStrings,
} from "./decimalJson.ts";
import { createDevStubTransport } from "./devStubTransport.ts";
import { ACTIVE_ISO4217_CODES, ACTIVE_ISO4217_CURRENCIES } from "./iso4217.ts";
import {
  NO_EVAL_RESULT_LABEL,
  beginExistingBillsLoad,
  buildEvalRunViewModel,
  completeExistingBillsLoadFailure,
  completeExistingBillsLoadSuccess,
  completeExportModalLoadFailure,
  completeExportModalLoadSuccess,
  createExistingBillsViewState,
  createExportModalCacheState,
  formatEvalMetricValue,
  getExistingBillsHeaderStatus,
  getExportModalActionEligibility,
  getExportResponseForBatchMode,
  parseExistingBillsImportInput,
  resetExportModalForBatch,
  setExistingBillsFilter,
  validateAccountingEntityInput,
  validateChargeDictionaryInput,
  validateReviewBundleInput,
} from "./managementHelpers.ts";
import {
  acceptPollResult,
  beginDocTotalsSave,
  beginRecordSave,
  buildDocTotalsDiffPatch,
  buildRecordDiffPatch,
  clearBatchDrafts,
  clearBatchScopedMap,
  clearRecordDraft,
  countCodePoints,
  createEmptyDraftStore,
  createPollRaceTracker,
  extractMutationErrorBanner,
  extractServerRecordFormValues,
  finishDocTotalsSaveError,
  finishDocTotalsSaveSuccess,
  finishMutationError,
  finishMutationSuccess,
  finishRecordSaveError,
  finishRecordSaveSuccess,
  getEffectiveDocTotalsValues,
  getEffectiveRecordFormValues,
  getRecordDraftVersion,
  hasDocTotalsDraft,
  hasRecordDraft,
  setDocTotalsDraftField,
  setRecordDraftField,
  shouldContinuePollingAfterDiscard,
  startMutation,
  startPoll,
  validateReviewReason,
} from "./reviewState.ts";

function collectLowerHeaders(
  headersInit: RequestInit["headers"]
): Record<string, string> {
  const out: Record<string, string> = {};
  if (!headersInit) return out;
  if (headersInit instanceof Headers) {
    headersInit.forEach((v, k) => {
      out[k.toLowerCase()] = v;
    });
  } else if (Array.isArray(headersInit)) {
    for (const [k, v] of headersInit) {
      out[k.toLowerCase()] = v;
    }
  } else {
    for (const [k, v] of Object.entries(headersInit)) {
      if (v !== undefined) out[k.toLowerCase()] = String(v);
    }
  }
  return out;
}

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (!Object.is(actual, expected)) {
    throw new Error(
      `${message} | expected: ${String(expected)}, actual: ${String(actual)}`
    );
  }
}

function assertTrue(condition: boolean, message: string): void {
  if (!condition) {
    throw new Error(`Assertion failed: ${message}`);
  }
}

function assertThrowsSync(fn: () => unknown, label: string): Error {
  try {
    fn();
  } catch (err) {
    if (err instanceof Error) {
      return err;
    }
    throw new Error(`${label} threw non-Error value`);
  }
  throw new Error(`${label} expected to throw, but succeeded`);
}

async function assertRejectsAsync(
  promise: Promise<unknown>,
  label: string
): Promise<unknown> {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  throw new Error(`${label} expected to reject, but resolved`);
}

export async function runDecimalJsonTests(): Promise<void> {
  const results: string[] = [];

  // 1. Big amount & tiny fractional lossless round-trip (9007199254740991.01 & 0.0025)
  const rawPayloadText =
    '{"id":"rec-1","charge_details":{"amount":9007199254740991.01,"unit_price":0.0025,"quantity":1,"credit":-40.00},"note":"USD 9007199254740991.01"}';

  assertEqual(
    Number("9007199254740991.01"),
    9007199254740991,
    "sanity check: native JS Number truncates 9007199254740991.01"
  );

  const parsed = parseLosslessJson<{
    id: string;
    charge_details: {
      amount: number;
      unit_price: number;
      quantity: number;
      credit: number;
    };
    note: string;
  }>(rawPayloadText);

  assertEqual(
    readDecimalString(parsed.charge_details, "amount"),
    "9007199254740991.01",
    "readDecimalString preserves 9007199254740991.01"
  );
  assertEqual(
    readDecimalString(parsed.charge_details, "unit_price"),
    "0.0025",
    "readDecimalString preserves 0.0025"
  );
  assertEqual(
    readDecimalString(parsed.charge_details, "credit"),
    "-40.00",
    "readDecimalString preserves trailing zeros -40.00"
  );
  assertEqual(
    formatLosslessAmount(parsed.charge_details, "amount"),
    "9007199254740991.01",
    "formatLosslessAmount keeps 9007199254740991.01 exact"
  );
  assertEqual(
    formatLosslessAmount(parsed.charge_details, "unit_price"),
    "0.0025",
    "formatLosslessAmount keeps 0.0025 4 decimal places"
  );
  assertEqual(
    formatLosslessAmount(parsed.charge_details, "quantity"),
    "1.00",
    "formatLosslessAmount pads integer 1 to 1.00"
  );
  assertEqual(
    stringifyLosslessJson(parsed),
    rawPayloadText,
    "stringifyLosslessJson(parseLosslessJson(raw)) preserves exact unquoted literals"
  );
  results.push("PASS 1: 9007199254740991.01 & 0.0025 lossless round-trip");

  // 2. RawDecimalLiteral user form editing -> unquoted JSON number serialization
  const patchBody = {
    amount: rawDecimal("009007199254740991.01"),
    unit_price: rawDecimal("+0.0025"),
    quantity: rawDecimal("-0"),
    nested_array: [rawDecimal("9007199254740991.09"), rawDecimal("0.0025")],
  };
  const encodedPatch = stringifyLosslessJson(patchBody);
  assertEqual(
    encodedPatch,
    '{"amount":9007199254740991.01,"unit_price":0.0025,"quantity":0,"nested_array":[9007199254740991.09,0.0025]}',
    "RawDecimalLiteral serializes unquoted JSON numbers with zero precision loss"
  );
  results.push("PASS 2: RawDecimalLiteral unquoted JSON number encoding");

  // 3. String escaping, unicode & HTML tags preserved untouched
  const textWithEscapes =
    '{"snippet":"<script>alert(\\"xss\\")</script> \\u822a\\u8fd0 9007199254740991.01","vals":[9007199254740991.01,0.0025]}';
  const parsedEsc = parseLosslessJson<{ snippet: string; vals: number[] }>(
    textWithEscapes
  );
  assertEqual(
    parsedEsc.snippet,
    '<script>alert("xss")</script> 航运 9007199254740991.01',
    "escaped quotes and unicode decoded cleanly"
  );
  assertEqual(
    readDecimalString(parsedEsc.vals, 0),
    "9007199254740991.01",
    "array element 0 preserves 9007199254740991.01"
  );
  assertEqual(
    readDecimalString(parsedEsc.vals, 1),
    "0.0025",
    "array element 1 preserves 0.0025"
  );
  results.push("PASS 3: JSON string escapes & array number preservation");

  // 4. Reject invalid decimal strings and malformed JSON
  for (const invalid of ["", "  ", "12.3.4", "1O0.00", "NaN", "Infinity", ".5"]) {
    const err = assertThrowsSync(
      () => rawDecimal(invalid),
      `rawDecimal("${invalid}")`
    );
    assertTrue(
      err instanceof DecimalSyntaxError,
      `expected DecimalSyntaxError for "${invalid}"`
    );
  }
  assertThrowsSync(() => parseLosslessJson("{bad}"), "malformed JSON");
  assertThrowsSync(() => parseLosslessJson('{"a":01}'), "leading zero JSON");
  assertThrowsSync(() => parseLosslessJson('{"a":1} trailing'), "trailing token");
  results.push("PASS 4: Invalid decimals & malformed JSON rejected");

  // 5. Client-side pre-upload validation (0 byte, >50 MiB, >50 items, bad URL)
  const badValidation = validateBatchIngestionInput({
    accountingEntity: "NEPTUNE SHIPPING LIMITED",
    files: [
      { name: "empty.pdf", size: 0 },
      { name: "huge.pdf", size: MAX_SINGLE_FILE_BYTES + 1 },
      { name: "script.exe", size: 100 },
    ],
    publicUrlLinesText: "ftp://invalid.example.com/a.pdf\nhttps://example.com/ok.pdf",
  });
  assertEqual(badValidation.valid, false, "invalid ingestion blocked");
  assertEqual(
    Object.keys(badValidation.fileErrors).length,
    3,
    "all 3 bad files flagged"
  );
  assertEqual(badValidation.urlErrors.length, 1, "ftp url flagged");

  const overCountFiles = Array.from({ length: 50 }, (_, i) => ({
    name: `doc-${i}.pdf`,
    size: 1024,
  }));
  const overLimitValidation = validateBatchIngestionInput({
    accountingEntity: "NEPTUNE SHIPPING LIMITED",
    files: overCountFiles,
    publicUrlLinesText: "https://example.com/51st.pdf",
  });
  assertEqual(overLimitValidation.valid, false, "51 items exceeds 50 limit");
  assertTrue(
    overLimitValidation.summaryErrors.some((msg) => msg.includes("50")),
    "summary error mentions 50 limit"
  );

  const cleanValidation = validateBatchIngestionInput({
    accountingEntity: "NEPTUNE SHIPPING LIMITED",
    files: [{ name: "valid.pdf", size: 1200 }],
    publicUrlLinesText: "https://example.com/ok.pdf",
  });
  assertEqual(cleanValidation.valid, true, "removing bad items passes validation");
  results.push("PASS 5: Pre-upload 0-byte / >50 MiB / >50 items / URL validation");

  // 6. BillingApiClient + devStubTransport integration contract checks
  const capturedCalls: Array<{
    url: string;
    method: string;
    bodyText?: string;
    headers: Record<string, string>;
  }> = [];
  const stubTransport = createDevStubTransport();
  const client = new BillingApiClient({
    transport: async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      capturedCalls.push({
        url,
        method: init?.method ?? "GET",
        bodyText: typeof init?.body === "string" ? init.body : undefined,
        headers: collectLowerHeaders(init?.headers),
      });
      return stubTransport(input, init);
    },
  });

  const demoQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  assertEqual(demoQueued.stage, "QUEUED", "wait=false returns QUEUED 202");
  const demoCall = capturedCalls[capturedCalls.length - 1];
  assertTrue(
    demoCall.url.includes("wait=false"),
    "createDemoBatch explicitly sends wait=false"
  );
  assertTrue(
    (demoCall.bodyText ?? "").includes('"ocr_mode":"REMOTE_ONLY"'),
    "createDemoBatch explicitly sends ocr_mode REMOTE_ONLY"
  );

  let detail = await client.getBatchDetail(demoQueued.id);
  const observedStages: string[] = [detail.stage];
  while (detail.stage !== "COMPLETED" && detail.stage !== "FAILED") {
    detail = await client.getBatchDetail(demoQueued.id);
    observedStages.push(detail.stage);
  }
  assertTrue(
    observedStages.includes("EXTRACTING") &&
      observedStages.includes("COMPLETED"),
    `polling observed EXTRACTING -> COMPLETED (${observedStages.join(" -> ")})`
  );

  assertTrue(
    detail.failed_documents.some(
      (f) => f.affected_record_ids.length === 0 && f.filename.includes("scan")
    ),
    "stub includes zero-record failed attachment card"
  );
  assertTrue(
    detail.records.some(
      (r) => r.document_info.reference_no === "DN-202609-001"
    ),
    "stub includes DN-202609-001"
  );

  const highPrecRecord = detail.records.find(
    (r) => r.document_info.reference_no === "SOA-MEGA-9007"
  );
  assertTrue(highPrecRecord !== undefined, "high precision record found");
  assertEqual(
    readDecimalString(highPrecRecord!.charge_details, "amount"),
    "9007199254740991.01",
    "stub response keeps 9007199254740991.01 lossless"
  );
  assertEqual(
    readDecimalString(highPrecRecord!.charge_details, "unit_price"),
    "0.0025",
    "stub response keeps 0.0025 lossless"
  );

  const rejectedErr = await assertRejectsAsync(
    client.approveBillingRecord(detail.id, highPrecRecord!.id, "尝试跳过红灯"),
    "approve BLOCKER record"
  );
  assertTrue(
    rejectedErr instanceof ApiClientError && rejectedErr.status === 422,
    "approving BLOCKER record returns 422 ApiClientError"
  );
  results.push(
    "PASS 6: BillingApiClient wait=false, polling stages, zero-record fail card, 9007199254740991.01/0.0025 & 422 BLOCKER"
  );

  // 7. BigInt exact decimal arithmetic without JS Number truncation
  assertEqual(
    addDecimalStrings("9007199254740991.01", "40.00"),
    "9007199254741031.01",
    "BigInt decimal addition avoids Number rounding"
  );
  assertEqual(
    subtractDecimalStrings("9007199254741031.01", "40.00"),
    "9007199254740991.01",
    "BigInt decimal subtraction preserves precision"
  );
  assertEqual(
    multiplyDecimalStrings("4", "0.0025"),
    "0.0100",
    "BigInt decimal multiplication preserves scale"
  );
  assertEqual(
    compareDecimalStrings("9007199254740991.01", "9007199254740991.00"),
    1,
    "compareDecimalStrings distinguishes 0.01 above MAX_SAFE_INTEGER"
  );
  assertTrue(
    isWithinDecimalTolerance("1410.41", "1410.40", "0.02"),
    "tolerance check passes within 0.02"
  );
  assertTrue(
    !isWithinDecimalTolerance("9007199254740991.01", "9007199254740991.04", "0.02"),
    "tolerance check fails when 0.03 apart above MAX_SAFE_INTEGER"
  );
  results.push("PASS 7: BigInt exact decimal comparison & arithmetic");

  // 8. Draft isolation across (batch_id, record_id) and (batch_id, doc_id)
  const recA = detail.records.find((r) => r.id === "rec-eml-hc")!;
  const recB = detail.records.find((r) => r.id === "rec-dn-update")!;
  const doc1 = detail.documents.find((d) => d.id === "doc-pdf-1")!;
  const doc2 = detail.documents.find((d) => d.id === "doc-pdf-2")!;

  let drafts = createEmptyDraftStore();
  drafts = setRecordDraftField(drafts, detail.id, recA, "amount", "9007199254740991.01");
  drafts = setRecordDraftField(drafts, detail.id, recA, "review_note", "A 草稿保留");
  drafts = setRecordDraftField(drafts, detail.id, recB, "issuer", "UPDATED AGENT B");
  drafts = setDocTotalsDraftField(drafts, detail.id, doc1, "total_currency", "EUR");
  drafts = setDocTotalsDraftField(drafts, detail.id, doc2, "total_amount", "999.99");

  const effA = getEffectiveRecordFormValues(recA, drafts, detail.id);
  const effB = getEffectiveRecordFormValues(recB, drafts, detail.id);
  assertEqual(effA.amount, "9007199254740991.01", "Record A draft retained after editing B");
  assertEqual(effA.review_note, "A 草稿保留", "Record A note isolated from B");
  assertEqual(effB.issuer, "UPDATED AGENT B", "Record B draft stored independently");
  assertEqual(effB.review_note, "", "Record A note did not leak into B");

  drafts = clearRecordDraft(drafts, detail.id, recB.id);
  assertTrue(!hasRecordDraft(drafts, detail.id, recB.id), "clearing B clears B only");
  assertTrue(hasRecordDraft(drafts, detail.id, recA.id), "clearing B keeps A intact");
  assertEqual(
    getEffectiveDocTotalsValues(doc1, drafts, detail.id).total_currency,
    "EUR",
    "doc 1 totals draft isolated from doc 2"
  );
  assertEqual(
    getEffectiveDocTotalsValues(doc2, drafts, detail.id).total_amount,
    "999.99",
    "doc 2 totals draft isolated from doc 1"
  );
  results.push("PASS 8: (batch_id, record_id) & (batch_id, doc_id) draft isolation");

  // 9. Poll Race Protection (stale poll returning after mutation must be discarded)
  let tracker = createPollRaceTracker(detail.id);
  const pollStartedBeforeSave = startPoll(tracker, detail.id);
  tracker = pollStartedBeforeSave.nextTracker;

  const mutStart = startMutation(tracker, detail.id);
  tracker = mutStart.nextTracker;

  const midFlightCheck = acceptPollResult(tracker, pollStartedBeforeSave.ticket);
  assertEqual(midFlightCheck.accepted, false, "poll ignored while mutation in-flight");

  const mutDone = finishMutationSuccess(tracker, mutStart.ticket);
  tracker = mutDone.nextTracker;
  assertEqual(mutDone.shouldApply, true, "mutation result applied");

  const lateArrivalCheck = acceptPollResult(tracker, pollStartedBeforeSave.ticket);
  assertEqual(
    lateArrivalCheck.accepted,
    false,
    "stale poll started before save is strictly rejected after save finishes"
  );

  const freshPollAfterSave = startPoll(tracker, detail.id);
  tracker = freshPollAfterSave.nextTracker;
  const freshArrivalCheck = acceptPollResult(tracker, freshPollAfterSave.ticket);
  assertEqual(
    freshArrivalCheck.accepted,
    true,
    "fresh poll started after save is accepted"
  );

  // Delayed-transport end-to-end poll race simulation
  let savedDetailAfterMut: typeof detail | null = null;
  let stateRecordsSnapshot: typeof detail.records = detail.records;
  const mutStartTransport = startMutation(tracker, detail.id);
  tracker = mutStartTransport.nextTracker;
  const stalePollTransport = startPoll(tracker, detail.id);
  tracker = stalePollTransport.nextTracker;

  // Stale poll runs slowly while mutation completes first
  let resolveSlowPoll!: (d: typeof detail) => void;
  const slowPollPromise = new Promise<typeof detail>((resolve) => {
    resolveSlowPoll = resolve;
  });
  const staleServerPayload = await client.getBatchDetail(detail.id);

  // Patch rec-eml-hc amount to 77.00 + doc-eml-1 total to 77.00
  const docEml = detail.documents.find((d) => d.id === "doc-eml-1")!;
  await client.patchDocumentTotals(detail.id, docEml.id, {
    total_amount: rawDecimal("77.00"),
  });
  savedDetailAfterMut = await client.patchBillingRecord(detail.id, recA.id, {
    amount: rawDecimal("77.00"),
  });
  const mutFinishTransport = finishMutationSuccess(tracker, mutStartTransport.ticket);
  tracker = mutFinishTransport.nextTracker;
  if (mutFinishTransport.shouldApply) {
    stateRecordsSnapshot = savedDetailAfterMut.records;
  }

  // Now the older poll finally arrives with stale 50.00
  resolveSlowPoll(staleServerPayload);
  const latePollDetail = await slowPollPromise;
  const lateCheck = acceptPollResult(tracker, stalePollTransport.ticket);
  tracker = lateCheck.nextTracker;
  if (lateCheck.accepted) {
    stateRecordsSnapshot = latePollDetail.records;
  }
  const latestRecA = stateRecordsSnapshot.find((r) => r.id === recA.id)!;
  assertEqual(
    readDecimalString(latestRecA.charge_details, "amount"),
    "77.00",
    "late poll (50.00) must not overwrite newly saved amount (77.00)"
  );
  results.push("PASS 9: Poll race protection drops stale polls after save");

  // 10. Diff-only PATCH + read-only raw_charge_name + 409 conflict + 9007199254740991.01/0.0025 lossless save->reload->export->download
  const freshBatchQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let batch2 = await client.getBatchDetail(freshBatchQueued.id);
  while (batch2.stage !== "COMPLETED") {
    batch2 = await client.getBatchDetail(freshBatchQueued.id);
  }

  const targetRec = batch2.records.find((r) => r.id === "rec-eml-hc")!;
  let editDrafts = createEmptyDraftStore();
  editDrafts = setRecordDraftField(
    editDrafts,
    batch2.id,
    targetRec,
    "amount",
    "9007199254740991.01"
  );
  editDrafts = setRecordDraftField(
    editDrafts,
    batch2.id,
    targetRec,
    "unit_price",
    "0.0025"
  );
  editDrafts = setRecordDraftField(
    editDrafts,
    batch2.id,
    targetRec,
    "issuer",
    "PRECISION LINE CARRIER"
  );
  // Set quantity to null so line arithmetic passes while preserving 0.0025 unit_price and 9007199254740991.01 amount
  editDrafts = setRecordDraftField(editDrafts, batch2.id, targetRec, "quantity", "");

  const diff = buildRecordDiffPatch(targetRec, editDrafts, batch2.id);
  assertEqual(diff.valid, true, "diff patch valid");
  assertEqual(diff.hasChanges, true, "diff patch detects changes");
  const patchKeys = Object.keys(diff.patch).sort();
  assertEqual(
    patchKeys.join(","),
    "amount,issuer,quantity,unit_price",
    "diff-only PATCH sends ONLY changed fields and excludes raw_charge_name/unchanged fields"
  );
  assertTrue(
    !("raw_charge_name" in (diff.patch as Record<string, unknown>)),
    "raw_charge_name is strictly excluded from PATCH"
  );

  // Align doc-eml-1 total so rec-eml-hc reconciles as CREATE/AUTO_READY with 9007199254740991.01
  const docEml2 = batch2.documents.find((d) => d.id === "doc-eml-1")!;
  let docDrafts2 = setDocTotalsDraftField(
    createEmptyDraftStore(),
    batch2.id,
    docEml2,
    "total_amount",
    "9007199254740991.01"
  );
  docDrafts2 = setDocTotalsDraftField(
    docDrafts2,
    batch2.id,
    docEml2,
    "total_currency",
    "USD"
  );
  const docDiff2 = buildDocTotalsDiffPatch(docEml2, docDrafts2, batch2.id);
  assertEqual(docDiff2.valid, true, "doc totals diff valid");
  const afterDocPatch2 = await client.patchDocumentTotals(
    batch2.id,
    docEml2.id,
    docDiff2.patch
  );
  assertTrue(afterDocPatch2.id === batch2.id, "doc totals patched cleanly");

  const patchedBatch2 = await client.patchBillingRecord(
    batch2.id,
    targetRec.id,
    diff.patch
  );
  assertEqual(
    readDecimalString(
      patchedBatch2.records.find((r) => r.id === targetRec.id)!.charge_details,
      "amount"
    ),
    "9007199254740991.01",
    "PATCH response returns updated lossless 9007199254740991.01"
  );
  const lastPatchCall = capturedCalls[capturedCalls.length - 1];
  assertTrue(
    lastPatchCall.method === "PATCH" &&
      (lastPatchCall.bodyText ?? "").includes('"amount":9007199254740991.01') &&
      (lastPatchCall.bodyText ?? "").includes('"unit_price":0.0025') &&
      !(lastPatchCall.bodyText ?? "").includes("raw_charge_name") &&
      lastPatchCall.headers["x-record-updated-at"] === undefined &&
      lastPatchCall.headers["x-resolved-warnings"] === undefined,
    "PATCH HTTP body carries unquoted 9007199254740991.01 & 0.0025, omits raw_charge_name and private headers"
  );

  // Reload & Export & Download round-trip checks
  const reloaded2 = await client.getBatchDetail(batch2.id);
  const reloadedTarget = reloaded2.records.find((r) => r.id === targetRec.id)!;
  assertEqual(
    readDecimalString(reloadedTarget.charge_details, "amount"),
    "9007199254740991.01",
    "reloaded record keeps 9007199254740991.01 lossless"
  );
  assertEqual(
    reloadedTarget.charge_details.raw_charge_name,
    "H/C",
    "read-only raw_charge_name preserved untouched"
  );
  const officialExport2 = await client.exportBatchJson(batch2.id, "official");
  assertEqual(
    readDecimalString(officialExport2.records[0].charge_details, "amount"),
    "9007199254740991.01",
    "official export preserves 9007199254740991.01"
  );
  const downloaded2 = await client.downloadBatchJsonArray(batch2.id, "official");
  assertTrue(
    downloaded2.rawJsonText.includes('"amount":9007199254740991.01') &&
      downloaded2.rawJsonText.includes('"unit_price":0.0025'),
    "export/download raw JSON array keeps unquoted 9007199254740991.01 & 0.0025"
  );
  results.push(
    "PASS 10: Diff-only PATCH, contract-only headers, read-only raw_charge_name & lossless save->reload->export->download"
  );

  // 11. 6-record mixed batch 3/5/6 counts, 422 BLOCKER guard (details.blocker_codes), contract-only approve resolving WARNING, doc repair & audit
  const freshBatch3Queued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let batch3 = await client.getBatchDetail(freshBatch3Queued.id);
  while (batch3.stage !== "COMPLETED") {
    batch3 = await client.getBatchDetail(freshBatch3Queued.id);
  }
  assertEqual(batch3.records.length, 6, "mixed batch contains exact 6 records");

  const expOfficial = await client.exportBatchJson(batch3.id, "official");
  const expFull = await client.exportBatchJson(batch3.id, "full_standard");
  const expAudit = await client.exportBatchJson(batch3.id, "audit");
  assertEqual(expOfficial.included_count, 3, "official export count = 3");
  assertEqual(expOfficial.excluded_pending_count, 2, "official excluded_pending_count = 2");
  assertEqual(expOfficial.excluded_rejected_count, 1, "official excluded_rejected_count = 1");
  assertEqual(expFull.included_count, 5, "full_standard export count = 5");
  assertEqual(expFull.excluded_pending_count, 0, "full_standard excluded_pending_count = 0");
  assertEqual(expFull.excluded_rejected_count, 1, "full_standard excluded_rejected_count = 1");
  assertEqual(expAudit.included_count, 6, "audit export count = 6");
  assertEqual(expAudit.excluded_pending_count, 0, "audit excluded_pending_count = 0");
  assertEqual(expAudit.excluded_rejected_count, 0, "audit excluded_rejected_count = 0");

  const dlOfficial = await client.downloadBatchJsonArray(batch3.id, "official");
  const dlFull = await client.downloadBatchJsonArray(batch3.id, "full_standard");
  const dlAudit = await client.downloadBatchJsonArray(batch3.id, "audit");
  assertEqual(dlOfficial.records.length, 3, "downloaded official array has 3 items");
  assertEqual(dlFull.records.length, 5, "downloaded full_standard array has 5 items");
  assertEqual(dlAudit.records.length, 6, "downloaded audit array has 6 items");

  // Validate BLOCKER cannot be approved (422 details.blocker_codes) while soft WARNING is resolved on approve
  const blockerRec = batch3.records.find((r) => r.id === "rec-precision-blocker")!;
  const blockerAlert = blockerRec.validation_alerts!.find(
    (a) => a.severity === "BLOCKER"
  )!;
  assertTrue(validateReviewReason("a") !== null, "1-char reason rejected");
  assertTrue(validateReviewReason("核对无误") === null, "valid reason accepted");

  const serverApproveBlockerErr = await assertRejectsAsync(
    client.approveBillingRecord(
      batch3.id,
      blockerRec.id,
      "试图强行批准含硬阻断的记录"
    ),
    "422 HARD_BLOCKER_CANNOT_APPROVE"
  );
  assertTrue(
    serverApproveBlockerErr instanceof ApiClientError &&
      serverApproveBlockerErr.status === 422 &&
      Array.isArray(serverApproveBlockerErr.details?.blocker_codes) &&
      (serverApproveBlockerErr.details?.blocker_codes as string[]).includes(
        blockerAlert.code
      ),
    "server rejects BLOCKER approval with 422 and standard details.blocker_codes"
  );

  // Soft warning record: approve directly with strict { reason } (NO private headers) -> APPROVED & WARNING -> RESOLVED
  const softRec = batch3.records.find((r) => r.id === "rec-soft-warning")!;
  const afterSoftApprove = await client.approveBillingRecord(
    batch3.id,
    softRec.id,
    "已核对提单原件，历史非标箱号特例放行"
  );
  const lastApproveCall = capturedCalls[capturedCalls.length - 1];
  assertTrue(
    lastApproveCall.method === "POST" &&
      lastApproveCall.url.endsWith(`/records/${softRec.id}/approve`) &&
      lastApproveCall.headers["x-record-updated-at"] === undefined &&
      lastApproveCall.headers["x-resolved-warnings"] === undefined,
    "approve request omits custom record-version and warning-waiver headers"
  );
  const updatedSoftRec = afterSoftApprove.records.find(
    (r) => r.id === softRec.id
  )!;
  assertEqual(
    updatedSoftRec.review_status,
    "APPROVED",
    "approving soft-warning record transitions NEEDS_REVIEW -> APPROVED"
  );
  assertEqual(
    updatedSoftRec.validation_alerts![0].severity,
    "RESOLVED",
    "soft warning marked RESOLVED upon approval"
  );

  // Doc repair: force-parse & retry
  const afterForce = await client.forceParseSkippedDocument(batch3.id, "doc-eml-sig");
  assertEqual(
    afterForce.documents.find((d) => d.id === "doc-eml-sig")?.status,
    "METADATA_ONLY",
    "force-parse updates SKIPPED_NOISE to METADATA_ONLY"
  );
  const afterRetry = await client.retryOrReplaceDocument(batch3.id, "doc-fail-zero", {
    ocr_mode: "REMOTE_WITH_LOCAL_FALLBACK",
  });
  assertEqual(afterRetry.failed_documents.length, 0, "retry clears failed_documents");
  assertEqual(afterRetry.failed_files, 0, "retry updates failed_files to 0");

  // On-demand audit check shows retry_count & human_review_history
  const auditAfter = await client.exportBatchJson(batch3.id, "audit");
  const softAudit = auditAfter.records.find(
    (r) => r.audit_trail.record_id === softRec.id
  )!;
  assertTrue(
    Array.isArray(softAudit.audit_trail.human_review_history) &&
      (softAudit.audit_trail.human_review_history as unknown[]).length >= 1 &&
      !("llm_calls_used" in softAudit.audit_trail),
    "audit mode exposes human_review_history and standard B03 audit_trail shape"
  );
  results.push(
    "PASS 11: 6-record 3/5/6 export counts, 422 blocker_codes guard, contract approve, doc retry/force-parse & audit history"
  );

  // 12. sync-baseline repeatable 3-cycle execution (1st create/update -> 2nd skip -> edit+approve -> 3rd update/create/skip)
  const syncBatchQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let syncBatch = await client.getBatchDetail(syncBatchQueued.id);
  while (syncBatch.stage !== "COMPLETED") {
    syncBatch = await client.getBatchDetail(syncBatchQueued.id);
  }

  // Cycle 1: 1 CREATE (rec-eml-hc) + 1 UPDATE (rec-dn-update), 0 skipped
  const sync1 = await client.syncBatchToBaseline(syncBatch.id);
  assertEqual(sync1.created_count, 1, "sync cycle 1 created_count = 1");
  assertEqual(sync1.updated_count, 1, "sync cycle 1 updated_count = 1");
  assertEqual(sync1.skipped_count, 0, "sync cycle 1 skipped_count = 0");

  // Cycle 2: repeat sync without edits -> all skipped idempotently
  const sync2 = await client.syncBatchToBaseline(syncBatch.id);
  assertEqual(sync2.created_count, 0, "sync cycle 2 created_count = 0");
  assertEqual(sync2.updated_count, 0, "sync cycle 2 updated_count = 0");
  assertEqual(sync2.skipped_count, 2, "sync cycle 2 skipped_count = 2");

  // Edit rec-eml-hc (and doc-eml-1 total) AND approve rec-soft-warning (NO blocker)
  await client.patchDocumentTotals(syncBatch.id, "doc-eml-1", {
    total_amount: rawDecimal("55.00"),
  });
  await client.patchBillingRecord(syncBatch.id, "rec-eml-hc", {
    unit_price: rawDecimal("55.00"),
    amount: rawDecimal("55.00"),
  });
  await client.approveBillingRecord(
    syncBatch.id,
    "rec-soft-warning",
    "核对原提单后批准此条特例"
  );

  // Cycle 3: button remains clickable -> 1 new created (rec-soft-warning), 1 updated (rec-eml-hc), 1 skipped (rec-dn-update)
  const sync3 = await client.syncBatchToBaseline(syncBatch.id);
  assertEqual(sync3.created_count, 1, "sync cycle 3 created_count = 1");
  assertEqual(sync3.updated_count, 1, "sync cycle 3 updated_count = 1");
  assertEqual(sync3.skipped_count, 1, "sync cycle 3 skipped_count = 1");

  results.push(
    "PASS 12: sync-baseline 3-cycle repeatable sync (created -> idempotent skipped -> edit/approve -> updated)"
  );

  // 13. Master data CRUD (no entity delete), dictionary CRUD + batch-scoped reapply, & lossless existing bills import/clear
  const initEntities = await client.listAccountingEntities();
  const entValidation = validateAccountingEntityInput(
    {
      canonicalName: "PACIFIC HORIZON LOGISTICS LTD.",
      aliasesText: "PHL, Pacific Horizon, 太平洋地平线",
      isDefault: true,
    },
    initEntities
  );
  assertEqual(entValidation.valid, true, "valid new entity passes validation");
  if (!entValidation.valid) throw new Error("unreachable");
  const createdEnt = await client.createAccountingEntity(entValidation.payload);
  assertEqual(createdEnt.is_default, true, "new entity marked default");
  const entitiesAfterCreate = await client.listAccountingEntities();
  assertEqual(
    entitiesAfterCreate.find((e) => e.id === "ent-neptune")?.is_default,
    false,
    "previous default entity toggled off when new default set"
  );

  const entEditCheck = validateAccountingEntityInput(
    {
      canonicalName: "PACIFIC HORIZON LOGISTICS LTD.",
      aliasesText: "PHL, Pacific Horizon Updated",
      isDefault: false,
    },
    entitiesAfterCreate,
    createdEnt.id
  );
  assertEqual(entEditCheck.valid, true, "entity edit allows same canonical name on self");
  if (!entEditCheck.valid) throw new Error("unreachable");
  const updatedEnt = await client.updateAccountingEntity(
    createdEnt.id,
    entEditCheck.payload
  );
  assertEqual(
    updatedEnt.aliases.join("|"),
    "PHL|Pacific Horizon Updated",
    "entity aliases updated cleanly"
  );
  assertTrue(
    !("deleteAccountingEntity" in client),
    "BillingApiClient must not expose deleteAccountingEntity"
  );
  const entityDeleteResp = await stubTransport(
    "http://127.0.0.1/api/v1/entities/ent-neptune",
    { method: "DELETE" }
  );
  assertEqual(
    entityDeleteResp.status,
    404,
    "DELETE /api/v1/entities/:id is strictly absent (404)"
  );

  // Dictionary add/edit/delete (builtin protected) & batch-scoped reapply
  const dictCheck = validateChargeDictionaryInput(
    "HIGH PRECISION TEST FEE",
    "Mega Precision Charge",
    "general"
  );
  assertEqual(dictCheck.valid, true, "dictionary input valid");
  if (!dictCheck.valid) throw new Error("unreachable");
  const customDictEntry = await client.upsertChargeDictionaryEntry(
    dictCheck.payload
  );
  assertEqual(customDictEntry.is_builtin, false, "created dictionary entry is custom");

  const builtinDelErr = await assertRejectsAsync(
    client.deleteChargeDictionaryEntry("dict-1"),
    "delete builtin dictionary entry"
  );
  assertTrue(
    builtinDelErr instanceof ApiClientError && builtinDelErr.status === 400,
    "deleting builtin dictionary entry returns 400"
  );

  // Create two independent batches A and B; lock rec-eml-hc in batch A, reapply only batch A
  const batchAQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let batchA = await client.getBatchDetail(batchAQueued.id);
  while (batchA.stage !== "COMPLETED") {
    batchA = await client.getBatchDetail(batchAQueued.id);
  }
  const batchBQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let batchB = await client.getBatchDetail(batchBQueued.id);
  while (batchB.stage !== "COMPLETED") {
    batchB = await client.getBatchDetail(batchBQueued.id);
  }

  // Lock rec-eml-hc charge_item manually in batch A and add H/C custom mapping
  await client.patchBillingRecord(batchA.id, "rec-eml-hc", {
    charge_item: "Manual Locked Handling",
  });
  const hcCustom = await client.upsertChargeDictionaryEntry({
    raw_alias: "H/C",
    standard_name: "Overridden H/C Name",
    category: "origin_dest",
  });

  const reappliedA = await client.reapplyBatchChargeDictionary(batchA.id);
  const recAUnlocked = reappliedA.records.find(
    (r) => r.id === "rec-precision-blocker"
  )!;
  const recALocked = reappliedA.records.find((r) => r.id === "rec-eml-hc")!;
  assertEqual(
    recAUnlocked.charge_details.charge_item,
    "Mega Precision Charge",
    "reapply updates unlocked record in target batch A"
  );
  assertEqual(
    readDecimalString(recAUnlocked.charge_details, "amount"),
    "9007199254740991.01",
    "reapply preserves 9007199254740991.01 lossless"
  );
  assertEqual(
    recALocked.charge_details.charge_item,
    "Manual Locked Handling",
    "reapply preserves human-locked charge_item"
  );

  const batchBAfter = await client.getBatchDetail(batchB.id);
  assertEqual(
    batchBAfter.records.find((r) => r.id === "rec-precision-blocker")!.charge_details
      .charge_item,
    "Handling Charge",
    "reapply on batch A must NOT touch batch B"
  );

  await client.deleteChargeDictionaryEntry(hcCustom.id);
  const dictAfterDel = await client.listChargeDictionary();
  assertTrue(
    !dictAfterDel.some((d) => d.id === hcCustom.id),
    "custom dictionary entry deleted via DELETE"
  );

  // Existing bills JSON + CSV lossless import and entity-scoped clear
  const jsonBillsCheck = parseExistingBillsImportInput(
    '[{"reference_no":"IMP-MEGA-01","charge_item":"Ocean Freight","amount":9007199254740991.01,"unit_price":0.0025,"currency":"USD","posting_type":"AP","accounting_entity":"ALL AMERICA FORWARDING, INC."}]'
  );
  assertEqual(jsonBillsCheck.valid, true, "JSON existing bills import valid");
  if (!jsonBillsCheck.valid) throw new Error("unreachable");
  await client.importExistingBills({
    default_accounting_entity: "ALL AMERICA FORWARDING, INC.",
    replace_existing: false,
    bills: jsonBillsCheck.bills,
  });

  const csvBillsCheck = parseExistingBillsImportInput(
    'reference_no,charge_item,amount,unit_price,currency,posting_type\n"CSV-001","Handling Charge",125.50,0.0025,USD,AR',
    {
      filename: "bills.csv",
      defaultAccountingEntity: "ALL AMERICA FORWARDING, INC.",
    }
  );
  assertEqual(csvBillsCheck.valid, true, "CSV existing bills import valid");
  if (!csvBillsCheck.valid) throw new Error("unreachable");
  await client.importExistingBills({
    default_accounting_entity: "ALL AMERICA FORWARDING, INC.",
    replace_existing: false,
    bills: csvBillsCheck.bills,
  });

  const aafBills = await client.listExistingBills("ALL AMERICA FORWARDING, INC.");
  const megaImportedBill = aafBills.find((b) => b.reference_no === "IMP-MEGA-01")!;
  assertEqual(
    readDecimalString(megaImportedBill, "amount"),
    "9007199254740991.01",
    "imported existing bill retains 9007199254740991.01"
  );
  assertEqual(
    readDecimalString(megaImportedBill, "unit_price"),
    "0.0025",
    "imported existing bill retains 0.0025"
  );

  await client.clearExistingBills("ALL AMERICA FORWARDING, INC.");
  assertEqual(
    (await client.listExistingBills("ALL AMERICA FORWARDING, INC.")).length,
    0,
    "entity-scoped clear removes target entity shared bills"
  );
  assertTrue(
    (await client.listExistingBills("NEPTUNE SHIPPING LIMITED")).length >= 3,
    "entity-scoped clear preserves other entity shared bills"
  );
  results.push(
    "PASS 13: Entity add/edit (no delete), dictionary CRUD, batch-scoped reapply & lossless baseline import/clear"
  );

  // 14. Harness Eval Dashboard: empty state, UNVERIFIED/FAILED/null -> "无结果" (NEVER 0%), COMPLETED metrics & fractions
  const initialRuns = await client.listEvalRuns();
  assertEqual(initialRuns.length, 0, "initial eval runs list is empty");

  const unverifiedRun = await client.triggerEvalRun({
    run_mode: "LIVE_LLM",
    k_trials: 3,
  });
  assertEqual(unverifiedRun.status, "UNVERIFIED", "LIVE_LLM stub returns UNVERIFIED");
  const unverifiedVm = buildEvalRunViewModel(unverifiedRun);
  assertEqual(unverifiedVm.trialsText, "0 / 3", "UNVERIFIED reports actual 0 / 3 trials");
  assertTrue(
    unverifiedVm.truthMetrics.every(
      (m) => m.displayText === NO_EVAL_RESULT_LABEL && m.isUnavailable
    ),
    "all truth metrics on UNVERIFIED render 无结果 rather than 0%"
  );
  assertTrue(
    unverifiedVm.stabilityMetrics.every(
      (m) => m.displayText === NO_EVAL_RESULT_LABEL && m.isUnavailable
    ),
    "all pass@k/pass^k metrics on UNVERIFIED render 无结果 rather than 0%"
  );

  const failedRun = await client.triggerEvalRun({
    run_mode: "LIVE_E2E",
    k_trials: 3,
  });
  assertEqual(failedRun.status, "FAILED", "LIVE_E2E stub returns FAILED");
  const failedVm = buildEvalRunViewModel(failedRun);
  assertEqual(failedVm.trialsText, "1 / 3", "FAILED reports actual 1 / 3 trials");
  assertTrue(
    failedVm.truthMetrics.every((m) => m.displayText === NO_EVAL_RESULT_LABEL) &&
      failedVm.stabilityMetrics.every((m) => m.displayText === NO_EVAL_RESULT_LABEL),
    "FAILED metrics strictly display 无结果"
  );

  // Direct guard assertions: even if 0 passed to UNVERIFIED/FAILED, or null to COMPLETED -> strictly "无结果"
  assertEqual(
    formatEvalMetricValue({ status: "UNVERIFIED" }, 0).displayText,
    "无结果",
    "UNVERIFIED never renders 0 as 0%"
  );
  assertEqual(
    formatEvalMetricValue({ status: "FAILED" }, 0).displayText,
    "无结果",
    "FAILED never renders 0 as 0%"
  );
  assertEqual(
    formatEvalMetricValue({ status: "COMPLETED" }, null).displayText,
    "无结果",
    "COMPLETED with null metric renders 无结果"
  );

  const offlineRun = await client.triggerEvalRun({
    run_mode: "OFFLINE_DETERMINISTIC",
    k_trials: 3,
  });
  assertEqual(offlineRun.status, "COMPLETED", "OFFLINE_DETERMINISTIC returns COMPLETED");
  const offlineVm = buildEvalRunViewModel(offlineRun);
  assertEqual(
    offlineVm.truthMetrics.length,
    9,
    "B05 view model exposes all 9 truth metrics including alert_precision"
  );
  const schemaMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "schema_valid_rate"
  )!;
  const exactMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "field_exact_match"
  )!;
  const amountMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "amount_accuracy"
  )!;
  const alertPrecisionMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "alert_precision"
  )!;
  const fpMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "false_positive_rate"
  )!;
  const passAtKMetric = offlineVm.stabilityMetrics.find(
    (m) => m.key === "pass_at_k"
  )!;
  assertEqual(schemaMetric.displayText, "100.0%", "COMPLETED schema_valid_rate formats 100.0%");
  assertEqual(schemaMetric.fractionText, "12 / 12", "COMPLETED schema_valid_rate shows 12 / 12");
  assertEqual(exactMetric.displayText, "97.92%", "COMPLETED field_exact_match formats 97.92%");
  assertEqual(exactMetric.fractionText, "47 / 48", "COMPLETED field_exact_match shows 47 / 48");
  assertEqual(amountMetric.displayText, "100.0%", "COMPLETED amount_accuracy parsed from B05 report object");
  assertEqual(amountMetric.fractionText, "24 / 24", "COMPLETED amount_accuracy shows 24 / 24");
  assertEqual(alertPrecisionMetric.fractionText, "8 / 8", "COMPLETED alert_precision shows 8 / 8");
  assertEqual(fpMetric.displayText, "0.0%", "COMPLETED legitimate 0 false positive rate formats 0.0%");
  assertEqual(passAtKMetric.displayText, "100.0%", "COMPLETED pass@3 formats 100.0%");
  assertEqual(passAtKMetric.fractionText, "12 / 12", "COMPLETED pass@3 shows 12 / 12");
  assertTrue(
    offlineVm.avgLatencyText.endsWith("ms") && offlineVm.executedCases === 36,
    "B05 telemetry avgLatencyText & executedCases populated"
  );
  results.push(
    "PASS 14: Harness eval formatting (UNVERIFIED/FAILED/null -> 无结果, B05 report metrics & fractions)"
  );

  // 15. CLI ReviewBundleV1 ("1.0") import contract: lossless decimals, BATCH_PRIVATE snapshot, review_audit_logs & XSS plain text
  const invalidBundleCheck = validateReviewBundleInput(
    '{"bundle_version":"2.0","accounting_entity":"NEPTUNE","documents":[],"records":[]}'
  );
  assertEqual(invalidBundleCheck.valid, false, "unsupported bundle_version 2.0 rejected");

  const cliBundlePayloadText = stringifyLosslessJson({
    bundle_version: "1.0",
    short_code: "B-CLI-15",
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    documents: [
      {
        id: "doc-eml-1",
        parent_doc_id: null,
        original_filename: "cli_invoice.pdf",
        sanitized_filename: "cli_invoice.pdf",
        source_kind: "PDF",
        extraction_channel: "REMOTE_OCR",
        status: "PARSED",
        error_message: null,
        affected_record_ids: ["rec-precision-blocker", "rec-soft-warning"],
        excluded_clauses: [],
        shipment_refs: { mbl_nos: [], container_nos: [] },
        subtotal_amount: rawDecimal("9007199254740991.01"),
        tax_amount: rawDecimal("0.00"),
        total_amount: rawDecimal("9007199254740991.01"),
        total_currency: "USD",
      },
      {
        id: "doc-fail-zero",
        parent_doc_id: null,
        original_filename: "scan_retry.png",
        sanitized_filename: "scan_retry.png",
        source_kind: "IMAGE",
        extraction_channel: "LOCAL_OCR_FALLBACK",
        status: "METADATA_ONLY",
        error_message: null,
        retry_count: 1,
        affected_record_ids: [],
        excluded_clauses: [],
        shipment_refs: { mbl_nos: [], container_nos: [] },
        subtotal_amount: null,
        tax_amount: null,
        total_amount: null,
        total_currency: null,
      },
    ],
    records: [
      {
        id: "rec-precision-blocker",
        source_doc_id: "doc-eml-1",
        source_line_index: 0,
        operation: "PENDING_REVIEW",
        review_status: "NEEDS_REVIEW",
        match_level: "NONE",
        matched_existing_bill_id: null,
        document_info: {
          document_type: "Debit Note",
          reference_no: "CLI-MEGA-9007",
          document_date: "2026-11-19",
        },
        transaction_party: {
          issuer: "COSCO SHIPPING",
          recipient: "NEPTUNE SHIPPING LIMITED",
        },
        charge_details: {
          charge_item: "Handling Charge",
          raw_charge_name: "HIGH PRECISION TEST FEE",
          quantity: rawDecimal("1"),
          unit_price: rawDecimal("0.0025"),
          amount: rawDecimal("9007199254740991.01"),
          currency: "USD",
          payment_term: "PP",
        },
        financial_attributes: {
          accounting_entity: "NEPTUNE SHIPPING LIMITED",
          posting_type: "AP",
        },
        shipment_refs: { mbl_nos: [], hbl_no: null, booking_no: null, container_nos: [] },
        source_evidence: {
          source_file: "cli_invoice.pdf",
          sheet: "FreightDetail",
          line_number: 18,
          snippet:
            "<script>alert('xss')</script><img src=x onerror=alert(1)> RAW_AMOUNT: 9007199254740991.01 USD | RATE: 0.0025",
        },
        ai_reasoning: ["CLI 复核包无损小数与 Excel 证据行透传验证"],
        human_overrides: {},
        reject_reason: null,
      },
      {
        id: "rec-soft-warning",
        source_doc_id: "doc-eml-1",
        source_line_index: 1,
        operation: "CREATE",
        review_status: "APPROVED",
        match_level: "NONE",
        matched_existing_bill_id: null,
        document_info: {
          document_type: "Debit Note",
          reference_no: "CLI-MEGA-9007",
          document_date: "2026-11-19",
        },
        transaction_party: {
          issuer: "COSCO SHIPPING",
          recipient: "NEPTUNE SHIPPING LIMITED",
        },
        charge_details: {
          charge_item: "Handling Charge",
          raw_charge_name: "H/C",
          quantity: rawDecimal("1"),
          unit_price: rawDecimal("245.00"),
          amount: rawDecimal("245.00"),
          currency: "USD",
          payment_term: "PP",
        },
        financial_attributes: {
          accounting_entity: "NEPTUNE SHIPPING LIMITED",
          posting_type: "AP",
        },
        shipment_refs: { mbl_nos: [], hbl_no: null, booking_no: null, container_nos: ["ABCD1234567"] },
        source_evidence: { source_file: "cli_invoice.pdf", page: 1, snippet: "H/C 245.00 USD" },
        ai_reasoning: ["已核对人工历史记录"],
        human_overrides: {},
        reject_reason: null,
        review_audit_logs: [
          {
            action: "APPROVED",
            operator: "CLI 复核员",
            timestamp: "2026-11-19T10:00:00Z",
            reason: "复核包互通测试批准原因",
          },
        ],
      },
    ],
    existing_bills_snapshot: [
      {
        id: "bill-private-only",
        accounting_entity: "NEPTUNE SHIPPING LIMITED",
        reference_no: "BATCH_PRIVATE",
        document_type: "Debit Note",
        issuer: "COSCO SHIPPING",
        recipient: "NEPTUNE SHIPPING LIMITED",
        charge_item: "Handling Charge",
        raw_charge_name: "H/C",
        amount: rawDecimal("245.00"),
        currency: "USD",
        payment_term: "PP",
        posting_type: "AP",
        version: 1,
      },
    ],
  });

  const validatedImport = validateReviewBundleInput(cliBundlePayloadText);
  assertEqual(validatedImport.valid, true, "CLI ReviewBundleV1 passes strict schema validation");
  if (!validatedImport.valid) throw new Error("unreachable");

  // Import into a brand new client instance to prove zero shared in-memory state
  const targetClient = new BillingApiClient({
    transport: createDevStubTransport(),
  });
  const importedBatch = await targetClient.importReviewBundle(
    validatedImport.bundle
  );
  const importedMega = importedBatch.records.find(
    (r) => r.id === "rec-precision-blocker"
  )!;
  assertEqual(
    readDecimalString(importedMega.charge_details, "amount"),
    "9007199254740991.01",
    "imported ReviewBundleV1 preserves 9007199254740991.01"
  );
  assertEqual(
    readDecimalString(importedMega.charge_details, "unit_price"),
    "0.0025",
    "imported ReviewBundleV1 preserves 0.0025"
  );
  assertEqual(
    importedMega.source_evidence?.snippet,
    "<script>alert('xss')</script><img src=x onerror=alert(1)> RAW_AMOUNT: 9007199254740991.01 USD | RATE: 0.0025",
    "XSS snippet preserved verbatim as plain text string"
  );
  const importedAudit = await targetClient.exportBatchJson(
    importedBatch.id,
    "audit"
  );
  const importedSoftAudit = importedAudit.records.find(
    (r) => r.audit_trail.record_id === "rec-soft-warning"
  )!;
  const importedHistory = importedSoftAudit.audit_trail
    .human_review_history as Array<{ action?: string; reason?: string }>;
  assertTrue(
    Array.isArray(importedHistory) &&
      importedHistory.some(
        (h) =>
          h.action === "APPROVED" && h.reason === "复核包互通测试批准原因"
      ),
    "imported ReviewBundleV1 restores review_audit_logs -> human_review_history without audit_snapshot"
  );
  const importedFailDocAudit = importedAudit.records.find(
    (r) => r.audit_trail.record_id === "rec-precision-blocker"
  )!;
  assertEqual(
    importedFailDocAudit.audit_trail.retry_count,
    0,
    "imported audit export reads standard retry_count without llm_calls_used"
  );
  const importedDocFailRecord = importedBatch.documents.find(
    (d) => d.id === "doc-fail-zero"
  )!;
  assertEqual(
    importedDocFailRecord.status,
    "METADATA_ONLY",
    "imported bundle preserves retried document status"
  );

  results.push(
    "PASS 15: CLI ReviewBundleV1 (1.0) import round-trip, BATCH_PRIVATE isolation, review_audit_logs & XSS plain text"
  );

  // 16. C2 + A1 + B3: Real openapi.yaml batch-edge-01 example parsing & full App SSR mount
  const nodeModuleSpec = "node:" + "module";
  const nodeFsSpec = "node:" + "fs";
  const nodePathSpec = "node:" + "path";
  const modPkg = (await import(/* @vite-ignore */ nodeModuleSpec)) as {
    createRequire: (url: string) => (id: string) => unknown;
  };
  const fsPkg = (await import(/* @vite-ignore */ nodeFsSpec)) as {
    readFileSync: (path: string, enc: string) => string;
  };
  const pathPkg = (await import(/* @vite-ignore */ nodePathSpec)) as {
    resolve: (...segments: string[]) => string;
  };
  const req = modPkg.createRequire(import.meta.url);
  const frontendRootDir = decodeURIComponent(
    new URL("../..", import.meta.url).pathname
  ).replace(/^\/([A-Za-z]:)/, "$1");
  const openApiYamlPath = pathPkg.resolve(
    frontendRootDir,
    "..",
    "docs",
    "workflow",
    "contracts",
    "openapi.yaml"
  );
  const openApiYamlText = fsPkg.readFileSync(openApiYamlPath, "utf8");
  const jsYaml = req("js-yaml") as { load: (src: string) => Record<string, unknown> };
  const openApiDoc = jsYaml.load(openApiYamlText) as {
    paths: Record<
      string,
      {
        get: {
          responses: Record<
            string,
            {
              content: {
                "application/json": {
                  examples: {
                    edge_credit_and_failure: {
                      value: Record<string, unknown>;
                    };
                  };
                };
              };
            }
          >;
        };
      }
    >;
  };
  const rawOpenApiEdgeExample =
    openApiDoc.paths["/api/v1/batches/{batch_id}"].get.responses["200"].content[
      "application/json"
    ].examples.edge_credit_and_failure.value;
  const openApiExampleClone = JSON.parse(
    JSON.stringify(rawOpenApiEdgeExample)
  ) as {
    records: Array<{
      inherited_fields?: unknown;
      charge_details: { unit_price: unknown; amount: unknown };
    }>;
  };
  assertTrue(
    !("inherited_fields" in openApiExampleClone.records[0]),
    "openapi.yaml batch-edge-01 real example omits inherited_fields"
  );
  assertTrue(
    openApiYamlText.includes("unit_price: -40.0") &&
      openApiYamlText.includes("amount: -40.0"),
    "openapi.yaml batch-edge-01 real example specifies -40.0 decimal literals"
  );
  openApiExampleClone.records[0].charge_details.unit_price = rawDecimal("-40.0");
  openApiExampleClone.records[0].charge_details.amount = rawDecimal("-40.0");
  const openApiBatchEdge01RawText = stringifyLosslessJson(openApiExampleClone);

  const edgeClient = new BillingApiClient({
    transport: async () =>
      new Response(openApiBatchEdge01RawText, {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  });
  const normalizedEdgeBatch = await edgeClient.getBatchDetail("batch-edge-01");
  assertEqual(
    normalizedEdgeBatch.records[0].inherited_fields.length,
    0,
    "missing inherited_fields on real OpenAPI batch-edge-01 normalized to []"
  );
  assertEqual(
    readDecimalString(normalizedEdgeBatch.records[0].charge_details, "amount"),
    "-40.0",
    "real OpenAPI batch-edge-01 preserves -40.0 decimal literal"
  );
  // Also test defensive normalizer directly on stripped BatchDetail object
  const strippedNormalized = normalizeBatchDetail({
    ...normalizedEdgeBatch,
    records: [
      {
        ...normalizedEdgeBatch.records[0],
        inherited_fields: undefined as unknown as string[],
        validation_alerts: undefined,
        field_diffs: undefined,
        candidate_bills: undefined,
        ai_reasoning: undefined as unknown as string[],
      },
    ],
  });
  assertEqual(
    strippedNormalized.records[0].inherited_fields.length,
    0,
    "normalizeBatchDetail safely supplies default empty arrays"
  );

  // Vite SSR UI mount verification for full App with real OpenAPI batch-edge-01 (configFile: false avoids .vite-temp)
  const viteModUrl = new URL(
    "../../node_modules/vite/dist/node/index.js",
    import.meta.url
  ).href;
  const { createServer } = (await import(viteModUrl)) as {
    createServer: (opts: Record<string, unknown>) => Promise<{
      ssrLoadModule: (id: string) => Promise<Record<string, unknown>>;
      close: () => Promise<void>;
    }>;
  };
  const viteServer = await createServer({
    root: frontendRootDir,
    configFile: false,
    plugins: [],
    esbuild: { jsx: "automatic" },
    server: { middlewareMode: true, hmr: false, ws: false },
    optimizeDeps: { noDiscovery: true },
    appType: "custom",
    logLevel: "silent",
  });
  let renderAppSsrHtml!: (props: Record<string, unknown>) => string;
  try {
    const ReactMod = req("react") as {
      createElement: (
        type: unknown,
        props: Record<string, unknown> | null
      ) => unknown;
    };
    const ReactDomServer = req("react-dom/server") as {
      renderToStaticMarkup: (el: unknown) => string;
    };
    const { default: AppComponent } = (await viteServer.ssrLoadModule(
      "/src/App.tsx"
    )) as { default: unknown };
    const { ReviewInspector } = (await viteServer.ssrLoadModule(
      "/src/components/ReviewInspector.tsx"
    )) as { ReviewInspector: unknown };

    renderAppSsrHtml = (props) =>
      ReactDomServer.renderToStaticMarkup(
        ReactMod.createElement(AppComponent, props)
      );

    const fullAppHtml = renderAppSsrHtml({
      initialBatch: normalizedEdgeBatch,
    });
    assertTrue(
      fullAppHtml.includes("B-1119-09") &&
        fullAppHtml.includes("ALL AMERICA FORWARDING, INC.") &&
        fullAppHtml.includes("scan_corrupted.png") &&
        fullAppHtml.includes("影响记录 0 条") &&
        fullAppHtml.includes("-40.00 USD") &&
        fullAppHtml.includes('value="-40.0"') &&
        fullAppHtml.includes("CREDIT(-): H/D Fee Rebate 40.00 USD"),
      "full App SSR mount with untouched OpenAPI batch-edge-01 renders -40.00, value=-40.0 and zero-record fail card"
    );

    const excelRec = {
      ...normalizedEdgeBatch.records[0],
      source_evidence: {
        ...normalizedEdgeBatch.records[0].source_evidence,
        sheet: "FreightSummary",
        line_number: 14,
      },
    };
    const noop = () => undefined;
    const noopAsync = async () => undefined;
    const excelInspectorHtml = ReactDomServer.renderToStaticMarkup(
      ReactMod.createElement(ReviewInspector, {
        batch: { ...normalizedEdgeBatch, records: [excelRec] },
        selectedRecord: excelRec,
        inspectedDocId: null,
        draftStore: createEmptyDraftStore(),
        dictionaryList: [],
        savingRecord: false,
        approvingOrRejecting: false,
        savingDocTotals: false,
        retryingDocId: null,
        forcingDocId: null,
        mutationError: null,
        auditHistoryByKey: {},
        onClearMutationError: noop,
        onRefreshBatch: noop,
        onClearInspectedDoc: noop,
        onUpdateRecordField: noop,
        onResetRecordDraft: noop,
        onSaveRecordPatch: noopAsync,
        onApproveRecord: noopAsync,
        onRejectRecord: noopAsync,
        onUpdateDocField: noop,
        onResetDocDraft: noop,
        onSaveDocTotals: noopAsync,
        onRetryDoc: noopAsync,
        onForceParseDoc: noopAsync,
        onOpenManualRecordModal: noop,
        onRequestAuditHistory: noopAsync,
      })
    );
    assertTrue(
      excelInspectorHtml.includes("工作表：FreightSummary") &&
        excelInspectorHtml.includes("第 14 行"),
      "ReviewInspector renders Excel sheet & line_number evidence"
    );

    // Also render F7 pending batch recovery screen through real App SSR inside same Vite session
    const f7RecoveryHtml = renderAppSsrHtml({
      initialPendingBatchRecovery: {
        id: "batch-f7-new",
        shortCode: "B-F7-01",
        accountingEntity: "NEPTUNE SHIPPING LIMITED",
        isDemo: false,
        loading: false,
        errorMessage: "HTTP 503: 详情服务暂不可用",
      },
    });
    assertTrue(
      f7RecoveryHtml.includes("批次详情读取失败") &&
        f7RecoveryHtml.includes("已创建入队，但首次读取详情失败") &&
        f7RecoveryHtml.includes("B-F7-01") &&
        f7RecoveryHtml.includes("batch-f7-new") &&
        f7RecoveryHtml.includes("重试读取此批次"),
      "full App SSR mount renders F7 recovery card with batch ID, shortCode and retry GET button"
    );
  } finally {
    await viteServer.close();
  }
  results.push(
    "PASS 16: Real OpenAPI batch-edge-01 full App SSR mount (-40.0, 0-record fail card, sheet/line_number) + F7 recovery UI"
  );

  // 17. A3: Export modal per-batch cache isolation & 503 failure clearing (A batch audit -> B batch 503)
  let exportCache = createExportModalCacheState("batch-a");
  const makeExportPayload = (
    mode: "official" | "full_standard" | "audit",
    count: number
  ) => ({
    mode,
    included_count: count,
    excluded_pending_count: 0,
    excluded_rejected_count: 0,
    records: Array.from({ length: count }, (_, idx) => ({
      operation: "CREATE" as const,
      document_info: {
        document_type: "Debit Note",
        reference_no: `REF-${idx}`,
        document_date: "2026-11-20",
      },
      transaction_party: {
        issuer: "COSCO",
        recipient: "NEPTUNE SHIPPING LIMITED",
      },
      charge_details: {
        charge_item: "Handling Charge",
        raw_charge_name: "H/C",
        quantity: 1,
        unit_price: 100,
        amount: 100,
        currency: "USD",
        payment_term: "PP" as const,
      },
      financial_attributes: {
        accounting_entity: "NEPTUNE SHIPPING LIMITED",
        posting_type: "AP" as const,
      },
      shipment_refs: {
        mbl_nos: [],
        hbl_no: null,
        booking_no: null,
        container_nos: [],
      },
      audit_trail: { record_id: `r-${idx}`, ai_reasoning: [] },
    })),
  });
  exportCache = completeExportModalLoadSuccess("batch-a", {
    official: makeExportPayload("official", 3),
    full_standard: makeExportPayload("full_standard", 5),
    audit: makeExportPayload("audit", 6),
  });
  const eligA = getExportModalActionEligibility(exportCache, "batch-a", "audit");
  assertEqual(eligA.canCopy, true, "batch-a audit ready for copy");
  assertEqual(eligA.activeExport?.included_count, 6, "batch-a audit count = 6");

  // Switch to batch-b and simulate three 503 failures
  exportCache = resetExportModalForBatch(exportCache, "batch-b");
  assertEqual(
    getExportResponseForBatchMode(exportCache, "batch-b", "audit"),
    null,
    "resetExportModalForBatch wipes batch-a entries immediately"
  );
  exportCache = completeExportModalLoadFailure(
    "batch-b",
    "HTTP 503: 导出服务暂不可用"
  );
  const eligB = getExportModalActionEligibility(exportCache, "batch-b", "audit");
  assertEqual(eligB.activeExport, null, "batch-b 503 clears all preview data");
  assertEqual(eligB.canCopy, false, "batch-b 503 disables copy button");
  assertEqual(eligB.canDownload, false, "batch-b 503 disables download button");
  results.push(
    "PASS 17: Export cache isolated per batch and cleared on 503 failure (copy/download disabled)"
  );

  // 18. C2 + A12: Real B05 runner.py EvalRunSummary shape + amount_exact_match alias + null COMPLETED -> "无结果"
  const b05RealShapeEvalRun = {
    id: "f086888f-fa66-466d-bc25-9c69ac79eec1",
    run_mode: "OFFLINE_DETERMINISTIC" as const,
    status: "COMPLETED" as const,
    error_message: null,
    model_name: "deterministic-rules",
    k_trials: 1,
    actual_trials: 1,
    total_cases: 1,
    schema_valid_rate: 1.0,
    field_exact_match: 1.0,
    ar_ap_accuracy: 1.0,
    operation_accuracy: 1.0,
    conflict_recall: null,
    pass_at_k: 1.0,
    pass_pow_k: 1.0,
    created_at: "2026-11-20T07:56:10.613213+00:00",
    report: {
      schema_valid_rate: { rate: 1.0, numerator: 4, denominator: 4 },
      field_exact_match: { rate: 1.0, numerator: 2, denominator: 2 },
      ar_ap_accuracy: { rate: 1.0, numerator: 2, denominator: 2 },
      conflict_recall: { rate: null, numerator: 0, denominator: 0 },
      amount_accuracy: { rate: 1.0, numerator: 2, denominator: 2 },
      currency_accuracy: { rate: 1.0, numerator: 2, denominator: 2 },
      operation_accuracy: { rate: 1.0, numerator: 2, denominator: 2 },
      false_positive_rate: { rate: 0.0, numerator: 0, denominator: 2 },
      alert_precision: { rate: null, numerator: 0, denominator: 0 },
      pass_at_k: { rate: 1.0, numerator: 1, denominator: 1 },
      pass_pow_k: { rate: 1.0, numerator: 1, denominator: 1 },
      avg_latency_ms: 963.60359999926,
      executed_cases: 1,
      llm_calls: 0,
      ocr_calls: 0,
      self_repair_evidence: "UNVERIFIED",
      failing_cases: [],
      case_results: [
        {
          trial: 1,
          group_id: "C05_PARTIAL_UPDATE_DN202609001",
          case_id: "C05_PARTIAL_UPDATE_DN202609001/payment_term",
          status: "PASSED",
          latency_ms: 963.60359999926,
          llm_calls: 0,
          ocr_calls: 0,
          checks: [],
        },
      ],
    },
  };
  const b05RealVm = buildEvalRunViewModel(b05RealShapeEvalRun);
  const realSchemaRow = b05RealVm.truthMetrics.find(
    (m) => m.key === "schema_valid_rate"
  )!;
  const realAmtRow = b05RealVm.truthMetrics.find(
    (m) => m.key === "amount_accuracy"
  )!;
  const realFpRow = b05RealVm.truthMetrics.find(
    (m) => m.key === "false_positive_rate"
  )!;
  const realConflictRow = b05RealVm.truthMetrics.find(
    (m) => m.key === "conflict_recall"
  )!;
  const realAlertPrecRow = b05RealVm.truthMetrics.find(
    (m) => m.key === "alert_precision"
  )!;
  assertEqual(realSchemaRow.displayText, "100.0%", "real B05 schema_valid_rate renders 100.0%");
  assertEqual(realSchemaRow.fractionText, "4 / 4", "real B05 schema_valid_rate renders 4 / 4");
  assertEqual(realAmtRow.displayText, "100.0%", "real B05 amount_accuracy renders 100.0%");
  assertEqual(realAmtRow.fractionText, "2 / 2", "real B05 amount_accuracy renders 2 / 2");
  assertEqual(realFpRow.displayText, "0.0%", "real B05 false_positive_rate renders 0.0%");
  assertTrue(
    realConflictRow.isUnavailable &&
      realConflictRow.displayText === NO_EVAL_RESULT_LABEL &&
      realAlertPrecRow.isUnavailable &&
      realAlertPrecRow.displayText === NO_EVAL_RESULT_LABEL,
    "real B05 null conflict_recall & alert_precision render 无结果"
  );
  assertEqual(
    b05RealVm.avgLatencyText,
    "963.6 ms",
    "real B05 avg_latency_ms rendered accurately"
  );

  const b05AliasVm = buildEvalRunViewModel({
    ...b05RealShapeEvalRun,
    amount_exact_match: 1.0,
    report: {
      ...b05RealShapeEvalRun.report,
      amount_accuracy: undefined,
      amount_exact_match: { rate: 1.0, numerator: 2, denominator: 2 },
    },
  } as unknown as Parameters<typeof buildEvalRunViewModel>[0]);
  const aliasAmtRow = b05AliasVm.truthMetrics.find(
    (m) => m.key === "amount_accuracy"
  )!;
  assertEqual(
    aliasAmtRow.displayText,
    "100.0%",
    "B05 amount_exact_match alias also renders 100.0%"
  );
  results.push(
    "PASS 18: Real B05 runner.py EvalRunSummary shape + amount_exact_match alias + null COMPLETED -> 无结果"
  );

  // 19. A6, A7 & A9: Zero phantom container diff on lowercase server value, explicit cleared amount -> null, blank initial review_note & blocker_codes banner
  const lowerCaseRec = {
    ...normalizedEdgeBatch.records[0],
    id: "rec-a9-check",
    charge_details: {
      ...normalizedEdgeBatch.records[0].charge_details,
      amount: 245.0,
    },
    shipment_refs: {
      ...normalizedEdgeBatch.records[0].shipment_refs,
      container_nos: ["abcd1234567"],
    },
    human_overrides: { review_note: "旧审计备注不应回填编辑框" },
    validation_alerts: [
      {
        code: "LINE_AMOUNT_MISMATCH",
        severity: "BLOCKER" as const,
        field: "amount",
        message: "数量×单价与行金额不一致",
        expected: "245.00",
        actual: "240.00",
      },
    ],
  };
  assertEqual(
    extractServerRecordFormValues(lowerCaseRec).review_note,
    "",
    "A6: review_note always initializes blank rather than copying human_overrides.review_note"
  );
  const emptyDraftDiff = buildRecordDiffPatch(
    lowerCaseRec,
    createEmptyDraftStore(),
    "batch-a9"
  );
  assertEqual(
    emptyDraftDiff.hasChanges,
    false,
    "A9: unedited lowercase container_nos ['abcd1234567'] emits zero phantom patch fields"
  );
  const clearAmtStore = setRecordDraftField(
    createEmptyDraftStore(),
    "batch-a9",
    lowerCaseRec,
    "amount",
    ""
  );
  const clearAmtDiff = buildRecordDiffPatch(
    lowerCaseRec,
    clearAmtStore,
    "batch-a9"
  );
  assertEqual(clearAmtDiff.valid, true, "A9: clearing existing amount is valid");
  assertEqual(
    stringifyLosslessJson(clearAmtDiff.patch),
    '{"amount":null}',
    'A9: cleared amount serializes as {"amount":null}'
  );

  const blockerErr422 = new ApiClientError({
    status: 422,
    code: "HARD_BLOCKER_CANNOT_APPROVE",
    message: "存在未解决的硬性阻断",
    details: { blocker_codes: ["LINE_AMOUNT_MISMATCH"] },
  });
  const extractedBanner = extractMutationErrorBanner(
    "批准入账被拦截",
    blockerErr422,
    lowerCaseRec.validation_alerts
  );
  assertTrue(
    extractedBanner.blockers?.length === 1 &&
      extractedBanner.blockers[0].code === "LINE_AMOUNT_MISMATCH" &&
      extractedBanner.blockers[0].field === "amount",
    "A7: 422 details.blocker_codes mapped cleanly against record validation_alerts"
  );
  results.push(
    "PASS 19: Zero lowercase container phantom diff, explicit amount:null PATCH, blank review_note & 422 blocker_codes"
  );

  // 20. A13: Unicode code-point counting across review reason client & stub checks
  assertEqual(countCodePoints("😀"), 1, "single supplementary-plane emoji counts as 1 code point");
  assertTrue(
    validateReviewReason("😀") !== null,
    "1 code point (2 UTF-16 units) rejected by client"
  );
  assertTrue(
    validateReviewReason("😀😀") === null,
    "2 code points accepted by client"
  );
  const valid500Surrogates = "\u{20000}".repeat(500);
  const invalid501Surrogates = "\u{20000}".repeat(501);
  assertEqual(
    countCodePoints(valid500Surrogates),
    500,
    "500 CJK Ext-B characters count as 500 code points"
  );
  assertTrue(
    validateReviewReason(valid500Surrogates) === null,
    "500 supplementary code points accepted"
  );
  assertTrue(
    validateReviewReason(invalid501Surrogates) !== null,
    "501 supplementary code points rejected"
  );
  const stubEmojiReject = await assertRejectsAsync(
    client.approveBillingRecord(batch3.id, "rec-precision-blocker", "😀"),
    "stub rejects single emoji reason"
  );
  assertTrue(
    stubEmojiReject instanceof ApiClientError &&
      stubEmojiReject.status === 422 &&
      stubEmojiReject.code === "INVALID_REASON_LENGTH",
    "stub enforces Unicode code-point minimum length"
  );
  results.push(
    "PASS 20: Unicode code-point validation (1 emoji rejected, 2 accepted, 500 Ext-B accepted, 501 rejected)"
  );

  // 21. A8 & A10: Pre-check rejections (existing_bills_json object, malformed ReviewBundleV1, file+URL conflict, missing currency/posting_type)
  const nonArrayBillsBatch = validateBatchIngestionInput({
    accountingEntity: "NEPTUNE SHIPPING LIMITED",
    files: [{ name: "a.pdf", size: 100 }],
    publicUrlLinesText: "",
    existingBillsJsonText: '{"bills":[]}',
  });
  assertEqual(
    nonArrayBillsBatch.valid,
    false,
    'existing_bills_json strictly rejects {"bills":[]} object wrapper'
  );
  assertEqual(
    validateReviewBundleInput(
      '{"bundle_version":"1.0","accounting_entity":"NEPTUNE","ocr_mode":"NOT_A_MODE","documents":[],"records":[]}'
    ).valid,
    false,
    "ReviewBundleV1 rejects invalid ocr_mode"
  );
  assertEqual(
    validateReviewBundleInput(
      '{"bundle_version":"1.0","accounting_entity":"NEPTUNE","ocr_mode":"REMOTE_ONLY","documents":[{}],"records":[]}'
    ).valid,
    false,
    "ReviewBundleV1 rejects empty document element {}"
  );
  assertEqual(
    validateReviewBundleInput(
      '{"bundle_version":"1.0","accounting_entity":"NEPTUNE","ocr_mode":"REMOTE_ONLY","documents":[],"records":[{}]}'
    ).valid,
    false,
    "ReviewBundleV1 rejects empty record element {}"
  );

  const conflictDocErr = await assertRejectsAsync(
    client.retryOrReplaceDocument(batch3.id, "doc-fail-zero", {
      file: new Blob(["pdf"], { type: "application/pdf" }),
      public_url: "https://example.com/file.pdf",
    }),
    "conflicting file + public_url"
  );
  assertTrue(
    conflictDocErr instanceof ApiClientError &&
      conflictDocErr.code === "CONFLICTING_SOURCE_INPUTS",
    "retryOrReplaceDocument blocks simultaneous file and public_url with CONFLICTING_SOURCE_INPUTS"
  );

  assertEqual(
    isValidIso4217Currency("USD"),
    true,
    "ISO 4217 accepts USD"
  );
  assertEqual(
    isValidIso4217Currency("ZZZ"),
    false,
    "ISO 4217 rejects synthetic ZZZ"
  );
  const badBillsImport = parseExistingBillsImportInput(
    '[{"charge_item":"Handling Charge","amount":120.00,"posting_type":"unknown"}]',
    { defaultAccountingEntity: "NEPTUNE SHIPPING LIMITED" }
  );
  assertEqual(
    badBillsImport.valid,
    false,
    "historical import refuses silent USD/AP defaults on missing currency & invalid posting_type"
  );
  if (!badBillsImport.valid) {
    assertTrue(
      badBillsImport.rowErrors.length >= 2,
      "historical import reports all row-level validation failures"
    );
  }
  results.push(
    "PASS 21: Strict pre-checks reject non-array bills, malformed ReviewBundleV1, file+URL conflict & missing currency/AR-AP"
  );

  // 22. A2: Concurrent draft version isolation during in-flight PATCH save + unsaved draft blocks approve/reject
  let vStore = createEmptyDraftStore();
  vStore = setRecordDraftField(vStore, "b-ver", recA, "amount", "111.00");
  const ver1 = getRecordDraftVersion(vStore, "b-ver", recA.id);
  assertTrue(ver1 !== null && hasRecordDraft(vStore, "b-ver", recA.id), "v1 draft active");
  // While v1 save is in-flight, user keeps typing v2:
  vStore = setRecordDraftField(vStore, "b-ver", recA, "amount", "111.05");
  const ver2 = getRecordDraftVersion(vStore, "b-ver", recA.id);
  assertTrue(ver2 !== null && ver2 > ver1!, "v2 draft version strictly incremented");

  // Simulate v1 save returning from server with amount = 111.00
  const recAtV1Server = {
    ...recA,
    charge_details: { ...recA.charge_details, amount: 111.0 },
  };
  vStore = clearRecordDraft(vStore, "b-ver", recA.id, ver1!, recAtV1Server);
  assertEqual(
    hasRecordDraft(vStore, "b-ver", recA.id),
    true,
    "completing stale v1 save must NOT wipe newer v2 user typing"
  );
  assertEqual(
    getEffectiveRecordFormValues(recAtV1Server, vStore, "b-ver").amount,
    "111.05",
    "newer v2 typed amount preserved intact"
  );
  // Now v2 save finishes with amount = 111.05
  const recAtV2Server = {
    ...recA,
    charge_details: { ...recA.charge_details, amount: 111.05 },
  };
  vStore = clearRecordDraft(vStore, "b-ver", recA.id, ver2!, recAtV2Server);
  assertEqual(
    hasRecordDraft(vStore, "b-ver", recA.id),
    false,
    "matching v2 save clears draft cleanly"
  );
  results.push(
    "PASS 22: Draft version isolation keeps concurrent edits alive when earlier PATCH completes"
  );

  // 23. F1 + F3: In-flight save preserves edit-back-to-old-value AND clears submitted review_note
  const f1BaseRec = {
    ...recA,
    id: "rec-f1-f3",
    charge_details: {
      ...recA.charge_details,
      quantity: 1,
      unit_price: 245,
      amount: 245,
    },
  };
  let f1Store = createEmptyDraftStore();
  f1Store = setRecordDraftField(f1Store, "b-f1", f1BaseRec, "amount", "246.01");
  f1Store = setRecordDraftField(
    f1Store,
    "b-f1",
    f1BaseRec,
    "review_note",
    "R2-AUDIT-ONCE"
  );
  const f1SaveVer = getRecordDraftVersion(f1Store, "b-f1", f1BaseRec.id);
  f1Store = beginRecordSave(f1Store, "b-f1", f1BaseRec.id, f1SaveVer);
  // While save (246.01 + R2-AUDIT-ONCE) is in-flight, user edits amount back to old server value 245:
  f1Store = setRecordDraftField(f1Store, "b-f1", f1BaseRec, "amount", "245");
  assertTrue(
    hasRecordDraft(f1Store, "b-f1", f1BaseRec.id),
    "F1: editing back to old server value while save in-flight must NOT prune draft"
  );
  assertEqual(
    getEffectiveRecordFormValues(f1BaseRec, f1Store, "b-f1").amount,
    "245",
    "F1: input keeps 245 during flight"
  );
  const f1UpdatedRec = {
    ...f1BaseRec,
    charge_details: { ...f1BaseRec.charge_details, amount: 246.01 },
  };
  f1Store = finishRecordSaveSuccess(
    f1Store,
    "b-f1",
    f1BaseRec.id,
    f1UpdatedRec,
    f1SaveVer ?? undefined
  );
  assertTrue(
    hasRecordDraft(f1Store, "b-f1", f1BaseRec.id),
    "F1: after server returns 246.01, reverted 245 remains marked unsaved"
  );
  const f1EffAfter = getEffectiveRecordFormValues(f1UpdatedRec, f1Store, "b-f1");
  assertEqual(
    f1EffAfter.amount,
    "245",
    "F1: effective amount remains 245 instead of reverting to 246.01"
  );
  assertEqual(
    f1EffAfter.review_note,
    "",
    "F3: untouched submitted review_note is cleared upon save success"
  );

  // F3: Save amount + R2-AUDIT-ONCE, edit quantity mid-flight -> second PATCH must NOT re-send R2-AUDIT-ONCE
  let f3Store = createEmptyDraftStore();
  f3Store = setRecordDraftField(f3Store, "b-f3", f1BaseRec, "amount", "246.01");
  f3Store = setRecordDraftField(
    f3Store,
    "b-f3",
    f1BaseRec,
    "review_note",
    "R2-AUDIT-ONCE"
  );
  const f3Ver = getRecordDraftVersion(f3Store, "b-f3", f1BaseRec.id);
  f3Store = beginRecordSave(f3Store, "b-f3", f1BaseRec.id, f3Ver);
  f3Store = setRecordDraftField(f3Store, "b-f3", f1BaseRec, "quantity", "2");
  f3Store = finishRecordSaveSuccess(
    f3Store,
    "b-f3",
    f1BaseRec.id,
    f1UpdatedRec,
    f3Ver ?? undefined
  );
  const f3SecondDiff = buildRecordDiffPatch(f1UpdatedRec, f3Store, "b-f3");
  assertEqual(f3SecondDiff.valid, true, "F3: second patch valid");
  assertEqual(
    stringifyLosslessJson(f3SecondDiff.patch),
    '{"quantity":2}',
    "F3: second PATCH sends ONLY quantity and omits old review_note"
  );

  // F1 + F3 for Document Totals as well + error pruning
  const f1DocBase = { ...doc1, total_amount: 245 };
  let f1DocStore = createEmptyDraftStore();
  f1DocStore = setDocTotalsDraftField(
    f1DocStore,
    "b-f1",
    f1DocBase,
    "total_amount",
    "246.01"
  );
  f1DocStore = setDocTotalsDraftField(
    f1DocStore,
    "b-f1",
    f1DocBase,
    "review_note",
    "DOC-AUDIT-ONCE"
  );
  f1DocStore = beginDocTotalsSave(f1DocStore, "b-f1", f1DocBase.id);
  f1DocStore = setDocTotalsDraftField(
    f1DocStore,
    "b-f1",
    f1DocBase,
    "total_amount",
    "245"
  );
  const f1DocUpdated = { ...f1DocBase, total_amount: 246.01 };
  f1DocStore = finishDocTotalsSaveSuccess(
    f1DocStore,
    "b-f1",
    f1DocBase.id,
    f1DocUpdated
  );
  assertTrue(
    hasDocTotalsDraft(f1DocStore, "b-f1", f1DocBase.id),
    "F1 doc totals: reverted 245 remains unsaved after server 246.01 returns"
  );
  const f1DocEff = getEffectiveDocTotalsValues(f1DocUpdated, f1DocStore, "b-f1");
  assertEqual(f1DocEff.total_amount, "245", "F1 doc totals: keeps 245");
  assertEqual(
    f1DocEff.review_note,
    "",
    "F3 doc totals: clears untouched submitted review_note"
  );

  // Error release: if save fails and user reverted value back to server value, finishSaveError prunes cleanly
  let errRecStore = setRecordDraftField(
    createEmptyDraftStore(),
    "b-err",
    f1BaseRec,
    "amount",
    "246.01"
  );
  errRecStore = beginRecordSave(errRecStore, "b-err", f1BaseRec.id);
  errRecStore = setRecordDraftField(
    errRecStore,
    "b-err",
    f1BaseRec,
    "amount",
    "245"
  );
  errRecStore = finishRecordSaveError(
    errRecStore,
    "b-err",
    f1BaseRec.id,
    f1BaseRec
  );
  assertEqual(
    hasRecordDraft(errRecStore, "b-err", f1BaseRec.id),
    false,
    "finishRecordSaveError prunes record draft matching server after releasing inFlight"
  );

  let errDocStore = setDocTotalsDraftField(
    createEmptyDraftStore(),
    "b-err",
    f1DocBase,
    "total_amount",
    "246.01"
  );
  errDocStore = beginDocTotalsSave(errDocStore, "b-err", f1DocBase.id);
  errDocStore = setDocTotalsDraftField(
    errDocStore,
    "b-err",
    f1DocBase,
    "total_amount",
    "245"
  );
  errDocStore = finishDocTotalsSaveError(errDocStore, "b-err", f1DocBase.id, f1DocBase);
  assertEqual(
    hasDocTotalsDraft(errDocStore, "b-err", f1DocBase.id),
    false,
    "finishDocTotalsSaveError prunes draft matching server after releasing inFlight"
  );
  results.push(
    "PASS 23: F1+F3 concurrent save edit-back-to-old-value retention & review_note single-submission"
  );

  // 24. F2: clearBatchDrafts & clearBatchScopedMap only clear target batch, preserving batch A drafts
  let f2Store = createEmptyDraftStore();
  f2Store = setRecordDraftField(f2Store, "batch-A", f1BaseRec, "amount", "299.99");
  f2Store = setDocTotalsDraftField(f2Store, "batch-A", f1DocBase, "total_amount", "299.99");
  f2Store = setRecordDraftField(f2Store, "batch-B", f1BaseRec, "amount", "888.88");
  f2Store = beginRecordSave(f2Store, "batch-B", f1BaseRec.id);
  f2Store = clearBatchDrafts(f2Store, "batch-B");
  assertEqual(
    hasRecordDraft(f2Store, "batch-B", f1BaseRec.id),
    false,
    "F2: batch-B record draft cleared on batch-B entity switch"
  );
  assertEqual(
    getEffectiveRecordFormValues(f1BaseRec, f2Store, "batch-A").amount,
    "299.99",
    "F2: batch-A record draft 299.99 preserved untouched"
  );
  assertEqual(
    getEffectiveDocTotalsValues(f1DocBase, f2Store, "batch-A").total_amount,
    "299.99",
    "F2: batch-A doc draft preserved untouched"
  );
  const f2AuditAfter = clearBatchScopedMap(
    { "batch-A::r1": { ok: true }, "batch-B::r1": { ok: true } },
    "batch-B"
  );
  assertTrue(
    "batch-A::r1" in f2AuditAfter && !("batch-B::r1" in f2AuditAfter),
    "F2: clearBatchScopedMap clears only target batch audit cache"
  );
  results.push("PASS 24: F2 per-batch draft & audit cache cleanup preserves sibling batches");

  // 25. F4: Active batch keeps scheduling next poll after discarding stale poll response on 409
  let f4Tracker = createPollRaceTracker("batch-poll-f4");
  const f4Poll1 = startPoll(f4Tracker, "batch-poll-f4");
  f4Tracker = f4Poll1.nextTracker;
  const f4Mut = startMutation(f4Tracker, "batch-poll-f4");
  f4Tracker = f4Mut.nextTracker;
  // Switch-entity hits 409 BATCH_PROCESSING during EXTRACTING
  f4Tracker = finishMutationError(f4Tracker);
  const f4Discard = acceptPollResult(f4Tracker, f4Poll1.ticket);
  f4Tracker = f4Discard.nextTracker;
  assertEqual(
    f4Discard.accepted,
    false,
    "F4: stale poll response before 409 mutation is discarded"
  );
  assertEqual(
    shouldContinuePollingAfterDiscard(f4Tracker, "batch-poll-f4", "EXTRACTING"),
    true,
    "F4: discarding stale poll while still active on batch-poll-f4 schedules next poll"
  );
  assertEqual(
    shouldContinuePollingAfterDiscard(f4Tracker, "batch-other", "EXTRACTING"),
    false,
    "F4: switched-away batch stops polling"
  );
  const f4Poll2 = startPoll(f4Tracker, "batch-poll-f4");
  f4Tracker = f4Poll2.nextTracker;
  const f4Accept2 = acceptPollResult(f4Tracker, f4Poll2.ticket);
  assertEqual(
    f4Accept2.accepted,
    true,
    "F4: rescheduled poll after discard accepts COMPLETED response"
  );
  results.push("PASS 25: F4 poll scheduler continues after discarding stale poll during active stage");

  // 26. F5 + B2: Existing bills filter race protection & 503 error state cleanup
  let billsState = createExistingBillsViewState("");
  const reqA = beginExistingBillsLoad(billsState, "NEPTUNE SHIPPING LIMITED");
  billsState = reqA.nextState;
  billsState = completeExistingBillsLoadSuccess(
    billsState,
    reqA.seq,
    "NEPTUNE SHIPPING LIMITED",
    "NEPTUNE SHIPPING LIMITED",
    [
      {
        id: "bill-n-1",
        accounting_entity: "NEPTUNE SHIPPING LIMITED",
        charge_item: "Handling Charge",
        amount: 245,
        currency: "USD",
        posting_type: "AP",
        version: 1,
        container_nos: [],
      },
    ],
    5
  ).nextState;
  assertEqual(
    getExistingBillsHeaderStatus(billsState).canClear,
    true,
    "B2: clear enabled when loaded filter matches activeFilter with items"
  );

  // User switches to ALL AMERICA; query returns 503 -> stale NEPTUNE counts wiped, clear disabled
  billsState = setExistingBillsFilter(billsState, "ALL AMERICA FORWARDING, INC.");
  assertEqual(
    getExistingBillsHeaderStatus(billsState).statusText,
    "查询中…",
    "B2: switching filter immediately clears stale count and shows 查询中…"
  );
  const reqB = beginExistingBillsLoad(billsState, "ALL AMERICA FORWARDING, INC.");
  billsState = reqB.nextState;
  billsState = completeExistingBillsLoadFailure(
    billsState,
    reqB.seq,
    "ALL AMERICA FORWARDING, INC.",
    "ALL AMERICA FORWARDING, INC.",
    "HTTP 503"
  ).nextState;
  const b2ErrHeader = getExistingBillsHeaderStatus(billsState);
  assertEqual(
    b2ErrHeader.statusText,
    "查询失败，暂无当前主体统计",
    "B2: 503 error removes previous entity counts from top bar"
  );
  assertEqual(b2ErrHeader.canClear, false, "B2: 503 error disables clear button");

  // F5: Stale NEPTUNE refresh response arriving after switch to ALL AMERICA is rejected
  const reqC = beginExistingBillsLoad(billsState, "ALL AMERICA FORWARDING, INC.");
  billsState = reqC.nextState;
  const staleNeptuneApply = completeExistingBillsLoadSuccess(
    billsState,
    reqA.seq,
    "NEPTUNE SHIPPING LIMITED",
    "ALL AMERICA FORWARDING, INC.",
    [],
    99
  );
  assertEqual(
    staleNeptuneApply.applied,
    false,
    "F5: stale NEPTUNE list response cannot overwrite ALL AMERICA view"
  );
  results.push("PASS 26: F5+B2 existing-bills filter race protection & 503 error cleanup");

  // 27. F6: Strict ReviewBundleV1 validation rejects coerced/invalid raw types before defaults
  const baseValidBundleObj = parseLosslessJson<Record<string, unknown>>(
    cliBundlePayloadText
  );
  const assertInvalidBundleVariant = (
    label: string,
    mutate: (clone: Record<string, unknown>) => void
  ) => {
    const clone = parseLosslessJson<Record<string, unknown>>(
      stringifyLosslessJson(baseValidBundleObj)
    );
    mutate(clone);
    const check = validateReviewBundleInput(stringifyLosslessJson(clone));
    assertEqual(check.valid, false, `F6 rejects ${label}`);
  };
  assertInvalidBundleVariant("numeric short_code 123", (b) => {
    b.short_code = 123;
  });
  assertInvalidBundleVariant("object short_code {}", (b) => {
    b.short_code = {};
  });
  assertInvalidBundleVariant("numeric exported_at", (b) => {
    b.exported_at = 999;
  });
  assertInvalidBundleVariant("object document_date {}", (b) => {
    const recs = b.records as Array<{ document_info: Record<string, unknown> }>;
    recs[0].document_info.document_date = {};
  });
  assertInvalidBundleVariant("array transaction_party.issuer", (b) => {
    const recs = b.records as Array<{ transaction_party: Record<string, unknown> }>;
    recs[0].transaction_party.issuer = [];
  });
  assertInvalidBundleVariant("numeric charge_details.currency", (b) => {
    const recs = b.records as Array<{ charge_details: Record<string, unknown> }>;
    recs[0].charge_details.currency = 840;
  });
  assertInvalidBundleVariant("array human_overrides", (b) => {
    const recs = b.records as Array<Record<string, unknown>>;
    recs[0].human_overrides = [];
  });
  assertInvalidBundleVariant("retry_count = 2 on document", (b) => {
    const docs = b.documents as Array<Record<string, unknown>>;
    docs[0].retry_count = 2;
  });
  assertInvalidBundleVariant("string ocr_suspicious on document", (b) => {
    const docs = b.documents as Array<Record<string, unknown>>;
    docs[0].ocr_suspicious = "true";
  });
  assertInvalidBundleVariant("empty object inside failed_documents", (b) => {
    b.failed_documents = [{}];
  });
  assertInvalidBundleVariant("HRK currency inside existing_bills_snapshot", (b) => {
    const bills = b.existing_bills_snapshot as Array<Record<string, unknown>>;
    bills[0].currency = "HRK";
  });

  // Valid bundle with CLF snapshot bill & RawDecimalLiteral passes
  const clfBundle = parseLosslessJson<Record<string, unknown>>(
    stringifyLosslessJson(baseValidBundleObj)
  );
  const clfBills = clfBundle.existing_bills_snapshot as Array<Record<string, unknown>>;
  clfBills[0].currency = "CLF";
  clfBills[0].amount = rawDecimal("9007199254740991.01");
  const clfBundleCheck = validateReviewBundleInput(
    stringifyLosslessJson(clfBundle)
  );
  assertEqual(
    clfBundleCheck.valid,
    true,
    "F6 accepts valid bundle with CLF bill and RawDecimalLiteral amount"
  );
  results.push(
    "PASS 27: F6 strict ReviewBundleV1 raw-type validation (no silent coercion on short_code/document_date)"
  );

  // 28. F7: Batch POST 202 succeeds + initial GET 503 retains new batch ID and recovers via GET only
  let f7PostCount = 0;
  let f7GetCount = 0;
  const f7Client = new BillingApiClient({
    transport: async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (method === "POST" && url.includes("/api/v1/batches")) {
        f7PostCount++;
        return new Response(
          stringifyLosslessJson({
            id: "batch-f7-new",
            short_code: "B-F7-01",
            accounting_entity: "NEPTUNE SHIPPING LIMITED",
            source_channel: "WEB",
            status: "PROCESSING",
            stage: "QUEUED",
            stage_message: "已入队",
            ocr_mode: "REMOTE_ONLY",
            total_files: 1,
            succeeded_files: 0,
            failed_files: 0,
            counts: {
              create: 0,
              update: 0,
              no_change: 0,
              pending_review: 0,
              rejected: 0,
            },
            created_at: "2026-11-20T08:00:00Z",
            updated_at: "2026-11-20T08:00:00Z",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } }
        );
      }
      if (method === "GET" && url.endsWith("/api/v1/batches/batch-f7-new")) {
        f7GetCount++;
        if (f7GetCount === 1) {
          return new Response(
            stringifyLosslessJson({
              code: "SERVICE_UNAVAILABLE",
              message: "临时 503 读取失败",
            }),
            { status: 503, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response(openApiBatchEdge01RawText, {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 404 });
    },
  });
  const f7Created = await f7Client.createBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    files: [new Blob(["%PDF-1.4"], { type: "application/pdf" })],
    wait: false,
  });
  assertEqual(f7Created.id, "batch-f7-new", "F7 POST returns created batch id");
  const f7InitialGetErr = await assertRejectsAsync(
    f7Client.getBatchDetail(f7Created.id),
    "F7 initial GET 503"
  );
  assertTrue(
    f7InitialGetErr instanceof ApiClientError && f7InitialGetErr.status === 503,
    "F7 initial detail GET surfaces 503 while retaining batch-f7-new"
  );
  const f7RetryGetDetail = await f7Client.getBatchDetail(f7Created.id);
  assertEqual(
    f7PostCount,
    1,
    "F7 retry flow triggers zero duplicate POSTs (POST count remains 1)"
  );
  assertEqual(f7GetCount, 2, "F7 retry invokes GET a second time");
  assertEqual(
    f7RetryGetDetail.id,
    "batch-edge-01",
    "F7 retry GET recovers full batch detail"
  );
  results.push(
    "PASS 28: F7 preserves created batch ID across detail GET 503 and recovers via GET only"
  );

  // 29. F8: Authoritative 176 active ISO 4217 whitelist matches backend (CLF ok, HRK/XXX/XTS rejected)
  assertEqual(ACTIVE_ISO4217_CODES.length, 176, "F8 exact 176 active ISO 4217 codes");
  assertEqual(ACTIVE_ISO4217_CURRENCIES.size, 176, "F8 set has 176 unique codes");
  assertEqual(isValidIso4217Currency("CLF"), true, "F8 accepts CLF");
  assertEqual(isValidIso4217Currency("HRK"), false, "F8 rejects retired HRK");
  assertEqual(isValidIso4217Currency("XXX"), false, "F8 rejects XXX");
  assertEqual(isValidIso4217Currency("XTS"), false, "F8 rejects XTS");
  const clfImportCheck = parseExistingBillsImportInput(
    '[{"charge_item":"Handling Charge","amount":120.00,"currency":"CLF","posting_type":"AP"}]',
    { defaultAccountingEntity: "NEPTUNE SHIPPING LIMITED" }
  );
  assertEqual(clfImportCheck.valid, true, "F8 allows CLF existing bill import");
  const hrkImportCheck = parseExistingBillsImportInput(
    '[{"charge_item":"Handling Charge","amount":120.00,"currency":"HRK","posting_type":"AP"}]',
    { defaultAccountingEntity: "NEPTUNE SHIPPING LIMITED" }
  );
  assertEqual(hrkImportCheck.valid, false, "F8 blocks HRK existing bill import");
  results.push(
    "PASS 29: F8 176-code ISO 4217 whitelist aligns with backend (CLF accepted, HRK/XXX/XTS rejected)"
  );

  for (const line of results) {
    console.log(line);
  }
}

const maybeProcess = (
  globalThis as { process?: { argv?: string[]; exitCode?: number } }
).process;
if (
  typeof window === "undefined" &&
  maybeProcess?.argv?.[1]?.includes("decimalJson.test")
) {
  runDecimalJsonTests().catch((err) => {
    console.error("TEST FAILED:", err);
    if (maybeProcess) {
      maybeProcess.exitCode = 1;
    }
  });
}
