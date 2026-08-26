// The signing key and the wire bytes of the anonymous cookie. The parse,
// verify and refresh rules are `domain/anonCookie`; the MAC is the `Signer`
// port; the header shape is `domain/cookies`.
import type { Signer } from '../../app/ports.js';
import {
  COOKIE_NAME,
  COOKIE_SAMESITE,
  MAX_AGE_SECONDS,
  SECRET_BYTES,
  assembleCookie,
  cookiePayload,
} from '../../domain/anonCookie.js';
import { hexToBytes } from '../../domain/base64.js';
import { setCookie } from '../../domain/cookies.js';
import { hkdfSha256 } from './hkdf.js';

export const SECRET_ENV = 'PREP_ANON_COOKIE_SECRET';
export const MASTER_ENV = 'PREP_KEY_ENCRYPTION_SECRET';
const HKDF_INFO = new TextEncoder().encode('prep-anon-cookie-v1');

export interface CookieSecretEnv {
  PREP_ANON_COOKIE_SECRET?: string;
  PREP_KEY_ENCRYPTION_SECRET?: string;
}

/**
 * The signing key, or null when anonymous accounts are off: the explicit
 * secret when it is 32 hex bytes, else HKDF of the master key. One key, one
 * purpose, the label doing the domain separation.
 */
export async function resolveCookieSecret(env: CookieSecretEnv, warn: (msg: string) => void = console.warn): Promise<Uint8Array | null> {
  const explicit = (env.PREP_ANON_COOKIE_SECRET ?? '').trim();
  if (explicit) {
    const key = hexToBytes(explicit);
    if (!key || key.length !== SECRET_BYTES) {
      warn(`${SECRET_ENV} is not ${SECRET_BYTES} hex bytes; anonymous accounts disabled`);
      return null;
    }
    return key;
  }
  const master = (env.PREP_KEY_ENCRYPTION_SECRET ?? '').trim();
  if (!master) return null;
  const ikm = hexToBytes(master);
  if (!ikm || ikm.length !== SECRET_BYTES) return null;
  return hkdfSha256(ikm, HKDF_INFO, SECRET_BYTES);
}

export class HmacSigner implements Signer {
  private key: Promise<CryptoKey> | null = null;

  constructor(private readonly secret: Uint8Array) {}

  private material(): Promise<CryptoKey> {
    this.key ??= crypto.subtle.importKey('raw', this.secret as unknown as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return this.key;
  }

  async sign(payload: string): Promise<Uint8Array> {
    const mac = await crypto.subtle.sign('HMAC', await this.material(), new TextEncoder().encode(payload) as unknown as BufferSource);
    return new Uint8Array(mac);
  }
}

/** The wire value for an id at an instant. */
export async function mintCookie(signer: Signer, externalId: string, issuedAtUnix: number): Promise<string> {
  const payload = cookiePayload(externalId, issuedAtUnix);
  return assembleCookie(payload, await signer.sign(payload));
}

const COOKIE_PATH = '/';

/** `prep_anon=<v>; HttpOnly; Max-Age=15552000; Path=/; SameSite=lax; Secure`. */
export function setCookieHeader(value: string, secure: boolean): string {
  return setCookie(COOKIE_NAME, value, { maxAge: MAX_AGE_SECONDS, path: COOKIE_PATH, httpOnly: true, sameSite: COOKIE_SAMESITE, secure });
}

/** The delete: an empty quoted value, `Max-Age=0` and an expiry stamped now. */
export function deleteCookieHeader(now: Date, secure: boolean): string {
  return setCookie(COOKIE_NAME, '', { expires: now, maxAge: 0, path: COOKIE_PATH, httpOnly: true, sameSite: COOKIE_SAMESITE, secure });
}
