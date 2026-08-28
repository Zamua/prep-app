// The contracts corpus, replayed in order against the TypeScript app. One
// env for the whole file: every pair sees what the pairs before it wrote,
// which is how the recording was made.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { comparable, type VolatileRule } from './compare.js';
import { loadCorpus, SEED_USER, replay, replayEnv, seed, type Pair } from './harness.js';
import type { Env } from '../../runtime/env.js';

// The five cards the canned LLM answers a generation with.
const INSTANT_DECK = JSON.stringify([
  { q: 'Year the Bastille fell?', a: '1789', r: '1789' },
  { q: 'The Estates-General had how many estates?', a: 'three', r: 'three|3' },
  { q: 'Who was executed in January 1793?', a: 'Louis XVI', r: 'louis (xvi|16)' },
  { q: 'Robespierre led which committee?', a: 'Committee of Public Safety', r: '(committee of )?public safety' },
  { q: 'The Directory fell to whom?', a: 'Napoleon', r: 'napoleon( bonaparte)?' },
]);


/** `.apkg` codecs land in phase 5. */
const PHASE_5 = new Set(['mcp-call-prep_export_deck_apkg', 'mcp-call-prep_import_apkg']);

/** The deck the deferred `prep_import_apkg` would have created. The list pair
 * that follows it still replays: the recorded list minus that one deck is the
 * whole of what this phase owns, and it is the only assertion that a CSV
 * import lands in the deck list. */
const APKG_DECK = 'mcp-restored';

function withoutApkgDeck(name: string, json: unknown): unknown {
  if (name !== 'v1-decks-list-after') return json;
  const body = json as { decks: { name: string }[] };
  return { ...body, decks: body.decks.filter((d) => d.name !== APKG_DECK) };
}

/** The instant each pair was recorded at, which the corpus states in its
 * notes rather than a header: the refresh window is reached by moving the
 * clock, not by waiting. A name here changes the clock from that pair on. */
const TEST_NOW = '2026-03-14T15:00:00Z';
const CLOCK_FROM: Record<string, string> = {
  'cookie-refreshed-after-window': '2026-04-13T15:00:01Z',
  'cookie-from-the-future-cleared': TEST_NOW,
};

const corpus = loadCorpus('api');
// The corpus header names every value another implementation cannot
// reproduce, including the row ids drawn after the first anonymous mint:
// The recording numbers all users' decks and questions from one sequence, so those
// carry the anonymous accounts' rows and per-cell id blocks do not.
const volatile: VolatileRule[] = corpus.header.volatile ?? [];

interface Outcome {
  pair: Pair;
  expected: Record<string, unknown>;
  actual: Record<string, unknown>;
}

const results = new Map<string, Outcome>();
let mintedCookie: string | null = null;
let bearer = '';
let cardId: number | null = null;

/** The four card tools name the row `prep_add_card` created. The recording
 * read that id off its own response and so does the replay: an id block is
 * a property of the cell, not of the call. */
const CARD_ID_PAIRS = new Set(['mcp-call-prep_get_card', 'mcp-call-prep_update_card', 'mcp-call-prep_suspend_card', 'mcp-call-prep_delete_card']);

function bodyFor(pair: Pair): unknown {
  if (cardId === null || !CARD_ID_PAIRS.has(pair.name)) return undefined;
  const recorded = pair.request.json as { params: { arguments: Record<string, unknown> } };
  return { ...recorded, params: { ...recorded.params, arguments: { ...recorded.params.arguments, card_id: cardId } } };
}

let clockNow = TEST_NOW;

function headersFor(pair: Pair): Record<string, string> {
  const extra: Record<string, string> = { 'x-prep-test-now': clockNow };
  // Every bearer pair follows the mint, so the replay carries the token the
  // settings page just issued, exactly as the recording did.
  if (pair.name !== 'v1-decks-bad-token' && pair.request.headers['authorization']?.startsWith('Bearer prep_pat_')) {
    extra['authorization'] = `Bearer ${bearer}`;
  }
  if (pair.name === 'instant-anonymous-second-deck' && mintedCookie) extra['cookie'] = mintedCookie;
  return extra;
}

beforeAll(async () => {
  const { env } = replayEnv();
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.includes('/chat/completions')) {
      return Response.json({ choices: [{ message: { content: INSTANT_DECK } }], usage: {} });
    }
    throw new Error(`unexpected outbound fetch to ${url}`);
  });
  await seed(env, 'reader', SEED_USER);
  for (const pair of corpus.pairs) {
    clockNow = CLOCK_FROM[pair.name] ?? clockNow;
    if (PHASE_5.has(pair.name)) continue;
    const actual = await replay(env as Env, pair, headersFor(pair), bodyFor(pair));
    if (pair.name === 'mcp-call-prep_add_card') {
      const text = (actual.json as { result: { content: { text: string }[] } }).result.content[0]!.text;
      cardId = (JSON.parse(text) as { id: number }).id;
    }
    // Shown in full exactly once, on the page that mints it; every later
    // render masks the secret, which is why the mask carries no dot.
    if (pair.name === 'settings-api-mint-token') {
      bearer = /prep_pat_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/.exec(actual.text ?? '')?.[0] ?? '';
    }
    if (pair.name === 'instant-visitor-mints') {
      const set = actual.setCookie.find((c) => c.startsWith('prep_anon='));
      if (set) mintedCookie = set.split(';')[0]!;
    }
    results.set(pair.name, {
      pair,
      expected: comparable(pair.name, {
        status: pair.response.status,
        json: withoutApkgDeck(pair.name, pair.response.json),
        text: pair.response.text,
        location: pair.response.location,
        setCookie: pair.response.set_cookie,
      }, volatile),
      actual: comparable(pair.name, actual, volatile),
    });
  }
}, 120_000);

describe('the contracts corpus replays against the TypeScript app', () => {
  const replayed = corpus.pairs.filter((p) => !PHASE_5.has(p.name));

  it.each(replayed.map((p) => p.name))('%s', (name) => {
    const outcome = results.get(name);
    expect(outcome, `${name} was not replayed`).toBeDefined();
    expect(outcome!.actual).toEqual(outcome!.expected);
  });

  it('covers every pair the phase owns', () => {
    expect(replayed.length).toBe(corpus.pairs.length - PHASE_5.size);
    expect(replayed.length).toBe(128);
  });
});
