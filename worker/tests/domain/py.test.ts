import { describe, expect, it } from 'vitest';
import { IsoFormatError, codePoints, isoUtc, parseIso, pyRound, pyStrip } from '../../domain/py';
import { pythonJson } from '../pyoracle';

// Every expectation comes from the venv Python, never from a hand-written
// literal: the point of py.ts is agreeing with CPython at the edges.

const ROUND_INPUTS: [number, number][] = [
  // exact binary ties, which Python resolves half-even
  [0.5, 0], [1.5, 0], [2.5, 0], [3.5, 0], [-0.5, 0], [-1.5, 0], [-2.5, 0],
  [0.125, 2], [0.375, 2], [-0.125, 2], [2.5, 0], [12.5, 0], [36499.5, 0],
  [0.0625, 3], [0.1875, 3],
  // decimal near-ties whose binary value is off the tie
  [0.0005, 3], [0.0015, 3], [0.0025, 3], [2.675, 2], [1.0005, 3], [0.285, 2],
  [2.4999999999999996, 0], [2.5000000000000004, 0],
  // the scheduler's retentions and clamps
  [0.5, 3], [0.7, 3], [0.8, 3], [0.9, 3], [0.95, 3], [0.97, 3], [0.99, 3], [0.8999999, 3], [0.9004999, 3], [0.9005, 3],
  // plain values
  [7.123456789, 3], [-7.123456789, 3], [123456.789, 1], [1e-9, 3], [0, 0], [-0, 0], [1e17, 3], [4503599627370497, 0],
];

describe('pyRound matches round()', () => {
  const expected = pythonJson<(number | null)[]>(
    `import json,math;xs=${JSON.stringify(ROUND_INPUTS)};print(json.dumps([round(x,n) for x,n in xs]))`,
  );
  it.each(ROUND_INPUTS.map((c, i) => [...c, expected[i]] as [number, number, number]))(
    'round(%s, %s) = %s',
    (x, nd, want) => {
      expect(Object.is(pyRound(x, nd), want) || pyRound(x, nd) === want).toBe(true);
    },
  );
  it('passes non-finite values through', () => {
    expect(pyRound(Infinity, 3)).toBe(Infinity);
    expect(Number.isNaN(pyRound(NaN, 0))).toBe(true);
  });
});

describe('pyStrip matches str.strip()', () => {
  it('strips exactly the code points str.isspace() accepts', () => {
    const spaces = new Set(pythonJson<number[]>('import json;print(json.dumps([c for c in range(0x110000) if chr(c).isspace()]))'));
    expect(spaces.size).toBeGreaterThan(20);
    for (let c = 0; c < 0x110000; c++) {
      if (c >= 0xd800 && c <= 0xdfff) continue;
      const ch = String.fromCodePoint(c);
      const stripped = pyStrip(`${ch}x${ch}`) === 'x';
      if (stripped !== spaces.has(c)) throw new Error(`U+${c.toString(16)} stripped=${stripped}`);
    }
  });
  const STRIP_INPUTS = ['  a b  ', '\t\n a\r\n', '', '   ', '\x1c\x85a　', '﻿a﻿', 'a', '​ a ​'];
  it.each(STRIP_INPUTS)('strip(%j)', (s) => {
    const want = pythonJson<string>(`import json;print(json.dumps(${JSON.stringify(s)}.strip()))`);
    expect(pyStrip(s)).toBe(want);
  });
});

describe('codePoints', () => {
  it('splits like Python indexes str', () => {
    const s = 'a\u{1F600}bé\ud800';
    const want = pythonJson<number>(`print(len(${JSON.stringify(s)}))`);
    expect(codePoints(s).length).toBe(want);
    expect(codePoints('')).toEqual([]);
  });
});

describe('isoUtc and parseIso', () => {
  const INSTANTS = ['2026-03-14T15:00:00Z', '2026-03-17T15:10:00.123Z', '0999-01-01T00:00:00Z', '2026-12-31T23:59:59.999Z'];
  it.each(INSTANTS)('isoUtc(%s) is the aware-UTC isoformat', (iso) => {
    const want = pythonJson<string>(
      `from datetime import datetime,timezone;print(__import__('json').dumps(datetime.fromisoformat(${JSON.stringify(iso)}).astimezone(timezone.utc).isoformat()))`,
    );
    expect(isoUtc(new Date(iso))).toBe(want);
    expect(parseIso(want).getTime()).toBe(new Date(iso).getTime());
  });
  const PARSES = [
    '2026-03-14T15:00:00+00:00', '2026-03-14T15:00:00', '2026-03-14 15:00:00', '2026-03-14', '2026-03-14T15:00',
    '2026-03-14T15:00:00.5', '2026-03-14T15:00:00.123456+00:00', '2026-03-14T10:00:00-05:00', '2026-03-14T20:30:00+05:30',
    '2026-03-14T15:00:00Z',
  ];
  it.each(PARSES)('parseIso(%s) agrees with fromisoformat', (s) => {
    const want = pythonJson<number>(
      `from datetime import datetime,timezone;d=datetime.fromisoformat(${JSON.stringify(s)});d=d if d.tzinfo else d.replace(tzinfo=timezone.utc);print(int(d.timestamp()*1000))`,
    );
    expect(parseIso(s).getTime()).toBe(want);
  });
  it.each(['', 'tomorrow', '2026-13-01', '2026-02-30T00:00:00', '2026-03-14T25:00:00', '2026-03-14T15:00:00+0', '20260314'])(
    'parseIso(%j) throws IsoFormatError',
    (s) => {
      expect(() => parseIso(s)).toThrow(IsoFormatError);
    },
  );
});
