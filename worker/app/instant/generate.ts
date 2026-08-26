// Anonymous free-tier deck generation, transcribed from
// prep/instant/routes.py: one topic in, one deck out. Every response
// carries a `kind` the client branches on; the endpoint never 500s, so
// an unexpected failure maps to `generation_failed` and counts as spend.
//
// This is the only path that mints an anonymous account, and it mints
// one only after a generation succeeds.
import { externalIdFromBytes, ID_BYTES } from '../../domain/anonCookie.js';
import { validateRegexUpdate } from '../../domain/grading/index.js';
import { buildPrompt, displayNameFor, extractCards, sanitizeTopic } from '../../domain/instant/index.js';
import { RowCapReached } from '../../domain/limits.js';
import { codePoints, isoUtc } from '../../domain/py.js';
import { AgentBusy, AgentTimeout, type AgentPort, type Clock, type Directory, type Limiter, type Random, type UserCells } from '../ports.js';

export const ANON_DISPLAY_NAME = 'Guest';

const RATE_LIMITED_COPY: Record<string, string> = {
  minute: 'One deck a minute. Try again shortly.',
  day: "You've reached today's limit. Create a free account to keep going.",
};

const ERROR_COPY: Record<string, string> = {
  busy: 'The free AI is busy right now. Try again in a few minutes.',
  generation_failed: "That didn't work. Try again.",
  invalid_topic: 'Describe your topic in 1 to 500 characters.',
  not_configured: "Instant decks aren't available on this deploy.",
  deck_limit: "You've reached the limit for a guest account. Create a free account to keep going.",
};

/** The upstream refused without spending: contention, not a bad deck. One
 * class with the taxonomy's, so the adapters raise a single busy signal. */
export { AgentBusy as InstantBusy };

export interface InstantDeps {
  clock: Clock;
  random: Random;
  limiter: Limiter;
  directory: Directory;
  cells: UserCells;
  /** null when the deploy funds no free tier. */
  agent: AgentPort | null;
  /** Anonymous accounts need a cookie signing secret to be reachable. */
  anonymousEnabled: boolean;
}

export interface InstantRequest {
  ip: string;
  /** The parsed body, or null when it was absent, oversized or malformed. */
  body: unknown;
  userId: string | null;
  userIsAnonymous: boolean | null;
}

export type InstantOutcome =
  | { ok: true; slug: string; mintedId: string | null }
  | { ok: false; status: number; payload: Record<string, unknown>; retryAfterS: number | null };

function refusal(status: number, kind: string): InstantOutcome {
  return { ok: false, status, payload: { kind, message: ERROR_COPY[kind]! }, retryAfterS: null };
}

function rateLimited(scope: 'minute' | 'day', retryAfterS: number | null): InstantOutcome {
  const payload: Record<string, unknown> = { kind: 'rate_limited', scope, message: RATE_LIMITED_COPY[scope]! };
  if (retryAfterS !== null) payload['retry_after_s'] = retryAfterS;
  return { ok: false, status: 429, payload, retryAfterS };
}

export async function generateInstantDeck(deps: InstantDeps, req: InstantRequest): Promise<InstantOutcome> {
  const raw = typeof req.body === 'object' && req.body !== null && !Array.isArray(req.body) ? (req.body as Record<string, unknown>)['topic'] : null;
  const topic = sanitizeTopic(raw);
  if (topic === null) return refusal(422, 'invalid_topic');
  if (deps.agent === null) return refusal(503, 'not_configured');
  // No signing secret means a signed-out generation has nowhere to land.
  if (req.userId === null && !deps.anonymousEnabled) return refusal(503, 'not_configured');

  const at = isoUtc(deps.clock.now());
  let gate;
  try {
    gate = await deps.limiter.reserve({ ip: req.ip, topicChars: codePoints(topic).length, userId: req.userId, userIsAnonymous: req.userIsAnonymous, at });
  } catch {
    return refusal(429, 'busy');
  }
  if ('refusal' in gate) {
    if (gate.refusal.kind === 'busy') return refusal(429, 'busy');
    return rateLimited(gate.refusal.kind, gate.refusal.retryAfterS);
  }
  const reservationId = gate.reservation.id;

  let cards;
  try {
    const text = await deps.agent.complete({ system: '', user: buildPrompt(topic) });
    cards = extractCards(text, validateRegexUpdate);
  } catch (e) {
    // A timeout is busy too, but the request went out: it spends.
    if (e instanceof AgentBusy && !(e instanceof AgentTimeout)) {
      await resolve(deps, reservationId, 'failed_free', null, null);
      return refusal(429, 'busy');
    }
    // The call went out: a parse failure, a degenerate deck, an adapter
    // failure or an unknown one all count as spend and read the same way.
    await resolve(deps, reservationId, 'failed_spent', null, null);
    return refusal(502, 'generation_failed');
  }

  const mintedId = req.userId === null ? externalIdFromBytes(deps.random.bytes(ID_BYTES)) : null;
  const ownerId = req.userId ?? (mintedId as string);
  let idx = 0;
  if (mintedId !== null) idx = (await deps.directory.register(mintedId, true, at)).idx;

  let slug: string;
  try {
    const stored = await deps.cells.cell(ownerId).createInstantDeck({
      displayName: displayNameFor(topic),
      cards,
      mint: mintedId === null ? null : { id: mintedId, displayName: ANON_DISPLAY_NAME, idx },
      at,
    });
    slug = stored.slug;
  } catch (e) {
    await resolve(deps, reservationId, 'failed_spent', null, req.userId);
    if (e instanceof RowCapReached) return refusal(429, 'deck_limit');
    return refusal(502, 'generation_failed');
  }

  await resolve(deps, reservationId, 'ok', cards.length, ownerId);
  return { ok: true, slug, mintedId };
}

/** Ledger resolution never changes the response: a failure leaves the row
 * pending, which already counts as spend. */
async function resolve(deps: InstantDeps, id: number, outcome: 'ok' | 'failed_spent' | 'failed_free', cards: number | null, userId: string | null): Promise<void> {
  try {
    await deps.limiter.resolve(id, outcome, cards, userId);
  } catch {
    // fail closed
  }
}
