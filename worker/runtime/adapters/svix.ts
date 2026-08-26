// Svix webhook signatures over WebCrypto. The signing key is the base64
// after `whsec_`; the signed string is `<id>.<timestamp>.<body>`; the header
// carries a space-separated list of `<version>,<signature>` and any listed
// v1 entry may match, so a rotated secret verifies during the overlap.
import type { WebhookFailure, WebhookVerifier } from '../../app/ports.js';
import { b64Decode, b64Encode } from '../../domain/base64.js';

export const TOLERANCE_SECONDS = 300;
const SECRET_PREFIX = 'whsec_';

export type SvixFailure = WebhookFailure;

export interface SvixHeaders {
  id: string | null;
  timestamp: string | null;
  signature: string | null;
}

export function svixHeaders(request: Request): SvixHeaders {
  return {
    id: request.headers.get('svix-id'),
    timestamp: request.headers.get('svix-timestamp'),
    signature: request.headers.get('svix-signature'),
  };
}

function keyBytes(secret: string): Uint8Array | null {
  const raw = secret.startsWith(SECRET_PREFIX) ? secret.slice(SECRET_PREFIX.length) : secret;
  return b64Decode(raw);
}

function tagsEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (i < a.length ? a.charCodeAt(i) : 0) ^ (i < b.length ? b.charCodeAt(i) : 0);
  return diff === 0;
}

/** null on success, else what failed. */
export async function verifySvix(secret: string, headers: SvixHeaders, body: string, now: Date): Promise<SvixFailure | null> {
  if (!secret) return 'no_secret';
  const { id, timestamp, signature } = headers;
  if (!id || !timestamp || !signature) return 'missing_headers';
  if (!/^-?\d+$/.test(timestamp.trim())) return 'bad_timestamp';
  const drift = Math.abs(Math.floor(now.getTime() / 1000) - Number(timestamp.trim()));
  if (drift > TOLERANCE_SECONDS) return 'bad_timestamp';
  const key = keyBytes(secret);
  if (!key) return 'no_secret';
  const material = await crypto.subtle.importKey('raw', key as unknown as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', material, new TextEncoder().encode(`${id}.${timestamp}.${body}`) as unknown as BufferSource);
  const expected = b64Encode(new Uint8Array(mac));
  for (const part of signature.split(' ')) {
    const comma = part.indexOf(',');
    if (comma < 0) continue;
    if (part.slice(0, comma) !== 'v1') continue;
    if (tagsEqual(part.slice(comma + 1), expected)) return null;
  }
  return 'bad_signature';
}

/** The header a sender builds; the tests sign with it. */
export async function signSvix(secret: string, id: string, timestamp: string, body: string): Promise<string> {
  const key = keyBytes(secret)!;
  const material = await crypto.subtle.importKey('raw', key as unknown as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', material, new TextEncoder().encode(`${id}.${timestamp}.${body}`) as unknown as BufferSource);
  return `v1,${b64Encode(new Uint8Array(mac))}`;
}

/** The `WebhookVerifier` port over one signing secret. */
export class SvixVerifier implements WebhookVerifier {
  constructor(private readonly secret: string) {}

  verify(request: Request, body: string, now: Date): Promise<SvixFailure | null> {
    return verifySvix(this.secret, svixHeaders(request), body, now);
  }
}
