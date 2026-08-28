// `NotificationPrefs` validation. The route merges submitted values over
// the stored ones, so validation runs on the merge, not on the request: a
// partial update cannot be rejected for the fields it left alone.
import { DEFAULT_NOTIFICATION_PREFS, type NotificationPrefs } from '../entities.js';
import { enumError, intRange, type ValidationDetail } from '../validation.js';

export const NOTIFY_MODES = ['off', 'digest', 'when-ready'] as const;
export const TZ_MAX_LENGTH = 64;

export class PrefsInvalid extends Error {
  constructor(readonly errors: ValidationDetail[]) {
    super('notification prefs failed validation');
  }
}

interface IntField {
  name: keyof NotificationPrefs;
  ge: number;
  le: number;
}

const INT_FIELDS: IntField[] = [
  { name: 'digest_hour', ge: 0, le: 23 },
  { name: 'threshold', ge: 1, le: 99 },
  { name: 'quiet_start_hour', ge: 0, le: 23 },
  { name: 'quiet_end_hour', ge: 0, le: 23 },
];

const intType = (name: string, input: unknown): ValidationDetail => ({
  type: 'int_type',
  loc: [name],
  msg: 'Input should be a valid integer',
  input,
});

const boolType = (name: string, input: unknown): ValidationDetail => ({
  type: 'bool_type',
  loc: [name],
  msg: 'Input should be a valid boolean',
  input,
});

const stringType = (name: string, input: unknown): ValidationDetail => ({
  type: 'string_type',
  loc: [name],
  msg: 'Input should be a valid string',
  input,
});

/** Validate the merged dict, reporting every failure in field order. */
export function validatePrefs(merged: Record<string, unknown>): NotificationPrefs {
  const errors: ValidationDetail[] = [];
  const mode = merged['mode'] ?? DEFAULT_NOTIFICATION_PREFS.mode;
  if (typeof mode !== 'string' || !(NOTIFY_MODES as readonly string[]).includes(mode)) errors.push(enumError(['mode'], mode, NOTIFY_MODES));

  const ints: Record<string, number> = {};
  for (const f of INT_FIELDS) {
    const raw = merged[f.name] ?? DEFAULT_NOTIFICATION_PREFS[f.name];
    if (typeof raw !== 'number' || !Number.isInteger(raw)) {
      errors.push(intType(f.name, raw));
      continue;
    }
    if (raw < f.ge) errors.push(intRange([f.name], raw, 'ge', f.ge));
    else if (raw > f.le) errors.push(intRange([f.name], raw, 'le', f.le));
    else ints[f.name] = raw;
  }

  const tz = merged['tz'] ?? DEFAULT_NOTIFICATION_PREFS.tz;
  if (typeof tz !== 'string') errors.push(stringType('tz', tz));
  else if (tz.length > TZ_MAX_LENGTH) {
    errors.push({
      type: 'string_too_long',
      loc: ['tz'],
      msg: `String should have at most ${TZ_MAX_LENGTH} characters`,
      input: tz,
      ctx: { max_length: TZ_MAX_LENGTH },
    });
  }

  const quiet = merged['quiet_hours_enabled'] ?? DEFAULT_NOTIFICATION_PREFS.quiet_hours_enabled;
  if (typeof quiet !== 'boolean') errors.push(boolType('quiet_hours_enabled', quiet));

  if (errors.length) throw new PrefsInvalid(errors);
  return {
    mode: mode as string,
    digest_hour: ints['digest_hour']!,
    tz: tz as string,
    threshold: ints['threshold']!,
    quiet_hours_enabled: quiet as boolean,
    quiet_start_hour: ints['quiet_start_hour']!,
    quiet_end_hour: ints['quiet_end_hour']!,
    last_digest_date: (merged['last_digest_date'] as string | null) ?? null,
    last_when_ready_at: (merged['last_when_ready_at'] as string | null) ?? null,
  };
}
