// The two use-case timings, wrapped around the use cases where they are
// wired so neither one carries a metric.
import type { InstantOutcome } from '../app/instant/generate.js';
import { observeAiGrade, observeInstantGenerate } from '../app/metrics.js';
import type { Judge } from '../app/trivia/grading.js';

/** A judge timed per call and labelled by what the model answered. */
export function observedJudge(judge: Judge): Judge {
  return async (input) => {
    const started = Date.now();
    const judgement = await judge(input);
    observeAiGrade(judgement.kind !== 'verdict' ? 'fallback' : judgement.correct ? 'right' : 'wrong', (Date.now() - started) / 1000);
    return judgement;
  };
}

export type InstantLabel = 'ok' | 'rate_limited' | 'busy' | 'failed_spent' | 'failed_free' | 'invalid';

/** What a generation charged decides first, so spend reads apart from
 * contention. A refusal before admission (a bad topic, or a deploy with no
 * free tier) is `invalid`. */
export function instantLabel(outcome: InstantOutcome): InstantLabel {
  if (outcome.ok) return 'ok';
  if (outcome.charged) return outcome.charged;
  const kind = outcome.payload['kind'];
  return kind === 'rate_limited' || kind === 'busy' ? kind : 'invalid';
}

/** The generator, timed from admission to its outcome. */
export function observedInstant<A extends unknown[]>(generate: (...args: A) => Promise<InstantOutcome>): (...args: A) => Promise<InstantOutcome> {
  return async (...args) => {
    const started = Date.now();
    const outcome = await generate(...args);
    observeInstantGenerate(instantLabel(outcome), (Date.now() - started) / 1000);
    return outcome;
  };
}
