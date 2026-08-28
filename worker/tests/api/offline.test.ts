// The offline corpus, replayed in order. The last pair is an anonymous
// account already at its question cap, written past the cap guard the way
// the recording writes it.
import { beforeAll, describe, expect, it } from 'vitest';
import { ANON_MAX_QUESTIONS } from '../../domain/limits.js';
import { COOKIE_NAME } from '../../domain/anonCookie.js';
import { HmacSigner, mintCookie, resolveCookieSecret } from '../../runtime/adapters/anonCookie.js';
import type { Env } from '../../runtime/env.js';
import { comparable, type VolatileRule } from './compare.js';
import { loadCorpus, PARITY_USER, replay, replayEnv, seed, type Pair } from './harness.js';
import type { FakeCellStorage } from '../fakes/sqlStorage.js';

const ANON_ID = 'anon:' + 'ab'.repeat(16);
const PARITY_NOW = '2026-03-14T15:00:00+00:00';

const corpus = loadCorpus('offline');
const volatile: VolatileRule[] = corpus.header.volatile ?? [];

const results = new Map<string, { expected: Record<string, unknown>; actual: Record<string, unknown> }>();

/** An anonymous account holding `questions` cards in one deck, written
 * directly: the cap guard refuses the repository path past the ceiling. */
function seedAnonymousAtCap(storage: FakeCellStorage, questions: number): void {
  const sql = storage.sql;
  sql.exec(
    `INSERT INTO profile (id, display_name, email, created_at, last_seen_at, is_anonymous) VALUES (?, 'Guest', NULL, ?, ?, 1)`,
    ANON_ID,
    PARITY_NOW,
    PARITY_NOW,
  );
  sql.exec(`INSERT INTO decks (name, display_name, created_at) VALUES ('full', 'Full deck', ?)`, PARITY_NOW);
  const deck = Number(sql.exec<{ id: number }>('SELECT id FROM decks WHERE name = ?', 'full').one().id);
  for (let i = 0; i < questions; i++) {
    sql.exec(
      `INSERT INTO questions (deck_id, type, prompt, answer, created_at) VALUES (?, 'short', ?, ?, ?)`,
      deck,
      `Question ${i}?`,
      `answer ${i}`,
      PARITY_NOW,
    );
    const qid = Number(sql.exec<{ id: number }>('SELECT MAX(id) AS id FROM questions').one().id);
    sql.exec('INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)', qid, PARITY_NOW);
  }
}

beforeAll(async () => {
  const { env, userStorage } = replayEnv();
  await seed(env, 'reader', PARITY_USER);
  seedAnonymousAtCap(userStorage(ANON_ID), ANON_MAX_QUESTIONS);
  const secret = await resolveCookieSecret(env, () => {});
  const cookie = await mintCookie(new HmacSigner(secret!), ANON_ID, Math.floor(new Date(PARITY_NOW).getTime() / 1000));

  for (const pair of corpus.pairs) {
    const extra: Record<string, string> = pair.name === 'sync-anonymous-at-cap' ? { cookie: `${COOKIE_NAME}=${cookie}` } : {};
    const actual = await replay(env as Env, pair, extra);
    results.set(pair.name, {
      expected: comparable(
        pair.name,
        {
          status: pair.response.status,
          json: pair.response.json,
          text: pair.response.text,
          location: pair.response.location,
          setCookie: pair.response.set_cookie,
        },
        volatile,
      ),
      actual: comparable(pair.name, actual, volatile),
    });
  }
}, 120_000);

describe('the offline corpus replays against the TypeScript app', () => {
  it.each(corpus.pairs.map((p: Pair) => p.name))('%s', (name) => {
    const outcome = results.get(name);
    expect(outcome, `${name} was not replayed`).toBeDefined();
    expect(outcome!.actual).toEqual(outcome!.expected);
  });

  it('replays all thirteen pairs', () => {
    expect(corpus.pairs.length).toBe(13);
  });
});
