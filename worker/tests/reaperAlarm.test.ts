// The directory's daily retention walk, driven by its own alarm. The walk
// itself is pinned by tests/reaper.test.ts; what is pinned here is the
// schedule around it, which is the part an eviction has to reconstruct.
import { describe, expect, it } from 'vitest';
import { DirectoryCell } from '../runtime/cells/DirectoryCell.js';
import { UserCell } from '../runtime/cells/UserCell.js';
import { composeWith } from '../runtime/compose.js';
import type { Env } from '../runtime/env.js';
import { AlarmBus } from './fakes/alarms.js';
import { fakeCellState, type FakeCellStorage } from './fakes/sqlStorage.js';
import { MutableClock } from './repos/setup.js';

const NOW = new Date('2027-06-01T12:00:00Z');
const IDLE = '2026-01-01T00:00:00+00:00';
const FRESH = '2027-05-01T00:00:00+00:00';
const DAY_MS = 86_400_000;
/** A wake is never asked for the past, so a due-now sweep lands just after it. */
const FLOOR = 1;

interface Harness {
  clock: MutableClock;
  bus: AlarmBus;
  storage: FakeCellStorage;
  cell(): DirectoryCell;
  userStorage(id: string): FakeCellStorage;
  mint(id: string, lastSeen: string): Promise<void>;
  ids(): Promise<string[]>;
  /** Drops the cell and rebuilds it over the same storage: an eviction. */
  evict(): Promise<void>;
}

function harness(): Harness {
  const clock = new MutableClock(NOW);
  const bus = new AlarmBus(clock);
  const users = new Map<string, { cell: UserCell; storage: FakeCellStorage }>();
  const userEntry = (id: string) => {
    let e = users.get(id);
    if (!e) {
      const state = fakeCellState();
      e = { cell: new UserCell(state, env), storage: state.fake };
      users.set(id, e);
    }
    return e;
  };

  const state = fakeCellState();
  const storage = state.fake;
  let directory: DirectoryCell;

  const namespace = (make: (name: string) => object): DurableObjectNamespace =>
    ({
      idFromName: (name: string) => ({ name, toString: () => name }),
      get: (id: { name: string }) => make(id.name),
    }) as unknown as DurableObjectNamespace;

  const env: Env = {
    USER: namespace((id) => userEntry(id).cell),
    DIRECTORY: namespace(() => directory),
    INSTANT_LIMITER: namespace(() => directory),
    JOB: namespace(() => directory),
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) } as unknown as Fetcher,
    PREP_ENV: 'dev',
    PREP_PARITY_MODE: '1',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_INTERNAL_TOKEN: 'parity-internal-token',
  };
  composeWith(env, { clock });

  directory = new DirectoryCell(state, env);
  bus.register(storage, () => directory.alarm());

  let nextIdx = 1;
  return {
    clock,
    bus,
    storage,
    cell: () => directory,
    userStorage: (id) => userEntry(id).storage,
    mint: async (id, lastSeen) => {
      await directory.register(id, true, lastSeen);
      await userEntry(id).cell.createInstantDeck({
        displayName: 'Capitals',
        cards: [{ prompt: 'Capital of France?', answer: 'Paris', answer_regex: null }],
        mint: { id, displayName: 'Guest', idx: nextIdx++ },
        at: lastSeen,
      });
    },
    ids: async () => (await directory.listAnonymous(null, 100)).map((u) => u.id),
    evict: async () => {
      directory = new DirectoryCell(fakeCellState(storage), env);
      bus.register(storage, () => directory.alarm());
      // The constructor's blockConcurrencyWhile is not awaited by the class.
      await new Promise((r) => setTimeout(r, 0));
    },
  };
}

/** Lets the constructor's blockConcurrencyWhile finish. */
const settled = () => new Promise((r) => setTimeout(r, 0));

describe('the directory alarm', () => {
  it('arms the first sweep the moment the cell exists', async () => {
    const h = harness();
    await settled();
    expect(h.storage.alarmAt).toBe(NOW.getTime() + FLOOR);
  });

  it('reaps the idle accounts on the first fire and comes back a day later', async () => {
    const h = harness();
    await h.mint('anon:a1', IDLE);
    await h.mint('anon:a2', FRESH);
    await settled();

    expect(await h.bus.settle()).toBe(1);
    const swept = h.clock.now().getTime();
    expect(await h.ids()).toEqual(['anon:a2']);
    expect(h.storage.alarmAt).toBe(swept + DAY_MS);

    // A day of wall time later, the sweep runs again and finds nothing.
    await h.bus.settleThrough(swept + DAY_MS + 1);
    expect(await h.ids()).toEqual(['anon:a2']);
    expect(h.storage.alarmAt).toBe(swept + 2 * DAY_MS);
  });

  it('carries a cursor between pages and closes the sweep on a short one', async () => {
    const h = harness();
    for (const n of [1, 2, 3, 4, 5]) await h.mint(`anon:a${n}`, IDLE);
    await settled();
    // A page is fifty accounts, so five is one short page and one sweep. The
    // day between sweeps is the assertion that the walk closed.
    await h.bus.settle();
    expect(await h.ids()).toEqual([]);
    expect(h.storage.alarmAt).toBe(h.clock.now().getTime() + DAY_MS);
  });

  it('re-arms from the row alone after an eviction', async () => {
    const h = harness();
    await h.mint('anon:a1', IDLE);
    await settled();
    await h.bus.settle();
    const armed = h.storage.alarmAt;
    expect(armed).toBe(h.clock.now().getTime() + DAY_MS);

    h.storage.alarmAt = null;
    h.clock.advance(6 * 3_600_000);
    await h.evict();
    // The instant the last activation chose, not one recomputed from the
    // moment the cell happened to come back.
    expect(h.storage.alarmAt).toBe(armed);
  });

  it('is a no-op when it fires before the day is up', async () => {
    const h = harness();
    await h.mint('anon:a1', FRESH);
    await settled();
    await h.bus.settle();
    const armed = h.storage.alarmAt!;

    h.clock.advance(3_600_000);
    await h.mint('anon:a2', IDLE);
    await h.cell().alarm();
    // Nothing was due, so the fresh idle account survives until the sweep.
    expect(await h.ids()).toEqual(['anon:a1', 'anon:a2']);
    expect(h.storage.alarmAt).toBe(armed);
  });

  it('leaves the deleted cell tombstoned and scrubbed, as the walk does', async () => {
    const h = harness();
    await h.mint('anon:a1', IDLE);
    await settled();
    await h.bus.settle();

    const tomb = h.userStorage('anon:a1').rows('tombstone')[0]!;
    const at = '2027-06-01T12:00:00.001000+00:00';
    expect(tomb).toMatchObject({ reason: 'reaped', at, scrubbed_at: at });
    expect(await h.cell().tombstoneOf('anon:a1')).toMatchObject({ reason: 'reaped' });
  });
});
