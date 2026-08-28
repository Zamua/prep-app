// Personal access tokens: the wire format and the display mask. Hashing is
// the adapter's.
//
// `prep_pat_<b64u(subject)>.<b64u(secret)>`. The subject travels in the
// token so the entry worker can route to its owner's cell without a global
// index; it is not a secret (every deep link carries it). An older token has
// no dot and parses as nothing, which is the same answer as an unknown
// token.
import { b64uDecodeText, b64uEncode, b64uEncodeText } from './base64.js';

export const TOKEN_PREFIX = 'prep_pat_';
export const SECRET_BYTES = 32;

export interface ParsedToken {
  subject: string;
  /** The secret half, still encoded: only its hash is ever compared. */
  secret: string;
}

export function assembleToken(subject: string, secret: Uint8Array): string {
  return `${TOKEN_PREFIX}${b64uEncodeText(subject)}.${b64uEncode(secret)}`;
}

const B64U = /^[A-Za-z0-9_-]+$/;

/** The owner named by a token, or null for any malformed or legacy value. */
export function parseToken(token: string | null | undefined): ParsedToken | null {
  const raw = (token ?? '').trim();
  if (!raw.startsWith(TOKEN_PREFIX)) return null;
  const body = raw.slice(TOKEN_PREFIX.length);
  const dot = body.indexOf('.');
  if (dot <= 0 || dot !== body.lastIndexOf('.')) return null;
  const [head, secret] = [body.slice(0, dot), body.slice(dot + 1)];
  if (!B64U.test(head) || !B64U.test(secret)) return null;
  const subject = b64uDecodeText(head);
  if (!subject) return null;
  return { subject, secret };
}

/** `prep_pat_Aa…x9zT`: two characters after the prefix and the last four. */
export function maskToken(token: string): string {
  if (!token || token.length <= TOKEN_PREFIX.length + 6) return '…';
  const middle = token.slice(TOKEN_PREFIX.length, TOKEN_PREFIX.length + 2);
  return `${TOKEN_PREFIX}${middle}…${token.slice(-4)}`;
}

/** The bearer refusals, in the order they are checked. */
export const MISSING_HEADER = 'missing Authorization header';
export const BAD_SCHEME = "Authorization must be 'Bearer <token>'";
export const BAD_TOKEN = 'invalid or revoked token';
export const NO_USER = 'user no longer exists';

export type BearerRefusal = typeof MISSING_HEADER | typeof BAD_SCHEME;

/** The bearer value, or why the header cannot yield one. */
export function bearerValue(authorization: string | null): { token: string } | { refusal: BearerRefusal } {
  if (!authorization) return { refusal: MISSING_HEADER };
  const space = authorization.indexOf(' ');
  const scheme = space < 0 ? authorization : authorization.slice(0, space);
  const value = space < 0 ? '' : authorization.slice(space + 1);
  if (scheme.toLowerCase() !== 'bearer' || !value) return { refusal: BAD_SCHEME };
  return { token: value };
}
