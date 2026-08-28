// Answer patterns are authored in the `re` dialect, so the translator decides
// per pattern whether a u-flag RegExp grades it the same way: it rewrites
// what the dialects spell differently, refuses what they would grade
// differently, and leaves the rest to the engine. Each row carries the
// verdict the engine owes, and refusal is `null`, which sends the answer to
// self-verdict rather than to a guess.
import { describe, expect, it } from 'vitest';
import { matchRegex, translatePattern, validateRegexUpdate } from '../../domain/grading';

type Row = [id: string, pattern: string, given: string];
type Graded = [id: string, pattern: string, given: string, verdict: boolean | null];

/** Rewritten by the scanner, and matching once rewritten. */
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
  ['optional-space', 'new\\s?york', 'newyork'],
  ['space-in-class', 'x[\\s,]y', 'x\u00a0y'],
  ['space-class-then-dash', 'x[\\s-]y', 'x-y'],
  ['space-class-after-dash', 'x[-\\s]y', 'x y'],
  ['ref-after-inner-alternation', '(a|b)\\1', 'bb'],
  ['ref-then-quantifier', '(a)\\1?', 'a'],
];

/** Syntax one engine lacks or reads differently. */
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
  ['space-range-end', '[a-\\s]', 'x'],
  ['space-range-start', '[\\s-z]', 'x'],
];

/** Left alone by the scanner. */
const PLAIN: Graded[] = [
  ['alternation', 'paris|par(is)?', 'Paris', true],
  ['anchored', '^paris$', 'paris', true],
  ['dotall', 'a.b', 'a\nb', true],
  ['digits', 'cat\\d+', 'cat42', true],
  ['boundary', '\\bcat\\b', 'cat', true],
  ['range-fold', '[a-z]+', 'Paris', true],
  ['optional', 'colou?r', 'COLOR', true],
  ['partial-miss', 'par', 'paris', false],
  ['numbered-backref', '(a)\\1', 'aa', true],
  ['hex-escape', '\\x41', 'a', true],
  ['unicode-escape', '\\u00e9', 'É', true],
  ['non-ascii-fold', 'é', 'É', true],
  ['sharp-s', 'straße', 'STRASSE', false],
  ['space-class', 'a\\sb', 'a b', true],
  ['unicode-strip', 'paris', ' paris　', true],
  ['at-cap', 'a'.repeat(500), 'a'.repeat(500), true],
  ['over-cap', 'a'.repeat(501), 'a'.repeat(501), null],
  ['astral-cap-in-code-points', '😀'.repeat(300), '😀'.repeat(300), true],
  ['empty-answer', 'a?', '', true],
  ['boundary-empty-answer', '\\b', '', false],
];

// The whitespace class is JavaScript's: `\s` and `String.trim()` take in the
// BOM and leave out NEL and the C1 separators. These rows sit on that edge.
const WHITESPACE: Graded[] = [
  ['space-nel', 'a\\sb', 'a\u0085b', false],
  ['space-file-separator', 'a\\sb', 'a\u001cb', false],
  ['nonspace-bom', 'a\\Sb', 'a\ufeffb', false],
  ['nonspace-in-class', '[^\\S]', ' ', false],
  ['bom-not-stripped', 'paris', '﻿paris', true],
  ['space-vs-bom', 'a\\sb', 'a\ufeffb', true],
  ['nonspace-vs-nel', 'a\\Sb', 'a\u0085b', true],
];

describe('translated patterns are rewritten, and grade', () => {
  it.each(TRANSLATED)('%s', (_id, pattern, given) => {
    expect(translatePattern(pattern)).not.toBeNull();
    expect(matchRegex(pattern, given)).toBe(true);
  });

  it('spells the translations the way the u flag reads them', () => {
    expect(translatePattern('(?P<x>yes|no)')).toBe('(?<x>yes|no)');
    expect(translatePattern('(?P<w>ha)(?P=w)')).toBe('(?<w>ha)\\k<w>');
    expect(translatePattern('(?is)a.b')).toBe('a.b');
    expect(translatePattern('a\\ b\\_\\#\\-[\\-\\ ]')).toBe('a b_#-[\\- ]');
    expect(translatePattern('\\.\\*\\/\\\\')).toBe('\\.\\*\\/\\\\');
    expect(translatePattern('[(?P<x]')).toBe('[(?P<x]');
    expect(translatePattern('a\\sb')).toBe('a\\sb');
    expect(translatePattern('a\\Sb')).toBe('a\\Sb');
    expect(translatePattern('[\\s,]')).toBe('[\\s,]');
  });
});

describe('refused patterns fall to self-verdict', () => {
  it.each(REFUSED)('%s', (_id, pattern, given) => {
    expect(matchRegex(pattern, given)).toBeNull();
  });

  it('refuses before the engine sees the pattern', () => {
    for (const p of ['(?i:a)', '(?<n>a)', '\\p{L}', '[]', '[^]', '[]a]', '(?#c)', '\\k<n>', '\\u{41}', '\\cJ']) {
      expect(translatePattern(p), p).toBeNull();
    }
  });
});

describe('plain patterns reach the engine untouched', () => {
  it.each(PLAIN)('%s', (_id, pattern, given, verdict) => {
    expect(matchRegex(pattern, given)).toBe(verdict);
  });
});

describe('the whitespace class is the JavaScript one', () => {
  it.each(WHITESPACE)('%s', (_id, pattern, given, verdict) => {
    expect(matchRegex(pattern, given)).toBe(verdict);
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
    expect(validateRegexUpdate('\\w+', 'caf\u00e9')).toBeNull();
    expect(validateRegexUpdate('\\w+', 'cafe', 'caf\u00e9')).toBeNull();
    expect(validateRegexUpdate('\\D', '\u0661')).toBeNull();
    expect(validateRegexUpdate('a?\\B', '')).toBeNull();
    expect(validateRegexUpdate('(?<=a*)b', 'b')).toBeNull();
    expect(validateRegexUpdate('(a)?\\1b', 'b')).toBeNull();
    expect(validateRegexUpdate('new\\s?york', 'New York', 'newyork')).toBe('new\\s?york');
  });
  it('refuses every pattern whose subjects it cannot be trusted on', () => {
    const refused: [string, string, string | null][] = [
      ['\\w+', 'caf\u00e9', null],
      ['\\D', '\u0661', null],
      ['\\W', '\u00e9', null],
      ['a?\\B', '', null],
      ['(?<=a*)b', 'b', null],
      ['(?P<n>a)|(?P<n>b)', 'b', null],
      ['(a)?\\1b', 'b', null],
    ];
    for (const [p, e, g] of refused) expect(validateRegexUpdate(p, e, g), p).toBeNull();
  });
  it('counts the cap in code points', () => {
    expect(validateRegexUpdate('\ud83d\ude00'.repeat(500), '\ud83d\ude00'.repeat(500))).toBe('\ud83d\ude00'.repeat(500));
    expect(validateRegexUpdate('\ud83d\ude00'.repeat(501), '\ud83d\ude00'.repeat(501))).toBeNull();
  });
});
