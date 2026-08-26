// The global cells and the user cells as ports over their namespace
// stubs. A cell is unreachable for a few seconds after a node restart, so
// idempotent calls retry with backoff; the one non-idempotent call does not.
import type { Directory, Limiter, UserCellRpc, UserCells } from '../../app/ports.js';

export const GLOBAL = 'global';

export interface RetryPolicy {
  attempts: number;
  baseMs: number;
  sleep(ms: number): Promise<void>;
}

/** Five tries over roughly eight seconds: the restart window measured in spike 6. */
export const DEFAULT_RETRY: RetryPolicy = { attempts: 5, baseMs: 250, sleep: (ms) => new Promise((r) => setTimeout(r, ms)) };

export async function retrying<T>(fn: () => Promise<T>, policy: RetryPolicy = DEFAULT_RETRY): Promise<T> {
  let delay = policy.baseMs;
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (e) {
      if (attempt >= policy.attempts) throw e;
      await policy.sleep(delay);
      delay *= 2;
    }
  }
}

/** Every method of the stub behind the retry, except the names in `direct`;
 * the stub itself is fetched per call, so nothing reaches a namespace at
 * composition time. */
function lazy<T extends object>(stub: () => object, policy: RetryPolicy, direct: ReadonlySet<string>): T {
  return new Proxy({} as T, {
    get(_target, prop) {
      const name = String(prop);
      return (...args: unknown[]) => {
        const call = () => {
          const target = stub() as Record<string, unknown>;
          const method = target[name];
          if (typeof method !== 'function') throw new TypeError(`no rpc method ${name}`);
          return (method as (...a: unknown[]) => Promise<unknown>).apply(target, args);
        };
        return direct.has(name) ? call() : retrying(call, policy);
      };
    },
  });
}

const NONE: ReadonlySet<string> = new Set();

export function namespaceDirectory(ns: DurableObjectNamespace, policy: RetryPolicy = DEFAULT_RETRY): Directory {
  return lazy<Directory>(() => ns.get(ns.idFromName(GLOBAL)), policy, NONE);
}

export function namespaceLimiter(ns: DurableObjectNamespace, policy: RetryPolicy = DEFAULT_RETRY): Limiter {
  return lazy<Limiter>(() => ns.get(ns.idFromName(GLOBAL)), policy, NONE);
}

const NOT_IDEMPOTENT: ReadonlySet<string> = new Set(['createInstantDeck']);

export function namespaceUserCells(ns: DurableObjectNamespace, policy: RetryPolicy = DEFAULT_RETRY): UserCells {
  return {
    cell: (id: string) => lazy<UserCellRpc>(() => ns.get(ns.idFromName(id)), policy, NOT_IDEMPOTENT),
  };
}
