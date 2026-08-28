// Duration parsing for the snooze and mute posts. Either a `preset` chip or
// a `custom` + `unit` pair resolves to an ISO-8601 UTC instant the repos
// store directly.
import { isoUtc } from '../domain/time.js';

/** Far enough out that "muted indefinitely" needs no special case. */
export const FOREVER_ISO = '2099-12-31T23:59:59+00:00';

const HOURS: Record<string, number> = {
  '1h': 1,
  '2h': 2,
  '4h': 4,
  '8h': 8,
  '1d': 24,
  '2d': 48,
  '3d': 72,
  '1w': 24 * 7,
  '2w': 24 * 14,
};

export class DurationError extends Error {}

const HOUR_MS = 3_600_000;

/** The cell runs on UTC, so "local" in the two day-relative presets is UTC. */
function endOfDay(now: Date): Date {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 23, 59, 0, 0));
}

function tomorrowMorning(now: Date): Date {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 8, 0, 0, 0));
}

export function parseUntil(opts: { preset?: string | null; custom?: unknown; unit?: unknown; now: Date }): string {
  const preset = (opts.preset ?? '').trim().toLowerCase() || null;
  if (preset) {
    if (preset === 'forever') return FOREVER_ISO;
    if (preset === 'tonight') return isoUtc(endOfDay(opts.now));
    if (preset === 'tomorrow') return isoUtc(tomorrowMorning(opts.now));
    const hours = HOURS[preset];
    if (hours === undefined) throw new DurationError(`unknown preset '${preset}'`);
    return isoUtc(new Date(opts.now.getTime() + hours * HOUR_MS));
  }
  const custom = opts.custom;
  const unit = opts.unit;
  if (!custom || !unit) throw new DurationError('missing preset OR (custom + unit)');
  const raw = typeof custom === 'number' ? String(custom) : String(custom);
  if (!/^[+-]?\d+$/.test(raw.trim())) throw new DurationError(`custom must be an integer, got '${raw}'`);
  const n = Number(raw.trim());
  if (n < 1 || n > 999) throw new DurationError(`custom out of range (1..999): ${n}`);
  const unitL = String(unit).trim().toLowerCase();
  const perUnit: Record<string, number> = { hours: HOUR_MS, days: 24 * HOUR_MS, weeks: 7 * 24 * HOUR_MS };
  const ms = perUnit[unitL];
  if (ms === undefined) throw new DurationError(`unknown unit '${String(unit)}'`);
  return isoUtc(new Date(opts.now.getTime() + n * ms));
}
