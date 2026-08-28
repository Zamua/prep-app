// Every filter, global and test the templates use beyond what nunjucks
// ships. Each is a small formatting decision the pages depend on, so the
// definitions live here rather than being inlined per template.
import nunjucks from "nunjucks/browser/nunjucks-slim.js";
import type { Clock } from "../../../app/ports";
import { markdownHTML } from "../../../domain/markdown";

const { SafeString, copySafeness, markSafe } = nunjucks.runtime;

type Kwargs = Record<string, unknown> & { __keywords?: true };

function isKwargs(v: unknown): v is Kwargs {
  return typeof v === "object" && v !== null && Object.prototype.hasOwnProperty.call(v, "__keywords");
}

// ---- scalars --------------------------------------------------------------

// A float as text: the shortest round-trip digits, `.0` on integral values
// so a percentage does not read as a count, exponent form below 1e-4 and
// from 1e16, two-digit exponents.
export function floatText(value: unknown): string {
  const n = Number(value);
  if (Number.isNaN(n)) return "nan";
  if (n === Infinity) return "inf";
  if (n === -Infinity) return "-inf";
  if (n === 0) return Object.is(n, -0) ? "-0.0" : "0.0";
  const mag = Math.abs(n);
  if (mag >= 1e16 || mag < 1e-4) {
    const [mant, exp] = n.toExponential().split("e") as [string, string];
    const sign = exp.startsWith("-") ? "-" : "+";
    const digits = exp.replace(/^[+-]/, "").padStart(2, "0");
    return `${mant}e${sign}${digits}`;
  }
  if (Number.isInteger(n)) return `${n}.0`;
  return String(n);
}

// Any value a template can hold, as text. Nothing there prints as nothing:
// a missing attribute and a null column both read as absent on the page,
// not as a word a reader would take for content.
export function asText(value: unknown): string {
  if (value === undefined || value === null) return "";
  return String(value);
}

// A whole number, truncating a float or a float-shaped string. Anything
// unparseable is the fallback, so a template never renders NaN.
export function toInt(value: unknown, fallback = 0): number {
  if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) : fallback;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (value instanceof SafeString || typeof value === "string") {
    const s = String(value).trim();
    if (/^[+-]?\d+$/.test(s)) return parseInt(s, 10);
    const f = Number(s);
    return s !== "" && Number.isFinite(f) ? Math.trunc(f) : fallback;
  }
  return fallback;
}

// Rounding with ties on the exact binary value going to even, so 0.5 is 0
// and 2.5 is 2: a bar width rounded up on every tie drifts visibly wide. A
// tie at precision p is an odd multiple of 2^-(p+1), which the power-of-two
// scaling detects exactly.
export function roundHalfEven(value: unknown, precision = 0): number {
  const x = Number(value);
  if (!Number.isFinite(x)) return x;
  const p = Math.max(0, Math.trunc(precision));
  const scaled = x * 2 ** (p + 1);
  const tie = Number.isInteger(scaled) && Math.abs(scaled) % 2 === 1;
  const factor = 10 ** p;
  if (tie) {
    const lo = Math.floor(x * factor);
    const even = lo % 2 === 0 ? lo : lo + 1;
    return even / factor;
  }
  if (p === 0) return Math.round(x) === 0 ? 0 : Math.round(x);
  return Number(x.toFixed(p));
}

// ---- `%` formatting -------------------------------------------------------

const FORMAT_RE = /%([-+ 0#]*)(\d+)?(?:\.(\d+))?([sdifr%])/g;

function pad(s: string, flags: string, width: number | undefined, numeric: boolean): string {
  if (width === undefined || s.length >= width) return s;
  if (flags.includes("-")) return s.padEnd(width, " ");
  if (numeric && flags.includes("0")) {
    const sign = /^[+-]/.test(s) ? s[0]! : "";
    return sign + s.slice(sign.length).padStart(width - sign.length, "0");
  }
  return s.padStart(width, " ");
}

function signed(s: string, flags: string): string {
  if (s.startsWith("-")) return s;
  if (flags.includes("+")) return `+${s}`;
  if (flags.includes(" ")) return ` ${s}`;
  return s;
}

// `%s`, `%d`/`%i`, `%f` with flags, width and precision, and `%%`. `args`
// is one value or a list.
export function printf(fmt: string, args: unknown): string {
  const list = Array.isArray(args) ? args : [args];
  let i = 0;
  const next = (): unknown => {
    if (i >= list.length) throw new Error("not enough arguments for format string");
    return list[i++];
  };
  const out = fmt.replace(FORMAT_RE, (_m, flags: string, w: string | undefined, prec: string | undefined, conv: string) => {
    const width = w === undefined ? undefined : parseInt(w, 10);
    if (conv === "%") return "%";
    if (conv === "s" || conv === "r") {
      let s = asText(next());
      if (prec !== undefined) s = s.slice(0, parseInt(prec, 10));
      return pad(s, flags, width, false);
    }
    const n = Number(next());
    if (conv === "d" || conv === "i") {
      return pad(signed(String(Math.trunc(n)), flags), flags, width, true);
    }
    const p = prec === undefined ? 6 : parseInt(prec, 10);
    return pad(signed(roundHalfEven(n, p).toFixed(p), flags), flags, width, true);
  });
  if (i < list.length) throw new Error("not all arguments converted during string formatting");
  return out;
}

// ---- slices, lookups, containers ----------------------------------------

function sliceBounds(length: number, start: unknown, end: unknown): [number, number] {
  const norm = (v: unknown, dflt: number): number => {
    if (v === undefined || v === null) return dflt;
    let n = Math.trunc(Number(v));
    if (n < 0) n = Math.max(0, length + n);
    return Math.min(n, length);
  };
  return [norm(start, 0), norm(end, length)];
}

// A subrange of a string (by code point, so an emoji is not cut in half) or
// an array. A negative bound counts from the end.
export function sliceOf<T>(value: string | T[] | null | undefined, start?: unknown, end?: unknown): string | T[] | null | undefined {
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) {
    const [a, b] = sliceBounds(value.length, start, end);
    return value.slice(a, b);
  }
  const chars = Array.from(String(value));
  const [a, b] = sliceBounds(chars.length, start, end);
  const out = chars.slice(a, b).join("");
  return (copySafeness(value, out) as string);
}

// A lookup with a default, taken only when the key is absent: a stored null
// is a value, not a miss.
export function getDefault(obj: unknown, key: unknown, dflt: unknown = null): unknown {
  if (obj === null || obj === undefined) return dflt;
  if (obj instanceof Map) return obj.has(key) ? obj.get(key) : dflt;
  const k = String(key);
  return Object.prototype.hasOwnProperty.call(obj, k) ? (obj as Record<string, unknown>)[k] : dflt;
}

// Key/value pairs in insertion order. JS reorders integer-like keys on a
// plain object, so a table keyed by ids arrives as a Map or an entries
// array instead.
export function items(obj: unknown): [string, unknown][] {
  if (obj === null || obj === undefined) return [];
  if (obj instanceof Map) return [...obj.entries()].map(([k, v]) => [String(k), v]);
  return Object.entries(obj as Record<string, unknown>);
}

export function replaceAll(value: unknown, old: unknown, replacement: unknown): unknown {
  if (value === null || value === undefined) return value;
  const s = String(value);
  const out = old === "" ? s : s.split(String(old)).join(asText(replacement));
  return copySafeness(value, out);
}

export function join(value: unknown, sep: unknown = ""): string {
  if (!Array.isArray(value)) return asText(value);
  return value.map(asText).join(asText(sep));
}

// ---- JSON ------------------------------------------------------------------

function jsonString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]!;
    const code = s.charCodeAt(i);
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (code < 0x20 || code > 0x7e) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += ch;
  }
  return `${out}"`;
}

// Stable JSON: ASCII-only, `, ` and `: ` separators, keys sorted by code
// point, so the same context always embeds the same bytes. Integral floats
// cannot be told from ints once a context has crossed JSON, so they print
// as ints.
export function stableJson(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") {
    if (Number.isNaN(value)) return "NaN";
    if (!Number.isFinite(value)) return value > 0 ? "Infinity" : "-Infinity";
    return String(value);
  }
  if (typeof value === "string" || value instanceof SafeString) return jsonString(String(value));
  if (Array.isArray(value)) return `[${value.map(stableJson).join(", ")}]`;
  if (value instanceof Map) return stableJson(Object.fromEntries(value));
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${keys.map((k) => `${jsonString(k)}: ${stableJson(obj[k])}`).join(", ")}}`;
  }
  return jsonString(String(value));
}

// Safe to embed inside `<script>`: `<`, `>`, `&` and `'` never appear
// literally, so no payload can close the tag.
export function htmlsafeJson(value: unknown): string {
  return stableJson(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/'/g, "\\u0027");
}

// ---- time ------------------------------------------------------------------

const ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?)?(Z|[+-]\d{2}(?::?\d{2})?)?$/;

// The ISO 8601 shapes the app writes; a value with no offset is UTC.
// Returns epoch milliseconds, or null when unparseable.
export function parseIso(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const m = ISO_RE.exec(value);
  if (!m) return null;
  const [, y, mo, d, h, mi, s, frac, tz] = m;
  const month = Number(mo);
  const day = Number(d);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const ms = frac ? Math.floor(Number(frac.padEnd(6, "0")) / 1000) : 0;
  let t = Date.UTC(Number(y), month - 1, day, Number(h ?? 0), Number(mi ?? 0), Number(s ?? 0), ms);
  if (Number.isNaN(t)) return null;
  if (tz && tz !== "Z") {
    const sign = tz.startsWith("-") ? -1 : 1;
    const digits = tz.slice(1).replace(":", "");
    const offMin = Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4) || "0");
    t -= sign * offMin * 60_000;
  }
  return t;
}

export function relativeTime(value: unknown, now: Date): string {
  if (!value) return "";
  const at = parseIso(value);
  if (at === null) return typeof value === "string" ? value : "";
  const secs = Math.trunc((now.getTime() - at) / 1000);
  if (secs < 0) return "in the future";
  if (secs < 45) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? "1 hr ago" : `${hours} hrs ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return days === 1 ? "1 day ago" : `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} mo ago`;
  const years = Math.floor(days / 365);
  return years === 1 ? "1 yr ago" : `${years} yrs ago`;
}

const FIVE_YEARS = 5 * 365 * 86400;

export function wakesIn(value: unknown, now: Date): string {
  if (!value) return "";
  const at = parseIso(value);
  if (at === null) return "";
  const secs = Math.trunc((at - now.getTime()) / 1000);
  if (secs <= 0) return "";
  if (secs > FIVE_YEARS) return "forever";
  if (secs < 60) return "in <1 min";
  const mins = Math.floor((secs + 30) / 60);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.floor((mins + 30) / 60);
  if (hours < 24) return hours === 1 ? "in 1 hr" : `in ${hours} hrs`;
  const days = Math.floor((hours + 12) / 24);
  if (days === 1) return "tomorrow";
  if (days < 30) return `in ${days} days`;
  const months = Math.floor(days / 30);
  if (months < 12) return months === 1 ? "next month" : `in ${months} months`;
  const years = Math.floor(days / 365);
  return years === 1 ? "next year" : `in ${years} years`;
}

// ---- registration ---------------------------------------------------------

export interface FilterOptions {
  clock: Clock;
  root: string;
}

interface FilterEnv {
  addFilter(name: string, fn: (...args: any[]) => unknown): unknown;
  addGlobal(name: string, value: unknown): unknown;
  addTest(name: string, fn: (...args: any[]) => boolean): unknown;
}

export function registerFilters(env: FilterEnv, { clock, root }: FilterOptions): void {
  env.addGlobal("root", root);
  env.addGlobal("get", (obj: unknown, key: unknown, dflt?: unknown) =>
    getDefault(obj, key, isKwargs(dflt) ? null : dflt),
  );
  env.addGlobal("items", items);

  env.addFilter("format", (fmt: unknown, ...args: unknown[]) => {
    const plain = args.filter((a) => !isKwargs(a));
    return printf(String(fmt), plain.length === 1 ? plain[0] : plain);
  });
  env.addFilter("slice", (value: unknown, start?: unknown, end?: unknown) =>
    sliceOf(value as string | unknown[] | null | undefined, start, isKwargs(end) ? undefined : end),
  );
  env.addFilter("tojson", (value: unknown) => markSafe(htmlsafeJson(value)));
  env.addFilter("round", (value: unknown, precision?: unknown) =>
    roundHalfEven(value, isKwargs(precision) || precision === undefined ? 0 : Number(precision)),
  );
  env.addFilter("floattext", floatText);
  env.addFilter("int", (value: unknown, fallback?: unknown) =>
    toInt(value, isKwargs(fallback) || fallback === undefined ? 0 : Number(fallback)),
  );
  env.addFilter("string", (value: unknown) => copySafeness(value, asText(value)));
  env.addFilter("replace", (value: unknown, old: unknown, replacement: unknown) =>
    replaceAll(value, old, replacement),
  );
  env.addFilter("join", (value: unknown, sep?: unknown) => join(value, isKwargs(sep) ? "" : sep ?? ""));
  env.addFilter("markdown", (value: unknown) => markSafe(value ? markdownHTML(String(value)) : ""));
  env.addFilter("wakes_in", (value: unknown) => wakesIn(value, clock.now()));
  env.addFilter("relative_time", (value: unknown) => relativeTime(value, clock.now()));

  env.addTest("none", (value: unknown) => value === null);
}
