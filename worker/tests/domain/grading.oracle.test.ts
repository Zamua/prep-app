import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MAX_REGEX_LEN, UnsupportedQuestionType, grade, matchRegex, validateRegexUpdate } from '../../domain/grading';
import * as client from '../../domain/grading/client';

// The Python grader's corpus and the shared offline fixture are the truth;
// no expectation here is hand-written.
const REPO = new URL('../../..', import.meta.url).pathname;
const read = (p: string) => JSON.parse(readFileSync(join(REPO, p), 'utf8'));

interface GradeRow {
  id: string;
  question: Record<string, unknown>;
  user_answer: string;
  idk: boolean;
  result?: Record<string, unknown>;
  error?: { type: string; message: string };
}
interface MatchRow { id: string; pattern: string | null; given: string; result: boolean | null }
interface ValidateRow { id: string; pattern: unknown; expected_literal: string; prior_given: string | null; result: string | null }
interface Corpus {
  header: { max_regex_len: number };
  grade: GradeRow[];
  match_regex: MatchRow[];
  validate_regex_update: ValidateRow[];
}
interface Case { id: string; module: string; fn: 'grade' | 'matchRegex'; args: unknown[]; expected: unknown }

const corpus = read('tests/fixtures/parity/grading/corpus.json') as Corpus;
const cases = (read('tests/offline/fixtures/grader_cases.json') as { cases: Case[] }).cases;

describe('grade matches the corpus', () => {
  it('shares the pattern cap', () => {
    expect(MAX_REGEX_LEN).toBe(corpus.header.max_regex_len);
  });

  it('has every branch', () => {
    expect(corpus.grade).toHaveLength(19);
    expect(corpus.grade.filter((r) => r.error)).toHaveLength(2);
  });

  it.each(corpus.grade.map((r) => [r.id, r] as const))('%s', (_id, row) => {
    if (row.error) {
      expect(row.error.type).toBe('ValueError');
      expect(() => grade(row.question, row.user_answer, row.idk)).toThrow(UnsupportedQuestionType);
    } else {
      expect(grade(row.question, row.user_answer, row.idk)).toEqual(row.result);
    }
  });
});

describe('matchRegex matches the corpus', () => {
  it('has every branch', () => {
    expect(corpus.match_regex).toHaveLength(12);
  });

  it.each(corpus.match_regex.map((r) => [r.id, r] as const))('%s', (_id, row) => {
    expect(matchRegex(row.pattern, row.given)).toBe(row.result);
  });
});

describe('validateRegexUpdate matches the corpus', () => {
  it('has every branch', () => {
    expect(corpus.validate_regex_update).toHaveLength(12);
  });

  it.each(corpus.validate_regex_update.map((r) => [r.id, r] as const))('%s', (_id, row) => {
    expect(validateRegexUpdate(row.pattern, row.expected_literal, row.prior_given)).toBe(row.result);
  });
});

describe('the client twin matches the offline fixture', () => {
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
