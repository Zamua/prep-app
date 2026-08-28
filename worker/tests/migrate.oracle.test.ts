// The phase 2 `domain/py.ts` helpers, re-aimed at migrated rows.
//
// Every timestamp column of a prod-shaped snapshot is round-tripped through
// `parseIso` and `isoUtc` and compared to the stored bytes. Two classes come
// out of it, and both shape the verifier:
//
//   * `Z` and naive forms re-serialise to `+00:00`. Same instant, different
//     byte, different rendered page, so tier 2 compares strings and never
//     parses.
//   * `parseIso` truncates a fraction to milliseconds, which JS Date cannot
//     go past, so a sub-millisecond value does not survive the round trip.
//
// `pyFormatG` renders a mismatch the way Python prints it, so a report line
// pastes into a repl; `pyRound` is used for reporting only.
import { describe, expect, it } from 'vitest';
import { isoUtc, parseIso, pyFormatG, pyRound } from '../domain/py';
import { pythonJson } from './pyoracle';

interface Corpus {
  /** `table.column` to the distinct values a prod-shaped snapshot holds. */
  columns: Record<string, string[]>;
  values: number;
  /** `%g`, `%.17g` and `round(x, 3)` over the snapshot's own doubles. */
  g6: [number, string][];
  g17: [number, string][];
  round3: [number, number][];
  /** `datetime.isoformat()` of a system-clock read: microseconds and all. */
  system_now: string;
}

const corpus = pythonJson<Corpus>(
  `import json, tempfile
from datetime import datetime, timezone
from pathlib import Path
from prep.migrate import snapshot as snap
from prep.migrate import synth

out = Path(tempfile.mkdtemp()) / "s.sqlite"
synth.generate(out, users=6, seed=7, anonymous=2, heavy_questions=4, heavy_reviews=6,
               now=datetime(2026, 8, 26, 14, 0, 0, tzinfo=timezone.utc))
conn = snap.open_snapshot(out)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

def looks_iso(v):
    if not isinstance(v, str) or "T" not in v or len(v) < 19:
        return False
    try:
        datetime.fromisoformat(v)
        return True
    except ValueError:
        return False

columns, total, floats = {}, 0, []
for table in tables:
    names = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    for row in conn.execute(f'SELECT * FROM "{table}"'):
        for name, value in zip(names, row):
            if looks_iso(value):
                seen = columns.setdefault(f"{table}.{name}", [])
                total += 1
                if value not in seen and len(seen) < 12:
                    seen.append(value)
            elif isinstance(value, float):
                floats.append(value)
floats = sorted(set(floats))[:40]
print(json.dumps({
    "columns": columns,
    "values": total,
    "g6": [[v, "%g" % v] for v in floats],
    "g17": [[v, "%.17g" % v] for v in floats],
    "round3": [[v, round(v, 3)] for v in floats],
    "system_now": datetime.now(timezone.utc).isoformat(),
}))`,
);

describe('every migrated timestamp column round-trips byte for byte', () => {
  it('scanned a prod-shaped snapshot rather than a handful of literals', () => {
    expect(corpus.values).toBeGreaterThan(200);
    expect(Object.keys(corpus.columns).length).toBeGreaterThanOrEqual(15);
    for (const column of ['cards.next_due', 'cards.last_review', 'reviews.ts', 'users.created_at', 'users.last_seen_at']) {
      expect(Object.keys(corpus.columns)).toContain(column);
    }
  });

  it('re-serialises to the stored bytes', () => {
    let checked = 0;
    for (const [column, values] of Object.entries(corpus.columns)) {
      for (const value of values) {
        expect(isoUtc(parseIso(value)), `${column} = ${value}`).toBe(value);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(0);
  });
});

describe('the two forms that are the same instant and different bytes', () => {
  const utc = '2026-08-26T14:00:00+00:00';

  it('Z and naive both re-serialise to +00:00', () => {
    for (const other of ['2026-08-26T14:00:00Z', '2026-08-26 14:00:00', '2026-08-26T16:00:00+02:00']) {
      expect(parseIso(other).getTime()).toBe(parseIso(utc).getTime());
      expect(isoUtc(parseIso(other))).toBe(utc);
      expect(isoUtc(parseIso(other))).not.toBe(other);
    }
  });

  it('a fraction past a millisecond is dropped, which is the one lossy parse', () => {
    // The Python app writes `datetime.isoformat()` with `timespec="auto"`,
    // so a real row carries microseconds; JS Date holds milliseconds.
    expect(corpus.system_now).toMatch(/\.\d{6}\+00:00$/);
    expect(isoUtc(parseIso('2026-08-26T14:00:00.123456+00:00'))).toBe('2026-08-26T14:00:00.123000+00:00');
    expect(isoUtc(parseIso('2026-08-26T14:00:00.123000+00:00'))).toBe('2026-08-26T14:00:00.123000+00:00');
  });
});

describe('a report line pastes into a Python repl', () => {
  it('pyFormatG renders the snapshot own doubles the way C does', () => {
    expect(corpus.g6.length).toBeGreaterThan(0);
    for (const [value, printed] of corpus.g6) expect(pyFormatG(value), String(value)).toBe(printed);
    for (const [value, printed] of corpus.g17) expect(pyFormatG(value, 17), String(value)).toBe(printed);
  });

  it('pyRound is reporting only, and still rounds the way Python does', () => {
    for (const [value, rounded] of corpus.round3) expect(pyRound(value, 3), String(value)).toBe(rounded);
  });
});
