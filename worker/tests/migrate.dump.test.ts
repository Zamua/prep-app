// `GET /_migrate/dump`: the verifier's read of the cell side. Bounded,
// projected, gated the way the rest of the family is, and closed by the
// same seal.
import { describe, expect, it } from 'vitest';
import type { CellSnapshot } from '../app/entities.js';
import { MAX_CHUNK_ROWS } from '../domain/migrate.js';
import { GLOBAL } from '../runtime/adapters/cells.js';
import type { Env } from '../runtime/env.js';
import { MAX_DUMP_ROWS } from '../runtime/routes/migrateDump.js';
import worker from '../runtime/worker.js';
import { INTERNAL_TOKEN, ORIGIN, replayEnv } from './api/harness.js';

const ALICE = 'alice@example.com';
const REVIEWS = 12;

interface Body {
  table: string;
  rows: Record<string, unknown>[];
  next: number | null;
}

function get(env: Env, query: string, token: string | null = INTERNAL_TOKEN): Promise<Response> {
  const headers = new Headers();
  if (token !== null) headers.set('x-internal-token', token);
  return worker.fetch(new Request(`${ORIGIN}/_migrate/dump?${query}`, { headers }), env);
}

async function body(res: Response): Promise<Body> {
  expect(res.status).toBe(200);
  return (await res.json()) as Body;
}

/** One cell filled through the pre-existing whole-snapshot import, so the
 * dump is read against rows this test did not also format. */
async function seed(env: Env): Promise<void> {
  const snapshot: CellSnapshot = {
    profile: null,
    tables: {
      decks: [{ id: 11, name: 'capitals', created_at: '2024-02-01T00:00:00+00:00', desired_retention: 0.8 }],
      questions: [{ id: 101, deck_id: 11, type: 'short', prompt: 'q', answer: 'a', created_at: '2024-04-01T00:00:00+00:00' }],
      cards: [
        {
          question_id: 101,
          step: 3,
          next_due: '2026-09-01T12:00:00+00:00',
          last_review: '2026-08-01T12:00:00+00:00',
          // A double whose shortest repr needs all 17 digits, and one that is
          // integral: JSON writes the second as `30`, which is the only
          // lossy hop in the whole comparison.
          stability: 12.345678901234567,
          difficulty: 5,
          fsrs_state: 2,
        },
      ],
      reviews: Array.from({ length: REVIEWS }, (_, i) => ({
        id: 500 + i,
        question_id: 101,
        ts: '2026-08-01T12:00:00+00:00',
        result: i % 2 ? 'right' : 'wrong',
        user_answer: `ans${i}`,
        grader_notes: '',
      })),
    },
  };
  const cell = env.USER.get(env.USER.idFromName(ALICE)) as unknown as { importRows(s: CellSnapshot): Promise<unknown> };
  await cell.importRows(snapshot);
}

describe('the dump is gated exactly as the rest of /_migrate is', () => {
  it('answers 503 unconfigured, 401 on a wrong token, 200 on the right one', async () => {
    const { env } = replayEnv();
    await seed(env);
    const bare = replayEnv({ PREP_INTERNAL_TOKEN: undefined }).env;
    expect((await get(bare, `user=${ALICE}&table=decks`)).status).toBe(503);
    expect((await get(env, `user=${ALICE}&table=decks`, 'wrong')).status).toBe(401);
    expect((await get(env, `user=${ALICE}&table=decks`, null)).status).toBe(401);
    expect((await get(env, `user=${ALICE}&table=decks`)).status).toBe(200);
  });

  it('runs outside testMode, because the data it verifies lives in production', async () => {
    const { env } = replayEnv({ PREP_ENV: 'prod', PREP_TEST_MODE: undefined, PREP_FAKE_NOW: undefined, PREP_PLACEHOLDER_INDEX: undefined });
    await seed(env);
    const page = await body(await get(env, `user=${ALICE}&table=decks`));
    expect(page.rows).toHaveLength(1);
  });

  it('answers 410 once the fleet is sealed', async () => {
    const { env } = replayEnv();
    await seed(env);
    const sealed = await worker.fetch(
      new Request(`${ORIGIN}/_migrate/seal`, { method: 'POST', headers: { 'x-internal-token': INTERNAL_TOKEN } }),
      env,
    );
    expect(sealed.status).toBe(200);
    expect((await get(env, `user=${ALICE}&table=decks`)).status).toBe(410);
  });
});

describe('one bounded page at a time', () => {
  it('pages by rowid and stops with a null cursor', async () => {
    const { env } = replayEnv();
    await seed(env);
    const seen: unknown[] = [];
    let after: number | null = null;
    let pages = 0;
    do {
      const page: Body = await body(await get(env, `user=${ALICE}&table=reviews&limit=5${after === null ? '' : `&after=${after}`}`));
      seen.push(...page.rows);
      after = page.next;
      pages += 1;
    } while (after !== null);
    expect(pages).toBe(3);
    expect(seen).toHaveLength(REVIEWS);
    expect(seen.map((r) => (r as { id: number }).id)).toEqual(Array.from({ length: REVIEWS }, (_, i) => 500 + i));
  });

  it('is bounded by the same argument the import is', async () => {
    expect(MAX_DUMP_ROWS).toBe(MAX_CHUNK_ROWS);
    const { env } = replayEnv();
    await seed(env);
    const over = await body(await get(env, `user=${ALICE}&table=reviews&limit=${MAX_DUMP_ROWS * 10}`));
    expect(over.rows).toHaveLength(REVIEWS);
    // A full page always names a cursor, even when it happens to be the
    // last: a short page is the only end-of-table signal.
    const exact = await body(await get(env, `user=${ALICE}&table=reviews&limit=${REVIEWS}`));
    expect(exact.next).not.toBeNull();
    const after = await body(await get(env, `user=${ALICE}&table=reviews&after=${exact.next}`));
    expect(after.rows).toEqual([]);
    expect(after.next).toBeNull();
  });

  it('refuses a cursor that is not a non-negative integer instead of reading from the start', async () => {
    const { env } = replayEnv();
    await seed(env);
    expect((await get(env, `user=${ALICE}&table=reviews&after=abc`)).status).toBe(400);
    expect((await get(env, `user=${ALICE}&table=reviews&after=-1`)).status).toBe(400);
    expect((await get(env, `user=${ALICE}&table=reviews&limit=1.5`)).status).toBe(400);
  });

  it('refuses a zero limit rather than answering that the table is empty', async () => {
    const { env } = replayEnv();
    await seed(env);
    const res = await get(env, `user=${ALICE}&table=reviews&limit=0`);
    expect([res.status, await res.json()]).toEqual([400, { detail: 'limit must be a positive integer' }]);
  });
});

describe('what a page carries', () => {
  it('returns every column verbatim, doubles included', async () => {
    const { env } = replayEnv();
    await seed(env);
    const page = await body(await get(env, `user=${ALICE}&table=cards`));
    expect(page.rows).toEqual([
      {
        question_id: 101,
        step: 3,
        next_due: '2026-09-01T12:00:00+00:00',
        last_review: '2026-08-01T12:00:00+00:00',
        stability: 12.345678901234567,
        difficulty: 5,
        fsrs_state: 2,
      },
    ]);
  });

  it('projects to the named columns, so a key check does not carry the bodies', async () => {
    const { env } = replayEnv();
    await seed(env);
    const page = await body(await get(env, `user=${ALICE}&table=reviews&columns=id`));
    expect(page.rows[0]).toEqual({ id: 500 });
    expect(Object.keys(page.rows[0]!)).toEqual(['id']);
  });

  it('never leaks the rowid it paged on', async () => {
    const { env } = replayEnv();
    await seed(env);
    const page = await body(await get(env, `user=${ALICE}&table=decks`));
    expect(Object.keys(page.rows[0]!)).not.toContain('_rowid');
  });
});

describe('the global cells', () => {
  it('dumps the directory and the limiter by name', async () => {
    const { env } = replayEnv();
    const directory = env.DIRECTORY.get(env.DIRECTORY.idFromName(GLOBAL)) as unknown as {
      register(id: string, anon: boolean, at: string, opts?: { idx?: number }): Promise<unknown>;
    };
    await directory.register(ALICE, false, '2024-01-02T03:04:05+00:00', { idx: 1 });
    const limiter = env.INSTANT_LIMITER.get(env.INSTANT_LIMITER.idFromName(GLOBAL)) as unknown as {
      reserve(req: { ip: string; topicChars: number; userId: string | null; userIsAnonymous: boolean | null; at: string }): Promise<unknown>;
    };
    await limiter.reserve({ ip: '203.0.113.7', topicChars: 12, userId: null, userIsAnonymous: null, at: '2026-08-26T13:00:00+00:00' });

    const users = await body(await get(env, 'cell=directory&table=users'));
    expect(users.rows).toEqual([{ id: ALICE, is_anonymous: 0, created_at: '2024-01-02T03:04:05+00:00', idx: 1 }]);
    const ledger = await body(await get(env, 'cell=limiter&table=instant_generations'));
    expect(ledger.rows).toHaveLength(1);
    expect(ledger.rows[0]!['ip']).toBe('203.0.113.7');
  });
});

describe('a name the cell does not have is the diagnosis, not a 500', () => {
  it('refuses an unknown table, an unknown column and a missing argument', async () => {
    const { env } = replayEnv();
    await seed(env);
    const table = await get(env, `user=${ALICE}&table=sqlite_master`);
    expect(table.status).toBe(422);
    expect(await table.json()).toEqual({ detail: 'no such table: sqlite_master' });
    const column = await get(env, `user=${ALICE}&table=decks&columns=id,nope`);
    expect(column.status).toBe(422);
    expect(await column.json()).toEqual({ detail: 'decks has no column nope' });
    expect((await get(env, `user=${ALICE}`)).status).toBe(422);
    expect((await get(env, 'table=decks')).status).toBe(422);
    expect((await get(env, 'cell=nowhere&table=decks')).status).toBe(422);
  });

  it('answers 405 to a write', async () => {
    const { env } = replayEnv();
    const res = await worker.fetch(
      new Request(`${ORIGIN}/_migrate/dump?user=${ALICE}&table=decks`, { method: 'POST', headers: { 'x-internal-token': INTERNAL_TOKEN } }),
      env,
    );
    expect(res.status).toBe(405);
  });
});
