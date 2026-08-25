import type { Clock } from '../../app/ports.js';

export const ENV_FAKE_NOW = 'PREP_FAKE_NOW';

export class SystemClock implements Clock {
  now(): Date {
    return new Date();
  }
}

export class FixedClock implements Clock {
  constructor(private readonly at: Date) {}
  now(): Date {
    return new Date(this.at.getTime());
  }
}

// ISO-8601 date or date-time; a missing zone means UTC, which is not what
// `new Date` assumes for a naive date-time.
const ISO_RE = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?))?(Z|[+-]\d{2}:\d{2})?$/;

export function parseFakeNow(raw: string): Date {
  const m = ISO_RE.exec(raw.trim());
  if (m) {
    const [, day, time, zone] = m;
    const parsed = new Date(`${day}T${time ?? '00:00:00'}${zone ?? 'Z'}`);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  throw new Error(`${ENV_FAKE_NOW}: cannot parse ${JSON.stringify(raw)} as an ISO-8601 instant`);
}

let warned = false;

/** `PREP_FAKE_NOW` set means a `FixedClock`, warned once per isolate. */
export function clockFromEnv(env: { PREP_FAKE_NOW?: string }, warn: (msg: string) => void = console.warn): Clock {
  const raw = env.PREP_FAKE_NOW;
  if (raw === undefined || raw.trim() === '') return new SystemClock();
  const at = parseFakeNow(raw);
  if (!warned) {
    warned = true;
    warn(`${ENV_FAKE_NOW}=${raw}: the clock is pinned`);
  }
  return new FixedClock(at);
}

export function resetClockWarning(): void {
  warned = false;
}
