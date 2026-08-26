import { describe, expect, it } from 'vitest';
import { JsonDecodeError, PyDict, PyTypeError, loads, toSet } from '../../domain/grading/pyjson';
import { GradingError, pyRepr, pyReprList, pySorted } from '../../domain/grading/pyrepr';
import { pythonJson } from '../pyoracle';

// Each input is JSON text; Python answers repr(sorted(set(json.loads(s))))
// or the exception class, and the port must say the same.

const TEXTS = [
  '["b", "a"]', '["Paris", "Lyon"]', '[]', '"héllo"', '{"b": 1, "a": 2, "b": 3}', '{"2": 1, "1": 2}',
  '[1, 1.0, true]', '[1.0, 1, true]', '[true, 1]', '[false, 0, 0.0]', '[-0.0, 0]',
  '[2, 1.5, true, 10000000000000000000000, 1e22, -1]', '[12345678901234567890123]', '[1E400, -1E400]', '[NaN]', '[-0]', '[-0.0]',
  '[1e16, 1e15, 0.0001, 0.00001, 1.5e-7, 1e100, 123456789012345680.0, 12345.678, 0.1, 2.5, 100.0, 1e21, 9999999999999998.0, 1e-4]',
  '[3.141592653589793, 0.3333333333333333, 1e23, 5e-324, 1.7976931348623157e308, 0.30000000000000004, 4.35, 2.675, 1e-7, 123456789.12345679, 0.14285714285714285, -1.5e-10, 1e-5, 9.999999999999999e15, 1.0000000000000002]',
  '["it\'s", "say \\"hi\\"", "it\'s \\"x\\"", "a\\\\b", "\\n\\t\\r"]',
  '["\\u0000\\u007f\\u0080\\u009f\\u00a0\\u00ad", "\\u001c", "\\u000b\\u000c", "\\u0085"]',
  '["é😀\\u200b\\u2028\\u2029\\ufeff\\u3000", "\\ue000", "\\udb40\\udc01", "\\ufffe", "\\uffff", "\\ud834\\udd73"]',
  '["\\ud83d\\ude00", "\\uffff", "\\ud800"]', '[null]', '[[]]', '["a", ["b"]]', '[{}]', '5', 'null', 'true', '1.5',
  '[1, "a"]', '[null, 1]', '[null, "a"]', '[true, "a"]', '[1.5, "a"]',
  '﻿[]', '[1,]', '[01]', "['a']", '[1.]', '[.5]', '[+1]', '[1e]', '["a\\x"]', '["\t"]', '[1] x', '', ' [1] ', '["\\u00e9"]', '[1', '{"a"}', '["a", "a", "b"]',
];

type Answer = { ok: string } | { err: string };

const python = pythonJson<Answer[]>(
  `import json
texts = json.loads(${JSON.stringify(JSON.stringify(TEXTS))})
out = []
for s in texts:
    try:
        out.append({"ok": repr(sorted(set(json.loads(s))))})
    except json.JSONDecodeError:
        out.append({"err": "JSONDecodeError"})
    except TypeError:
        out.append({"err": "TypeError"})
print(json.dumps(out))`,
);

function ours(text: string): Answer {
  try {
    return { ok: pyReprList(pySorted(toSet(loads(text)))) };
  } catch (e) {
    if (e instanceof JsonDecodeError) return { err: 'JSONDecodeError' };
    if (e instanceof PyTypeError || e instanceof GradingError) return { err: 'TypeError' };
    throw e;
  }
}

describe('repr(sorted(set(json.loads(s)))) matches Python', () => {
  it('exercises every outcome', () => {
    const kinds = new Set(python.map((a) => ('ok' in a ? 'ok' : a.err)));
    expect(kinds).toEqual(new Set(['ok', 'JSONDecodeError', 'TypeError']));
  });

  it.each(TEXTS.map((t, i) => [JSON.stringify(t), t, python[i]!] as const))('%s', (_label, text, want) => {
    expect(ours(text)).toEqual(want);
  });
});

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
});
