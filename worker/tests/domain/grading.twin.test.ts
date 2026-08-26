import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as client from '../../domain/grading/client';

// The committed browser grader and the shared module must give the same
// verdict everywhere, so the file can become a build output of client.ts.
// The one allowed difference: patterns the shared module translates where
// the old file fell to self-verdict.
const REPO = new URL('../../..', import.meta.url).pathname;
const read = (p: string) => JSON.parse(readFileSync(join(REPO, p), 'utf8'));

type Grader = {
  MAX_REGEX_LEN: number;
  grade: (card: unknown, answer: unknown, idk?: boolean) => unknown;
  matchRegex: (pattern: unknown, given: unknown) => boolean | null;
};
const legacy = (await import(pathToFileURL(join(REPO, 'static/js/offline/grader.js')).href)) as Grader;

const LEGACY_SELF_VERDICT = new Set(['short-regex-inline-flag-diverges']);

interface Case { id: string; fn: 'grade' | 'matchRegex'; args: unknown[]; expected: unknown }
interface GradeRow { id: string; question: Record<string, unknown>; user_answer: string; idk: boolean }
interface MatchRow { id: string; pattern: string | null; given: string }

const corpus = read('tests/fixtures/parity/grading/corpus.json') as { grade: GradeRow[]; match_regex: MatchRow[] };
const cases = (read('tests/offline/fixtures/grader_cases.json') as { cases: Case[] }).cases;

describe('client.ts twins grader.js', () => {
  it('exports the same names', () => {
    expect(Object.keys(client).sort()).toEqual(Object.keys(legacy).sort());
    expect(client.MAX_REGEX_LEN).toBe(legacy.MAX_REGEX_LEN);
  });

  it.each(corpus.grade.map((r) => [r.id, r] as const))('corpus grade %s', (_id, r) => {
    expect(client.grade(r.question, r.user_answer, r.idk)).toEqual(legacy.grade(r.question, r.user_answer, r.idk));
  });

  it.each(corpus.match_regex.map((r) => [r.id, r] as const))('corpus match_regex %s', (_id, r) => {
    expect(client.matchRegex(r.pattern, r.given)).toBe(legacy.matchRegex(r.pattern, r.given));
  });

  it.each(cases.map((c) => [c.id, c] as const))('fixture %s', (id, c) => {
    const ours = (client[c.fn] as (...a: unknown[]) => unknown)(...c.args);
    const theirs = (legacy[c.fn] as (...a: unknown[]) => unknown)(...c.args);
    if (LEGACY_SELF_VERDICT.has(id)) {
      expect(theirs).toBeNull();
      expect(ours).toEqual(c.expected);
    } else {
      expect(ours).toEqual(theirs);
    }
  });
});
