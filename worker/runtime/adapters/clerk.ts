// Clerk sessions: RS256 verification over WebCrypto with a single-flight
// JWKS, the two token sources, the dormant-session read and the hosted URLs.
// No SDK: the session JWT is the whole credential.
import type { Clock, Identity, IdentityProvider, SignInUrls } from '../../app/ports.js';
import { b64Decode, b64uDecodeText } from '../../domain/base64.js';
import { parseCookieHeader } from '../../domain/cookies.js';

export const SESSION_COOKIE = '__session';
export const CLIENT_UAT_COOKIE = '__client_uat';
/** Clerk's own default tolerance for a client clock that drifts. */
export const CLOCK_SKEW_MS = 5000;

export interface ClerkEnv {
  CLERK_ISSUER?: string;
  CLERK_JWKS_URL?: string;
  CLERK_AUTHORIZED_PARTIES?: string;
  CLERK_ACCOUNTS_URL?: string;
  CLERK_PUBLISHABLE_KEY?: string;
  CLERK_SECRET_KEY?: string;
}

export interface ClerkConfig {
  issuer: string;
  jwksUrl: string;
  authorizedParties: readonly string[];
  accountsUrl: string;
  publishableKey: string;
  secretKey: string;
}

export class ClerkConfigError extends Error {}

const trimmed = (v: string | undefined) => (v ?? '').trim();

/** Every required var, or the first missing one named. */
export function clerkConfig(env: ClerkEnv): ClerkConfig {
  const need = (name: keyof ClerkEnv): string => {
    const value = trimmed(env[name]);
    if (!value) throw new ClerkConfigError(`${name} must be set when the identity provider is clerk`);
    return value;
  };
  // Checked in declaration order so the message names the first gap.
  const issuer = need('CLERK_ISSUER');
  const jwksUrl = need('CLERK_JWKS_URL');
  const parties = need('CLERK_AUTHORIZED_PARTIES')
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);
  if (parties.length === 0) throw new ClerkConfigError('CLERK_AUTHORIZED_PARTIES must name at least one origin');
  return {
    issuer,
    jwksUrl,
    authorizedParties: parties,
    accountsUrl: need('CLERK_ACCOUNTS_URL').replace(/\/+$/, ''),
    publishableKey: trimmed(env.CLERK_PUBLISHABLE_KEY),
    secretKey: trimmed(env.CLERK_SECRET_KEY),
  };
}

export interface Jwk {
  kid?: string;
  kty?: string;
  alg?: string;
  n?: string;
  e?: string;
  use?: string;
}

export type JwtPayload = Record<string, unknown>;

const RSA = { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' } as const;

/** One in-flight fetch per URL per isolate; a cache only, never state. */
const jwksCache = new Map<string, Promise<Jwk[]>>();

export function resetJwksCache(): void {
  jwksCache.clear();
}

async function loadJwks(url: string, fetchImpl: typeof fetch): Promise<Jwk[]> {
  let inflight = jwksCache.get(url);
  if (!inflight) {
    inflight = (async () => {
      const res = await fetchImpl(url);
      if (!res.ok) throw new Error(`jwks ${url}: HTTP ${res.status}`);
      const body = (await res.json()) as { keys?: Jwk[] };
      return body.keys ?? [];
    })();
    jwksCache.set(url, inflight);
    inflight.catch(() => jwksCache.delete(url));
  }
  return inflight;
}

function decodeSegment(segment: string): JwtPayload | null {
  const json = b64uDecodeText(segment);
  if (json === null) return null;
  try {
    const parsed: unknown = JSON.parse(json);
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as JwtPayload) : null;
  } catch {
    return null;
  }
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

/**
 * Verifies a Clerk session JWT. A credential that is present but fails any
 * check answers null, exactly as a missing one does: a forged token is an
 * unauthenticated request, not an error the forger can trigger.
 */
export class ClerkVerifier {
  constructor(
    private readonly config: ClerkConfig,
    private readonly clock: Clock,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async verify(token: string): Promise<JwtPayload | null> {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const [rawHeader, rawPayload, rawSignature] = parts as [string, string, string];
    const header = decodeSegment(rawHeader);
    const payload = decodeSegment(rawPayload);
    if (!header || !payload) return null;
    if (header['alg'] !== 'RS256') return null;
    const signature = b64Decode(rawSignature.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (rawSignature.length % 4)) % 4));
    if (!signature) return null;
    const kid = str(header['kid']);
    if (!(await this.signatureOk(kid, `${rawHeader}.${rawPayload}`, signature))) return null;
    return this.claimsOk(payload) ? payload : null;
  }

  private async signatureOk(kid: string | null, signed: string, signature: Uint8Array): Promise<boolean> {
    for (const refresh of [false, true]) {
      if (refresh) jwksCache.delete(this.config.jwksUrl);
      let keys: Jwk[];
      try {
        keys = await loadJwks(this.config.jwksUrl, this.fetchImpl);
      } catch {
        return false;
      }
      const match = keys.filter((k) => k.kty === 'RSA' && (kid === null || k.kid === undefined || k.kid === kid));
      if (match.length === 0) continue;
      const data = new TextEncoder().encode(signed) as unknown as BufferSource;
      for (const jwk of match) {
        let key: CryptoKey;
        try {
          key = await crypto.subtle.importKey('jwk', { kty: 'RSA', n: jwk.n, e: jwk.e, alg: 'RS256', ext: true }, RSA, false, ['verify']);
        } catch {
          continue;
        }
        if (await crypto.subtle.verify(RSA.name, key, signature as unknown as BufferSource, data)) return true;
      }
      // A known kid that did not verify is a bad token, not a stale cache.
      if (kid !== null && match.some((k) => k.kid === kid)) return false;
    }
    return false;
  }

  private claimsOk(payload: JwtPayload): boolean {
    if (str(payload['iss']) !== this.config.issuer) return false;
    if (!str(payload['sub'])) return false;
    const now = this.clock.now().getTime();
    const exp = num(payload['exp']);
    if (exp === null || exp * 1000 + CLOCK_SKEW_MS <= now) return false;
    const nbf = num(payload['nbf']);
    if (nbf !== null && nbf * 1000 - CLOCK_SKEW_MS > now) return false;
    const azp = str(payload['azp']);
    if (azp !== null && !this.config.authorizedParties.includes(azp)) return false;
    return true;
  }
}

/** `sub`, plus whatever the session template happens to carry. */
export function identityFromClaims(payload: JwtPayload): Identity {
  return {
    subject: String(payload['sub']),
    kind: 'clerk',
    email: str(payload['email']) ?? str(payload['primary_email']),
    displayName: str(payload['name']) ?? str(payload['full_name']) ?? str(payload['username']),
    profilePicUrl: str(payload['picture']) ?? str(payload['image_url']),
  };
}

/** Python's `quote_plus`: unreserved characters pass, a space becomes `+`. */
export function quotePlus(value: string): string {
  let out = '';
  for (const byte of new TextEncoder().encode(value)) {
    const ch = String.fromCharCode(byte);
    if (/[A-Za-z0-9_.\-~]/.test(ch)) out += ch;
    else if (ch === ' ') out += '+';
    else out += `%${byte.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

/** The frontend API host encoded in the publishable key, or null. */
export function frontendApiHost(publishableKey: string): string | null {
  const pk = (publishableKey || '').trim();
  if (!pk || !pk.includes('_')) return null;
  const encoded = pk.split('_').slice(2).join('_');
  const raw = b64Decode(encoded + '='.repeat((4 - (encoded.length % 4)) % 4));
  if (!raw) return null;
  const host = new TextDecoder('utf-8').decode(raw).replace(/\$+$/, '').trim();
  return host || null;
}

export class ClerkProvider implements IdentityProvider {
  readonly name = 'clerk';

  constructor(
    private readonly config: ClerkConfig,
    private readonly verifier: ClerkVerifier,
  ) {}

  async identify(request: Request): Promise<Identity | null> {
    const token = bearerOrSessionCookie(request);
    if (!token) return null;
    const payload = await this.verifier.verify(token);
    return payload ? identityFromClaims(payload) : null;
  }

  /**
   * `__client_uat` is Clerk's durable "user auth timestamp" on the app's
   * own domain: non-zero means ClerkJS holds a live client session even
   * though the short-lived `__session` JWT has expired.
   */
  hasDormantSession(request: Request): boolean {
    const uat = parseCookieHeader(request.headers.get('cookie'))[CLIENT_UAT_COOKIE];
    return Boolean(uat && uat.trim() !== '0');
  }

  urls(): SignInUrls {
    const back = quotePlus(`${this.config.authorizedParties[0]}/`);
    return {
      sign_in: `${this.config.accountsUrl}/sign-in?redirect_url=${back}`,
      sign_up: `${this.config.accountsUrl}/sign-up?redirect_url=${back}`,
      sign_out: `${this.config.accountsUrl}/sign-out?redirect_url=${back}`,
      account: `${this.config.accountsUrl}/user`,
    };
  }

  get publishableKey(): string {
    return this.config.publishableKey;
  }

  get frontendApiHost(): string | null {
    return frontendApiHost(this.config.publishableKey);
  }

  get secretKey(): string {
    return this.config.secretKey;
  }
}

export function bearerOrSessionCookie(request: Request): string | null {
  const authorization = request.headers.get('authorization') ?? '';
  const space = authorization.indexOf(' ');
  if (space > 0 && authorization.slice(0, space).toLowerCase() === 'bearer') {
    const value = authorization.slice(space + 1).trim();
    if (value) return value;
  }
  return parseCookieHeader(request.headers.get('cookie'))[SESSION_COOKIE] ?? null;
}

/** `DELETE /v1/users/<id>` with the secret key: the webhook does the wipe. */
export async function deleteClerkUser(config: ClerkConfig, userId: string, fetchImpl: typeof fetch = fetch): Promise<Response> {
  return fetchImpl(`https://api.clerk.com/v1/users/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    headers: { authorization: `Bearer ${config.secretKey}` },
  });
}
