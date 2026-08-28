// The multi-answer grader compares a stored answer against a submitted one
// as sorted unique values, so the parser has to keep int and float apart,
// keep first-insertion order, and refuse what cannot be a set member.
import { describe, expect, it } from 'vitest';
import { AnswerJsonError, JsonObject, AnswerShapeError, parseJson, toSet } from '../../domain/grading/answerJson';
import { GradingError, literal, literalList, sortedValues } from '../../domain/grading/literal';

describe('the pieces', () => {
  it('keeps ints and floats apart', () => {
    expect(parseJson('[1, 1.0, 1e2]')).toEqual([1n, 1, 100]);
    expect(literal(1n)).toBe('1');
    expect(literal(1)).toBe('1.0');
  });
  it('keeps object keys in first-insertion order', () => {
    expect(parseJson('{"2": 1, "1": 2, "2": 3}')).toEqual(new JsonObject(['2', '1']));
  });
  it('dedups by value, first occurrence wins', () => {
    expect(toSet([1n, 1, true])).toEqual([1n]);
    expect(toSet([true, 1n])).toEqual([true]);
  });
  it('rejects a scalar and a nested container with AnswerShapeError', () => {
    expect(() => toSet(5n)).toThrow(AnswerShapeError);
    expect(() => toSet([[]])).toThrow(AnswerShapeError);
    expect(() => toSet([new JsonObject([])])).toThrow(AnswerShapeError);
  });
  it('rejects mixed classes in sorted with GradingError', () => {
    expect(() => sortedValues([1n, 'a'])).toThrow(GradingError);
    expect(() => sortedValues([null, 'a'])).toThrow(GradingError);
    expect(sortedValues([null])).toEqual([null]);
    expect(sortedValues(['b', 'a', '😀', '￿'])).toEqual(['a', 'b', '￿', '😀']);
  });
  it('names a malformed document rather than throwing something else', () => {
    for (const text of ['', '[1', '{"a"}', '[1,]', "['a']", '[+1]', '[1] x']) {
      expect(() => parseJson(text), text).toThrow(AnswerJsonError);
    }
  });
  it('renders a list the way a stored answer spells it', () => {
    expect(literalList(['Lyon', 'Paris'])).toBe("['Lyon', 'Paris']");
    expect(literalList([])).toBe('[]');
    // A value holding an apostrophe takes the other quote rather than an
    // escape; one holding both falls back to escaping.
    expect(literalList(["it's", 'say "hi"'])).toBe(`["it's", 'say "hi"']`);
    expect(literalList([`it's "x"`])).toBe(`['it\\'s "x"']`);
  });
});
