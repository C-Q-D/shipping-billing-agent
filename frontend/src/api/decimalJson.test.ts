import {
  ApiClientError,
  BillingApiClient,
  MAX_SINGLE_FILE_BYTES,
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
import {
  NO_EVAL_RESULT_LABEL,
  buildEvalRunViewModel,
  buildReviewBundleV1,
  formatEvalMetricValue,
  parseExistingBillsImportInput,
  validateAccountingEntityInput,
  validateChargeDictionaryInput,
  validateReviewBundleInput,
} from "./managementHelpers.ts";
import {
  acceptPollResult,
  buildDocTotalsDiffPatch,
  buildRecordDiffPatch,
  canWaiveValidationAlert,
  clearRecordDraft,
  createEmptyDraftStore,
  createPollRaceTracker,
  finishMutationSuccess,
  getEffectiveDocTotalsValues,
  getEffectiveRecordFormValues,
  hasRecordDraft,
  setDocTotalsDraftField,
  setRecordDraftField,
  setRecordWarningWaiver,
  startMutation,
  startPoll,
  validateReviewReason,
} from "./reviewState.ts";

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
  const capturedCalls: Array<{ url: string; method: string; bodyText?: string }> =
    [];
  const stubTransport = createDevStubTransport();
  const client = new BillingApiClient({
    transport: async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      capturedCalls.push({
        url,
        method: init?.method ?? "GET",
        bodyText: typeof init?.body === "string" ? init.body : undefined,
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
  const refreshedTarget2 = afterDocPatch2.records.find(
    (r) => r.id === targetRec.id
  )!;

  const patchedBatch2 = await client.patchBillingRecord(
    batch2.id,
    targetRec.id,
    diff.patch,
    { expectedRecordUpdatedAt: refreshedTarget2.updated_at ?? null }
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
      !(lastPatchCall.bodyText ?? "").includes("raw_charge_name"),
    "PATCH HTTP body carries unquoted 9007199254740991.01 & 0.0025 and omits raw_charge_name"
  );

  // Stale updated_at triggers 409 RECORD_CONFLICT
  const conflictErr = await assertRejectsAsync(
    client.patchBillingRecord(
      batch2.id,
      targetRec.id,
      { review_note: "stale overwrite attempt" },
      { expectedRecordUpdatedAt: targetRec.updated_at ?? null }
    ),
    "stale updated_at conflict"
  );
  assertTrue(
    conflictErr instanceof ApiClientError && conflictErr.status === 409,
    "stale optimistic lock triggers 409 RECORD_CONFLICT"
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
    "PASS 10: Diff-only PATCH, read-only raw_charge_name, 409 conflict & lossless save->reload->export->download"
  );

  // 11. 6-record mixed batch 3/5/6 counts, 422 BLOCKER guard, per-item WARNING waiver, 2-500 reason, doc repair & on-demand audit
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

  // Validate BLOCKER cannot be waived while soft WARNING can be waived by exact (code, field, actual)
  const blockerRec = batch3.records.find((r) => r.id === "rec-precision-blocker")!;
  const blockerAlert = blockerRec.validation_alerts!.find(
    (a) => a.severity === "BLOCKER"
  )!;
  const warningAlertOnBlocker = blockerRec.validation_alerts!.find(
    (a) => a.severity === "WARNING"
  )!;
  assertEqual(canWaiveValidationAlert(blockerAlert), false, "BLOCKER cannot be waived");
  assertEqual(
    canWaiveValidationAlert(warningAlertOnBlocker),
    true,
    "CONTAINER_ISO6346_MISMATCH WARNING is waivable"
  );
  assertTrue(validateReviewReason("a") !== null, "1-char reason rejected");
  assertTrue(validateReviewReason("核对无误") === null, "valid reason accepted");

  // Client rejects waiver attempt on BLOCKER
  let badWaiveStore = setRecordWarningWaiver(createEmptyDraftStore(), batch3.id, blockerRec.id, {
    code: blockerAlert.code,
    field: blockerAlert.field ?? null,
    actual: blockerAlert.actual ?? null,
    reason: "强行跳过总计错误",
  });
  const badWaiveDiff = buildRecordDiffPatch(blockerRec, badWaiveStore, batch3.id);
  assertEqual(badWaiveDiff.valid, false, "client rejects waiving BLOCKER");

  // Server also rejects waiving BLOCKER with 422
  const serverWaiveErr = await assertRejectsAsync(
    client.patchBillingRecord(
      batch3.id,
      blockerRec.id,
      { review_note: "try waive blocker" },
      {
        resolvedWarnings: [
          {
            code: blockerAlert.code,
            field: blockerAlert.field ?? null,
            actual: blockerAlert.actual ?? null,
            reason: "试图强行解除红灯",
          },
        ],
      }
    ),
    "422 CANNOT_WAIVE_HARD_BLOCKER"
  );
  assertTrue(
    serverWaiveErr instanceof ApiClientError && serverWaiveErr.status === 422,
    "server rejects BLOCKER waiver with 422"
  );

  // Soft warning record: waive by exact (code, field, actual) -> transitions to CREATE/AUTO_READY
  const softRec = batch3.records.find((r) => r.id === "rec-soft-warning")!;
  const softWarning = softRec.validation_alerts![0];
  let softStore = setRecordWarningWaiver(createEmptyDraftStore(), batch3.id, softRec.id, {
    code: softWarning.code,
    field: softWarning.field ?? null,
    actual: softWarning.actual ?? null,
    reason: "已核对提单原件，历史非标箱号特例放行",
  });
  const softDiff = buildRecordDiffPatch(softRec, softStore, batch3.id);
  assertEqual(softDiff.valid, true, "soft warning waiver valid");
  assertEqual(softDiff.resolvedWarnings.length, 1, "1 resolved warning produced");
  const afterSoftWaive = await client.patchBillingRecord(
    batch3.id,
    softRec.id,
    softDiff.patch,
    {
      expectedRecordUpdatedAt: softRec.updated_at ?? null,
      resolvedWarnings: softDiff.resolvedWarnings,
    }
  );
  const updatedSoftRec = afterSoftWaive.records.find((r) => r.id === softRec.id)!;
  assertEqual(updatedSoftRec.review_status, "AUTO_READY", "soft waiver clears NEEDS_REVIEW");
  assertEqual(
    updatedSoftRec.validation_alerts![0].severity,
    "RESOLVED",
    "warning marked RESOLVED"
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
      (softAudit.audit_trail.human_review_history as unknown[]).length >= 1,
    "audit mode exposes human_review_history"
  );
  results.push(
    "PASS 11: 6-record 3/5/6 export counts, 422 BLOCKER guard, soft WARNING waiver, doc retry/force-parse & audit history"
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
  const schemaMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "schema_valid_rate"
  )!;
  const exactMetric = offlineVm.truthMetrics.find(
    (m) => m.key === "field_exact_match"
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
  assertEqual(fpMetric.displayText, "0.0%", "COMPLETED legitimate 0 false positive rate formats 0.0%");
  assertEqual(passAtKMetric.displayText, "100.0%", "COMPLETED pass@3 formats 100.0%");
  assertEqual(passAtKMetric.fractionText, "12 / 12", "COMPLETED pass@3 shows 12 / 12");
  results.push(
    "PASS 14: Harness eval formatting (UNVERIFIED/FAILED/null -> 无结果, COMPLETED percentages & fractions)"
  );

  // 15. ReviewBundleV1 ("1.0") export -> import round-trip preserving 9007199254740991.01 / 0.0025, review history & XSS plain text
  const invalidBundleCheck = validateReviewBundleInput(
    '{"bundle_version":"2.0","accounting_entity":"NEPTUNE","documents":[],"records":[]}'
  );
  assertEqual(invalidBundleCheck.valid, false, "unsupported bundle_version 2.0 rejected");

  const bundleSourceQueued = await client.createDemoBatch({
    accounting_entity: "NEPTUNE SHIPPING LIMITED",
    ocr_mode: "REMOTE_ONLY",
    wait: false,
  });
  let bundleSource = await client.getBatchDetail(bundleSourceQueued.id);
  while (bundleSource.stage !== "COMPLETED") {
    bundleSource = await client.getBatchDetail(bundleSourceQueued.id);
  }
  await client.retryOrReplaceDocument(bundleSource.id, "doc-fail-zero", {
    ocr_mode: "REMOTE_WITH_LOCAL_FALLBACK",
  });
  bundleSource = await client.approveBillingRecord(
    bundleSource.id,
    "rec-soft-warning",
    "复核包互通测试批准原因"
  );
  const sourceAuditExport = await client.exportBatchJson(bundleSource.id, "audit");
  const sourceBillsSnapshot = await client.listExistingBills(
    bundleSource.accounting_entity
  );

  const builtBundle = buildReviewBundleV1(
    bundleSource,
    sourceBillsSnapshot,
    sourceAuditExport
  );
  assertEqual(builtBundle.bundle_version, "1.0", 'bundle_version strictly "1.0"');
  const serializedBundle = stringifyLosslessJson(builtBundle);
  assertTrue(
    serializedBundle.includes('"bundle_version":"1.0"') &&
      serializedBundle.includes("9007199254740991.01") &&
      serializedBundle.includes("0.0025"),
    "serialized ReviewBundleV1 keeps bundle_version 1.0 and lossless decimals"
  );

  const validatedImport = validateReviewBundleInput(serializedBundle);
  assertEqual(validatedImport.valid, true, "serialized bundle passes pre-import check");
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
    "imported ReviewBundleV1 restores human_review_history cleanly"
  );
  const importedFailDocAudit = importedAudit.records.find(
    (r) => r.audit_trail.source_doc_id === "doc-eml-1"
  );
  assertTrue(
    importedFailDocAudit !== undefined,
    "imported audit export intact"
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
    "PASS 15: ReviewBundleV1 (1.0) export->import round-trip, 9007199254740991.01/0.0025, review history & XSS plain text"
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
