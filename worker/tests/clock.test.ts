import { beforeEach, describe, expect, it } from 'vitest';
import {
  FixedClock,
  SystemClock,
  clockFromEnv,
  parseFakeNow,
  resetClockWarning,
} from '../runtime/adapters/clock.js';

const FAKE = '2026-03-14T15:00:00Z';
const FAKE_MS = Date.UTC(2026, 2, 14, 15, 0, 0);

beforeEach(() => resetClockWarning());

describe('parseFakeNow', () => {
  it('parses a Z suffix', () => {
    expect(parseFakeNow(FAKE).getTime()).toBe(FAKE_MS);
  });
  it('normalizes an offset to the same instant', () => {
    expect(parseFakeNow('2026-03-14T11:00:00-04:00').getTime()).toBe(FAKE_MS);
  });
  it('reads a naive date-time as UTC', () => {
    expect(parseFakeNow('2026-03-14T15:00:00').getTime()).toBe(FAKE_MS);
    expect(parseFakeNow('2026-03-14').getTime()).toBe(Date.UTC(2026, 2, 14));
  });
  it('names the variable on a malformed value', () => {
    expect(() => parseFakeNow('yesterday')).toThrow(/PREP_FAKE_NOW/);
    expect(() => parseFakeNow('2026-13-45T15:00:00Z')).toThrow(/PREP_FAKE_NOW/);
  });
});

describe('providers', () => {
  it('SystemClock reads the wall clock', () => {
    expect(Math.abs(new SystemClock().now().getTime() - Date.now())).toBeLessThan(5000);
  });
  it('FixedClock returns the pinned instant, a fresh Date each time', () => {
    const clock = new FixedClock(new Date(FAKE_MS));
    const a = clock.now();
    a.setFullYear(1999);
    expect(clock.now().getTime()).toBe(FAKE_MS);
  });
  it('an unset variable resolves the system clock', () => {
    expect(clockFromEnv({})).toBeInstanceOf(SystemClock);
    expect(clockFromEnv({ PREP_FAKE_NOW: '  ' })).toBeInstanceOf(SystemClock);
  });
  it('the variable resolves a fixed clock', () => {
    const clock = clockFromEnv({ PREP_FAKE_NOW: FAKE }, () => {});
    expect(clock).toBeInstanceOf(FixedClock);
    expect(clock.now().toISOString()).toBe('2026-03-14T15:00:00.000Z');
  });
  it('warns once per isolate', () => {
    const warnings: string[] = [];
    clockFromEnv({ PREP_FAKE_NOW: FAKE }, (m) => warnings.push(m));
    clockFromEnv({ PREP_FAKE_NOW: FAKE }, (m) => warnings.push(m));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('PREP_FAKE_NOW');
  });
});
