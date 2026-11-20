import {
  ApiClientError,
  BillingApiClient,
  MAX_SINGLE_FILE_BYTES,
  validateBatchIngestionInput,
} from "./client.ts";
import {
  DecimalSyntaxError,
  formatLosslessAmount,
  parseLosslessJson,
  rawDecimal,
  readDecimalString,
  stringifyLosslessJson,
} from "./decimalJson.ts";
import { createDevStubTransport } from "./devStubTransport.ts";

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
