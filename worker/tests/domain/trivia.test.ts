import { describe, expect, it } from 'vitest';
import { flipDoneVerdict, formatDone, parseCardIds, parseDone } from '../../domain/trivia';

describe('trivia session state', () => {
  it('parseCardIds: the docstring example, empties and junk', () => {
    expect(parseCardIds('1,2,3')).toEqual([1, 2, 3]);
    expect(parseCardIds('')).toEqual([]);
    expect(parseCardIds(null)).toEqual([]);
    expect(parseCardIds(undefined)).toEqual([]);
    expect(parseCardIds(' 4 , 5,,x,6a,-1,1.5, 007')).toEqual([4, 5, 7]);
  });

  it('parseDone: the docstring example and malformed chunks dropped', () => {
    expect(parseDone('42r,17w,99r')).toEqual([
      [42, 'r'],
      [17, 'w'],
      [99, 'r'],
    ]);
    expect(parseDone('')).toEqual([]);
    expect(parseDone(null)).toEqual([]);
    expect(parseDone('r,1,1x,1R, 2w ,r3,1.5r,-1w')).toEqual([[2, 'w']]);
  });

  it('formatDone inverts parseDone', () => {
    const chain = '42r,17w,99r';
    expect(formatDone(parseDone(chain))).toBe(chain);
    expect(formatDone([])).toBe('');
  });

  it('flipDoneVerdict rewrites only the regraded card', () => {
    const items = parseDone('42r,17w,99r');
    expect(flipDoneVerdict(items, 17, true)).toBe('42r,17r,99r');
    expect(flipDoneVerdict(items, 42, false)).toBe('42w,17w,99r');
    expect(flipDoneVerdict(items, 7, true)).toBe('42r,17w,99r');
  });

  // Only ASCII digits count: other scripts and
  // unbounded magnitudes.
  it('digits are ASCII and ids stay within MAX_SAFE_INTEGER', () => {
    expect(parseCardIds('٣,9007199254740991,9007199254740992,1')).toEqual([9007199254740991, 1]);
    expect(parseDone('٣r,9007199254740992w,2r')).toEqual([[2, 'r']]);
  });
});
