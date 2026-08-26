import { describe, expect, it } from 'vitest';
import {
  DAY_WINDOW_S,
  DEFAULT_LIMITS,
  SPEND_OUTCOMES,
  TERMINAL_OUTCOMES,
  checkWindows,
  retryAfter,
  type GenerationRow,
  type WindowRequest,
} from '../../domain/instant/limiter.js';
import { isoUtc } from '../../domain/py.js';

const AT = new Date('2026-03-14T15:00:00Z');
const ago = (seconds: number) => isoUtc(new Date(AT.getTime() - seconds * 1000));
const row = (ip: string, secondsAgo: number, outcome = 'ok', user_id: string | null = null): GenerationRow => ({
  ip,
  created_at: ago(secondsAgo),
  outcome,
  user_id,
});
const req = (over: Partial<WindowRequest> = {}): WindowRequest => ({
  ip: '203.0.113.5',
  userId: null,
  userIsAnonymous: null,
  at: AT,
  ...over,
});
/** n rows from distinct other ips, outside the burst window but inside the day. */
const others = (n: number, secondsAgo = 3600) => Array.from({ length: n }, (_, i) => row(`198.51.100.${i}`, secondsAgo + i));

describe('constants', () => {
  it('pins the defaults and outcome classes', () => {
    expect(DEFAULT_LIMITS).toEqual({
      burstLimit: 1,
      burstWindowS: 60,
      perIpPerDay: 3,
      perAnonUserPerDay: 3,
      perUserPerDay: 20,
      globalPerDay: 200,
      globalPerMinute: 4,
    });
    expect(SPEND_OUTCOMES).toEqual(['pending', 'ok', 'failed_spent']);
    expect(TERMINAL_OUTCOMES).toEqual(['ok', 'failed_spent', 'failed_free']);
  });
});

describe('retryAfter', () => {
  it('ceils the remaining seconds, floors at 1, and gives the window when unknown', () => {
    expect(retryAfter(AT, ago(10.5), 60)).toBe(50);
    expect(retryAfter(AT, ago(10), 60)).toBe(50);
    expect(retryAfter(AT, ago(59.999), 60)).toBe(1);
    expect(retryAfter(AT, ago(60), 60)).toBe(1);
    expect(retryAfter(AT, ago(600), 60)).toBe(1);
    expect(retryAfter(AT, null, 60)).toBe(60);
    expect(retryAfter(AT, '', 60)).toBe(60);
    expect(retryAfter(AT, 'not a date', 86400)).toBe(86400);
  });

  it('a naive instant is UTC', () => {
    expect(retryAfter(AT, '2026-03-14T14:59:30', 60)).toBe(30);
  });
});

describe('checkWindows', () => {
  it('burst: limit - 1 admits, limit refuses with the newest row', () => {
    expect(checkWindows([], req())).toBeNull();
    const rows = [row('203.0.113.5', 40, 'failed_free'), row('203.0.113.5', 20, 'failed_free')];
    expect(checkWindows(rows, req())).toEqual({ kind: 'minute', retryAfterS: 40 });
    expect(checkWindows(rows, req(), { ...DEFAULT_LIMITS, burstLimit: 3 })).toBeNull();
  });

  it('a row past the burst window no longer counts', () => {
    expect(checkWindows([row('203.0.113.5', 61, 'failed_free')], req())).toBeNull();
  });

  it('per-ip day: limit - 1 admits, limit refuses with the row that must age out', () => {
    const two = [row('203.0.113.5', 7200, 'pending'), row('203.0.113.5', 3600, 'failed_spent')];
    expect(checkWindows(two, req())).toBeNull();
    const three = [...two, row('203.0.113.5', 600)];
    expect(checkWindows(three, req())).toEqual({ kind: 'day', retryAfterS: DAY_WINDOW_S - 7200 });
    const four = [...three, row('203.0.113.5', 5400)];
    expect(checkWindows(four, req())).toEqual({ kind: 'day', retryAfterS: DAY_WINDOW_S - 5400 });
  });

  it('failed_free counts for burst only', () => {
    const free = [row('203.0.113.5', 7200, 'failed_free'), row('203.0.113.5', 3600, 'failed_free'), row('203.0.113.5', 600, 'failed_free')];
    expect(checkWindows(free, req())).toBeNull();
    expect(checkWindows([...free, row('203.0.113.5', 30, 'failed_free')], req())).toEqual({ kind: 'minute', retryAfterS: 30 });
  });

  it('burst wins over day', () => {
    const rows = [row('203.0.113.5', 7200), row('203.0.113.5', 3600), row('203.0.113.5', 30)];
    expect(checkWindows(rows, req())).toEqual({ kind: 'minute', retryAfterS: 30 });
  });

  it('a row past the day window no longer counts', () => {
    const rows = [row('203.0.113.5', 86401), row('203.0.113.5', 3600), row('203.0.113.5', 600)];
    expect(checkWindows(rows, req())).toBeNull();
  });

  it('per-user day: anonymous 3, missing 3, provider 20', () => {
    const spent = (n: number) => Array.from({ length: n }, (_, i) => row(`198.51.100.${i}`, 3600 + i, 'ok', 'u1'));
    const anon = req({ userId: 'u1', userIsAnonymous: true });
    expect(checkWindows(spent(2), anon)).toBeNull();
    expect(checkWindows(spent(3), anon)).toEqual({ kind: 'day', retryAfterS: DAY_WINDOW_S - 3602 });
    expect(checkWindows(spent(3), req({ userId: 'u1', userIsAnonymous: null }))).toEqual({ kind: 'day', retryAfterS: DAY_WINDOW_S - 3602 });
    const user = req({ userId: 'u1', userIsAnonymous: false });
    expect(checkWindows(spent(3), user)).toBeNull();
    expect(checkWindows(spent(19), user)).toBeNull();
    expect(checkWindows(spent(20), user)).toEqual({ kind: 'day', retryAfterS: DAY_WINDOW_S - 3619 });
    expect(checkWindows(spent(3), req({ userId: 'u2', userIsAnonymous: true }))).toBeNull();
    expect(checkWindows(spent(3), req())).toBeNull();
  });

  it('global minute: 3 admits, 4 refuses busy', () => {
    expect(checkWindows(others(3, 10), req())).toBeNull();
    expect(checkWindows(others(4, 10), req())).toEqual({ kind: 'busy', retryAfterS: null });
    expect(checkWindows(others(4, 10).map((r) => ({ ...r, outcome: 'failed_free' })), req())).toBeNull();
  });

  it('global day: 199 admits, 200 refuses busy', () => {
    expect(checkWindows(others(199), req())).toBeNull();
    expect(checkWindows(others(200), req())).toEqual({ kind: 'busy', retryAfterS: null });
  });

  it('rows without a readable instant are in no window', () => {
    const rows: GenerationRow[] = [
      { ip: '203.0.113.5', created_at: null, outcome: 'ok', user_id: null },
      { ip: '203.0.113.5', created_at: 'garbage', outcome: 'ok', user_id: null },
    ];
    expect(checkWindows(rows, req())).toBeNull();
  });
});
