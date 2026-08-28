// The multi-answer grader compares a stored answer against a submitted one
// as sorted unique values, so the parser has to keep int and float apart,
// keep first-insertion order, and refuse what cannot be a set member.
import { describe, expect, it } from 'vitest';
import { JsonDecodeError, PyDict, PyTypeError, loads, toSet } from '../../domain/grading/pyjson';
import { GradingError, pyRepr, pyReprList, pySorted } from '../../domain/grading/pyrepr';

describe('the pieces', () => {
  it('keeps ints and floats apart', () => {
    expect(loads('[1, 1.0, 1e2]')).toEqual([1n, 1, 100]);
    expect(pyRepr(1n)).toBe('1');
    expect(pyRepr(1)).toBe('1.0');
  });
  it('keeps object keys in first-insertion order', () => {
    expect(loads('{"2": 1, "1": 2, "2": 3}')).toEqual(new PyDict(['2', '1']));
  });
  it('dedups by hash equality, first occurrence wins', () => {
    expect(toSet([1n, 1, true])).toEqual([1n]);
    expect(toSet([true, 1n])).toEqual([true]);
  });
  it('rejects non-iterables and unhashables with PyTypeError', () => {
    expect(() => toSet(5n)).toThrow(PyTypeError);
    expect(() => toSet([[]])).toThrow(PyTypeError);
    expect(() => toSet([new PyDict([])])).toThrow(PyTypeError);
  });
  it('rejects mixed classes in sorted with GradingError', () => {
    expect(() => pySorted([1n, 'a'])).toThrow(GradingError);
    expect(() => pySorted([null, 'a'])).toThrow(GradingError);
    expect(pySorted([null])).toEqual([null]);
    expect(pySorted(['b', 'a', '😀', '￿'])).toEqual(['a', 'b', '￿', '😀']);
  });
  it('names a malformed document rather than throwing something else', () => {
    for (const text of ['', '[1', '{"a"}', '[1,]', "['a']", '[+1]', '[1] x']) {
      expect(() => loads(text), text).toThrow(JsonDecodeError);
    }
  });
  it('renders a list the way a stored answer spells it', () => {
    expect(pyReprList(['Lyon', 'Paris'])).toBe("['Lyon', 'Paris']");
    expect(pyReprList([])).toBe('[]');
    // A value holding an apostrophe takes the other quote rather than an
    // escape; one holding both falls back to escaping.
    expect(pyReprList(["it's", 'say "hi"'])).toBe(`["it's", 'say "hi"']`);
    expect(pyReprList([`it's "x"`])).toBe(`['it\\'s "x"']`);
  });
});
