// Sync orchestration for the offline context. Cards first, then reviews in
// reviewed_at order across the whole batch, each replayed through the real
// scheduler. A bad item lands in `rejected` and the batch proceeds; only the
// batch caps are a parse-level rule.
import { RowCapReached } from '../../domain/limits.js';
import { validateRegexUpdate } from '../../domain/grading/index.js';
import { IsoFormatError, parseIso } from '../../domain/time.js';
import type { ReviewResult } from '../entities.js';
import { SyncItemRejected, type Clock, type UserRepos } from '../ports.js';
import { listTooLong, listType, modelAttributesType, RequestValidationError, type ValidationDetail } from '../validation.js';

export const MAX_SYNC_CARDS = 100;
export const MAX_SYNC_REVIEWS = 500;

/** `graded_by` vocabulary -> the marker written to the reviews log. */
const GRADER_NOTES: Record<string, string> = {
  auto: '(offline auto)',
  self: '(offline self-graded)',
};

// Client ids are UUIDs (36 chars); anything wildly longer is a protocol
// violation worth rejecting before it lands in a PK column.
const MAX_CLIENT_ID_CHARS = 64;
// Longest deck label named resolution accepts; past it the card files
// into the inbox instead.
const MAX_DECK_NAME_CHARS = 80;

export interface CardResult {
  client_id?: string;
  status: string;
  question_id?: number;
  error?: string;
}

export interface ReviewResultRow {
  client_id?: string;
  status: string;
  error?: string;
}

export interface SyncResponse {
  cards: CardResult[];
  reviews: ReviewResultRow[];
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** The two batch caps and the item-shape check. */
export function parseBatch(body: unknown): { new_cards: Record<string, unknown>[]; reviews: Record<string, unknown>[] } {
  const o = isObject(body) ? body : {};
  const errors: ValidationDetail[] = [];
  const list = (name: string, max: number): Record<string, unknown>[] => {
    const raw = o[name];
    if (raw === undefined || raw === null) return [];
    if (!Array.isArray(raw)) {
      errors.push(listType(['body', name], raw));
      return [];
    }
    raw.forEach((item, i) => {
      if (!isObject(item)) errors.push(modelAttributesType(['body', name, i], item));
    });
    if (raw.length > max) errors.push(listTooLong(['body', name], raw, max));
    return raw.filter(isObject);
  };
  const newCards = list('new_cards', MAX_SYNC_CARDS);
  const reviews = list('reviews', MAX_SYNC_REVIEWS);
  if (errors.length) throw new RequestValidationError(errors);
  return { new_cards: newCards, reviews };
}

/** Client ids are strings; anything else cannot be correlated to an outbox row. */
function requireClientId(raw: unknown): string {
  const clientId = typeof raw === 'string' ? raw.trim() : '';
  if (!clientId) throw new SyncItemRejected('client_id required');
  if (clientId.length > MAX_CLIENT_ID_CHARS) throw new SyncItemRejected('client_id too long');
  return clientId;
}

/** The id to echo on a reject: the raw value when it is a string. */
const echoClientId = (raw: unknown): string | undefined => (typeof raw === 'string' ? raw : undefined);

/** A label usable for named resolution; anything else files into the inbox. */
function usableDeckName(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const name = raw.trim();
  if (!name || name.length > MAX_DECK_NAME_CHARS) return null;
  if (name.includes('\n') || name.includes('\r')) return null;
  return name;
}

const UTC_OFFSET = /(Z|[+-]\d{2}(:?\d{2})?)$/;

/** ISO-8601 with an offset, required: a naive instant cannot be ordered honestly. */
function parseReviewedAt(raw: unknown): Date {
  if (!raw || typeof raw !== 'string') throw new SyncItemRejected('reviewed_at required');
  let parsed: Date;
  try {
    parsed = parseIso(raw);
  } catch (e) {
    if (e instanceof IsoFormatError) throw new SyncItemRejected('reviewed_at is not ISO-8601');
    throw e;
  }
  if (!UTC_OFFSET.test(raw)) throw new SyncItemRejected('reviewed_at must carry a UTC offset');
  return parsed;
}

const isSafeInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 1 && v < 2 ** 53;

// ---- cards -----------------------------------------------------------------

function processCard(repos: UserRepos, item: Record<string, unknown>): CardResult {
  try {
    const clientId = requireClientId(item['client_id']);
    const prior = repos.idempotency.findSync(clientId);
    if (prior !== null) {
      if (prior.kind !== 'card') throw new SyncItemRejected('client_id already used by a review item');
      return { client_id: clientId, status: 'created', question_id: prior.question_id ?? undefined };
    }
    const prompt = typeof item['prompt'] === 'string' ? (item['prompt'] as string).trim() : '';
    const answer = typeof item['answer'] === 'string' ? (item['answer'] as string).trim() : '';
    if (!prompt) throw new SyncItemRejected('prompt required');
    if (!answer) throw new SyncItemRejected('answer required');
    // The client's regex is never trusted: an unusable pattern stores null.
    const regex = validateRegexUpdate(item['answer_regex'], answer);

    let deckId: number;
    const rawDeckId = item['deck_id'];
    if (rawDeckId === undefined || rawDeckId === null) {
      const deckName = usableDeckName(item['deck_name']);
      deckId = deckName === null ? repos.offline.resolveSrsInbox() : repos.offline.resolveNamedSrsDeck(deckName);
    } else if (!isSafeInt(rawDeckId)) {
      throw new SyncItemRejected('unknown deck_id');
    } else {
      deckId = rawDeckId;
    }
    let qid: number;
    try {
      qid = repos.offline.createCard(clientId, deckId, prompt, answer, regex);
    } catch (e) {
      // Concurrent flush of the same outbox: the winner's committed
      // outcome is this item's outcome.
      if (e instanceof SyncItemRejected || e instanceof RowCapReached) throw e;
      const again = repos.idempotency.findSync(clientId);
      if (again === null || again.kind !== 'card') throw e;
      qid = again.question_id as number;
    }
    return { client_id: clientId, status: 'created', question_id: qid };
  } catch (e) {
    // The cap is a property of the account, not of the item, but it still
    // reports per item: a full account must not 4xx a replayable batch.
    if (e instanceof SyncItemRejected || e instanceof RowCapReached) {
      const echoed = echoClientId(item['client_id']);
      return echoed === undefined ? { status: 'rejected', error: e.message } : { client_id: echoed, status: 'rejected', error: e.message };
    }
    throw e;
  }
}

// ---- reviews ---------------------------------------------------------------

interface PreparedReview {
  client_id: string;
  question_id: number;
  verdict: ReviewResult;
  user_answer: string;
  reviewed_at: Date;
  notes: string;
}

function prepareReview(repos: UserRepos, item: Record<string, unknown>, serverNow: Date): { replay: ReviewResultRow } | PreparedReview {
  const clientId = requireClientId(item['client_id']);
  const prior = repos.idempotency.findSync(clientId);
  if (prior !== null) {
    if (prior.kind !== 'review') throw new SyncItemRejected('client_id already used by a card item');
    return { replay: { client_id: clientId, status: prior.status } };
  }
  const rawVerdict = item['verdict'];
  if (rawVerdict !== 'right' && rawVerdict !== 'wrong') throw new SyncItemRejected('unknown verdict');
  const verdict = rawVerdict as ReviewResult;

  const gradedBy = item['graded_by'];
  let notes = typeof gradedBy === 'string' ? GRADER_NOTES[gradedBy] : undefined;
  if (notes === undefined) throw new SyncItemRejected('unknown graded_by');

  let reviewedAt = parseReviewedAt(item['reviewed_at']);
  if (reviewedAt.getTime() > serverNow.getTime()) {
    // Clock skew: clamp before ordering, keeping the original in the trail.
    notes += ` (client reviewed_at ${String(item['reviewed_at'])} clamped to server now)`;
    reviewedAt = serverNow;
  }

  const hasQuestion = item['question_id'] !== undefined && item['question_id'] !== null;
  const hasClientCard = item['card_client_id'] !== undefined && item['card_client_id'] !== null;
  if (hasQuestion && hasClientCard) throw new SyncItemRejected('give question_id or card_client_id, not both');
  let questionId: number;
  if (hasQuestion) {
    if (!isSafeInt(item['question_id'])) throw new SyncItemRejected('unknown question_id');
    questionId = item['question_id'] as number;
  } else if (hasClientCard) {
    if (typeof item['card_client_id'] !== 'string') throw new SyncItemRejected('unknown card_client_id');
    const resolved = repos.offline.resolveCardClientId(item['card_client_id'] as string);
    if (resolved === null) throw new SyncItemRejected('unknown card_client_id');
    questionId = resolved;
  } else {
    throw new SyncItemRejected('question_id or card_client_id required');
  }

  return {
    client_id: clientId,
    question_id: questionId,
    verdict,
    user_answer: typeof item['user_answer'] === 'string' ? (item['user_answer'] as string) : '',
    reviewed_at: reviewedAt,
    notes,
  };
}

function processReviews(repos: UserRepos, items: Record<string, unknown>[], serverNow: Date): ReviewResultRow[] {
  const results: (ReviewResultRow | null)[] = items.map(() => null);
  const runnable: { at: number; index: number; prepared: PreparedReview }[] = [];

  items.forEach((item, i) => {
    let prepared: { replay: ReviewResultRow } | PreparedReview;
    try {
      prepared = prepareReview(repos, item, serverNow);
    } catch (e) {
      if (!(e instanceof SyncItemRejected)) throw e;
      const echoed = echoClientId(item['client_id']);
      results[i] = echoed === undefined ? { status: 'rejected', error: e.message } : { client_id: echoed, status: 'rejected', error: e.message };
      return;
    }
    if ('replay' in prepared) {
      results[i] = prepared.replay;
      return;
    }
    runnable.push({ at: prepared.reviewed_at.getTime(), index: i, prepared });
  });

  runnable.sort((a, b) => a.at - b.at || a.index - b.index);
  // Ids already pinned by an apply THIS batch: a duplicate later in the
  // same request replays the first outcome, as a retried batch would.
  const appliedThisBatch = new Map<string, string>();
  for (const { index, prepared } of runnable) {
    const cid = prepared.client_id;
    const priorStatus = appliedThisBatch.get(cid);
    if (priorStatus !== undefined) {
      results[index] = { client_id: cid, status: priorStatus };
      continue;
    }
    try {
      let status: string;
      try {
        status = repos.offline.applyReview(cid, prepared.question_id, prepared.verdict, prepared.user_answer, prepared.reviewed_at, prepared.notes);
      } catch (e) {
        if (e instanceof SyncItemRejected) throw e;
        const again = repos.idempotency.findSync(cid);
        if (again === null || again.kind !== 'review') throw e;
        status = again.status;
      }
      appliedThisBatch.set(cid, status);
      results[index] = { client_id: cid, status };
    } catch (e) {
      if (!(e instanceof SyncItemRejected)) throw e;
      // A reject writes no pin, so a later same-id item still gets its shot.
      results[index] = { client_id: cid, status: 'rejected', error: e.message };
    }
  }
  return results.filter((r): r is ReviewResultRow => r !== null);
}

export function syncBatch(deps: { repos: UserRepos; clock: Clock }, body: unknown): SyncResponse {
  const batch = parseBatch(body);
  const serverNow = deps.clock.now();
  // Cards first: reviews later in this batch may name an offline-authored
  // card by card_client_id, and the protocol guarantees that order.
  const cards = batch.new_cards.map((item) => processCard(deps.repos, item));
  const reviews = processReviews(deps.repos, batch.reviews, serverNow);
  return { cards, reviews };
}
