// The ports the app layer speaks through. Adapters live in runtime/adapters
// and meet these interfaces at the composition root only.

export interface Clock {
  now(): Date;
}

export interface Identity {
  subject: string;
  displayName: string;
}

export interface IdentityProvider {
  identify(request: Request): Promise<Identity | null>;
}

export interface Renderer {
  render(template: string, context: Record<string, unknown>): string;
}

/** One recorded Python response (docs/PHASE-1.md A7). A page either names
 * the template and the context the route passed, or carries a body. `sets`
 * are the flags the request leaves behind; `state` is the flag the recording
 * depended on, or null. */
export interface FixturePage {
  method: string;
  path: string;
  status: number;
  headers: { 'content-type': string; location?: string };
  template?: string;
  context?: Record<string, unknown>;
  body?: string;
  sets: string[];
  state: string | null;
}

/** Phase 1 stand-in for the repositories: the pages a profile's routes
 * rendered, replayed by state. */
export interface FixturePages {
  seed(profile: string): Record<string, unknown> | null;
  resolve(profile: string, method: string, path: string, flags: readonly string[]): FixturePage | null;
}
