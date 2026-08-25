// The composition root: the only place adapters meet ports, memoized per
// isolate. Cross-cutting wrappers live here and are applied by the router,
// never inside a handler.
import type { Clock, FixturePages, IdentityProvider, Renderer } from '../app/ports.js';
import { clockFromEnv } from './adapters/clock.js';
import { FakeIdentityProvider, NoIdentityProvider } from './adapters/fakeIdentity.js';
import { fixturePagesFromBuild } from './adapters/fixturePages.js';
import { createRenderer } from './adapters/nunjucks/index.js';
import { resolveBuildToken } from './buildToken.js';
import type { Env } from './env.js';

export interface Composition {
  clock: Clock;
  identity: IdentityProvider;
  renderer: Renderer;
  pages: FixturePages;
  buildToken: string;
  parity: boolean;
  internalToken: string;
}

const compositions = new WeakMap<Env, Composition>();

export function compose(env: Env): Composition {
  const memo = compositions.get(env);
  if (memo) return memo;
  const parity = env.PREP_PARITY_MODE === '1';
  // The fake provider trusts a header; on prod that would be an open door.
  if (parity && env.PREP_ENV === 'prod') throw new Error('refusing the fake identity provider on prod');
  const clock = clockFromEnv(env);
  const composition: Composition = {
    clock,
    identity: parity ? new FakeIdentityProvider() : new NoIdentityProvider(),
    renderer: createRenderer({ clock, root: '' }),
    pages: fixturePagesFromBuild(),
    buildToken: resolveBuildToken(env.PREP_BUILD_ID),
    parity,
    internalToken: env.PREP_INTERNAL_TOKEN ?? '',
  };
  compositions.set(env, composition);
  return composition;
}

/** `Cache-Control: no-cache` on every HTML response, as the Python
 * middleware does; other content types pass untouched. */
export function noCacheHtml(res: Response): Response {
  if (!(res.headers.get('content-type') ?? '').startsWith('text/html')) return res;
  const out = new Response(res.body, res);
  out.headers.set('cache-control', 'no-cache');
  return out;
}

/** The response-path hook the anonymous cookie takes in phase 3; the
 * identity function until then. */
export function cookieHooks(_req: Request, res: Response): Response {
  return res;
}

/** The composition with some ports replaced, memoized for `env`: how a test
 * hands a cell or the router a spy through the same root. */
export function composeWith(env: Env, overrides: Partial<Composition>): Composition {
  const composition = { ...compose(env), ...overrides };
  compositions.set(env, composition);
  return composition;
}
