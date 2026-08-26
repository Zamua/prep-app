import { describe, expect, it } from 'vitest';
import type { Limiter } from '../app/ports.js';
import { InstantLimiterCell } from '../runtime/cells/InstantLimiterCell.js';
import { limitsFromEnv } from '../runtime/compose.js';
import { DEFAULT_LIMITS } from '../domain/instant/limiter.js';
import { FakeLimiter } from './fakes/cells.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { fakeEnv } from './helpers.js';

const T0 = '2026-03-14T15:00:00+00:00';
const plus = (s: number) => new Date(Date.parse(T0) + s * 1000).toISOString().replace('.000Z', '+00:00');
const req = (over: Partial<Parameters<Limiter['reserve']>[0]> = {}) => ({ ip: '198.51.100.7', topicChars: 12, userId: null, userIsAnonymous: null, at: T0, ...over });

describe.each([
  ['InstantLimiterCell', () => new InstantLimiterCell(fakeCellState(), fakeEnv({ PREP_INSTANT_BURST_LIMIT: '2' })) as Limiter],
  ['FakeLimiter', () => new FakeLimiter({ ...DEFAULT_LIMITS, burstLimit: 2 }) as Limiter],
])('%s', (_name, make) => {
  it('reserves under the windows and refuses the burst with a retry-after', async () => {
    const limiter = make();
    const first = await limiter.reserve(req());
    expect(first).toEqual({ reservation: { id: 1 } });
    const second = await limiter.reserve(req({ at: plus(10) }));
    expect('reservation' in second).toBe(true);
    const third = await limiter.reserve(req({ at: plus(20) }));
    expect(third).toEqual({ refusal: { kind: 'minute', retryAfterS: 50 } });
    const other = await limiter.reserve(req({ ip: '198.51.100.8', at: plus(20) }));
    expect('reservation' in other).toBe(true);
  });

  it('per-user day windows: anonymous budget for an unknown or anonymous user', async () => {
    const limiter = make();
    const user = 'anon:' + 'ab'.repeat(16);
    for (let i = 0; i < DEFAULT_LIMITS.perAnonUserPerDay; i++) {
      const r = await limiter.reserve(req({ ip: `10.0.0.${i}`, userId: user, userIsAnonymous: true, at: plus(i * 3600) }));
      expect('reservation' in r, `attempt ${i}`).toBe(true);
    }
    const refused = await limiter.reserve(req({ ip: '10.0.1.1', userId: user, userIsAnonymous: true, at: plus(4 * 3600) }));
    expect(refused).toMatchObject({ refusal: { kind: 'day' } });
    const signedIn = await limiter.reserve(req({ ip: '10.0.1.2', userId: 'u', userIsAnonymous: false, at: plus(4 * 3600) }));
    expect('reservation' in signedIn).toBe(true);
  });

  it('resolve stamps the outcome and back-fills the user id without clearing it', async () => {
    const limiter = make();
    const r = await limiter.reserve(req());
    const id = 'reservation' in r ? r.reservation.id : 0;
    await limiter.resolve(id, 'ok', 5, 'anon:new');
    await limiter.resolve(id, 'failed_free', null, null);
    await expect(limiter.resolve(id, 'pending' as 'ok', null, null)).rejects.toThrow(RangeError);
    const day = await limiter.reserve(req({ ip: '10.9.9.9', userId: 'anon:new', userIsAnonymous: true, at: plus(120) }));
    expect('reservation' in day).toBe(true);
  });
});

describe('InstantLimiterCell', () => {
  it('prunes rows past the retention window and keeps the ledger columns', async () => {
    const state = fakeCellState();
    const cell = new InstantLimiterCell(state, fakeEnv());
    await cell.reserve(req({ at: '2026-03-01T00:00:00+00:00' }));
    await cell.reserve(req({ at: T0, topicChars: 7, userId: 'u', userIsAnonymous: false }));
    expect(state.fake.rows('instant_generations')).toEqual([{ id: 1, ip: '198.51.100.7', created_at: T0, outcome: 'pending', cards: null, topic_chars: 7, user_id: 'u' }]);
    await cell.resolve(1, 'ok', 5, null);
    expect(state.fake.rows('instant_generations')[0]).toMatchObject({ outcome: 'ok', cards: 5, user_id: 'u' });
    expect((await cell.fetch(new Request('https://x/'))).status).toBe(501);
  });

  it('reads its limits from the PREP_INSTANT_* vars, defaults on garbage', () => {
    expect(limitsFromEnv({})).toEqual(DEFAULT_LIMITS);
    expect(limitsFromEnv({ PREP_INSTANT_PER_USER_PER_DAY: ' 40 ', PREP_INSTANT_GLOBAL_PER_MINUTE: 'x', PREP_INSTANT_BURST_WINDOW_S: '' })).toEqual({
      ...DEFAULT_LIMITS,
      perUserPerDay: 40,
    });
  });
});
