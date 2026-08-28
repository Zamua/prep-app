// The importer's contract: one chunk lands whole, a replay lands nothing,
// and a run killed mid-user resumes from what the cells already hold.
import { beforeEach, describe, expect, it } from 'vitest';
import type { CellSnapshot, MigrationStatus } from '../app/entities.js';
import { applyDispositions, MAX_CHUNK_ROWS } from '../domain/migrate.js';
import { GLOBAL } from '../runtime/adapters/cells.js';
import { DATA_TABLES } from '../runtime/adapters/sql/schema.js';
import type { Env } from '../runtime/env.js';
import worker from '../runtime/worker.js';
import { INTERNAL_TOKEN, ORIGIN, replayEnv } from './api/harness.js';

type Row = Record<string, unknown>;

interface UserExport {
  user: string;
  idx: number;
  profile: Row;
  tables: Record<string, Row[]>;
}

/** Small enough that the fixture produces many chunks per table, which is
 * what makes a kill land in the middle of one. */
const CHUNK_ROWS = 3;

const ALICE = 'alice@example.com';
const BOB = 'bob@example.com';

/**
 * A snapshot shaped like the exporter's output: the `users` row under
 * its own key, per-user tables verbatim, ids preserved and far below the
 * 2^32 id block.
 */
function fixture(): UserExport[] {
  const alice: UserExport = {
    user: ALICE,
    idx: 1,
    profile: {
      tailscale_login: ALICE,
      display_name: 'Alice',
      profile_pic_url: null,
      email: ALICE,
      created_at: '2024-01-02T03:04:05+00:00',
      last_seen_at: '2026-08-01T09:10:11+00:00',
      is_anonymous: 0,
      notification_prefs: '{"digest": true}',
      editor_input_mode: 'vim',
      active_byok_provider: 'claude-subscription',
      desired_retention: 0.9500000000000001,
    },
    tables: {
      decks: [
        { id: 11, name: 'capitals', created_at: '2024-02-01T00:00:00+00:00', deck_type: 'srs', desired_retention: 0.8, notifications_enabled: 1 },
        { id: 12, name: 'databases', created_at: '2024-03-01T00:00:00+00:00', deck_type: 'trivia', desired_retention: null, notifications_enabled: 0 },
      ],
      questions: [11, 12].flatMap((deck, d) =>
        [0, 1, 2, 3].map((i) => ({
          id: 100 + d * 10 + i,
          deck_id: deck,
          type: 'short',
          prompt: `q${d}${i}`,
          answer: `a${d}${i}`,
          created_at: '2024-04-01T00:00:00+00:00',
          suspended: 0,
        })),
      ),
      cards: [11, 12].flatMap((_, d) =>
        [0, 1, 2, 3].map((i) => ({
          question_id: 100 + d * 10 + i,
          step: i,
          next_due: '2026-09-01T12:00:00+00:00',
          last_review: i ? '2026-08-01T12:00:00+00:00' : null,
          stability: i ? 12.345678901234567 : null,
          difficulty: i ? 5.1 : null,
          fsrs_state: i === 0 ? 1 : 2,
        })),
      ),
      reviews: Array.from({ length: 11 }, (_, i) => ({
        id: 500 + i,
        question_id: 100 + (i % 4),
        ts: '2026-08-01T12:00:00+00:00',
        result: i % 2 ? 'correct' : 'incorrect',
        user_answer: `ans${i}`,
        grader_notes: '',
      })),
      byok_credentials: [
        { provider: 'claude-subscription', ciphertext: 'x', key_prefix: 'sk-', created_at: '2025-01-01T00:00:00+00:00', last_used_at: null },
        { provider: 'openai', ciphertext: 'y', key_prefix: 'sk-o', created_at: '2025-02-01T00:00:00+00:00', last_used_at: null },
      ],
      api_tokens: [{ id: 7, token_hash: 'deadbeef', label: 'cli', key_prefix: 'prep_', created_at: '2025-03-01T00:00:00+00:00', last_used_at: null }],
      push_subscriptions: [
        { endpoint: 'https://push.example/1', p256dh: 'p', auth: 'a', created_at: '2025-04-01T00:00:00+00:00', last_seen_at: '2026-08-01T00:00:00+00:00' },
      ],
    },
  };
  const bob: UserExport = {
    user: BOB,
    idx: 2,
    profile: {
      tailscale_login: BOB,
      display_name: 'anon-bob',
      profile_pic_url: null,
      email: null,
      created_at: '2026-07-01T00:00:00+00:00',
      last_seen_at: '2026-07-20T00:00:00+00:00',
      is_anonymous: 1,
      notification_prefs: null,
      editor_input_mode: null,
      active_byok_provider: null,
      desired_retention: null,
    },
    tables: {
      decks: [{ id: 21, name: 'inbox', created_at: '2026-07-01T00:00:00+00:00', deck_type: 'srs' }],
      questions: [0, 1].map((i) => ({ id: 200 + i, deck_id: 21, type: 'short', prompt: `b${i}`, answer: `c${i}`, created_at: '2026-07-02T00:00:00+00:00' })),
      cards: [0, 1].map((i) => ({ question_id: 200 + i, step: 0, next_due: '2026-08-30T00:00:00+00:00', last_review: null, stability: null, difficulty: null, fsrs_state: 1 })),
      reviews: [0, 1, 2].map((i) => ({ id: 600 + i, question_id: 200 + (i % 2), ts: '2026-07-03T00:00:00+00:00', result: 'correct', user_answer: 'x', grader_notes: '' })),
    },
  };
  return [alice, bob];
}

/** What the cells must hold once the dispositions have run: the manifest's
 * counts minus the rows the importer drops. */
function expectedCounts(u: UserExport): Record<string, number> {
  const out: Record<string, number> = {};
  for (const table of DATA_TABLES) out[table] = applyDispositions(table, u.tables[table] ?? []).rows.length;
  return out;
}

// ---- the migrator, reduced to what the endpoint contract needs -------------

interface Chunk {
  user: string;
  idx: number;
  table: string | null;
  rows: Row[];
  profile: Row | null;
}

/**
 * The spec's resume rule: the first table in insert order whose cell count is
 * short of the export's. Re-sending a whole table is always safe, so this is
 * a floor on the work, never a correctness argument.
 */
function firstShortTable(expected: Record<string, number>, status: MigrationStatus): string | null {
  for (const table of DATA_TABLES) if ((status.tables[table] ?? 0) < (expected[table] ?? 0)) return table;
  return null;
}

/** The chunks a pass owes a user. With no status it is the whole export, in
 * `DATA_TABLES` order so parents precede children; with one it starts at the
 * first short table and skips the profile the cell already has. */
function chunksFrom(u: UserExport, status: MigrationStatus | null): Chunk[] {
  const chunks: Chunk[] = [];
  if (!status?.profile) chunks.push({ user: u.user, idx: u.idx, table: null, rows: [], profile: u.profile });
  const start = status === null ? DATA_TABLES[0]! : firstShortTable(expectedCounts(u), status);
  if (start === null) return chunks;
  for (const table of DATA_TABLES.slice(DATA_TABLES.indexOf(start as (typeof DATA_TABLES)[number]))) {
    const rows = u.tables[table] ?? [];
    for (let i = 0; i < rows.length; i += CHUNK_ROWS) chunks.push({ user: u.user, idx: u.idx, table, rows: rows.slice(i, i + CHUNK_ROWS), profile: null });
  }
  return chunks;
}

class Killed extends Error {}

interface RunReport {
  posted: number;
  inserted: Record<string, number>;
  byUser: Record<string, Record<string, number>>;
  dropped: number;
}

/**
 * One pass of the migrator. `resume` reads the server-side point first;
 * without it the whole export is sent again, which is the shape of a re-run
 * that has lost its local progress file. `killAfter` stops the pass dead
 * between chunks, the way a lost connection or a killed process would.
 */
async function runImport(env: Env, exported: UserExport[], opts: { resume?: boolean; killAfter?: number } = {}): Promise<RunReport> {
  const killAfter = opts.killAfter ?? Infinity;
  const report: RunReport = { posted: 0, inserted: {}, byUser: {}, dropped: 0 };
  for (const u of exported) {
    const status = opts.resume ? await getStatus(env, u.user) : null;
    const mine = (report.byUser[u.user] ??= {});
    for (const chunk of chunksFrom(u, status)) {
      if (report.posted >= killAfter) throw new Killed(`killed after ${report.posted} chunks`);
      const res = await post(env, '/_migrate/import', chunk);
      if (res.status !== 200) throw new Error(`${chunk.user} ${chunk.table}: ${res.status} ${await res.text()}`);
      const body = (await res.json()) as { inserted: Record<string, number>; dropped: number };
      report.posted += 1;
      report.dropped += body.dropped;
      for (const [table, n] of Object.entries(body.inserted)) {
        report.inserted[table] = (report.inserted[table] ?? 0) + n;
        mine[table] = (mine[table] ?? 0) + n;
      }
    }
  }
  return report;
}

// ---- transport ------------------------------------------------------------

function request(path: string, init: RequestInit = {}, token: string | null = INTERNAL_TOKEN): Request {
  const headers = new Headers(init.headers);
  if (token !== null) headers.set('x-internal-token', token);
  return new Request(`${ORIGIN}${path}`, { ...init, headers });
}

async function post(env: Env, path: string, body: unknown, token: string | null = INTERNAL_TOKEN): Promise<Response> {
  const text = JSON.stringify(body);
  return worker.fetch(
    request(path, { method: 'POST', body: text, headers: { 'content-type': 'application/json', 'content-length': String(new TextEncoder().encode(text).length) } }, token),
    env,
  );
}

async function getStatus(env: Env, user: string): Promise<MigrationStatus> {
  const res = await worker.fetch(request(`/_migrate/status?user=${encodeURIComponent(user)}`), env);
  expect(res.status).toBe(200);
  return (await res.json()) as MigrationStatus;
}

const dump = (env: Env, user: string): Promise<CellSnapshot> =>
  (env.USER.get(env.USER.idFromName(user)) as unknown as { dump(): Promise<CellSnapshot> }).dump();

/** The directory's own rows, through the test-only dump so the test never has
 * to know which name addresses the singleton. */
const directoryUsers = async (env: Env): Promise<Row[]> => {
  const res = await worker.fetch(request('/_test/dump?cell=directory'), env);
  expect(res.status).toBe(200);
  return ((await res.json()) as { tables: Record<string, Row[]> }).tables['users'] ?? [];
};

// ---- the suite ------------------------------------------------------------

describe('the import endpoint', () => {
  let env: Env;
  beforeEach(() => {
    env = replayEnv().env;
  });

  it('answers 503 unconfigured and 401 on a token mismatch, as the seed does', async () => {
    const bare = replayEnv({ PREP_INTERNAL_TOKEN: '' }).env;
    expect((await post(bare, '/_migrate/import', {})).status).toBe(503);
    expect((await post(env, '/_migrate/import', {}, 'wrong')).status).toBe(401);
    expect((await worker.fetch(request('/_migrate/status?user=x', {}, null), env)).status).toBe(401);
  });

  // It has to run where the data goes, so it cannot be one of the pins the
  // composition refuses outside dev and staging.
  it('serves on a host with test mode off', async () => {
    const prod = replayEnv({ PREP_ENV: 'prod', PREP_TEST_MODE: undefined, PREP_FAKE_NOW: undefined, PREP_PLACEHOLDER_INDEX: undefined }).env;
    const [alice] = fixture();
    const res = await post(prod, '/_migrate/import', { user: alice!.user, idx: alice!.idx, table: null, rows: [], profile: alice!.profile });
    expect(res.status).toBe(200);
    expect((await getStatus(prod, ALICE)).profile).toBe(true);
  });

  it('refuses a chunk over the caps before it reaches a cell', async () => {
    const over = new Request(`${ORIGIN}/_migrate/import`, {
      method: 'POST',
      headers: { 'x-internal-token': INTERNAL_TOKEN, 'content-type': 'application/json', 'content-length': String(4 * 1024 * 1024 + 1) },
      body: '{}',
    });
    expect(await (await worker.fetch(over, env)).json()).toEqual({ detail: 'chunk over 4 MiB' });

    const rows = Array.from({ length: MAX_CHUNK_ROWS + 1 }, (_, i) => ({ id: 900 + i, name: `d${i}`, created_at: 't' }));
    const many = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'decks', rows });
    expect([many.status, await many.json()]).toEqual([413, { detail: 'chunk over 2000 rows' }]);

    const multi = await post(env, '/_migrate/import', { user: ALICE, idx: 1, tables: { decks: [] } });
    expect([multi.status, await multi.json()]).toEqual([422, { detail: 'one table per chunk' }]);
  });

  it('refuses block 0 and the tables the migration resets', async () => {
    const zero = await post(env, '/_migrate/import', { user: ALICE, idx: 0, table: 'decks', rows: [] });
    expect([zero.status, await zero.json()]).toEqual([422, { detail: 'idx must be an integer above 0' }]);

    const workflows = await post(env, '/_migrate/import', {
      user: ALICE,
      idx: 1,
      table: 'active_workflows',
      rows: [{ workflow_id: 'w', workflow_type: 'grade', status: 'running', started_at: 't', url_path: '/' }],
    });
    expect([workflows.status, await workflows.json()]).toEqual([422, { detail: 'active_workflows is not migrated' }]);

    const unknown = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'profile', rows: [] });
    expect([unknown.status, await unknown.json()]).toEqual([422, { detail: 'unknown table "profile"' }]);
  });
});

describe('a full import', () => {
  let env: Env;
  let exported: UserExport[];
  beforeEach(() => {
    env = replayEnv().env;
    exported = fixture();
  });

  it('lands every row, splits the user row, and drops the subscription credential', async () => {
    const report = await runImport(env, exported);
    expect(report.dropped).toBe(1);

    for (const u of exported) {
      const snapshot = await dump(env, u.user);
      const expected = expectedCounts(u);
      for (const table of DATA_TABLES) expect([table, snapshot.tables[table]!.length]).toEqual([table, expected[table]]);
      expect((await getStatus(env, u.user)).idx).toBe(u.idx);
    }

    // `last_seen_at` verbatim: it is the anonymous reaper's only input, and a
    // clock-stamped upsert would spare every idle account a full retention
    // period.
    const alice = (await dump(env, ALICE)).profile!;
    expect(alice['last_seen_at']).toBe('2026-08-01T09:10:11+00:00');
    expect(alice['created_at']).toBe('2024-01-02T03:04:05+00:00');
    expect(alice['editor_input_mode']).toBe('vim');
    expect(alice['desired_retention']).toBe(0.9500000000000001);
    // The credential is gone, so the column naming it must be too.
    expect(alice['active_byok_provider']).toBeNull();
    expect((await dump(env, ALICE)).tables['byok_credentials']!.map((r) => r['provider'])).toEqual(['openai']);

    expect(await directoryUsers(env)).toEqual([
      { id: ALICE, is_anonymous: 0, created_at: '2024-01-02T03:04:05+00:00', idx: 1 },
      { id: BOB, is_anonymous: 1, created_at: '2026-07-01T00:00:00+00:00', idx: 2 },
    ]);
  });

  it('mints past the migrated ids, so a post-cutover row cannot collide', async () => {
    await runImport(env, exported);
    const cell = env.USER.get(env.USER.idFromName(ALICE)) as unknown as { importChunk(w: { idx: number; table: string; rows: Row[]; profile: null }): Promise<Record<string, number>> };
    // A row with no id of its own takes the next one from the seeded block.
    await cell.importChunk({ idx: 1, table: 'decks', rows: [{ name: 'minted', created_at: '2026-08-26T00:00:00+00:00' }], profile: null });
    const minted = (await dump(env, ALICE)).tables['decks']!.find((r) => r['name'] === 'minted')!;
    expect(Number(minted['id'])).toBeGreaterThan(2 ** 32);
  });
});

describe('re-runnability', () => {
  let env: Env;
  let exported: UserExport[];
  beforeEach(() => {
    env = replayEnv().env;
    exported = fixture();
  });

  it('imports twice and the second run inserts nothing', async () => {
    const first = await runImport(env, exported);
    expect(Object.values(first.inserted).reduce((a, b) => a + b, 0)).toBeGreaterThan(0);
    const after = await Promise.all(exported.map((u) => dump(env, u.user)));

    const second = await runImport(env, exported);
    // Every chunk still goes over the wire; every one of them moves nothing.
    expect(second.posted).toBeGreaterThan(0);
    expect(second.inserted).toEqual({});
    expect(await Promise.all(exported.map((u) => dump(env, u.user)))).toEqual(after);
    expect(await directoryUsers(env)).toEqual([
      { id: ALICE, is_anonymous: 0, created_at: '2024-01-02T03:04:05+00:00', idx: 1 },
      { id: BOB, is_anonymous: 1, created_at: '2026-07-01T00:00:00+00:00', idx: 2 },
    ]);
  });

  it('resumes a run killed mid-user and converges on the same rows', async () => {
    const clean = replayEnv().env;
    await runImport(clean, fixture());
    const whole = await Promise.all(exported.map((u) => dump(clean, u.user)));

    // Killed inside Alice's `questions`, so she is half imported, Bob is
    // untouched, and nothing local records where it stopped.
    await expect(runImport(env, exported, { resume: true, killAfter: 4 })).rejects.toBeInstanceOf(Killed);
    const killed = await getStatus(env, ALICE);
    expect(killed.profile).toBe(true);
    expect(killed.tables['decks']).toBe(2);
    expect(killed.tables['questions']).toBe(2 * CHUNK_ROWS);
    expect(killed.tables['cards']).toBe(0);
    expect(firstShortTable(expectedCounts(exported[0]!), killed)).toBe('questions');
    expect((await getStatus(env, BOB)).profile).toBe(false);

    // The resume reads the cells, restarts at the first short table and
    // re-sends it whole: the chunks that landed insert nothing, and only the
    // rows the kill cost arrive. The complete table before it is not re-sent.
    const resumed = await runImport(env, exported, { resume: true });
    expect(resumed.byUser[ALICE]).toEqual({ questions: 8 - 2 * CHUNK_ROWS, cards: 8, reviews: 11, byok_credentials: 1, api_tokens: 1, push_subscriptions: 1 });
    expect(resumed.byUser[BOB]).toEqual({ decks: 1, questions: 2, cards: 2, reviews: 3 });
    expect(resumed.dropped).toBe(1);

    for (const [i, u] of exported.entries()) {
      expect(await dump(env, u.user)).toEqual(whole[i]);
      expect((await getStatus(env, u.user)).tables).toEqual(expectedCounts(u));
    }
    expect(await directoryUsers(env)).toEqual(await directoryUsers(clean));

    // And the resumed fleet is still a fixed point: a third run moves nothing.
    expect((await runImport(env, exported, { resume: true })).inserted).toEqual({});
  });

  it('lands a chunk whole or not at all, so a killed run holds no half-row', async () => {
    await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: null, rows: [], profile: exported[0]!.profile });
    await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'decks', rows: exported[0]!.tables['decks'] });
    const questions = exported[0]!.tables['questions']!;
    const doomed = await post(env, '/_migrate/import', {
      user: ALICE,
      idx: 1,
      table: 'questions',
      rows: [questions[0], questions[1], { ...questions[2], deck_id: 999 }],
    });
    expect(doomed.status).toBe(422);
    expect(String(((await doomed.json()) as { detail: string }).detail)).toMatch(/FOREIGN KEY/);
    expect((await getStatus(env, ALICE)).tables['questions']).toBe(0);
  });
});

describe('the second pass', () => {
  let env: Env;
  let exported: UserExport[];
  beforeEach(async () => {
    env = replayEnv().env;
    exported = fixture();
    await runImport(env, exported);
  });

  /** The window: the user studies, so the whole FSRS state of one card
   * moves. Nothing about the row's identity changes. */
  function studied(u: UserExport): Row {
    const card = u.tables['cards']![1]!;
    return { ...card, step: Number(card['step']) + 5, next_due: '2026-12-01T00:00:00+00:00', last_review: '2026-08-27T10:00:00+00:00', stability: 42.5, difficulty: 6.25, fsrs_state: 2 };
  }

  it('carries a changed row, which is the only reason the pass exists', async () => {
    // `cards` is never inserted into after creation and always rewritten, so
    // an import that could only insert would put a studying user's PRE-window
    // schedule on the fleet - and no re-run of the same import could repair
    // it, because the key is already there.
    const alice = exported[0]!;
    const moved = studied(alice);
    const res = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'cards', rows: [moved] });
    expect([res.status, await res.json()]).toEqual([200, { idx: 1, inserted: { cards: 1 }, dropped: 0 }]);

    const landed = (await dump(env, ALICE)).tables['cards']!.find((r) => r['question_id'] === moved['question_id']);
    expect(landed).toEqual(moved);
  });

  it('counts rows written, so an unchanged row is not one', async () => {
    // The runbook reads this count as the window's writes. A row re-sent
    // unchanged has to cost nothing, or the number means only "rows sent".
    const alice = exported[0]!;
    const same = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'cards', rows: alice.tables['cards'] });
    expect(await same.json()).toEqual({ idx: 1, inserted: {}, dropped: 0 });

    const mixed = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'cards', rows: [alice.tables['cards']![0], studied(alice)] });
    expect(await mixed.json()).toEqual({ idx: 1, inserted: { cards: 1 }, dropped: 0 });
  });

  it('carries an edit to every mutable table, not only to cards', async () => {
    const alice = exported[0]!;
    const question: Record<string, unknown> = { ...alice.tables['questions']![0]!, prompt: 'edited during the window', suspended: 1 };
    const deck: Record<string, unknown> = { ...alice.tables['decks']![0]!, name: 'renamed', notifications_enabled: 0 };
    for (const [table, row] of [['questions', question], ['decks', deck]] as const) {
      const res = await post(env, '/_migrate/import', { user: ALICE, idx: 1, table, rows: [row] });
      expect([table, await res.json()]).toEqual([table, { idx: 1, inserted: { [table]: 1 }, dropped: 0 }]);
    }
    const held = await dump(env, ALICE);
    expect(held.tables['questions']!.find((r) => r['id'] === question['id'])).toMatchObject({ prompt: 'edited during the window', suspended: 1 });
    expect(held.tables['decks']!.find((r) => r['id'] === deck['id'])).toMatchObject({ name: 'renamed', notifications_enabled: 0 });
  });

  it('carries a profile edit, because the upsert is the only route those columns have', async () => {
    const alice = exported[0]!;
    const edited = { ...alice.profile, display_name: 'Alice II', desired_retention: 0.8, last_seen_at: '2026-08-27T09:00:00+00:00' };
    await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: null, rows: [], profile: edited });
    expect(await dump(env, ALICE)).toMatchObject({ profile: { display_name: 'Alice II', desired_retention: 0.8, last_seen_at: '2026-08-27T09:00:00+00:00' } });
  });
});

describe('the run header', () => {
  const SNAPSHOT = 'a'.repeat(64);

  it('records the snapshot the fleet is being built from, and the status hands it back', async () => {
    const env = replayEnv().env;
    const res = await post(env, '/_migrate/import', { snapshot: SNAPSHOT });
    expect(res.status).toBe(200);
    expect((await res.json()) as { run: { snapshot: string } }).toMatchObject({ run: { snapshot: SNAPSHOT } });

    const status = await worker.fetch(request('/_migrate/status?cell=directory'), env);
    expect((await status.json()) as { run: { snapshot: string } }).toMatchObject({ run: { snapshot: SNAPSHOT } });
  });

  it('refuses anything that is not a sha256 digest', async () => {
    const env = replayEnv().env;
    for (const snapshot of ['', 'nope', 'A'.repeat(64), 'a'.repeat(63)]) {
      const res = await post(env, '/_migrate/import', { snapshot });
      expect([snapshot, res.status, await res.json()]).toEqual([snapshot, 422, { detail: 'snapshot must be a sha256 hex digest' }]);
    }
  });

  it('is cleared by the seal, which is what puts the retention sweep back on', async () => {
    const env = replayEnv().env;
    await post(env, '/_migrate/import', { snapshot: SNAPSHOT });
    await post(env, '/_migrate/seal', {});
    const directory = env.DIRECTORY.get(env.DIRECTORY.idFromName(GLOBAL)) as unknown as { migrationRun(): Promise<unknown> };
    expect(await directory.migrationRun()).toBeNull();
  });
});

describe('the id block', () => {
  it('refuses an idx another account already holds instead of throwing a 500', async () => {
    // A deleted account renumbers a bare rank, and `users.idx` is UNIQUE, so
    // a second pass that re-derived the rank would collide. The answer names
    // both accounts rather than being an HTML error page the RPC layer also
    // spends its whole backoff on.
    const env = replayEnv().env;
    const [alice, bob] = fixture();
    await post(env, '/_migrate/import', { user: alice!.user, idx: 1, table: null, rows: [], profile: alice!.profile });
    const clash = await post(env, '/_migrate/import', { user: bob!.user, idx: 1, table: null, rows: [], profile: bob!.profile });
    expect(clash.status).toBe(422);
    expect(String(((await clash.json()) as { detail: string }).detail)).toBe(`idx 1 already belongs to ${ALICE}, not ${BOB}`);

    // Alice keeps hers, and a re-register of an account already there is
    // still the idx it was minted against.
    const again = await post(env, '/_migrate/import', { user: alice!.user, idx: 9, table: null, rows: [], profile: alice!.profile });
    expect(await again.json()).toMatchObject({ idx: 1 });
  });
});

describe('the global cells', () => {
  const MERGES = [
    { id: 3, anon_user_id: 'anon:aa', target_user_id: ALICE, started_at: '2026-05-01T00:00:00+00:00', completed_at: '2026-05-01T00:00:01+00:00', status: 'completed', counts: '{}', error: null },
    { id: 4, anon_user_id: 'anon:bb', target_user_id: ALICE, started_at: '2026-06-01T00:00:00+00:00', completed_at: null, status: 'started', counts: null, error: null },
  ];
  const LEDGER = [
    { id: 71, ip: '1.2.3.0/24', created_at: '2026-08-25T00:00:00+00:00', outcome: 'ok', cards: 5, topic_chars: 12, user_id: null },
    { id: 72, ip: '1.2.3.0/24', created_at: '2026-08-25T01:00:00+00:00', outcome: 'failed_free', cards: null, topic_chars: 9, user_id: ALICE },
  ];

  it('carries account_merges and the limiter ledger with their ids, and a replay adds nothing', async () => {
    const env = replayEnv().env;
    await runImport(env, fixture());
    const merges = await post(env, '/_migrate/import', { cell: 'directory', table: 'account_merges', rows: MERGES });
    expect(await merges.json()).toEqual({ inserted: { account_merges: 2 }, dropped: 0 });
    const ledger = await post(env, '/_migrate/import', { cell: 'limiter', table: 'instant_generations', rows: LEDGER });
    expect(await ledger.json()).toEqual({ inserted: { instant_generations: 2 }, dropped: 0 });

    // `previous_ids` reads the completed row and nothing else; the row still
    // `started` at export has no marker here and never resumes.
    const directory = env.DIRECTORY.get(env.DIRECTORY.idFromName(GLOBAL)) as unknown as {
      previousIds(id: string): Promise<string[]>;
      dumpTables(): Promise<Record<string, Row[]>>;
    };
    expect(await directory.previousIds(ALICE)).toEqual(['anon:aa']);
    expect((await directory.dumpTables())['account_merges']).toEqual(MERGES);

    expect(await (await post(env, '/_migrate/import', { cell: 'directory', table: 'account_merges', rows: MERGES })).json()).toEqual({ inserted: {}, dropped: 0 });
    expect(await (await post(env, '/_migrate/import', { cell: 'limiter', table: 'instant_generations', rows: LEDGER })).json()).toEqual({ inserted: {}, dropped: 0 });

    const status = await worker.fetch(request('/_migrate/status?cell=directory'), env);
    expect(await status.json()).toEqual({ tables: { users: 2, account_merges: 2 }, run: null });
    const limiter = await worker.fetch(request('/_migrate/status?cell=limiter'), env);
    expect(await limiter.json()).toEqual({ tables: { instant_generations: 2 } });
  });

  it('carries a merge that completed during the window, rather than keeping the started row', async () => {
    // `account_merges` is not append-only: a merge `started` at the first
    // snapshot is `completed` at the second, and `previous_ids` reads the
    // completed row.
    const env = replayEnv().env;
    await runImport(env, fixture());
    await post(env, '/_migrate/import', { cell: 'directory', table: 'account_merges', rows: MERGES });
    const finished = { ...MERGES[1]!, status: 'completed', completed_at: '2026-06-01T00:00:02+00:00', counts: '{"decks":1}' };

    const second = await post(env, '/_migrate/import', { cell: 'directory', table: 'account_merges', rows: [MERGES[0], finished] });
    expect(await second.json()).toEqual({ inserted: { account_merges: 1 }, dropped: 0 });
    const directory = env.DIRECTORY.get(env.DIRECTORY.idFromName(GLOBAL)) as unknown as {
      previousIds(id: string): Promise<string[]>;
      dumpTables(): Promise<Record<string, Row[]>>;
    };
    expect((await directory.dumpTables())['account_merges']).toEqual([MERGES[0], finished]);
    expect(await directory.previousIds(ALICE)).toEqual(['anon:aa', 'anon:bb']);
  });

  it('refuses a global table its cell does not own', async () => {
    const env = replayEnv().env;
    // The directory's `users` rows are the per-user register's, which is also
    // what hands out the id block.
    const users = await post(env, '/_migrate/import', { cell: 'directory', table: 'users', rows: [] });
    expect([users.status, await users.json()]).toEqual([422, { detail: '"users" is not a table the directory cell takes' }]);
    const wrong = await post(env, '/_migrate/import', { cell: 'limiter', table: 'account_merges', rows: [] });
    expect(wrong.status).toBe(422);
    const nowhere = await post(env, '/_migrate/import', { cell: 'jobs', table: 'x', rows: [] });
    expect([nowhere.status, await nowhere.json()]).toEqual([422, { detail: 'unknown cell "jobs"' }]);
  });
});

describe('the seal', () => {
  it('closes every migrate route once the cutover verifies', async () => {
    const env = replayEnv().env;
    await runImport(env, fixture());
    expect(await (await post(env, '/_migrate/seal', {})).json()).toEqual({ sealed: true });

    for (const res of [
      await post(env, '/_migrate/import', { user: ALICE, idx: 1, table: 'decks', rows: [] }),
      await worker.fetch(request(`/_migrate/status?user=${ALICE}`), env),
      await post(env, '/_migrate/seal', {}),
    ]) {
      expect([res.status, await res.json()]).toEqual([410, { detail: 'migration sealed' }]);
    }
    // Still gated: the seal is not a way to learn the fleet's state.
    expect((await post(env, '/_migrate/import', {}, 'wrong')).status).toBe(401);
  });
});
