// The signed cookie naming an anonymous account, as pure encode/decode over
// bytes. Value: `v1.<id>.<iat>.<sig>`: 16 id bytes as unpadded base64url,
// unix seconds, and the first 16 bytes of HMAC-SHA256 over `v1.<id>.<iat>`.
// The MAC itself comes from the caller's signer; nothing here holds a key.

export const COOKIE_NAME = 'prep_anon';
export const COOKIE_VERSION = 'v1';
export const COOKIE_SAMESITE = 'lax';

export const ID_BYTES = 16;
export const SIG_BYTES = 16;
export const SECRET_BYTES = 32;

export const MAX_AGE_SECONDS = 15552000; // 180 days
// A sixth of the window: re-mint at most monthly with 150 days of slack.
export const REFRESH_AFTER_SECONDS = 2592000; // 30 days
export const FUTURE_SKEW_SECONDS = 60;

export const EXTERNAL_ID_PREFIX = 'anon:';

/** A verified cookie value. */
export interface AnonCookie {
  externalId: string; // "anon:" + 32 hex chars
  issuedAt: number; // unix seconds
}

/** A well-formed value whose MAC is still unchecked. */
export interface ParsedCookie {
  /** `v1.<id>.<iat>` exactly as received: the bytes the MAC covers. */
  payload: string;
  idBytes: Uint8Array;
  issuedAt: number;
  sig: string;
}

export class BadExternalId extends Error {}

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const B64U = /^[A-Za-z0-9_-]*$/;

/** Unpadded base64url. */
export function b64u(bytes: Uint8Array): string {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i]!;
    const b = i + 1 < bytes.length ? bytes[i + 1]! : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2]! : 0;
    const n = (a << 16) | (b << 8) | c;
    out += ALPHABET[(n >> 18) & 63]! + ALPHABET[(n >> 12) & 63]!;
    if (i + 1 < bytes.length) out += ALPHABET[(n >> 6) & 63]!;
    if (i + 2 < bytes.length) out += ALPHABET[n & 63]!;
  }
  return out;
}

/** Strict unpadded base64url yielding exactly `expect` bytes, else null. */
export function b64uDecode(value: string, expect: number): Uint8Array | null {
  if (!B64U.test(value) || value.length % 4 === 1) return null;
  const size = Math.floor((value.length * 3) / 4);
  if (size !== expect) return null;
  const out = new Uint8Array(size);
  let bits = 0;
  let acc = 0;
  let j = 0;
  for (const ch of value) {
    acc = (acc << 6) | ALPHABET.indexOf(ch);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[j++] = (acc >> bits) & 255;
    }
  }
  return out;
}

const HEX = /^[0-9a-f]{32}$/;

export function externalIdFromBytes(raw: Uint8Array): string {
  return EXTERNAL_ID_PREFIX + Array.from(raw, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** The 16 id bytes of an external id; throws `BadExternalId` otherwise. */
export function idBytes(externalId: string): Uint8Array {
  if (!externalId.startsWith(EXTERNAL_ID_PREFIX)) throw new BadExternalId('not an anonymous external id');
  const hex = externalId.slice(EXTERNAL_ID_PREFIX.length).toLowerCase();
  if (!HEX.test(hex)) throw new BadExternalId(`anonymous external id must carry ${ID_BYTES} bytes`);
  return Uint8Array.from(hex.match(/../g)!, (h) => parseInt(h, 16));
}

/** `v1.<b64u(id)>.<iat>`: the string the MAC signs. */
export function cookiePayload(externalId: string, issuedAt: number): string {
  return `${COOKIE_VERSION}.${b64u(idBytes(externalId))}.${Math.trunc(issuedAt)}`;
}

/** The wire value: payload plus the first SIG_BYTES of the MAC. */
export function assembleCookie(payload: string, mac: Uint8Array): string {
  return `${payload}.${b64u(mac.subarray(0, SIG_BYTES))}`;
}

const ASCII = /^[\x00-\x7f]*$/;
// A decimal integer as the cookie may spell it: optional ASCII whitespace
// and sign, digits with single underscores between them.
const DECIMAL_INT = /^[ \t\n\r\v\f]*[+-]?[0-9](?:_?[0-9])*[ \t\n\r\v\f]*$/;

/** Shape check only; a value failing any check is treated as absent. */
export function parseCookie(raw: string | null | undefined): ParsedCookie | null {
  if (!raw || !ASCII.test(raw)) return null;
  const parts = raw.split('.');
  if (parts.length !== 4) return null;
  const [version, idPart, iatPart, sig] = parts as [string, string, string, string];
  if (version !== COOKIE_VERSION) return null;
  const id = b64uDecode(idPart, ID_BYTES);
  if (id === null) return null;
  if (!DECIMAL_INT.test(iatPart)) return null;
  const issuedAt = Number(iatPart.replace(/[ \t\n\r\v\f_]/g, '')) + 0;
  return { payload: `${version}.${idPart}.${iatPart}`, idBytes: id, issuedAt, sig };
}

function tagsEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (i < a.length ? a.charCodeAt(i) : 0) ^ (i < b.length ? b.charCodeAt(i) : 0);
  return diff === 0;
}

/** `mac` is HMAC-SHA256 over `parsed.payload`; `now` is unix seconds. */
export function verifyCookie(parsed: ParsedCookie, mac: Uint8Array, now: number): AnonCookie | null {
  if (!tagsEqual(b64u(mac.subarray(0, SIG_BYTES)), parsed.sig)) return null;
  const ts = Math.trunc(now);
  const iat = parsed.issuedAt;
  if (iat > ts + FUTURE_SKEW_SECONDS || iat < ts - MAX_AGE_SECONDS) return null;
  return { externalId: externalIdFromBytes(parsed.idBytes), issuedAt: iat };
}

/** True once the signed `iat` is old enough to re-mint. */
export function needsRefresh(cookie: AnonCookie, now: number): boolean {
  return Math.trunc(now) - cookie.issuedAt > REFRESH_AFTER_SECONDS;
}
