import { describe, expect, it } from 'vitest';
import { matchRegex, translatePattern, validateRegexUpdate } from '../../domain/grading';
import { pythonJson } from '../pyoracle';

// Every pattern runs through Python's match_regex too. The port may answer
// null where Python has a verdict (self-verdict is safe) but must never
// contradict it. The named groups pin which patterns translate and which
// are refused, so an engine that starts accepting more (V8 already takes
// scoped flag groups) cannot widen the port silently.

type Row = [id: string, pattern: string, given: string];

const TRANSLATED: Row[] = [
  ['named-group', '(?P<x>yes|no)', 'YES'],
  ['named-backref', '(?P<w>ha)(?P=w)', 'haha'],
  ['leading-i', '(?i)paris', 'PARIS'],
  ['leading-s', '(?s)a.b', 'a\nb'],
  ['leading-is', '(?is)a.b', 'A\nB'],
  ['leading-si', '(?si)a.b', 'A\nB'],
  ['escaped-underscore', 'a\\_b', 'a_b'],
  ['escaped-space', 'a\\ b', 'a b'],
  ['escaped-hash', '\\#1', '#1'],
  ['escaped-quote', "it\\'s", "it's"],
  ['escaped-dash', 'a\\-b', 'a-b'],
  ['escaped-non-ascii', 'caf\\é', 'café'],
  ['escaped-space-in-class', 'a[\\ ]b', 'a b'],
  ['escaped-dash-in-class', '[\\-a]+', '-a-'],
  ['escaped-bracket-in-class', '[a\\]]+', 'a]a'],
  ['escaped-slash', '\\/', '/'],
  ['escaped-backslash-then-w', 'x\\\\wé', 'x\\wé'],
];

const REFUSED: Row[] = [
  ['anchors-A-Z', '\\Aparis\\Z', 'paris'],
  ['flag-x', '(?x)paris', 'paris'],
  ['flag-m', '(?m)paris', 'paris'],
  ['flag-u', '(?u)paris', 'paris'],
  ['flag-a', '(?a)paris', 'paris'],
  ['flag-not-leading', 'pa(?i)ris', 'paris'],
  ['second-flag-group', '(?i)(?s)a.b', 'A\nB'],
  ['empty-flag-group', '(?)a', 'a'],
  ['scoped-flag', '(?i:pa)ris', 'PAris'],
  ['scoped-flag-off', '(?-i:pa)ris', 'paris'],
  ['comment-group', '(?#note)paris', 'paris'],
  ['atomic-group', '(?>par)is', 'paris'],
  ['possessive', 'pari++s', 'paris'],
  ['conditional', '(a)?(?(1)b|c)', 'ab'],
  ['open-min-quantifier', 'a{,3}', 'aa'],
  ['named-unicode-escape', '\\N{LATIN SMALL LETTER A}', 'a'],
  ['unknown-letter-escape', '\\q', 'q'],
  ['empty-class', '[]', ''],
  ['negated-empty-class', '[^]', 'x'],
  ['leading-bracket-in-class', '[]a]+', ']a'],
  ['js-named-group', '(?<n>a)', 'a'],
  ['js-named-backref', '(?<n>a)\\k<n>', 'aa'],
  ['bare-k-escape', '\\k<n>', 'a'],
  ['property-escape', '\\p{L}+', 'abc'],
  ['control-escape', '\\cJ', '\n'],
  ['brace-unicode-escape', '\\u{41}', 'a'],
  ['unbalanced-paren', '(', 'x'],
  ['unclosed-class', '[unclosed', 'x'],
  ['inverted-quantifier', 'a{2,1}', 'aa'],
  ['trailing-backslash', 'a\\', 'a'],
  ['shorthand-non-ascii-pattern', 'caf\\w+', 'café'],
  ['shorthand-non-ascii-answer', '\\w+', 'naïve'],
  ['boundary-non-ascii-answer', '\\bcafé\\b', 'café'],
];

const PLAIN: Row[] = [
  ['alternation', 'paris|par(is)?', 'Paris'],
  ['anchored', '^paris$', 'paris'],
  ['dotall', 'a.b', 'a\nb'],
  ['digits', 'cat\\d+', 'cat42'],
  ['boundary', '\\bcat\\b', 'cat'],
  ['range-fold', '[a-z]+', 'Paris'],
  ['optional', 'colou?r', 'COLOR'],
  ['partial-miss', 'par', 'paris'],
  ['numbered-backref', '(a)\\1', 'aa'],
  ['hex-escape', '\\x41', 'a'],
  ['unicode-escape', '\\u00e9', 'É'],
  ['non-ascii-fold', 'é', 'É'],
  ['sharp-s', 'straße', 'STRASSE'],
  ['space-class', 'a\\sb', 'a b'],
  ['unicode-strip', 'paris', ' paris　'],
  ['bom-not-stripped', 'paris', '﻿paris'],
  ['at-cap', 'a'.repeat(500), 'a'.repeat(500)],
  ['over-cap', 'a'.repeat(501), 'a'.repeat(501)],
  ['astral-cap-in-code-points', '😀'.repeat(300), '😀'.repeat(300)],
  ['empty-answer', 'a?', ''],
];

const ROWS = [...TRANSLATED, ...REFUSED, ...PLAIN];

const python = pythonJson<(boolean | null)[]>(
  `import json
from prep.domain.grading import match_regex
rows = json.loads(${JSON.stringify(JSON.stringify(ROWS))})
print(json.dumps([match_regex(p, g) for _, p, g in rows]))`,
);
const py = new Map(ROWS.map((r, i) => [r[0], python[i]]));

describe('matchRegex never contradicts Python', () => {
  it.each(ROWS.map((r) => [r[0], r[1], r[2]]))('%s', (id, pattern, given) => {
    const ours = matchRegex(pattern, given);
    if (ours !== null) expect(ours).toBe(py.get(id));
  });
});

describe('translated patterns grade', () => {
  it.each(TRANSLATED.map((r) => [r[0], r[1], r[2]]))('%s', (id, pattern, given) => {
    expect(py.get(id)).toBe(true);
    expect(matchRegex(pattern, given)).toBe(true);
  });

  it('spells the translations the way the u flag reads them', () => {
    expect(translatePattern('(?P<x>yes|no)')).toBe('(?<x>yes|no)');
    expect(translatePattern('(?P<w>ha)(?P=w)')).toBe('(?<w>ha)\\k<w>');
    expect(translatePattern('(?is)a.b')).toBe('a.b');
    expect(translatePattern('a\\ b\\_\\#\\-[\\-\\ ]')).toBe('a b_#-[\\- ]');
    expect(translatePattern('\\.\\*\\/\\\\')).toBe('\\.\\*\\/\\\\');
    expect(translatePattern('[(?P<x]')).toBe('[(?P<x]');
  });
});

describe('refused patterns fall to self-verdict', () => {
  it.each(REFUSED.map((r) => [r[0], r[1], r[2]]))('%s', (_id, pattern, given) => {
    expect(matchRegex(pattern, given)).toBeNull();
  });

  it('refuses before the engine sees the pattern', () => {
    for (const p of ['(?i:a)', '(?<n>a)', '\\p{L}', '[]', '[^]', '[]a]', '(?#c)', '\\k<n>', '\\u{41}', '\\cJ']) {
      expect(translatePattern(p), p).toBeNull();
    }
  });
});

describe('plain patterns agree exactly', () => {
  it.each(PLAIN.map((r) => [r[0], r[1], r[2]]))('%s', (id, pattern, given) => {
    expect(matchRegex(pattern, given)).toBe(py.get(id));
  });
});

describe('measured parity', () => {
  it('has no contradictions and only the refused rows diverge', () => {
    const diverged = ROWS.filter(([id, p, g]) => matchRegex(p, g) === null && py.get(id) !== null).map((r) => r[0]);
    const refusedWithVerdict = REFUSED.filter(([id]) => py.get(id) !== null).map((r) => r[0]);
    expect(diverged).toEqual(refusedWithVerdict);
    expect(ROWS.length - diverged.length).toBe(TRANSLATED.length + PLAIN.length + (REFUSED.length - refusedWithVerdict.length));
  });
});

describe('validateRegexUpdate uses the same translation', () => {
  it('accepts a translated pattern and returns it stripped', () => {
    expect(validateRegexUpdate('  (?P<x>paris|lyon)  ', 'Paris', 'LYON')).toBe('(?P<x>paris|lyon)');
  });
  it('refuses what matchRegex refuses', () => {
    expect(validateRegexUpdate('\\Aparis\\Z', 'Paris')).toBeNull();
    expect(validateRegexUpdate('(?i:paris)', 'Paris')).toBeNull();
  });
  it('keeps the shorthand rule out of validation', () => {
    expect(validateRegexUpdate('caf\\w+', 'cafe')).toBe('caf\\w+');
  });
  it('counts the cap in code points', () => {
    expect(validateRegexUpdate('😀'.repeat(500), '😀'.repeat(500))).toBe('😀'.repeat(500));
    expect(validateRegexUpdate('😀'.repeat(501), '😀'.repeat(501))).toBeNull();
  });
});
