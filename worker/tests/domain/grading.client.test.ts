// The browser twin of the grader against the fixture it shares with
// static/js/study/grader.js, so an offline verdict and an online one cannot
// disagree.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import * as client from '../../domain/grading/client';

const FIXTURES = new URL('../fixtures/', import.meta.url).pathname;
const read = (p: string) => JSON.parse(readFileSync(join(FIXTURES, p), 'utf8'));

interface Case { id: string; module: string; fn: 'grade' | 'matchRegex'; args: unknown[]; expected: unknown }

const cases = (read('offline/grader_cases.json') as { cases: Case[] }).cases;

describe('the client twin matches the offline fixture', () => {
  it('exports the browser surface', () => {
    expect(Object.keys(client).sort()).toEqual(['MAX_REGEX_LEN', 'grade', 'matchRegex']);
  });

  it('covers the whole fixture', () => {
    expect(cases).toHaveLength(46);
    expect(new Set(cases.map((c) => c.fn))).toEqual(new Set(['grade', 'matchRegex']));
    expect(cases.every((c) => c.module === 'grader')).toBe(true);
  });

  it.each(cases.map((c) => [c.id, c] as const))('%s', (_id, c) => {
    const fn = client[c.fn] as (...args: unknown[]) => unknown;
    expect(fn(...c.args)).toEqual(c.expected);
  });
});
