// Who a request is, and what its anonymous cookie deserves.
//
// The precedence rule lives here in full so no call site has to remember it:
// signed-in > dormant session > anonymous cookie > visitor. The dormant step
// is load-bearing. A returning provider user on a PWA cold launch has an
// expired session token and durable evidence of one, so `identify` answers
// null while `hasDormantSession` is true; falling through to a `prep_anon`
// cookie left on that browser would serve a signed-in person their old
// anonymous account and stop every recovery path keyed on "no user".
import { needsRefresh, parseCookie, verifyCookie, type AnonCookie } from '../../domain/anonCookie.js';
import type { Identity, IdentityProvider, Signer } from '../ports.js';

export const ANON_DISPLAY_NAME = 'Guest';

/** What the response must do to the cookie, before any handler speaks. */
export type CookieVerdict =
  | { kind: 'none' }
  /** The value is dead: delete it. */
  | { kind: 'stale' }
  /** The value is live but old: re-mint at this instant. */
  | { kind: 'refresh'; externalId: string };

export interface Resolution {
  /** The identity to forward, or null for a visitor. */
  identity: Identity | null;
  /** True when a provider session is dormant, so no cookie was consulted. */
  dormant: boolean;
  cookie: CookieVerdict;
  /** The anonymous id a signed-in request must merge into its subject. */
  merge: string | null;
}

const visitor = (dormant: boolean, cookie: CookieVerdict = { kind: 'none' }): Resolution => ({
  identity: null,
  dormant,
  cookie,
  merge: null,
});

/** The cookie of this request, verified, or why it is not usable. */
export async function readCookie(
  raw: string | null | undefined,
  signer: Signer | null,
  nowUnix: number,
): Promise<{ cookie: AnonCookie } | { stale: boolean }> {
  if (!raw || signer === null) return { stale: false };
  const parsed = parseCookie(raw);
  if (parsed === null) return { stale: true };
  const verified = verifyCookie(parsed, await signer.sign(parsed.payload), nowUnix);
  if (verified === null) return { stale: true };
  return { cookie: verified };
}

export interface ResolveDeps {
  provider: IdentityProvider;
  /** null when no signing secret resolved: anonymous accounts are off. */
  signer: Signer | null;
  nowUnix: number;
  cookieValue: string | null;
}

/**
 * The whole precedence rule, over one request's headers. Nothing here reads
 * storage: whether the anonymous row still exists is the cell's answer, and
 * whether the merge succeeds is the saga's.
 */
export async function resolveIdentity(request: Request, deps: ResolveDeps): Promise<Resolution> {
  const signedIn = await deps.provider.identify(request);
  if (signedIn) return withSignedIn(signedIn, deps);
  if (deps.provider.hasDormantSession(request)) return visitor(true);
  const read = await readCookie(deps.cookieValue, deps.signer, deps.nowUnix);
  if (!('cookie' in read)) return visitor(false, read.stale ? { kind: 'stale' } : { kind: 'none' });
  const identity: Identity = {
    subject: read.cookie.externalId,
    kind: 'anon',
    displayName: ANON_DISPLAY_NAME,
    email: null,
    profilePicUrl: null,
  };
  const cookie: CookieVerdict = needsRefresh(read.cookie, deps.nowUnix) ? { kind: 'refresh', externalId: read.cookie.externalId } : { kind: 'none' };
  return { identity, dormant: false, cookie, merge: null };
}

async function withSignedIn(identity: Identity, deps: ResolveDeps): Promise<Resolution> {
  const read = await readCookie(deps.cookieValue, deps.signer, deps.nowUnix);
  if (!('cookie' in read)) {
    return { identity, dormant: false, cookie: read.stale ? { kind: 'stale' } : { kind: 'none' }, merge: null };
  }
  // The cookie names the account it is presented on, so it points at a row
  // that is no longer anonymous. Nothing to move, and nothing left for the
  // cookie to be.
  if (read.cookie.externalId === identity.subject) {
    return { identity, dormant: false, cookie: { kind: 'stale' }, merge: null };
  }
  return { identity, dormant: false, cookie: { kind: 'none' }, merge: read.cookie.externalId };
}
