import { describe, expect, it, vi } from 'vitest';
import {
  CARD_ANSWER_MAX_CHARS,
  CARD_PROMPT_MAX_CHARS,
  DISPLAY_NAME_MAX_CHARS,
  DegenerateOutput,
  MAX_CARDS,
  MIN_CARDS,
  QaParseError,
  TOPIC_MAX_CHARS,
  extractCards,
} from '../../domain/instant/cards.js';

const card = (i: number, r: unknown = null) => ({ q: ` Q${i} `, a: `A${i}`, r });
const json = (items: unknown[]) => JSON.stringify(items);
const accept = vi.fn((pattern: unknown) => (typeof pattern === 'string' ? pattern : null));

describe('constants', () => {
  it('pins the six caps', () => {
    expect([TOPIC_MAX_CHARS, DISPLAY_NAME_MAX_CHARS, MAX_CARDS, MIN_CARDS, CARD_PROMPT_MAX_CHARS, CARD_ANSWER_MAX_CHARS]).toEqual([
      500, 60, 5, 3, 2000, 500,
    ]);
  });
});

describe('extractCards', () => {
  it('strips, caps the list at MAX_CARDS and hands truthy regexes to the validator', () => {
    accept.mockClear();
    const out = extractCards(json([card(1, 'a1'), card(2, ''), card(3, null), card(4, []), card(5, {}), card(6, 'a6')]), accept);
    expect(out).toEqual([
      { prompt: 'Q1', answer: 'A1', answer_regex: 'a1' },
      { prompt: 'Q2', answer: 'A2', answer_regex: null },
      { prompt: 'Q3', answer: 'A3', answer_regex: null },
      { prompt: 'Q4', answer: 'A4', answer_regex: null },
      { prompt: 'Q5', answer: 'A5', answer_regex: null },
    ]);
    expect(accept.mock.calls).toEqual([['a1', 'A1']]);
  });

  it('a rejected regex is stored as null', () => {
    const out = extractCards(json([card(1, 'bad'), card(2), card(3)]), () => null);
    expect(out[0]!.answer_regex).toBeNull();
  });

  it('skips non-objects, missing or blank fields and over-cap items', () => {
    const items = [
      1,
      'x',
      null,
      [card(9)],
      { q: 'only q' },
      { a: 'only a' },
      { q: '  ', a: 'A' },
      { q: 7, a: 'A' },
      { q: 'x'.repeat(CARD_PROMPT_MAX_CHARS + 1), a: 'A' },
      { q: 'Q', a: 'y'.repeat(CARD_ANSWER_MAX_CHARS + 1) },
      { q: '😀'.repeat(CARD_PROMPT_MAX_CHARS), a: '😀'.repeat(CARD_ANSWER_MAX_CHARS) },
      card(2),
      card(3),
    ];
    const out = extractCards(json(items), accept);
    expect(out.map((c) => c.answer)).toEqual(['😀'.repeat(CARD_ANSWER_MAX_CHARS), 'A2', 'A3']);
  });

  it('fewer than MIN_CARDS survivors is degenerate', () => {
    expect(() => extractCards(json([card(1), card(2)]), accept)).toThrow(DegenerateOutput);
    expect(() => extractCards(json([card(1), card(2), 'junk']), accept)).toThrow(DegenerateOutput);
    expect(() => extractCards('[]', accept)).toThrow(DegenerateOutput);
  });

  it('unparseable output throws QaParseError', () => {
    expect(() => extractCards('no json', accept)).toThrow(QaParseError);
    expect(() => extractCards('[1, 2', accept)).toThrow(QaParseError);
    expect(() => extractCards('{"q": 1}', accept)).toThrow(QaParseError);
  });
});
