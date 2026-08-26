import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { AnonCellVanished, MAX_ATTEMPTS, MERGE_FAILED, MERGE_IN_PROGRESS, destroyAccount, hexFrom, mergeAnonymous, type MergeDeps } from '../app/auth/mergeSaga.js';
import { anonAccess, cookieVerdict } from '../app/auth/anonymous.js';
import type { Clock, UserCellRpc } from '../app/ports.js';
import { RowCapReached } from '../domain/limits.js';
import type { Row } from '../domain/merge.js';
import { PARITY_SEED } from '../runtime/compose.js';
import { namespaceUserCells, retrying } from '../runtime/adapters/cells.js';
import { SeededRandom } from '../runtime/adapters/random.js';
import { DATA_TABLES } from '../runtime/adapters/sql/schema.js';
import type { Env } from '../runtime/env.js';
import { FakeDirectory, FakeLimiter, FakeUserCells } from './fakes/cells.js';
import type { FakeCellStorage } from './fakes/sqlStorage.js';
import { fakeEnv, namespaceOf, ROOT } from './helpers.js';

const CORPUS = join(ROOT, '..', 'tests', 'fixtures', 'parity', 'merge');
const read = (name: string) => JSON.parse(readFileSync(join(CORPUS, `${name}.json`), 'utf8'));

interface Corpus {
  header: { anon: string; target: string; user_scoped_tables: Record<string, string[]> };
  users: Record<string, Row | null>;
  tables: Record<string, Record<string, Record<string, Row[]>>>;
}
interface After extends Corpus {
  account_merges: Row[];
  previous_ids: string[];
  result: { counts: Record<string, number>; merged: boolean; reason: string | null; resolved: boolean };
  target_deck_slugs: string[];
}

const before: Corpus = read('before');
const after: After = read('after');
const ANON = before.header.anon;
const TARGET = before.header.target;
const NOW = '2026-03-14T15:00:00+00:00';
const USER_COLUMNS = new Set(['user_id', 'user_login']);
/** The suffix the corpus recorded for the second colliding slug. */
const SUFFIX = 'fd58dd';

const key = (row: Row) => JSON.stringify(row, Object.keys(row).sort());
const multiset = (rows: readonly Row[]) => rows.map(key).sort();
const withoutUser = (row: Row): Row => Object.fromEntries(Object.entries(row).filter(([k]) => !USER_COLUMNS.has(k)));

// ---- loading the corpus into two cells -------------------------------------

function columnsOf(storage: FakeCellStorage, table: string): Set<string> {
  const info = storage.db.prepare(`PRAGMA table_info("${table}")`).all() as { name: string }[];
  return new Set(info.map((c) => c.name));
}

/** Inserts the corpus rows a cell would hold. Every dropped key must be an
 * owner column: anything else means the cell schema drifted from Python's. */
function insertRows(storage: FakeCellStorage, table: string, rows: readonly Row[]): void {
  const columns = columnsOf(storage, table);
  for (const row of rows) {
    const keys = Object.keys(row).filter((k) => columns.has(k));
    const dropped = Object.keys(row).filter((k) => !columns.has(k));
    expect(dropped.filter((k) => !USER_COLUMNS.has(k)), `${table} columns missing from the cell schema`).toEqual([]);
    storage.sql.exec(
      `INSERT INTO "${table}" (${keys.map((k) => `"${k}"`).join(', ')}) VALUES (${keys.map(() => '?').join(', ')})`,
      ...keys.map((k) => row[k] as string),
    );
  }
}

function loadProfile(storage: FakeCellStorage, user: string): void {
  const row = before.users[user]!;
  const profile: Row = { ...row, id: row['tailscale_login'] };
  delete profile['tailscale_login'];
  insertRows(storage, 'profile', [profile]);
}

/** The rows one user owns, per table, in parent-before-child order. */
function loadTables(storage: FakeCellStorage, user: string): void {
  for (const table of DATA_TABLES) {
    const columns = before.tables[table];
    if (!columns) continue;
    for (const byUser of Object.values(columns)) insertRows(storage, table, byUser[user] ?? []);
  }
}

/**
 * The rows the corpus seeds but does not snapshot: they carry no owner
 * column, so Python's merge never named them and ownership followed the
 * foreign key. Across cells nothing follows anything, so they are exactly
 * what the import has to bring along, and the merge oracle cannot pin them.
 */
function loadDerived(storage: FakeCellStorage, opts: { questionId: number; sessionId: string }): void {
  insertRows(storage, 'cards', [
    { question_id: opts.questionId, step: 2, next_due: NOW, stability: 7.5, difficulty: 5.0, fsrs_state: 2, last_review: NOW },
  ]);
  insertRows(storage, 'reviews', [{ id: 1, question_id: opts.questionId, ts: NOW, result: 'right', user_answer: 'Paris' }]);
  insertRows(storage, 'study_session_answers', [
    { session_id: opts.sessionId, question_id: opts.questionId, answered_at: NOW, result: 'right' },
  ]);
  insertRows(storage, 'trivia_queue', [{ question_id: opts.questionId, queue_position: 1 }]);
}

interface Fixture {
  env: Env;
  cells: FakeUserCells;
  directory: FakeDirectory;
  limiter: FakeLimiter;
  deps: MergeDeps;
  clock: { at: Date };
}

function fixture(): Fixture {
  const env = fakeEnv();
  const cells = new FakeUserCells(env);
  const directory = new FakeDirectory();
  const limiter = new FakeLimiter();
  const at = new Date(NOW);
  const clock: Clock = { now: () => new Date(at.getTime()) };

  const anon = cells.entry(ANON).storage;
  loadProfile(anon, ANON);
  loadTables(anon, ANON);
  loadDerived(anon, { questionId: 1, sessionId: 'anonsession000001' });

  const target = cells.entry(TARGET).storage;
  loadProfile(target, TARGET);
  loadTables(target, TARGET);

  for (const row of before.tables['instant_generations']!['user_id']![ANON]!) {
    limiter.rows.push({
      id: Number(row['id']),
      ip: String(row['ip']),
      created_at: String(row['created_at']),
      outcome: String(row['outcome']),
      user_id: ANON,
      cards: null,
      topic_chars: 0,
    });
  }

  void directory.register(ANON, true, NOW);
  void directory.register(TARGET, false, NOW);

  return {
    env,
    cells,
    directory,
    limiter,
    clock: { at },
    deps: { cells, directory, limiter, clock, randomHex: () => SUFFIX },
  };
}

const targetTables = async (f: Fixture) => (await f.cells.cell(TARGET).dump()).tables;

// ---- the oracle ------------------------------------------------------------

describe('the merge saga over the oracle corpus', () => {
  let f: Fixture;
  let result: Awaited<ReturnType<typeof mergeAnonymous>>;

  beforeEach(async () => {
    f = fixture();
    result = await mergeAnonymous(ANON, TARGET, f.deps);
  });

  it('answers the MergeResult the reference did', () => {
    expect(result).toEqual(after.result);
  });

  it('leaves the target holding the rows the reference left, per table', async () => {
    const tables = await targetTables(f);
    let compared = 0;
    for (const [table, columns] of Object.entries(after.tables)) {
      if (table === 'instant_generations') continue;
      for (const byUser of Object.values(columns)) {
        compared++;
        expect(multiset(tables[table] ?? []), table).toEqual(multiset((byUser[TARGET] ?? []).map(withoutUser)));
      }
    }
    expect(compared).toBe(10);
  });

  it('carries the anonymous derived rows the corpus cannot pin', async () => {
    const tables = await targetTables(f);
    expect(tables['cards']).toEqual([expect.objectContaining({ question_id: 1, step: 2, fsrs_state: 2 })]);
    expect(tables['reviews']).toEqual([expect.objectContaining({ question_id: 1, result: 'right' })]);
    expect(tables['study_session_answers']).toEqual([expect.objectContaining({ session_id: 'anonsession000001', question_id: 1 })]);
    expect(tables['trivia_queue']).toEqual([expect.objectContaining({ question_id: 1, queue_position: 1 })]);
  });

  it('carries the preferences the target had not set, and keeps the ones it had', async () => {
    const profile = (await f.cells.cell(TARGET).dump()).profile!;
    expect(profile).toEqual(after.users[TARGET]);
  });

  it('gives the target the decollided slugs', async () => {
    const decks = (await targetTables(f))['decks']!;
    expect(decks.map((d) => String(d['name'])).sort()).toEqual(after.target_deck_slugs);
    expect(decks.find((d) => d['name'] === `capitals-2`)?.['display_name']).toBe('World Capitals');
  });

  it('moves the instant ledger through the limiter, not the row import', async () => {
    expect(f.limiter.rows.map((r) => r.user_id)).toEqual([TARGET]);
    expect((await targetTables(f))['instant_generations']).toBeUndefined();
  });

  it('writes the audit row and the previous ids the offline snapshot reports', async () => {
    const audit = f.directory.merges[0]!;
    expect(audit).toMatchObject({ anon_user_id: ANON, target_user_id: TARGET, status: 'completed', completed_at: NOW, started_at: NOW });
    expect(audit.counts).toEqual(JSON.parse(String(after.account_merges[0]!['counts'])));
    expect(await f.directory.previousIds(TARGET)).toEqual(after.previous_ids);
  });

  it('destroys the anonymous cell in three steps and clears the marker', async () => {
    const storage = f.cells.entry(ANON).storage;
    const tomb = storage.rows('tombstone')[0]!;
    expect(tomb).toMatchObject({ reason: 'merged', at: NOW, scrubbed_at: NOW });
    expect(Number(tomb['former_bytes'])).toBeGreaterThan(0);
    expect(storage.db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decks'").get()).toBeUndefined();
    expect(await f.directory.marker(ANON)).toBeNull();
    expect(await f.directory.tombstoneOf(ANON)).toEqual({ reason: 'merged', at: NOW });
    expect(await f.directory.lookup(ANON)).toBeNull();
  });

  it('refuses the same cookie afterwards without touching the target', async () => {
    const rowsBefore = multiset((await targetTables(f))['decks']!);
    const again = await mergeAnonymous(ANON, TARGET, f.deps);
    expect(again).toEqual({ resolved: true, merged: false, counts: {}, reason: 'anon_missing' });
    expect(cookieVerdict(again)).toBe('clear');
    expect(multiset((await targetTables(f))['decks']!)).toEqual(rowsBefore);
    expect(f.directory.merges).toHaveLength(1);
  });
});

// ---- crashes at every step boundary ----------------------------------------

class Crash extends Error {}

/** Runs `method` and then throws, once: the write landed, the caller did not
 * see the answer. The harder half of a crash boundary, and the one a retry
 * has to be idempotent against. */
function crashAfter(deps: MergeDeps, method: string): MergeDeps {
  let armed = true;
  const wrap = <T extends object>(obj: T): T =>
    new Proxy(obj, {
      get(target, prop) {
        const value = Reflect.get(target, prop) as unknown;
        if (typeof value !== 'function') return value;
        const fn = value as (...args: unknown[]) => unknown;
        return (...args: unknown[]) => {
          const out = fn.apply(target, args);
          if (String(prop) !== method || !armed) return out;
          armed = false;
          return Promise.resolve(out).then(() => {
            throw new Crash(method);
          });
        };
      },
    });
  return {
    ...deps,
    directory: wrap(deps.directory),
    limiter: wrap(deps.limiter),
    cells: { cell: (id: string) => wrap(deps.cells.cell(id)) },
  };
}

/** Everything the merge is supposed to have left behind, less the audit
 * counts, which are the run's record rather than its outcome. */
async function world(f: Fixture): Promise<unknown> {
  const dump = await f.cells.cell(TARGET).dump();
  return {
    tables: Object.fromEntries(Object.entries(dump.tables).map(([t, rows]) => [t, multiset(rows)])),
    profile: dump.profile,
    limiter: f.limiter.rows.map((r) => [r.id, r.user_id]),
    users: [...f.directory.users.keys()].sort(),
    tombstones: [...f.directory.tombstones.entries()],
    markers: [...f.directory.markers.keys()],
    merges: f.directory.merges.map(({ counts: _counts, ...rest }) => rest),
    anonTombstone: f.cells.entry(ANON).storage.rows('tombstone'),
  };
}

/** What a resumed step three still has to do: rows a previous attempt moved
 * are already the target's and count for nobody, and the ledger keyed by a
 * device's client id reads as a conflict rather than a move. */
const REMAINING = {
  active_workflows: 1,
  api_tokens: 1,
  byok_credentials: 1,
  decks: 3,
  notifications_log: 1,
  'offline_sync_idempotency.dropped': 2,
  push_subscriptions: 1,
  questions: 1,
  study_sessions: 1,
  trivia_sessions: 1,
};

/** Boundaries inside step three: what the resumed attempt records, given how
 * far the crashed one got. */
const RESUMED_COUNTS: Record<string, Record<string, number>> = {
  importRows: { ...REMAINING, instant_generations: 1, 'users.editor_input_mode': 1 },
  reassign: { ...REMAINING, 'users.editor_input_mode': 1 },
  carryPreferences: REMAINING,
};

const BOUNDARIES = [
  'beginMerge',
  'dump',
  'importRows',
  'reassign',
  'carryPreferences',
  'completeMerge',
  'destroy',
  'scrub',
  'tombstone',
  'remove',
  'clearMarker',
] as const;

describe('a crash at any step boundary converges on the retry', () => {
  let clean: unknown;

  beforeEach(async () => {
    const f = fixture();
    await mergeAnonymous(ANON, TARGET, f.deps);
    clean = await world(f);
  });

  it.each(BOUNDARIES)('crashing after %s', async (method) => {
    const f = fixture();
    await expect(mergeAnonymous(ANON, TARGET, crashAfter(f.deps, method))).rejects.toBeInstanceOf(Crash);

    const retry = await mergeAnonymous(ANON, TARGET, f.deps);
    expect(await world(f)).toEqual(clean);
    expect(f.directory.merges).toHaveLength(1);
    if (method === 'clearMarker') {
      // Every write had landed; only the flag saying so was lost, and the
      // anonymous cell now answers for itself.
      expect(retry).toEqual({ resolved: true, merged: false, counts: {}, reason: 'anon_missing' });
      return;
    }
    expect(retry).toMatchObject({ resolved: true, merged: true, reason: null });
    expect(retry.counts).toEqual(RESUMED_COUNTS[method] ?? after.result.counts);
  });

  it('a second crash on the same step still converges', async () => {
    const f = fixture();
    await expect(mergeAnonymous(ANON, TARGET, crashAfter(f.deps, 'importRows'))).rejects.toBeInstanceOf(Crash);
    await expect(mergeAnonymous(ANON, TARGET, crashAfter(f.deps, 'importRows'))).rejects.toBeInstanceOf(Crash);
    await mergeAnonymous(ANON, TARGET, f.deps);
    expect(await world(f)).toEqual(clean);
  });
});

// ---- refusals --------------------------------------------------------------

describe('the merge refuses', () => {
  it('the same user, without an audit row', async () => {
    const f = fixture();
    expect(await mergeAnonymous(TARGET, TARGET, f.deps)).toEqual({ resolved: true, merged: false, counts: {}, reason: 'same_user' });
    expect(f.directory.merges).toEqual([]);
  });

  it('an unknown anonymous id, keeping nothing to retry', async () => {
    const f = fixture();
    const result = await mergeAnonymous('anon:' + 'cd'.repeat(16), TARGET, f.deps);
    expect(result).toEqual({ resolved: true, merged: false, counts: {}, reason: 'anon_missing' });
    expect(cookieVerdict(result)).toBe('clear');
    expect(f.directory.merges).toEqual([]);
  });

  it('an id that is not anonymous, keeping the cookie', async () => {
    const f = fixture();
    const result = await mergeAnonymous(TARGET, 'someone@example.com', f.deps);
    expect(result).toEqual({ resolved: false, merged: false, counts: {}, reason: 'not_anonymous' });
    expect(cookieVerdict(result)).toBe('keep');
  });

  it('a target the directory does not know', async () => {
    const f = fixture();
    expect(await mergeAnonymous(ANON, 'stranger@example.com', f.deps)).toEqual({
      resolved: false,
      merged: false,
      counts: {},
      reason: 'target_missing',
    });
    expect(f.directory.merges).toEqual([]);
  });

  it('a second target while a merge of the same id is in flight', async () => {
    const f = fixture();
    await expect(mergeAnonymous(ANON, TARGET, crashAfter(f.deps, 'beginMerge'))).rejects.toBeInstanceOf(Crash);
    const result = await mergeAnonymous(ANON, 'other@example.com', f.deps);
    expect(result).toEqual({ resolved: false, merged: false, counts: {}, reason: MERGE_IN_PROGRESS });
    expect(await f.directory.marker(ANON)).toMatchObject({ target_id: TARGET });
  });
});

// ---- the id the cookie still names -----------------------------------------

describe('a tombstoned id resolves as gone', () => {
  it('after the merge, from the cell and from the directory', async () => {
    const f = fixture();
    await mergeAnonymous(ANON, TARGET, f.deps);
    const state = await f.cells.cell(ANON).precheck();
    expect(state).toEqual({ exists: false, isAnonymous: false, tombstoned: 'merged' });
    expect(anonAccess(state)).toEqual({ kind: 'gone', reason: 'merged' });
    expect(await f.directory.tombstoneOf(ANON)).toMatchObject({ reason: 'merged' });
  });

  it('while the merge is still moving the rows', async () => {
    const f = fixture();
    await expect(mergeAnonymous(ANON, TARGET, crashAfter(f.deps, 'beginMerge'))).rejects.toBeInstanceOf(Crash);
    const state = await f.cells.cell(ANON).precheck();
    expect(state.tombstoned).toBeNull();
    expect(anonAccess(state, await f.directory.marker(ANON))).toEqual({ kind: 'gone', reason: 'merging' });
  });
});

describe('the deletion survives a lost durability ack', () => {
  /** Fails the first call to each named method: the celld output gate rejects
   * a wipe combined with a large write and rolls the whole RPC back. */
  function failsOnce(cell: UserCellRpc, methods: readonly string[]): UserCellRpc {
    const pending = new Set(methods);
    return new Proxy(cell, {
      get(target, prop) {
        const value = Reflect.get(target, prop) as unknown;
        if (typeof value !== 'function') return value;
        const fn = value as (...args: unknown[]) => unknown;
        return (...args: unknown[]) => {
          if (pending.delete(String(prop))) return Promise.reject(new Error('DurabilityUnproven'));
          return fn.apply(target, args);
        };
      },
    });
  }

  it('retries the wipe and the scrub through the cells adapter', async () => {
    const f = fixture();
    const wrapped = new Map<string, UserCellRpc>();
    const namespace = namespaceOf((name) => {
      const cell = failsOnce(f.cells.cell(name), ['destroy', 'scrub']);
      wrapped.set(name, cell);
      return cell;
    });
    const cells = namespaceUserCells(namespace, { attempts: 3, baseMs: 0, sleep: async () => {} });

    await destroyAccount(ANON, 'merged', { cells, directory: f.directory, clock: f.deps.clock });

    expect(wrapped.has(ANON)).toBe(true);
    expect(f.cells.entry(ANON).storage.rows('tombstone')[0]).toMatchObject({ reason: 'merged', scrubbed_at: NOW });
    expect(await f.directory.tombstoneOf(ANON)).toEqual({ reason: 'merged', at: NOW });
  });
});

describe('hexFrom', () => {
  it('draws the suffix the corpus recorded from the parity merge generator', () => {
    expect(hexFrom(new SeededRandom(PARITY_SEED + 1))(3)).toBe(SUFFIX);
  });
});

// ---- giving up -------------------------------------------------------------

/** Fails `method` on every call, not just the first. */
function alwaysFails(deps: MergeDeps, method: string): MergeDeps {
  const wrap = <T extends object>(obj: T): T =>
    new Proxy(obj, {
      get(target, prop) {
        const value = Reflect.get(target, prop) as unknown;
        if (typeof value !== 'function') return value;
        const fn = value as (...args: unknown[]) => unknown;
        if (String(prop) !== method) return (...args: unknown[]) => fn.apply(target, args);
        return () => Promise.reject(new Crash(method));
      },
    });
  return { ...deps, cells: { cell: (id: string) => wrap(deps.cells.cell(id)) } };
}

describe('a merge that fails the same way every time', () => {
  it('gives up after MAX_ATTEMPTS, records why, and resolves the cookie', async () => {
    const f = fixture();
    const broken = alwaysFails(f.deps, 'importRows');
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      await expect(mergeAnonymous(ANON, TARGET, broken)).rejects.toBeInstanceOf(Crash);
    }

    const verdict = await mergeAnonymous(ANON, TARGET, broken);

    expect(verdict).toEqual({ resolved: true, merged: false, counts: {}, reason: MERGE_FAILED });
    expect(cookieVerdict(verdict)).toBe('clear');
    expect(f.directory.merges).toHaveLength(1);
    expect(f.directory.merges[0]).toMatchObject({ status: 'failed', error: `gave up after ${MAX_ATTEMPTS} attempts` });
    // The marker goes with the audit row, so nothing keeps retrying.
    expect(await f.directory.marker(ANON)).toBeNull();
    // The rows never moved: they are still the anonymous account's.
    expect(f.cells.entry(ANON).storage.rows('decks').length).toBeGreaterThan(0);
  });

  it('refuses to record a merge of nothing when the anonymous cell is destroyed mid-saga', async () => {
    const f = fixture();
    const reaped: MergeDeps = {
      ...f.deps,
      directory: new Proxy(f.directory, {
        get(target, prop) {
          const value = Reflect.get(target, prop) as unknown;
          if (String(prop) !== 'beginMerge') return value;
          return async (...args: [string, string, string]) => {
            const out = await target.beginMerge(...args);
            await destroyAccount(ANON, 'reaped', { cells: f.cells, directory: f.directory, clock: f.deps.clock });
            return out;
          };
        },
      }),
    };

    await expect(mergeAnonymous(ANON, TARGET, reaped)).rejects.toBeInstanceOf(AnonCellVanished);
    expect(f.directory.merges[0]).toMatchObject({ status: 'started' });
  });
});

describe('retrying', () => {
  const policy = { attempts: 5, baseMs: 0, sleep: async () => {} };

  it('retries a call that could not reach its cell', async () => {
    let calls = 0;
    await retrying(async () => {
      calls++;
      if (calls < 3) throw new Error('cell unreachable');
    }, policy);
    expect(calls).toBe(3);
  });

  it('does not retry a cap the cell decided on', async () => {
    let calls = 0;
    await expect(
      retrying(async () => {
        calls++;
        throw new RowCapReached('too many decks');
      }, policy),
    ).rejects.toBeInstanceOf(RowCapReached);
    expect(calls).toBe(1);
  });
});
