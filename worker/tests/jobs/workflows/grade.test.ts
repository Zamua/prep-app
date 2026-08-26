// AI grading end to end: the verdict, the FSRS write under the job id, and
// the two replies that are not a verdict.
import { describe, expect, it } from 'vitest';
import { buildGradePrompt, gradeRecord, gradeJobInput, IDK_FEEDBACK, MissingCard, NO_RUBRIC, parseVerdict } from '../../../app/jobs/grade.js';
import { workflowHarness, writeCtx } from './harness.js';
import { cell } from '../../repos/setup.js';

const ID = 'grade-capitals-q1-0000000004';

const CARD = { type: 'short', prompt: 'Capital of France?', answer: 'Paris', rubric: '- names the city' };

const verdictJson = (result: string) => JSON.stringify({ result, feedback: 'close enough', model_answer_summary: 'It is Paris.' });

function seedQuestion(h: ReturnType<typeof workflowHarness>): { deck: number; qid: number } {
  const deck = h.repos().decks.create('capitals');
  const qid = h.repos().questions.add(deck, { type: 'short', prompt: CARD.prompt, answer: CARD.answer, rubric: CARD.rubric });
  return { deck, qid };
}

const input = (qid: number, extra: Record<string, unknown> = {}) => ({ questionId: qid, deckName: 'capitals', userAnswer: 'paris', idk: false, card: CARD, ...extra });

describe('GradeAnswer', () => {
  it('grades, records the review and answers with the shape the poll returns', async () => {
    const h = workflowHarness(() => verdictJson('right'));
    const { deck, qid } = seedQuestion(h);
    await h.start('GradeAnswer', ID, input(qid));
    expect(await h.run(ID)).toBe('terminal');

    expect(h.statuses(ID)).toEqual(['grading', 'recording', 'done']);
    expect(h.agent.prompts[0]).toContain('**Rubric (what a correct answer must demonstrate):**\n- names the city');
    const result = h.progress(ID)?.progress['result'] as Record<string, unknown>;
    expect(result).toMatchObject({ question_id: qid, user_answer: 'paris', idk: false, verdict: { result: 'right', feedback: 'close enough', model_answer_summary: 'It is Paris.' } });
    expect(Object.keys(result['state'] as object).sort()).toEqual(['interval_minutes', 'next_due', 'step']);

    const reviews = h.repos().reviews.listReviewsForDeck(deck);
    expect(reviews.map((r) => [r.result, r.user_answer, r.grader_notes])).toEqual([['right', 'paris', 'close enough']]);
    // The grading ledger is keyed on the job id itself, not a per-step suffix.
    expect(h.repos().idempotency.findGrading(ID)).toEqual(result['state']);
  });

  it("answers an I-don't-know without asking the model", async () => {
    const h = workflowHarness(() => new Error('the model must not be called'));
    const { qid } = seedQuestion(h);
    await h.start('GradeAnswer', ID, input(qid, { idk: true, userAnswer: '' }));
    expect(await h.run(ID)).toBe('terminal');

    expect(h.agent.prompts).toEqual([]);
    const result = h.progress(ID)?.progress['result'] as { idk: boolean; verdict: { result: string; feedback: string } };
    expect(result.idk).toBe(true);
    expect(result.verdict).toEqual({ result: 'wrong', feedback: IDK_FEEDBACK, model_answer_summary: 'Paris' });
  });

  it('grades wrong and quotes itself when the reply is not a verdict', async () => {
    const h = workflowHarness(() => 'I am afraid I cannot do that');
    const { qid } = seedQuestion(h);
    await h.start('GradeAnswer', ID, input(qid));
    expect(await h.run(ID)).toBe('terminal');

    const verdict = (h.progress(ID)?.progress['result'] as { verdict: { result: string; feedback: string } }).verdict;
    expect(verdict.result).toBe('wrong');
    expect(verdict.feedback).toBe('(grader returned non-JSON: I am afraid I cannot do that)');
  });

  it('grades wrong on any verdict word but `right`', () => {
    expect(parseVerdict(verdictJson('partial'), 'Paris').result).toBe('wrong');
    expect(parseVerdict(JSON.stringify({ result: 'right' }), 'Paris').model_answer_summary).toBe('Paris');
  });

  it('tells the model there is no rubric rather than showing it an empty one', () => {
    expect(buildGradePrompt({ ...CARD, rubric: '' }, 'x')).toContain(NO_RUBRIC);
  });

  it('refuses a job input with no card, since a JobCell cannot read the question', () => {
    expect(() => gradeJobInput({ questionId: 1, userAnswer: 'a', idk: false })).toThrow(MissingCard);
  });
});

describe('the record step', () => {
  it('records the review once however often the step is redelivered', async () => {
    const c = cell();
    const deck = c.repos.decks.create('capitals');
    const qid = c.repos.questions.add(deck, { type: 'short', prompt: CARD.prompt, answer: CARD.answer });
    const ctx = writeCtx({
      repos: c.repos,
      clock: c.clock,
      stepKey: ID,
      name: 'record',
      kind: 'GradeAnswer',
      jobId: ID,
      input: input(qid),
      outputs: { grade: { result: 'right', feedback: 'good', model_answer_summary: 'Paris' } },
    });

    const first = (await gradeRecord(ctx)).value as { state: unknown };
    const second = (await gradeRecord(ctx)).value as { state: unknown };
    expect(second.state).toEqual(first.state);
    expect(c.repos.reviews.listReviewsForDeck(deck).length).toBe(1);
    expect(c.repos.idempotency.findGrading(ID)).toEqual(first.state);
  });
});
