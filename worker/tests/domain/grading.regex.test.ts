import { describe, expect, it } from 'vitest';
import { matchRegex, translatePattern, validateRegexUpdate } from '../../domain/grading';
import { PY_SPACE } from '../../domain/py';
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
  ['space-nel', 'a\\sb', 'a\u0085b'],
  ['space-file-separator', 'a\\sb', 'a\u001cb'],
  ['optional-space', 'new\\s?york', 'newyork'],
  ['nonspace-bom', 'a\\Sb', 'a\ufeffb'],
  ['space-in-class', 'x[\\s,]y', 'x\u00a0y'],
  ['space-class-then-dash', 'x[\\s-]y', 'x-y'],
  ['space-class-after-dash', 'x[-\\s]y', 'x y'],
  ['ref-after-inner-alternation', '(a|b)\\1', 'bb'],
  ['ref-then-quantifier', '(a)\\1?', 'a'],
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
  ['nonboundary-empty-answer', '\\B', ''],
  ['nonboundary-empty-answer-optional', 'a?\\B', ''],
  ['lookbehind-variable', '(?<=a*)b', 'b'],
  ['lookbehind-fixed', '(?<=a)b', 'ab'],
  ['negative-lookbehind', '(?<!a)b', 'b'],
  ['duplicate-group-name', '(?P<n>a)|(?P<n>b)', 'b'],
  ['forward-named-ref', '(?P=n)(?P<n>a)', 'a'],
  ['forward-numbered-ref', '\\1(a)', 'a'],
  ['open-group-ref', '(a\\1)', 'a'],
  ['ref-across-alternation', '(a)|\\1', ''],
  ['ref-to-optional-group', '(a)?\\1b', 'b'],
  ['ref-to-nested-group', '(?:(a)|b)\\1', 'b'],
  ['two-digit-ref', '\\10', 'a'],
  ['surrogate-pair-escapes', '\\uD83D\\uDE00', '😀'],
  ['lone-surrogate-escape', '\\uD83D', 'x'],
  ['nonspace-in-class', '[^\\S]', ' '],
  ['space-range-end', '[a-\\s]', 'x'],
  ['space-range-start', '[\\s-z]', 'x'],
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
  ['space-vs-bom', 'a\\sb', 'a\ufeffb'],
  ['nonspace-vs-nel', 'a\\Sb', 'a\u0085b'],
  ['boundary-empty-answer', '\\b', ''],
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
    expect(translatePattern('a\\sb')).toBe(`a[${PY_SPACE}]b`);
    expect(translatePattern('a\\Sb')).toBe(`a[^${PY_SPACE}]b`);
    expect(translatePattern('[\\s,]')).toBe(`[${PY_SPACE},]`);
  });
});

describe('the translated \\s is str.isspace() on every BMP code point', () => {
  const probe = [...Array.from({ length: 0xd800 }, (_, i) => i), ...Array.from({ length: 0x10000 - 0xe000 }, (_, i) => 0xe000 + i), 0x1680, 0x1f600, 0x10ffff];
  const python = pythonJson<string[]>(
    `import json, re
cps = json.loads(${JSON.stringify(JSON.stringify(probe))})
flags = re.I | re.S
s = re.compile(r"\\s", flags); S = re.compile(r"\\S", flags)
print(json.dumps(["".join("1" if s.fullmatch(chr(c)) else "0" for c in cps), "".join("1" if S.fullmatch(chr(c)) else "0" for c in cps)]))`,
  );
  it.each([['\\s', 0], ['\\S', 1]] as const)('%s', (pattern, column) => {
    const re = new RegExp(`^(?:${translatePattern(pattern)})$`, 'isu');
    const ours = probe.map((c) => (re.test(String.fromCodePoint(c)) ? '1' : '0')).join('');
    expect(ours).toBe(python[column]);
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
  it('applies the trust rule over the expected and prior answers', () => {
    expect(validateRegexUpdate('caf\\w+', 'cafe')).toBe('caf\\w+');
    expect(validateRegexUpdate('\\w+', 'café')).toBeNull();
    expect(validateRegexUpdate('\\w+', 'cafe', 'café')).toBeNull();
    expect(validateRegexUpdate('\\D', '١')).toBeNull();
    expect(validateRegexUpdate('a?\\B', '')).toBeNull();
    expect(validateRegexUpdate('(?<=a*)b', 'b')).toBeNull();
    expect(validateRegexUpdate('(a)?\\1b', 'b')).toBeNull();
    expect(validateRegexUpdate('new\\s?york', 'New York', 'newyork')).toBe('new\\s?york');
  });
  it('never persists what Python refuses', () => {
    const rows: [string, string, string | null][] = [
      ['\\w+', 'café', null],
      ['\\D', '١', null],
      ['\\W', 'é', null],
      ['a?\\B', '', null],
      ['(?<=a*)b', 'b', null],
      ['(?P<n>a)|(?P<n>b)', 'b', null],
      ['(a)?\\1b', 'b', null],
      ['new\\s?york', 'New York', 'newyork'],
      ['a\\sb', 'a\u0085b', null],
    ];
    const python = pythonJson<(string | null)[]>(
      `import json
from prep.domain.grading import validate_regex_update
rows = json.loads(${JSON.stringify(JSON.stringify(rows))})
print(json.dumps([validate_regex_update(p, expected_literal=e, prior_given=g) for p, e, g in rows]))`,
    );
    rows.forEach(([p, e, g], i) => {
      const ours = validateRegexUpdate(p, e, g);
      if (ours !== null) expect(ours, p).toBe(python[i]);
    });
    expect(validateRegexUpdate('new\\s?york', 'New York', 'newyork')).toBe(python[7]);
  });
  it('counts the cap in code points', () => {
    expect(validateRegexUpdate('😀'.repeat(500), '😀'.repeat(500))).toBe('😀'.repeat(500));
    expect(validateRegexUpdate('😀'.repeat(501), '😀'.repeat(501))).toBeNull();
  });
});
