// The contracts corpus, replayed in order against the TypeScript app. One
// env for the whole file: every pair sees what the pairs before it wrote,
// which is how the Python recording was made.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { comparable, type VolatileRule } from './compare.js';
import { loadCorpus, mintToken, PARITY_USER, replay, replayEnv, seed, type Pair } from './harness.js';
import type { Env } from '../../runtime/env.js';

// The five cards the parity LLM stub answers a generation with.
const INSTANT_DECK = JSON.stringify([
  { q: 'Year the Bastille fell?', a: '1789', r: '1789' },
  { q: 'The Estates-General had how many estates?', a: 'three', r: 'three|3' },
  { q: 'Who was executed in January 1793?', a: 'Louis XVI', r: 'louis (xvi|16)' },
  { q: 'Robespierre led which committee?', a: 'Committee of Public Safety', r: '(committee of )?public safety' },
  { q: 'The Directory fell to whom?', a: 'Napoleon', r: 'napoleon( bonaparte)?' },
]);


/** `.apkg` codecs land in phase 5; the third pair reads the deck the
 * import would have created. */
const PHASE_5 = new Set(['mcp-call-prep_export_deck_apkg', 'mcp-call-prep_import_apkg', 'v1-decks-list-after']);

/** Owned by the auth and pages lanes: the cookie lifecycle needs the
 * harness clock the corpus does not carry, and the mint page is lane C's. */
const OTHER_LANES = new Set([
  'settings-api-mint-token',
  'cookie-fresh-no-refresh',
  'cookie-refreshed-after-window',
  'cookie-refreshed-value-accepted',
  'cookie-from-the-future-cleared',
  'cookie-bad-signature-cleared',
  'cookie-garbage-cleared',
  'forget-device',
  'forget-device-cross-site',
]);

const corpus = loadCorpus('contracts');
// The corpus header names every value another implementation cannot
// reproduce, including the row ids drawn after the first anonymous mint:
// Python numbers all users' decks and questions from one sequence, so those
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

function headersFor(pair: Pair): Record<string, string> {
  const extra: Record<string, string> = {};
  // The recorded bearer is the token the settings page minted; the reader
  // seed's own token proves the same thing and the header is volatile.
  if (pair.name !== 'v1-decks-bad-token' && pair.request.headers['authorization']?.startsWith('Bearer prep_pat_')) {
    extra['authorization'] = `Bearer ${bearer}`;
  }
  if (pair.name === 'instant-anonymous-second-deck' && mintedCookie) extra['cookie'] = mintedCookie;
  return extra;
}

beforeAll(async () => {
  const { env, userStorage } = replayEnv();
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.includes('/chat/completions')) {
      return Response.json({ choices: [{ message: { content: INSTANT_DECK } }], usage: {} });
    }
    throw new Error(`unexpected outbound fetch to ${url}`);
  });
  await seed(env, 'reader', PARITY_USER);
  bearer = await mintToken(userStorage(PARITY_USER), PARITY_USER, 'parity');
  for (const pair of corpus.pairs) {
    if (PHASE_5.has(pair.name) || OTHER_LANES.has(pair.name)) continue;
    const actual = await replay(env as Env, pair, headersFor(pair), bodyFor(pair));
    if (pair.name === 'mcp-call-prep_add_card') {
      const text = (actual.json as { result: { content: { text: string }[] } }).result.content[0]!.text;
      cardId = (JSON.parse(text) as { id: number }).id;
    }
    if (pair.name === 'instant-visitor-mints') {
      const set = actual.setCookie.find((c) => c.startsWith('prep_anon='));
      if (set) mintedCookie = set.split(';')[0]!;
    }
    results.set(pair.name, {
      pair,
      expected: comparable(pair.name, {
        status: pair.response.status,
        json: pair.response.json,
        text: pair.response.text,
        location: pair.response.location,
        setCookie: pair.response.set_cookie,
      }, volatile),
      actual: comparable(pair.name, actual, volatile),
    });
  }
}, 120_000);

describe('the contracts corpus replays against the TypeScript app', () => {
  const replayed = corpus.pairs.filter((p) => !PHASE_5.has(p.name) && !OTHER_LANES.has(p.name));

  it.each(replayed.map((p) => p.name))('%s', (name) => {
    const outcome = results.get(name);
    expect(outcome, `${name} was not replayed`).toBeDefined();
    expect(outcome!.actual).toEqual(outcome!.expected);
  });

  it('covers every pair the phase owns', () => {
    expect(replayed.length).toBe(corpus.pairs.length - PHASE_5.size - OTHER_LANES.size);
    expect(replayed.length).toBe(118);
  });
});
