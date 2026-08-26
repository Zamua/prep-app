// A refusal is the runtime declining to take the work, not the work
// failing. Nothing was half-written, so it costs an attempt of nothing: it
// increments `refusals`, backs off, and the same step runs again.
//
// The three shapes celld raises, by the strings in the binary:
//   `celld output gate: durability unproven: ...`  (DurabilityUnproven)
//   `The Durable Object owner is currently unreachable` (DurableObjectRoutingError),
//   which is the 6-8s window after a node restart
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
  /no longer runs on/i,
  /node is shedding load/i,
  /stateless request limit/i,
  /node lease not renewed/i,
];

export function isRefusal(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  if (REFUSAL_NAMES.has(e.name)) return true;
  return REFUSAL_MESSAGES.some((re) => re.test(e.message));
}

/** 250ms doubling to 8s. The unreachable window is 6-8s, so twelve refusals
 * cover it with headroom; past that the step is genuinely stuck. */
export const REFUSAL_BACKOFF = { initialMs: 250, coefficient: 2, capMs: 8_000 } as const;
export const MAX_REFUSALS = 12;

/** Delay before refusal number `refusals + 1` is retried. */
export function refusalBackoffMs(refusals: number): number {
  const { initialMs, coefficient, capMs } = REFUSAL_BACKOFF;
  return Math.min(capMs, Math.round(initialMs * coefficient ** Math.max(0, refusals)));
}
