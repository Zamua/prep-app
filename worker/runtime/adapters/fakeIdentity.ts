import type { Identity, IdentityProvider } from '../../app/ports.js';

// The headers the parity harness injects (tests/parity/harness/contextspec.py).
export const LOGIN_HEADER = 'tailscale-user-login';
export const NAME_HEADER = 'tailscale-user-name';
export const DEFAULT_DISPLAY_NAME = 'Parity';

/** Identity from the tailscale headers, unverified: parity targets only. */
export class FakeIdentityProvider implements IdentityProvider {
  async identify(request: Request): Promise<Identity | null> {
    const login = request.headers.get(LOGIN_HEADER);
    if (!login) return null;
    return { subject: login, displayName: request.headers.get(NAME_HEADER) || DEFAULT_DISPLAY_NAME };
  }
}

/** Phase 1 without parity: nobody is identified. Phase 3 adds Clerk. */
export class NoIdentityProvider implements IdentityProvider {
  async identify(): Promise<Identity | null> {
    return null;
  }
}
