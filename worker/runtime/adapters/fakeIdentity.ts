import type { Identity, IdentityProvider, SignInUrls } from '../../app/ports.js';

// The headers the parity harness injects (tests/parity/harness/contextspec.py).
export const LOGIN_HEADER = 'tailscale-user-login';
export const NAME_HEADER = 'tailscale-user-name';
export const PIC_HEADER = 'tailscale-user-profile-pic';
export const INTERNAL_TOKEN_HEADER = 'x-internal-token';
export const DEFAULT_DISPLAY_NAME = 'Parity';

/** Auth is implicit on the tailnet shape, so there is no in-app flow to
 * point at and the templates hide the sign-in chrome. */
const NO_URLS: SignInUrls = { sign_in: null, sign_up: null, sign_out: null, account: null };

function tagsEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (i < a.length ? a.charCodeAt(i) : 0) ^ (i < b.length ? b.charCodeAt(i) : 0);
  return diff === 0;
}

/**
 * Identity from the tailscale headers, which nothing verifies, so the same
 * request must also carry the harness's `X-Internal-Token` (decision 7.0,
 * option c). Without that gate the parity host would hand any caller any
 * user's cell.
 */
export class FakeIdentityProvider implements IdentityProvider {
  readonly name = 'tailscale';

  constructor(
    private readonly internalToken: string,
    private readonly signOutUrl: string = '',
  ) {}

  async identify(request: Request): Promise<Identity | null> {
    const login = request.headers.get(LOGIN_HEADER);
    if (!login) return null;
    if (!this.internalToken || !tagsEqual(request.headers.get(INTERNAL_TOKEN_HEADER) ?? '', this.internalToken)) return null;
    const subject = login.trim();
    return {
      subject,
      kind: 'fake',
      displayName: request.headers.get(NAME_HEADER) || subject.split('@')[0] || DEFAULT_DISPLAY_NAME,
      email: subject,
      profilePicUrl: request.headers.get(PIC_HEADER) || null,
    };
  }

  hasDormantSession(): boolean {
    return false;
  }

  /** A sign-out URL only where one is configured: the masthead's sign-out row
   * follows the provider, and the recorded corpus has none. */
  urls(): SignInUrls {
    return this.signOutUrl ? { ...NO_URLS, sign_out: this.signOutUrl } : NO_URLS;
  }
}

/** No provider configured: nobody is identified, and no flow exists. */
export class NoIdentityProvider implements IdentityProvider {
  readonly name = 'none';

  async identify(): Promise<Identity | null> {
    return null;
  }

  hasDormantSession(): boolean {
    return false;
  }

  urls(): SignInUrls {
    return NO_URLS;
  }
}
