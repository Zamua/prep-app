// A refusal is the runtime declining to take the work, not the work
// failing. Nothing was half-written, so it costs an attempt of nothing: it
// increments `refusals`, backs off, and the same step runs again.
//
// The shapes celld raises, from the strings in the binary and from a node
// killed under the crash suite:
//   `celld output gate: durability unproven: ...`  (DurabilityUnproven)
//   `The Durable Object owner is currently unreachable` (DurableObjectRoutingError)
//   `remote RPC owner was stale`, which is what the window after a node
//   restart actually answers
//   `node is shedding load` / `node is at its stateless request limit`

/** Raised by an adapter that knows its write was refused, not lost. */
export class DurabilityUnproven extends Error {
  override readonly name = 'DurabilityUnproven';
}

const REFUSAL_NAMES = new Set(['DurabilityUnproven', 'DurableObjectRoutingError', 'NodeUnavailable', 'NodeFenced']);

const REFUSAL_MESSAGES = [
  /durability unproven/i,
  /output gate/i,
  /owner is currently unreachable/i,
  /owner was stale/i,
  /no longer runs on/i,
  /node is shedding load/i,
  /stateless request limit/i,
  /node lease not renewed/i,
];

export function isRefusal(e: unknown): boolean {
  for (let cur: unknown = e, hops = 0; cur instanceof Error && hops < 8; cur = cur.cause, hops++) {
    if (REFUSAL_NAMES.has(cur.name)) return true;
    // celld re-raises a refusal wrapped, as `route failed: DurabilityUnproven`,
    // where the name survives only inside the message and the prose patterns
    // below do not match it.
    if ([...REFUSAL_NAMES].some((n) => cur.message.includes(n))) return true;
    if (REFUSAL_MESSAGES.some((re) => re.test(cur.message))) return true;
  }
  return false;
}

/** 250ms doubling to 8s. The unreachable window is 6-8s, so twelve refusals
 * cover it with headroom; past that the step is genuinely stuck. */
export const REFUSAL_BACKOFF = { initialMs: 250, coefficient: 2, capMs: 8_000 } as const;
export const MAX_REFUSALS = 12;

/**
 * How many times one outbox row is offered to the owner before it is
 * abandoned. A step is bounded by its retry policy and by `MAX_REFUSALS`; a
 * status write had no bound at all, so an owner in a permanently refusing
 * state (a tombstone, an unmigratable schema) kept the cell awake forever.
 * Same number as the refusal cap, on the same backoff: ~64s of trying.
 */
export const MAX_DELIVERY_ATTEMPTS = MAX_REFUSALS;

/** An undelivered row nobody will retry: the job's own progress is unaffected. */
export const isAbandoned = (row: { delivered_at: string | null; attempt: number }): boolean =>
  row.delivered_at === null && row.attempt >= MAX_DELIVERY_ATTEMPTS;

/** Delay before refusal number `refusals + 1` is retried. */
export function refusalBackoffMs(refusals: number): number {
  const { initialMs, coefficient, capMs } = REFUSAL_BACKOFF;
  return Math.min(capMs, Math.round(initialMs * coefficient ** Math.max(0, refusals)));
}
