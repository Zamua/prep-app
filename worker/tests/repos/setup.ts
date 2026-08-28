// One migrated user cell's repositories over fake storage, on the pinned clock.
import type { Clock, Random, UserRepos } from '../../app/ports.js';
import { FixedClock } from '../../runtime/adapters/clock.js';
import { SeededRandom, SeededSessionIds } from '../../runtime/adapters/random.js';
import { migrate, USER_MIGRATIONS, userRepos } from '../../runtime/adapters/sql/index.js';
import { FakeCellStorage } from '../fakes/sqlStorage.js';

export const TEST_NOW = new Date('2026-03-14T15:00:00Z');
export const USER = 'seed@example.com';

export interface Cell {
  storage: FakeCellStorage;
  repos: UserRepos;
  clock: MutableClock;
  random: Random;
}

export class MutableClock implements Clock {
  constructor(public at: Date) {}
  now(): Date {
    return new Date(this.at.getTime());
  }
  set(at: Date): void {
    this.at = at;
  }
  advance(ms: number): void {
    this.at = new Date(this.at.getTime() + ms);
  }
}

export function cell(opts: { anonymous?: boolean; profile?: boolean; at?: Date } = {}): Cell {
  const storage = new FakeCellStorage();
  migrate(storage.sql, USER_MIGRATIONS);
  const clock = new MutableClock(opts.at ?? TEST_NOW);
  let counter = 0;
  const random = new SeededRandom(20260314);
  const repos = userRepos(storage, {
    clock,
    random,
    fuzz: false,
    sessionIds: new SeededSessionIds({ get: async () => counter, set: async (n) => void (counter = n) }),
  });
  if (opts.profile !== false) {
    if (opts.anonymous) repos.prefs.createAnonymous('anon:' + 'ab'.repeat(16), 'Guest');
    else repos.prefs.upsert(USER, { email: USER, displayName: 'Seed' });
  }
  return { storage, repos, clock, random };
}

export const at = (base: Date, ms: number) => new Date(base.getTime() + ms);
export const H = 3_600_000;
export const D = 86_400_000;
export const M = 60_000;

export { FixedClock };
