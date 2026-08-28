// Parsing a multi-select answer and reducing it to a set. Written out
// instead of JSON.parse because the feedback has to tell 1 from 1.0 and
// has to keep an object's key order; JSON.parse keeps neither.

/** A value that can be a set element: bigint is an int, number a float. */
export type Scalar = string | null | boolean | bigint | number;
export type JsonValue = Scalar | JsonValue[] | JsonObject;

/** A parsed object: only its keys survive, in first-insertion order. */
export class JsonObject {
  constructor(readonly keys: string[]) {}
}

export class AnswerJsonError extends Error {}
export class AnswerShapeError extends Error {}

const NUMBER = /-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/y;
const WS = /[ \t\n\r]*/y;
const ESCAPES: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' };

class Parser {
  i = 0;
  constructor(private readonly s: string) {}

  fail(what: string): never {
    throw new AnswerJsonError(`${what} at ${this.i}`);
  }

  ws(): void {
    WS.lastIndex = this.i;
    WS.exec(this.s);
    this.i = WS.lastIndex;
  }

  literal(word: string, value: Scalar): Scalar {
    if (!this.s.startsWith(word, this.i)) this.fail('expecting value');
    this.i += word.length;
    return value;
  }

  value(): JsonValue {
    const c = this.s[this.i];
    switch (c) {
      case '{': return this.object();
      case '[': return this.array();
      case '"': return this.string();
      case 't': return this.literal('true', true);
      case 'f': return this.literal('false', false);
      case 'n': return this.literal('null', null);
      case 'N': return this.literal('NaN', NaN);
      case 'I': return this.literal('Infinity', Infinity);
      default: return this.number();
    }
  }

  number(): Scalar {
    if (this.s.startsWith('-Infinity', this.i)) return this.literal('-Infinity', -Infinity);
    NUMBER.lastIndex = this.i;
    const m = NUMBER.exec(this.s);
    if (!m) this.fail('expecting value');
    this.i = NUMBER.lastIndex;
    return m[1] === undefined && m[2] === undefined ? BigInt(m[0]) : Number(m[0]);
  }

  string(): string {
    let out = '';
    this.i++;
    for (;;) {
      const c = this.s[this.i];
      if (c === undefined) this.fail('unterminated string');
      if (c === '"') { this.i++; return out; }
      if (c < ' ') this.fail('invalid control character');
      if (c !== '\\') { out += c; this.i++; continue; }
      const e = this.s[this.i + 1];
      if (e === 'u') {
        const hex = this.s.slice(this.i + 2, this.i + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) this.fail('invalid \\uXXXX escape');
        out += String.fromCharCode(parseInt(hex, 16));
        this.i += 6;
      } else if (e !== undefined && e in ESCAPES) {
        out += ESCAPES[e];
        this.i += 2;
      } else {
        this.fail('invalid \\escape');
      }
    }
  }

  array(): JsonValue[] {
    const out: JsonValue[] = [];
    this.i++;
    this.ws();
    if (this.s[this.i] === ']') { this.i++; return out; }
    for (;;) {
      this.ws();
      out.push(this.value());
      this.ws();
      const c = this.s[this.i++];
      if (c === ']') return out;
      if (c !== ',') this.fail("expecting ',' delimiter");
    }
  }

  object(): JsonObject {
    const keys: string[] = [];
    const seen = new Set<string>();
    this.i++;
    this.ws();
    if (this.s[this.i] === '}') { this.i++; return new JsonObject(keys); }
    for (;;) {
      this.ws();
      if (this.s[this.i] !== '"') this.fail('expecting property name enclosed in double quotes');
      const key = this.string();
      this.ws();
      if (this.s[this.i++] !== ':') this.fail("expecting ':' delimiter");
      this.ws();
      this.value();
      if (!seen.has(key)) { seen.add(key); keys.push(key); }
      this.ws();
      const c = this.s[this.i++];
      if (c === '}') return new JsonObject(keys);
      if (c !== ',') this.fail("expecting ',' delimiter");
    }
  }
}

/** Strict JSON plus NaN, Infinity and -Infinity, which stored answers
 * predating the current writer can carry. */
export function parseJson(text: string): JsonValue {
  const p = new Parser(text);
  p.ws();
  const v = p.value();
  p.ws();
  if (p.i !== text.length) p.fail('extra data');
  return v;
}

/**
 * Hash-equality key: 1, 1.0 and true share one, so an answer stored as a
 * float still matches a choice stored as an int. Every NaN collapses to
 * one element.
 */
export function scalarKey(v: Scalar): string {
  if (typeof v === 'string') return 's' + v;
  if (v === null) return 'n';
  if (typeof v === 'boolean') return 'i' + (v ? '1' : '0');
  if (typeof v === 'bigint') return 'i' + v.toString();
  return Number.isInteger(v) ? 'i' + BigInt(v).toString() : 'f' + String(v);
}

/**
 * The set of a value: the elements of a list, the code points of a string,
 * the keys of an object. A scalar is not iterable and a nested list or
 * object cannot be an element. Duplicates keep their first occurrence.
 */
export function toSet(value: JsonValue): Scalar[] {
  let items: Scalar[];
  if (Array.isArray(value)) {
    for (const item of value) {
      if (Array.isArray(item) || item instanceof JsonObject) throw new AnswerShapeError('a list or an object cannot be a set element');
    }
    items = value as Scalar[];
  } else if (typeof value === 'string') {
    items = Array.from(value);
  } else if (value instanceof JsonObject) {
    items = value.keys;
  } else {
    throw new AnswerShapeError('a scalar has no elements');
  }
  const seen = new Set<string>();
  const out: Scalar[] = [];
  for (const item of items) {
    const k = scalarKey(item);
    if (!seen.has(k)) {
      seen.add(k);
      out.push(item);
    }
  }
  return out;
}

/** Set equality. */
export function sameSet(a: Scalar[], b: Scalar[]): boolean {
  if (a.length !== b.length) return false;
  const keys = new Set(a.map(scalarKey));
  return b.every((v) => keys.has(scalarKey(v)));
}
