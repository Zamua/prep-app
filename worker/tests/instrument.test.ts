import { describe, expect, it } from 'vitest';
import type { InstantOutcome } from '../app/instant/generate.js';
import type { Judgement } from '../app/trivia/grading.js';
import { instantLabel, observedInstant, observedJudge } from '../runtime/instrument.js';
import { metricCount } from './metricCount.js';

const GRADE = 'prep_ai_grade_duration_seconds';
const INSTANT = 'prep_instant_generate_duration_seconds';
const INPUT = { prompt: 'p', expected: 'e', given: 'g', currentRegex: null };

const refused = (kind: string, charged: 'failed_spent' | 'failed_free' | null = null): InstantOutcome => ({
  ok: false,
  status: 429,
  payload: { kind },
  retryAfterS: null,
  charged,
});

describe('the instant outcome label', () => {
  it('puts what was charged ahead of the refusal the client reads', () => {
    expect(instantLabel({ ok: true, slug: 's', mintedId: null })).toBe('ok');
    expect(instantLabel(refused('busy', 'failed_free'))).toBe('failed_free');
    expect(instantLabel(refused('generation_failed', 'failed_spent'))).toBe('failed_spent');
    expect(instantLabel(refused('deck_limit', 'failed_spent'))).toBe('failed_spent');
    expect(instantLabel(refused('busy'))).toBe('busy');
    expect(instantLabel(refused('rate_limited'))).toBe('rate_limited');
    expect(instantLabel(refused('invalid_topic'))).toBe('invalid');
    expect(instantLabel(refused('not_configured'))).toBe('invalid');
  });

  it('records one observation per generation under its label', async () => {
    const before = metricCount(INSTANT, 'outcome="failed_free"');
    const outcome = refused('busy', 'failed_free');
    expect(await observedInstant(async () => outcome)()).toBe(outcome);
    expect(metricCount(INSTANT, 'outcome="failed_free"')).toBe(before + 1);
  });
});

describe('the observed judge', () => {
  it.each([
    [{ kind: 'verdict', correct: true, feedback: '', regexUpdate: null }, 'right'],
    [{ kind: 'verdict', correct: false, feedback: '', regexUpdate: null }, 'wrong'],
    [{ kind: 'busy' }, 'fallback'],
    [{ kind: 'unreachable' }, 'fallback'],
    [{ kind: 'malformed' }, 'fallback'],
  ] as [Judgement, string][])('labels %o as %s and passes it through', async (judgement, verdict) => {
    const before = metricCount(GRADE, `verdict="${verdict}"`);
    expect(await observedJudge(async () => judgement)(INPUT)).toBe(judgement);
    expect(metricCount(GRADE, `verdict="${verdict}"`)).toBe(before + 1);
  });
});
