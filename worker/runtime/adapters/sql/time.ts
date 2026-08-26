// Timestamps in the column format Python writes: `datetime.isoformat()`
// of an aware UTC instant.
import type { Clock } from '../../../app/ports.js';
import { isoUtc } from '../../../domain/py.js';

export { isoUtc };

export const isoNow = (clock: Clock): string => isoUtc(clock.now());

const pad = (n: number) => String(n).padStart(2, '0');

/** `isoformat(timespec="seconds")`. */
export function isoSeconds(d: Date): string {
  return (
    `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}+00:00`
  );
}

export const shifted = (d: Date, ms: number): Date => new Date(d.getTime() + ms);

export const DAY_MS = 86_400_000;
