// UTC instants as RFC-3339 text. Timestamp columns are ordered and filtered
// as strings in SQL, so the exact spelling is part of the schema and not a
// display choice.

export class IsoFormatError extends Error {}

/**
 * `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`, the column format. The fraction is
 * six digits when there is one and absent otherwise, which keeps the text of
 * a whole-second instant shorter than any instant later in the same second.
 */
export function isoUtc(d: Date): string {
  const iso = d.toISOString();
  return iso.endsWith('.000Z') ? `${iso.slice(0, -5)}+00:00` : `${iso.slice(0, -1)}000+00:00`;
}

const ISO =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?(Z|[+-]\d{2}(?::?\d{2})?)?$/;

/**
 * Every RFC-3339 form a column, an archive cell or a client has ever written:
 * a date, optionally a time, optionally a fraction, optionally an offset.
 * Missing parts default to zero and a missing offset means UTC, never local.
 * A fraction finer than a millisecond is truncated, the one lossy case, since
 * `Date` holds no more.
 */
export function parseIso(s: string): Date {
  const m = ISO.exec(s);
  if (!m) throw new IsoFormatError(`invalid isoformat string: ${JSON.stringify(s)}`);
  const [, y, mo, d, h = '0', mi = '0', sec = '0', frac = '', off = ''] = m;
  const year = Number(y), month = Number(mo), day = Number(d);
  const hour = Number(h), minute = Number(mi), second = Number(sec);
  const ms = Number(frac.padEnd(6, '0').slice(0, 3));
  const utc = Date.UTC(year, month - 1, day, hour, minute, second, ms);
  const back = new Date(utc);
  // Date.UTC rolls a day or month past its end over rather than rejecting it.
  const valid =
    back.getUTCFullYear() === year && back.getUTCMonth() === month - 1 && back.getUTCDate() === day &&
    hour < 24 && minute < 60 && second < 60;
  if (!valid) throw new IsoFormatError(`out of range: ${JSON.stringify(s)}`);
  let offsetMin = 0;
  if (off && off !== 'Z') {
    const sign = off[0] === '-' ? -1 : 1;
    const digits = off.slice(1).replace(':', '');
    const oh = Number(digits.slice(0, 2)), om = Number(digits.slice(2) || '0');
    if (oh > 23 || om > 59) throw new IsoFormatError(`offset out of range: ${JSON.stringify(s)}`);
    offsetMin = sign * (oh * 60 + om);
  }
  return new Date(utc - offsetMin * 60_000);
}
