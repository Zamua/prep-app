// The multipart parser exists because the runtime's own decodes a file part
// as UTF-8: 256 raw bytes came back as 512 with U+FFFD in place of every byte
// above 0x7f, which turns a `.apkg` into an unreadable zip. So the byte-exact
// cases are the ones that matter here, not the shape of the envelope.
import { describe, expect, it } from 'vitest';
import { boundaryOf, parseMultipart } from '../domain/multipart.js';

const enc = new TextEncoder();

function body(boundary: string, parts: { headers: string; bytes: Uint8Array }[], close = true): Uint8Array {
  const chunks: Uint8Array[] = [];
  for (const p of parts) {
    chunks.push(enc.encode(`--${boundary}\r\n${p.headers}\r\n\r\n`));
    chunks.push(p.bytes);
    chunks.push(enc.encode('\r\n'));
  }
  chunks.push(enc.encode(close ? `--${boundary}--\r\n` : `--${boundary}\r\n`));
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

const field = (name: string, value: string) => ({ headers: `Content-Disposition: form-data; name="${name}"`, bytes: enc.encode(value) });
const file = (name: string, filename: string, bytes: Uint8Array) => ({
  headers: `Content-Disposition: form-data; name="${name}"; filename="${filename}"\r\nContent-Type: application/octet-stream`,
  bytes,
});

describe('boundaryOf', () => {
  it('reads a bare and a quoted parameter', () => {
    expect(boundaryOf('multipart/form-data; boundary=abc123')).toBe('abc123');
    expect(boundaryOf('multipart/form-data; boundary="a b; c"')).toBe('a b; c');
    expect(boundaryOf('multipart/form-data; charset=utf-8; boundary=xyz')).toBe('xyz');
  });

  it('answers null when there is none', () => {
    expect(boundaryOf('multipart/form-data')).toBeNull();
    expect(boundaryOf('application/json')).toBeNull();
  });
});

describe('parseMultipart', () => {
  const B = '----prepBoundary9x';
  const type = `multipart/form-data; boundary=${B}`;

  it('keeps every byte of a file part, including the ones UTF-8 would replace', () => {
    const raw = new Uint8Array(256).map((_, i) => i);
    const parts = parseMultipart(body(B, [field('name', 'deck'), file('file', 'x.apkg', raw)]), type);
    expect(parts.map((p) => p.name)).toEqual(['name', 'file']);
    expect(parts[1]!.bytes).toEqual(raw);
    expect(parts[1]!.bytes.length).toBe(256);
  });

  it('keeps a body that ends in a byte a boundary scan could mistake', () => {
    const raw = new Uint8Array([0x50, 0x4b, 0x05, 0x06, 0x0d, 0x0a, 0x2d, 0x2d, 0xff]);
    const parts = parseMultipart(body(B, [file('file', 'x.zip', raw)]), type);
    expect(parts[0]!.bytes).toEqual(raw);
  });

  it('decodes a plain field as UTF-8 text', () => {
    const parts = parseMultipart(body(B, [field('name', 'café 日本')]), type);
    expect(parts[0]).toMatchObject({ name: 'name', filename: null });
    expect(new TextDecoder().decode(parts[0]!.bytes)).toBe('café 日本');
  });

  it('reports an empty file part rather than dropping it', () => {
    const parts = parseMultipart(body(B, [file('file', '', new Uint8Array(0))]), type);
    expect(parts).toEqual([{ name: 'file', filename: '', bytes: new Uint8Array(0) }]);
  });

  it('handles an empty field value', () => {
    const parts = parseMultipart(body(B, [field('name', ''), field('other', 'x')]), type);
    expect(parts.map((p) => p.bytes.length)).toEqual([0, 1]);
  });

  it('keeps a file whose own bytes hold the boundary with no CRLF ahead of it', () => {
    // The client picks the boundary, so a short one turns up inside a zip or
    // a CSV by chance. Only `CRLF--boundary` delimits a part.
    const short = 'ab';
    const raw = new Uint8Array([1, 2, 3, ...enc.encode(`--${short}`), 9, 9, 9]);
    const parts = parseMultipart(body(short, [file('file', 'x.apkg', raw)]), `multipart/form-data; boundary=${short}`);
    expect(parts).toHaveLength(1);
    expect(parts[0]!.bytes).toEqual(raw);
  });

  it('keeps every row of a CSV whose cells hold the boundary', () => {
    const short = 'x';
    const text = `prompt,answer\r\nwhat,--${short}\r\nsecond,row\r\n`;
    const parts = parseMultipart(body(short, [file('file', 'deck.csv', enc.encode(text))]), `multipart/form-data; boundary=${short}`);
    expect(new TextDecoder().decode(parts[0]!.bytes)).toBe(text);
  });

  it('skips a preamble ahead of the first delimiter', () => {
    const preamble = enc.encode('ignored preamble\r\n');
    const framed = body(B, [field('name', 'deck')]);
    const withPreamble = new Uint8Array(preamble.length + framed.length);
    withPreamble.set(preamble, 0);
    withPreamble.set(framed, preamble.length);
    expect(parseMultipart(withPreamble, type).map((p) => p.name)).toEqual(['name']);
  });

  it('is not confused by a part whose bytes contain CRLF runs', () => {
    const raw = enc.encode('a\r\n\r\nb\r\n');
    expect(parseMultipart(body(B, [file('file', 'x', raw)]), type)[0]!.bytes).toEqual(raw);
  });

  it('answers nothing for a body with no boundary, or none present', () => {
    expect(parseMultipart(enc.encode('x'), 'multipart/form-data')).toEqual([]);
    expect(parseMultipart(enc.encode('nothing here'), type)).toEqual([]);
  });

  it('stops at a truncated part rather than inventing one', () => {
    const truncated = enc.encode(`--${B}\r\nContent-Disposition: form-data; name="a"\r\n`);
    expect(parseMultipart(truncated, type)).toEqual([]);
  });

  it('reads several file parts and keeps their order', () => {
    const one = new Uint8Array([0xff, 0x00]);
    const two = new Uint8Array([0x01, 0x80, 0x7f]);
    const parts = parseMultipart(body(B, [file('file', 'a', one), file('extra', 'b', two)]), type);
    expect(parts.map((p) => [p.name, [...p.bytes]])).toEqual([
      ['file', [0xff, 0x00]],
      ['extra', [0x01, 0x80, 0x7f]],
    ]);
  });
});
