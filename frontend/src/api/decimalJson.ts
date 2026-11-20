export const LOSSLESS_DECIMALS_SYMBOL = Symbol.for(
  "shipping_billing.lossless_decimals"
);

const DECIMAL_INPUT_PATTERN =
  /^([+-]?)(\d+)(?:\.(\d+))?(?:([eE][+-]?\d+))?$/;

const MAX_JSON_DEPTH = 512;

type DecimalMap = Record<string, string>;

interface WithDecimalMeta {
  [LOSSLESS_DECIMALS_SYMBOL]?: DecimalMap;
}

export class DecimalSyntaxError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DecimalSyntaxError";
  }
}

export class RawDecimalLiteral {
  readonly raw: string;

  constructor(input: string | number | RawDecimalLiteral) {
    if (input instanceof RawDecimalLiteral) {
      this.raw = input.raw;
      return;
    }

    if (typeof input === "number") {
      if (!Number.isFinite(input)) {
        throw new DecimalSyntaxError("数值必须为有限十进制数字");
      }
      this.raw = Object.is(input, -0) ? "0" : String(input);
      return;
    }

    if (typeof input !== "string") {
      throw new DecimalSyntaxError("金额或数值输入必须为合法十进制字符串");
    }

    const trimmed = input.trim();
    if (!trimmed) {
      throw new DecimalSyntaxError("十进制数字不能为空");
    }

    const match = DECIMAL_INPUT_PATTERN.exec(trimmed);
    if (!match) {
      throw new DecimalSyntaxError(
        `非法十进制数值 "${input}"，仅允许合法数字字符`
      );
    }

    const [, rawSign, intPart, fracPart, expPart] = match;
    const normalizedInt = intPart.replace(/^0+(?=\d)/, "");
    const isAllZero =
      normalizedInt === "0" &&
      (fracPart === undefined || /^0+$/.test(fracPart));

    const sign = rawSign === "-" && !isAllZero ? "-" : "";
    const fracSegment = fracPart !== undefined ? `.${fracPart}` : "";
    const expSegment = expPart ?? "";

    this.raw = `${sign}${normalizedInt}${fracSegment}${expSegment}`;
  }

  toString(): string {
    return this.raw;
  }

  valueOf(): number {
    return Number(this.raw);
  }

  toJsonLiteral(): string {
    return this.raw;
  }
}

export function rawDecimal(
  input: string | number | RawDecimalLiteral
): RawDecimalLiteral {
  return input instanceof RawDecimalLiteral
    ? input
    : new RawDecimalLiteral(input);
}

/**
 * 将十进制输入桥接为符合 schema.d.ts number 类型的出站值；
 * 实际经 stringifyLosslessJson 序列化时将输出不带引号的原样 JSON 数字字面量。
 */
export function toSchemaDecimal(
  input: string | number | RawDecimalLiteral
): number {
  return rawDecimal(input) as unknown as number;
}

export function tryNormalizeDecimalInput(
  input: string | number | RawDecimalLiteral | null | undefined
): RawDecimalLiteral | null {
  if (input === null || input === undefined) {
    return null;
  }
  if (typeof input === "string" && input.trim() === "") {
    return null;
  }
  return rawDecimal(input);
}

function attachDecimalMeta(target: object, decimalMap: DecimalMap): void {
  if (Object.keys(decimalMap).length === 0) {
    return;
  }
  Object.defineProperty(target, LOSSLESS_DECIMALS_SYMBOL, {
    value: decimalMap,
    enumerable: false,
    writable: true,
    configurable: true,
  });
}

export function copyDecimalMeta<T extends object>(source: unknown, target: T): T {
  if (source === null || source === undefined || typeof source !== "object") {
    return target;
  }
  const map = (source as WithDecimalMeta)[LOSSLESS_DECIMALS_SYMBOL];
  if (map && typeof map === "object") {
    attachDecimalMeta(target, { ...map });
  }
  return target;
}

function readStoredLiteral(
  holder: unknown,
  key: string | number
): string | undefined {
  if (holder === null || holder === undefined || typeof holder !== "object") {
    return undefined;
  }
  const map = (holder as WithDecimalMeta)[LOSSLESS_DECIMALS_SYMBOL];
  if (!map) {
    return undefined;
  }
  const keyStr = String(key);
  if (!Object.prototype.hasOwnProperty.call(map, keyStr)) {
    return undefined;
  }
  return map[keyStr];
}

export function readDecimalString(
  holder: unknown,
  key: string | number
): string | null {
  if (holder === null || holder === undefined || typeof holder !== "object") {
    return null;
  }
  const propVal = (holder as Record<string | number, unknown>)[key];
  if (propVal === null || propVal === undefined) {
    return null;
  }
  if (propVal instanceof RawDecimalLiteral) {
    return propVal.raw;
  }
  if (typeof propVal === "number") {
    const stored = readStoredLiteral(holder, key);
    if (stored !== undefined && Number(stored) === propVal) {
      return stored;
    }
    if (!Number.isFinite(propVal)) {
      return null;
    }
    return Object.is(propVal, -0) ? "0" : String(propVal);
  }
  if (typeof propVal === "string") {
    return propVal;
  }
  return null;
}

export function formatDecimalLiteral(
  literal: string | null | undefined,
  fallback = "—"
): string {
  if (literal === null || literal === undefined) {
    return fallback;
  }
  const trimmed = literal.trim();
  if (!trimmed) {
    return fallback;
  }
  if (/[eE]/.test(trimmed)) {
    return trimmed;
  }
  const dotIndex = trimmed.indexOf(".");
  if (dotIndex === -1) {
    return `${trimmed}.00`;
  }
  const fracLength = trimmed.length - dotIndex - 1;
  if (fracLength === 0) {
    return `${trimmed}00`;
  }
  if (fracLength === 1) {
    return `${trimmed}0`;
  }
  return trimmed;
}

export function formatLosslessAmount(
  holder: unknown,
  key: string | number,
  fallback = "—"
): string {
  return formatDecimalLiteral(readDecimalString(holder, key), fallback);
}

interface ParsedNumberValue {
  isNumber: true;
  raw: string;
  value: number;
}

interface ParsedOtherValue {
  isNumber: false;
  value: unknown;
}

type ParsedNode = ParsedNumberValue | ParsedOtherValue;

class LosslessJsonParser {
  private readonly text: string;
  private pos = 0;

  constructor(text: string) {
    this.text = text;
  }

  parseRoot(): unknown {
    this.skipWhitespace();
    const node = this.parseValue(0);
    this.skipWhitespace();
    if (this.pos < this.text.length) {
      throw new SyntaxError(
        `Unexpected token '${this.text[this.pos]}' in JSON at position ${this.pos}`
      );
    }
    return node.value;
  }

  private skipWhitespace(): void {
    while (this.pos < this.text.length) {
      const code = this.text.charCodeAt(this.pos);
      if (code === 0x20 || code === 0x09 || code === 0x0a || code === 0x0d) {
        this.pos++;
      } else {
        break;
      }
    }
  }

  private parseValue(depth: number): ParsedNode {
    if (depth > MAX_JSON_DEPTH) {
      throw new SyntaxError("JSON nesting depth exceeds safe limit");
    }
    this.skipWhitespace();
    if (this.pos >= this.text.length) {
      throw new SyntaxError("Unexpected end of JSON input");
    }
    const ch = this.text[this.pos];
    if (ch === "{") {
      return { isNumber: false, value: this.parseObject(depth + 1) };
    }
    if (ch === "[") {
      return { isNumber: false, value: this.parseArray(depth + 1) };
    }
    if (ch === '"') {
      return { isNumber: false, value: this.parseString() };
    }
    if (ch === "t") {
      this.consumeLiteral("true");
      return { isNumber: false, value: true };
    }
    if (ch === "f") {
      this.consumeLiteral("false");
      return { isNumber: false, value: false };
    }
    if (ch === "n") {
      this.consumeLiteral("null");
      return { isNumber: false, value: null };
    }
    if (ch === "-" || (ch >= "0" && ch <= "9")) {
      return this.parseNumber();
    }
    throw new SyntaxError(
      `Unexpected token '${ch}' in JSON at position ${this.pos}`
    );
  }

  private parseObject(depth: number): Record<string, unknown> {
    this.pos++; // consume '{'
    this.skipWhitespace();

    const obj: Record<string, unknown> = {};
    const decimalMap: DecimalMap = {};

    if (this.text[this.pos] === "}") {
      this.pos++;
      return obj;
    }

    while (true) {
      this.skipWhitespace();
      if (this.text[this.pos] !== '"') {
        throw new SyntaxError(
          `Expected property name in JSON object at position ${this.pos}`
        );
      }
      const key = this.parseString();

      this.skipWhitespace();
      if (this.text[this.pos] !== ":") {
        throw new SyntaxError(
          `Expected ':' after property name in JSON object at position ${this.pos}`
        );
      }
      this.pos++;

      const valNode = this.parseValue(depth);
      Object.defineProperty(obj, key, {
        value: valNode.value,
        enumerable: true,
        writable: true,
        configurable: true,
      });
      if (valNode.isNumber) {
        decimalMap[key] = valNode.raw;
      }

      this.skipWhitespace();
      const next = this.text[this.pos];
      if (next === "}") {
        this.pos++;
        attachDecimalMeta(obj, decimalMap);
        return obj;
      }
      if (next !== ",") {
        throw new SyntaxError(
          `Expected ',' or '}' in JSON object at position ${this.pos}`
        );
      }
      this.pos++;
    }
  }

  private parseArray(depth: number): unknown[] {
    this.pos++; // consume '['
    this.skipWhitespace();

    const arr: unknown[] = [];
    const decimalMap: DecimalMap = {};

    if (this.text[this.pos] === "]") {
      this.pos++;
      return arr;
    }

    let index = 0;
    while (true) {
      const itemNode = this.parseValue(depth);
      arr.push(itemNode.value);
      if (itemNode.isNumber) {
        decimalMap[String(index)] = itemNode.raw;
      }
      index++;

      this.skipWhitespace();
      const next = this.text[this.pos];
      if (next === "]") {
        this.pos++;
        attachDecimalMeta(arr, decimalMap);
        return arr;
      }
      if (next !== ",") {
        throw new SyntaxError(
          `Expected ',' or ']' in JSON array at position ${this.pos}`
        );
      }
      this.pos++;
    }
  }

  private parseString(): string {
    const start = this.pos;
    this.pos++; // consume opening quote

    while (this.pos < this.text.length) {
      const ch = this.text.charCodeAt(this.pos);
      if (ch === 0x22) {
        this.pos++;
        return JSON.parse(this.text.slice(start, this.pos)) as string;
      }
      if (ch === 0x5c) {
        this.pos++;
        if (this.pos >= this.text.length) {
          throw new SyntaxError("Unterminated escape sequence in JSON string");
        }
        const esc = this.text[this.pos];
        if (esc === "u") {
          const hex = this.text.slice(this.pos + 1, this.pos + 5);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
            throw new SyntaxError(
              `Invalid unicode escape in JSON string at position ${this.pos}`
            );
          }
          this.pos += 5;
        } else if ('"\\/bfnrt'.includes(esc)) {
          this.pos++;
        } else {
          throw new SyntaxError(
            `Invalid escape character '${esc}' in JSON string at position ${this.pos}`
          );
        }
        continue;
      }
      if (ch < 0x20) {
        throw new SyntaxError(
          `Unescaped control character in JSON string at position ${this.pos}`
        );
      }
      this.pos++;
    }

    throw new SyntaxError("Unterminated string in JSON input");
  }

  private parseNumber(): ParsedNumberValue {
    const start = this.pos;

    if (this.text[this.pos] === "-") {
      this.pos++;
    }

    if (this.pos >= this.text.length) {
      throw new SyntaxError(`Invalid number at position ${start}`);
    }

    if (this.text[this.pos] === "0") {
      this.pos++;
      const afterZero = this.text[this.pos];
      if (afterZero !== undefined && afterZero >= "0" && afterZero <= "9") {
        throw new SyntaxError(
          `Leading zero is not allowed in JSON number at position ${start}`
        );
      }
    } else if (this.text[this.pos] >= "1" && this.text[this.pos] <= "9") {
      this.pos++;
      while (
        this.pos < this.text.length &&
        this.text[this.pos] >= "0" &&
        this.text[this.pos] <= "9"
      ) {
        this.pos++;
      }
    } else {
      throw new SyntaxError(`Invalid number at position ${start}`);
    }

    if (this.text[this.pos] === ".") {
      this.pos++;
      if (
        this.pos >= this.text.length ||
        this.text[this.pos] < "0" ||
        this.text[this.pos] > "9"
      ) {
        throw new SyntaxError(
          `Expected fractional digit after '.' at position ${this.pos}`
        );
      }
      while (
        this.pos < this.text.length &&
        this.text[this.pos] >= "0" &&
        this.text[this.pos] <= "9"
      ) {
        this.pos++;
      }
    }

    if (this.text[this.pos] === "e" || this.text[this.pos] === "E") {
      this.pos++;
      if (this.text[this.pos] === "+" || this.text[this.pos] === "-") {
        this.pos++;
      }
      if (
        this.pos >= this.text.length ||
        this.text[this.pos] < "0" ||
        this.text[this.pos] > "9"
      ) {
        throw new SyntaxError(
          `Expected exponent digit in JSON number at position ${this.pos}`
        );
      }
      while (
        this.pos < this.text.length &&
        this.text[this.pos] >= "0" &&
        this.text[this.pos] <= "9"
      ) {
        this.pos++;
      }
    }

    const raw = this.text.slice(start, this.pos);
    return {
      isNumber: true,
      raw,
      value: Number(raw),
    };
  }

  private consumeLiteral(expected: string): void {
    if (this.text.startsWith(expected, this.pos)) {
      this.pos += expected.length;
      return;
    }
    throw new SyntaxError(
      `Unexpected token in JSON at position ${this.pos}, expected '${expected}'`
    );
  }
}

export function parseLosslessJson<T = unknown>(jsonText: string): T {
  if (typeof jsonText !== "string") {
    throw new SyntaxError("JSON input must be a string");
  }
  const parser = new LosslessJsonParser(jsonText);
  return parser.parseRoot() as T;
}

function serializeLosslessValue(
  value: unknown,
  rawOverride: string | undefined,
  indentStep: string,
  depth: number
): string | undefined {
  if (rawOverride !== undefined) {
    return rawOverride;
  }
  if (value instanceof RawDecimalLiteral) {
    return value.toJsonLiteral();
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new DecimalSyntaxError("无法将 NaN 或 Infinity 序列化为 JSON 数字");
    }
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (
    typeof value === "undefined" ||
    typeof value === "function" ||
    typeof value === "symbol"
  ) {
    return undefined;
  }
  if (value instanceof Date) {
    return JSON.stringify(value.toISOString());
  }

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    const childIndent = indentStep ? indentStep.repeat(depth + 1) : "";
    const closingIndent = indentStep ? indentStep.repeat(depth) : "";
    const items: string[] = [];

    for (let i = 0; i < value.length; i++) {
      const itemVal = value[i];
      const stored =
        typeof itemVal === "number" ? readStoredLiteral(value, i) : undefined;
      const override =
        stored !== undefined && Number(stored) === itemVal ? stored : undefined;
      const serialized = serializeLosslessValue(
        itemVal,
        override,
        indentStep,
        depth + 1
      );
      items.push(serialized ?? "null");
    }

    if (!indentStep) {
      return `[${items.join(",")}]`;
    }
    return `[\n${childIndent}${items.join(`,\n${childIndent}`)}\n${closingIndent}]`;
  }

  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length === 0) {
      return "{}";
    }
    const childIndent = indentStep ? indentStep.repeat(depth + 1) : "";
    const closingIndent = indentStep ? indentStep.repeat(depth) : "";
    const gap = indentStep ? " " : "";
    const pairs: string[] = [];

    for (const key of keys) {
      const propVal = obj[key];
      const stored =
        typeof propVal === "number" ? readStoredLiteral(obj, key) : undefined;
      const override =
        stored !== undefined && Number(stored) === propVal ? stored : undefined;
      const serialized = serializeLosslessValue(
        propVal,
        override,
        indentStep,
        depth + 1
      );
      if (serialized !== undefined) {
        pairs.push(`${JSON.stringify(key)}:${gap}${serialized}`);
      }
    }

    if (pairs.length === 0) {
      return "{}";
    }
    if (!indentStep) {
      return `{${pairs.join(",")}}`;
    }
    return `{\n${childIndent}${pairs.join(`,\n${childIndent}`)}\n${closingIndent}}`;
  }

  return undefined;
}

export function stringifyLosslessJson(value: unknown, space?: number): string {
  const clamped =
    typeof space === "number" && Number.isFinite(space)
      ? Math.max(0, Math.min(10, Math.floor(space)))
      : 0;
  const indentStep = clamped > 0 ? " ".repeat(clamped) : "";
  const result = serializeLosslessValue(value, undefined, indentStep, 0);
  return result ?? "undefined";
}

interface DecimalParts {
  sign: 1 | -1;
  coeff: bigint;
  scale: number;
}

function parseDecimalParts(input: string): DecimalParts {
  const normalized = rawDecimal(input).raw;
  const match = DECIMAL_INPUT_PATTERN.exec(normalized);
  if (!match) {
    throw new DecimalSyntaxError(`非法十进制数值 "${input}"`);
  }
  const [, rawSign, intPart, fracPart = "", expPart = ""] = match;
  const exponent = expPart ? Number.parseInt(expPart.slice(1), 10) : 0;
  const digits = `${intPart}${fracPart}`;
  let coeff = BigInt(digits);
  if (coeff === 0n) {
    return { sign: 1, coeff: 0n, scale: 0 };
  }
  let scale = fracPart.length - exponent;
  if (scale < 0) {
    coeff *= 10n ** BigInt(-scale);
    scale = 0;
  }
  return {
    sign: rawSign === "-" ? -1 : 1,
    coeff,
    scale,
  };
}

function alignDecimalParts(
  a: DecimalParts,
  b: DecimalParts
): { aVal: bigint; bVal: bigint; scale: number } {
  const scale = Math.max(a.scale, b.scale);
  const aScaled =
    a.coeff * (scale > a.scale ? 10n ** BigInt(scale - a.scale) : 1n);
  const bScaled =
    b.coeff * (scale > b.scale ? 10n ** BigInt(scale - b.scale) : 1n);
  return {
    aVal: BigInt(a.sign) * aScaled,
    bVal: BigInt(b.sign) * bScaled,
    scale,
  };
}

function formatBigIntDecimal(value: bigint, scale: number): string {
  if (value === 0n) {
    return scale > 0 ? `0.${"0".repeat(scale)}` : "0";
  }
  const signPrefix = value < 0n ? "-" : "";
  const absStr = (value < 0n ? -value : value).toString();
  if (scale <= 0) {
    return `${signPrefix}${absStr}`;
  }
  if (absStr.length <= scale) {
    return `${signPrefix}0.${"0".repeat(scale - absStr.length)}${absStr}`;
  }
  const splitIdx = absStr.length - scale;
  return `${signPrefix}${absStr.slice(0, splitIdx)}.${absStr.slice(splitIdx)}`;
}

export function compareDecimalStrings(a: string, b: string): -1 | 0 | 1 {
  const { aVal, bVal } = alignDecimalParts(
    parseDecimalParts(a),
    parseDecimalParts(b)
  );
  if (aVal < bVal) return -1;
  if (aVal > bVal) return 1;
  return 0;
}

export function addDecimalStrings(a: string, b: string): string {
  const { aVal, bVal, scale } = alignDecimalParts(
    parseDecimalParts(a),
    parseDecimalParts(b)
  );
  return formatBigIntDecimal(aVal + bVal, scale);
}

export function subtractDecimalStrings(a: string, b: string): string {
  const { aVal, bVal, scale } = alignDecimalParts(
    parseDecimalParts(a),
    parseDecimalParts(b)
  );
  return formatBigIntDecimal(aVal - bVal, scale);
}

export function multiplyDecimalStrings(a: string, b: string): string {
  const pa = parseDecimalParts(a);
  const pb = parseDecimalParts(b);
  const product = BigInt(pa.sign * pb.sign) * pa.coeff * pb.coeff;
  const scale = pa.scale + pb.scale;
  return formatBigIntDecimal(product, scale);
}

export function isWithinDecimalTolerance(
  a: string,
  b: string,
  tolerance = "0.02"
): boolean {
  const { aVal, bVal, scale } = alignDecimalParts(
    parseDecimalParts(a),
    parseDecimalParts(b)
  );
  const diff = aVal >= bVal ? aVal - bVal : bVal - aVal;
  const diffFormatted = formatBigIntDecimal(diff, scale);
  return compareDecimalStrings(diffFormatted, tolerance) <= 0;
}
