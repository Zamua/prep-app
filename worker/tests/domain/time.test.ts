import { describe, expect, it } from 'vitest';
import { IsoFormatError, isoUtc, parseIso } from '../../domain/time';

// Timestamp columns are ordered and filtered as strings in SQL, so these
// pin the exact text, not the instant it denotes.

describe('isoUtc', () => {
  it.each([
    ['2026-03-14T15:00:00Z', '2026-03-14T15:00:00+00:00'],
    ['2026-03-17T15:10:00.123Z', '2026-03-17T15:10:00.123000+00:00'],
    ['0999-01-01T00:00:00Z', '0999-01-01T00:00:00+00:00'],
    ['2026-12-31T23:59:59.999Z', '2026-12-31T23:59:59.999000+00:00'],
    ['2026-01-01T00:00:00.001Z', '2026-01-01T00:00:00.001000+00:00'],
  ])('%s renders as %s', (input, want) => {
    expect(isoUtc(new Date(input))).toBe(want);
  });

  it('drops the fraction only when it is zero, and always writes six digits', () => {
    expect(isoUtc(new Date(0))).toBe('1970-01-01T00:00:00+00:00');
    expect(isoUtc(new Date(1))).toBe('1970-01-01T00:00:00.001000+00:00');
  });

  it('refuses an invalid date rather than emitting NaN text', () => {
    expect(() => isoUtc(new Date('nope'))).toThrow(RangeError);
  });
});

describe('parseIso', () => {
  it.each([
    ['2026-03-14T15:00:00+00:00', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14T15:00:00Z', Date.UTC(2026, 2, 14, 15)],
    // No offset means UTC. Reading these as local time would shift every
    // stored timestamp by the host's zone.
    ['2026-03-14T15:00:00', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14 15:00:00', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14T15:00', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14', Date.UTC(2026, 2, 14)],
    ['2026-03-14T15:00:00.5', Date.UTC(2026, 2, 14, 15, 0, 0, 500)],
    ['2026-03-14T15:00:00.123456+00:00', Date.UTC(2026, 2, 14, 15, 0, 0, 123)],
    ['2026-03-14T10:00:00-05:00', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14T20:30:00+05:30', Date.UTC(2026, 2, 14, 15)],
    ['2026-03-14T20:30:00+0530', Date.UTC(2026, 2, 14, 15)],
  ])('%s', (input, want) => {
    expect(parseIso(input).getTime()).toBe(want);
  });

  it.each(['', 'tomorrow', '2026-13-01', '2026-02-30T00:00:00', '2026-03-14T25:00:00', '2026-03-14T15:00:00+0', '20260314'])(
    '%j throws IsoFormatError',
    (s) => {
      expect(() => parseIso(s)).toThrow(IsoFormatError);
    },
  );
});

describe('a stored column round-trips through parse and render', () => {
  // Every shape the timestamp columns hold, whole-second and sub-second.
  const COLUMNS = [
    '2026-08-26T14:00:00+00:00',
    '2026-08-26T14:00:00.123000+00:00',
    '2026-03-14T15:00:00+00:00',
    '2099-12-31T23:59:59+00:00',
    '1970-01-01T00:00:00+00:00',
  ];

  it.each(COLUMNS)('%s survives byte for byte', (value) => {
    expect(isoUtc(parseIso(value))).toBe(value);
  });

  it('normalises the other spellings of the same instant onto that one', () => {
    for (const other of ['2026-08-26T14:00:00Z', '2026-08-26 14:00:00', '2026-08-26T16:00:00+02:00']) {
      expect(isoUtc(parseIso(other))).toBe('2026-08-26T14:00:00+00:00');
    }
  });

  it('truncates below a millisecond, the one lossy read', () => {
    expect(isoUtc(parseIso('2026-08-26T14:00:00.123456+00:00'))).toBe('2026-08-26T14:00:00.123000+00:00');
  });

  it('orders as text the way the instants order', () => {
    const sorted = [...COLUMNS].sort();
    expect(sorted).toEqual([...COLUMNS].sort((a, b) => parseIso(a).getTime() - parseIso(b).getTime()));
  });
});
