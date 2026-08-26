import { describe, expect, it } from 'vitest';
import { reapIdleAnonymous, type ReapDeps } from '../app/auth/reaper.js';
import type { Clock, UserCellRpc } from '../app/ports.js';
import { MAX_AGE_SECONDS } from '../domain/anonCookie.js';
import { BATCH_LIMIT, IDLE_DAYS, cutoffFor, isIdle } from '../domain/reaper.js';
import { UserCell } from '../runtime/cells/UserCell.js';
import type { Env } from '../runtime/env.js';
import { FakeDirectory, FakeJobCells, FakeUserCells } from './fakes/cells.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { fakeEnv } from './helpers.js';

const NOW = new Date('2027-06-01T12:00:00Z');
const IDLE = '2026-01-01T00:00:00+00:00';
const FRESH = '2027-05-01T00:00:00+00:00';

interface Fixture {
  env: Env;
  cells: FakeUserCells;
  directory: FakeDirectory;
  deps: ReapDeps;
  at: { now: Date };
}

function fixture(): Fixture {
  const env = fakeEnv();
  const cells = new FakeUserCells(env);
  const directory = new FakeDirectory();
  const at = { now: NOW };
  const clock: Clock = { now: () => new Date(at.now.getTime()) };
  return { env, cells, directory, at, deps: { cells, jobs: new FakeJobCells(), directory, clock } };
}

let nextIdx = 1;

/** An anonymous account as the instant route mints one: a directory row and a
 * cell holding the guest profile, its deck and one card. */
async function mint(f: Fixture, id: string, lastSeen: string): Promise<void> {
  await f.directory.register(id, true, lastSeen);
  await f.cells.cell(id).createInstantDeck({
    displayName: 'Capitals',
    cards: [{ prompt: 'Capital of France?', answer: 'Paris', answer_regex: null }],
    mint: { id, displayName: 'Guest', idx: nextIdx++ },
    at: lastSeen,
  });
}

/** A fresh activation over the same storage: what celld does after a wipe,
 * and the only way a destroyed cell re-migrates. */
function reactivate(f: Fixture, id: string): void {
  const { storage } = f.cells.entry(id);
  f.cells.cells.set(id, { cell: new UserCell(fakeCellState(storage), f.env), storage });
}

describe('the retention policy', () => {
  it('outlives the cookie, so a cookie that verifies names a live account', () => {
    expect(IDLE_DAYS * 86_400).toBeGreaterThan(MAX_AGE_SECONDS);
  });

  it('keeps an account last seen exactly at the cutoff', () => {
    const cutoff = cutoffFor(NOW);
    expect(cutoff).toBe('2026-06-01T12:00:00+00:00');
    expect(isIdle(cutoff, cutoff)).toBe(false);
    expect(isIdle('2026-06-01T11:59:59+00:00', cutoff)).toBe(true);
  });
});

describe('the reaper walk', () => {
  it('destroys the idle anonymous accounts and leaves the rest alone', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    await mint(f, 'anon:a2', FRESH);
    await mint(f, 'anon:a3', IDLE);

    expect(await reapIdleAnonymous(f.deps)).toEqual({ scanned: 3, reaped: 2, cleaned: 0, failed: 0, cursor: null });
    expect([...f.directory.users.keys()]).toEqual(['anon:a2']);
    expect(await f.cells.cell('anon:a2').lastSeenAt()).toBe(FRESH);
  });

  it('leaves an idle account a merge is already moving', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    await mint(f, 'anon:a2', IDLE);
    await f.directory.beginMerge('anon:a1', 'reader@example.com', IDLE);

    expect(await reapIdleAnonymous(f.deps)).toEqual({ scanned: 2, reaped: 1, cleaned: 0, failed: 0, cursor: null });
    expect([...f.directory.users.keys()]).toEqual(['anon:a1']);
    expect(f.cells.entry('anon:a1').storage.rows('decks')).toHaveLength(1);
  });

  it('deletes in three steps: wipe, tombstone, scrub, then the directory', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    const { storage } = f.cells.entry('anon:a1');
    const before = storage.rows('decks');
    expect(before).toHaveLength(1);

    await reapIdleAnonymous(f.deps);

    const tomb = storage.rows('tombstone')[0]!;
    expect(tomb).toMatchObject({ reason: 'reaped', at: '2027-06-01T12:00:00+00:00', scrubbed_at: '2027-06-01T12:00:00+00:00' });
    expect(Number(tomb['former_bytes'])).toBeGreaterThan(0);
    expect(storage.db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decks'").get()).toBeUndefined();
    expect(await f.directory.tombstoneOf('anon:a1')).toEqual({ reason: 'reaped', at: '2027-06-01T12:00:00+00:00' });
    expect(await f.directory.lookup('anon:a1')).toBeNull();
  });

  it('carries a cursor across walks and stops when the page is short', async () => {
    const f = fixture();
    for (const n of [1, 2, 3, 4, 5]) await mint(f, `anon:a${n}`, IDLE);

    const first = await reapIdleAnonymous(f.deps, { limit: 2 });
    expect(first).toMatchObject({ scanned: 2, reaped: 2, cursor: 'anon:a2' });
    const second = await reapIdleAnonymous(f.deps, { after: first.cursor, limit: 2 });
    expect(second).toMatchObject({ scanned: 2, reaped: 2, cursor: 'anon:a4' });
    const third = await reapIdleAnonymous(f.deps, { after: second.cursor, limit: 2 });
    expect(third).toEqual({ scanned: 1, reaped: 1, cleaned: 0, failed: 0, cursor: null });
    expect(f.directory.users.size).toBe(0);
  });

  it('never lists a provider account', async () => {
    const f = fixture();
    await f.directory.register('parity@example.com', false, IDLE);
    await mint(f, 'anon:a1', IDLE);
    expect(await reapIdleAnonymous(f.deps)).toMatchObject({ scanned: 1, reaped: 1 });
    expect([...f.directory.users.keys()]).toEqual(['parity@example.com']);
  });

  it('clears a directory row whose account is already tombstoned', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    await f.directory.tombstone('anon:a1', 'merged', IDLE);

    expect(await reapIdleAnonymous(f.deps)).toMatchObject({ reaped: 0, cleaned: 1 });
    expect(await f.directory.lookup('anon:a1')).toBeNull();
    // The merge owns that cell's deletion; the reaper only tidies the row.
    expect(f.cells.entry('anon:a1').storage.rows('decks')).toHaveLength(1);
  });

  it('finishes a deletion that stopped after the wipe', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    await f.cells.cell('anon:a1').destroy('reaped', IDLE);
    reactivate(f, 'anon:a1');

    expect(await reapIdleAnonymous(f.deps)).toMatchObject({ reaped: 1, failed: 0 });
    expect(f.cells.entry('anon:a1').storage.rows('tombstone')[0]).toMatchObject({ reason: 'reaped', at: IDLE, scrubbed_at: '2027-06-01T12:00:00+00:00' });
    expect(await f.directory.tombstoneOf('anon:a1')).toMatchObject({ reason: 'reaped' });
  });

  it('reaps an account whose cell never got a profile, on its directory date', async () => {
    const f = fixture();
    await f.directory.register('anon:a1', true, IDLE);
    expect(await reapIdleAnonymous(f.deps)).toMatchObject({ reaped: 1 });
    expect(await f.directory.lookup('anon:a1')).toBeNull();
  });

  it('costs one account, not the batch, when one fails', async () => {
    const f = fixture();
    for (const n of [1, 2, 3]) await mint(f, `anon:a${n}`, IDLE);
    const cells = {
      cell: (id: string): UserCellRpc => {
        const rpc = f.cells.cell(id);
        if (id !== 'anon:a2') return rpc;
        return { ...rpc, destroy: async () => Promise.reject(new Error('unreachable')) } as unknown as UserCellRpc;
      },
    };

    expect(await reapIdleAnonymous({ ...f.deps, cells })).toMatchObject({ scanned: 3, reaped: 2, failed: 1 });
    expect([...f.directory.users.keys()]).toEqual(['anon:a2']);
    expect(await reapIdleAnonymous(f.deps)).toMatchObject({ scanned: 1, reaped: 1, failed: 0 });
  });

  it('is a no-op once the sweep has caught up', async () => {
    const f = fixture();
    await mint(f, 'anon:a1', IDLE);
    await reapIdleAnonymous(f.deps);
    expect(await reapIdleAnonymous(f.deps)).toEqual({ scanned: 0, reaped: 0, cleaned: 0, failed: 0, cursor: null });
  });

  it('defaults to the policy batch', async () => {
    const f = fixture();
    const seen: number[] = [];
    const directory = { ...f.directory, listAnonymous: async (_after: string | null, limit: number) => (seen.push(limit), []) };
    await reapIdleAnonymous({ ...f.deps, directory: directory as unknown as FakeDirectory });
    expect(seen).toEqual([BATCH_LIMIT]);
  });
});
