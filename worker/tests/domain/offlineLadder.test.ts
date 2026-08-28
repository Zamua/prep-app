// The offline ladder the PWA re-surfaces cards with while it cannot reach
// FSRS. The fixture is the table both this suite and the shipped module read,
// so a drift in either is a card that reappears at the wrong hour offline.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

const CASES = new URL('../fixtures/offline/ladder_cases.json', import.meta.url).pathname;
const MODULE = new URL('../../../static/js/offline/scheduler.js', import.meta.url).href;

interface Scheduler {
  LADDER_MINUTES: number[];
  TERMINAL_STEP: number;
  transition(step: number, verdict: string): unknown;
  due(now: string, nextDue: string): boolean;
  nextDueIso(now: string, minutes: number): string;
}

const scheduler = (await import(MODULE)) as unknown as Scheduler;

interface Case {
  id: string;
  module: string;
  fn: 'transition' | 'due' | 'nextDueIso';
  args: unknown[];
  expected: unknown;
}

const fixture = JSON.parse(readFileSync(CASES, 'utf8')) as { ladder_minutes: number[]; cases: Case[] };

describe('the offline ladder', () => {
  it('exports the table the fixture names', () => {
    expect(scheduler.LADDER_MINUTES).toEqual(fixture.ladder_minutes);
    expect(scheduler.TERMINAL_STEP).toBe(fixture.ladder_minutes.length - 1);
  });

  it('has cases to run', () => {
    expect(fixture.cases.length).toBeGreaterThan(0);
  });

  it.each(fixture.cases.map((c) => [c.id, c] as const))('%s', (_id, c) => {
    const fn = scheduler[c.fn] as (...args: unknown[]) => unknown;
    expect(fn(...c.args)).toEqual(c.expected);
  });
});
