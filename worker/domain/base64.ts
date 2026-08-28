// Base64, both alphabets, decoded strictly: a character outside the
// alphabet or bad padding is a refusal, never a silent skip, because these
// values carry signatures. Unpadded wire values are re-padded first.

const STD = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
const URL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

function encode(bytes: Uint8Array, alphabet: string, pad: boolean): string {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i]!;
    const b = i + 1 < bytes.length ? bytes[i + 1]! : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2]! : 0;
    const n = (a << 16) | (b << 8) | c;
    out += alphabet[(n >> 18) & 63]! + alphabet[(n >> 12) & 63]!;
    out += i + 1 < bytes.length ? alphabet[(n >> 6) & 63]! : pad ? '=' : '';
    out += i + 2 < bytes.length ? alphabet[n & 63]! : pad ? '=' : '';
  }
  return out;
}

function decode(value: string, alphabet: string): Uint8Array | null {
  const body = value.endsWith('==') ? value.slice(0, -2) : value.endsWith('=') ? value.slice(0, -1) : value;
  if (body.includes('=')) return null;
  if (body.length % 4 === 1) return null;
  const out = new Uint8Array(Math.floor((body.length * 3) / 4));
  let acc = 0;
  let bits = 0;
  let j = 0;
  for (const ch of body) {
    const v = alphabet.indexOf(ch);
    if (v < 0) return null;
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[j++] = (acc >> bits) & 255;
    }
  }
  return out;
}

export const b64Encode = (bytes: Uint8Array): string => encode(bytes, STD, true);
export const b64uEncode = (bytes: Uint8Array): string => encode(bytes, URL, false);

/** Strict: padding must be present and correct, as `validate=True` requires. */
export function b64Decode(value: string): Uint8Array | null {
  if (value.length % 4 !== 0) return null;
  return decode(value, STD);
}

/** Padding optional, as the re-padding callers of `urlsafe_b64decode` do. */
export const b64uDecode = (value: string): Uint8Array | null => decode(value, URL);

const TEXT = new TextEncoder();
const FROM = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false });

export const b64uEncodeText = (text: string): string => b64uEncode(TEXT.encode(text));

export function b64uDecodeText(value: string): string | null {
  const raw = b64uDecode(value);
  if (raw === null) return null;
  try {
    return FROM.decode(raw);
  } catch {
    return null;
  }
}

export function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

const HEX = /^(?:[0-9a-fA-F]{2})*$/;

export function hexToBytes(hex: string): Uint8Array | null {
  if (!HEX.test(hex)) return null;
  return Uint8Array.from(hex.match(/../g) ?? [], (h) => parseInt(h, 16));
}
