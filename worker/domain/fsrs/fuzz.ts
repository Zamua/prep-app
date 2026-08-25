// Port of py-fsrs 6.3.2 `_get_fuzzed_interval` (MIT, Open Spaced
// Repetition; notice in scheduler.ts). Applied to Review intervals only.

import { pyRound } from '../py';

const FUZZ_RANGES: readonly { start: number; end: number; factor: number }[] = [
  { start: 2.5, end: 7.0, factor: 0.15 },
  { start: 7.0, end: 20.0, factor: 0.1 },
  { start: 20.0, end: Infinity, factor: 0.05 },
];

/** The inclusive bounds py-fsrs draws between for a whole-day interval. */
export function fuzzRange(days: number, maximumInterval: number): [number, number] {
  let delta = 1.0;
  for (const r of FUZZ_RANGES) delta += r.factor * Math.max(Math.min(days, r.end) - r.start, 0.0);
  let min = Math.max(2, pyRound(days - delta));
  const max = Math.min(pyRound(days + delta), maximumInterval);
  min = Math.min(min, max);
  return [min, max];
}

/**
 * The fuzzed whole-day interval. Below 2.5 days the interval is returned
 * unchanged. The draw is `random() * (max - min + 1) + min` rounded
 * half-even, as in the reference, so a draw near 1 lands on max + 1.
 */
export function fuzzedIntervalDays(days: number, random: () => number, maximumInterval: number): number {
  if (days < 2.5) return days;
  const [min, max] = fuzzRange(days, maximumInterval);
  const drawn = random() * (max - min + 1) + min;
  return Math.min(pyRound(drawn), maximumInterval);
}
