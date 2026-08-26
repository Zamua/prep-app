// AI grading of one free-text answer: the model returns a verdict, then the
// FSRS path records it. Prompt and parser are the Go worker's, transcribed
// byte for byte, because the free-tier stub keys its canned replies on the
// message it is sent.
//
// The Go activity loaded the question from the database; a JobCell holds the
// agent and no repositories, so the card travels in the job input instead,
// which also pins what was graded across a retry.
import { coerceVerdict, gradeResult, truncate, MODEL_ANSWER_SUMMARY_CHARS, type GradeAnswerResult, type SrsState, type Verdict } from '../../domain/jobs/progress.js';
import { llmStep, type StepOutput, type WriteStepContext } from './registry.js';
import { parseJsonObject } from './plan.js';

export const NO_RUBRIC = '(no explicit rubric — judge against the model answer)';
export const IDK_FEEDBACK = "Marked as 'I don't know' — see again soon.";
/** How much of the raw reply a non-JSON verdict quotes back to the user. */
export const RAW_QUOTE_CHARS = 200;

/** The question as the grade step needs it: what the route reads out of the
 * owner's cell before starting the job. */
export interface GradeCard {
  type: string;
  prompt: string;
  answer: string;
  rubric: string;
}

export interface GradeJobInput {
  questionId: number;
  userAnswer: string;
  idk: boolean;
  card: GradeCard;
}

export class MissingCard extends Error {}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

export function gradeJobInput(input: Readonly<Record<string, unknown>>): GradeJobInput {
  const raw = input['card'];
  if (typeof raw !== 'object' || raw === null) throw new MissingCard('grade job input carries no card');
  const card = raw as Record<string, unknown>;
  return {
    questionId: Number(input['questionId'] ?? 0),
    userAnswer: str(input['userAnswer']),
    idk: input['idk'] === true,
    card: { type: str(card['type']), prompt: str(card['prompt']), answer: str(card['answer']), rubric: str(card['rubric']) },
  };
}

export function buildGradePrompt(card: GradeCard, userAnswer: string): string {
  return `You are grading a flashcard answer in a spaced-repetition learning app. Be strict but fair.

**Question type:** ${card.type}
**Prompt:**
${card.prompt}

**Model answer:**
${card.answer}

**Rubric (what a correct answer must demonstrate):**
${card.rubric || NO_RUBRIC}

**User's answer:**
${userAnswer}

Decide: is the user's answer substantively correct? Partial credit counts as wrong (we'll re-show it soon). For \`code\` questions, accept any correct approach — don't require the exact syntax of the model answer.

Output a single JSON object (no prose, no fences) with:
- "result": "right" or "wrong"
- "feedback": 1-3 sentences of feedback the user will see. Be concrete: name what they got/missed.
- "model_answer_summary": 1-2 sentence summary of the model answer for the user to compare against.

Output ONLY the JSON object.`;
}

/** A reply that is not a verdict grades wrong and quotes itself, rather than
 * failing the job: the user gets a card back, and the card comes round again. */
export function parseVerdict(raw: string, modelAnswer: string): Verdict {
  try {
    return coerceVerdict(parseJsonObject(raw), modelAnswer);
  } catch {
    return { result: 'wrong', feedback: `(grader returned non-JSON: ${truncate(raw, RAW_QUOTE_CHARS)})`, model_answer_summary: truncate(modelAnswer, MODEL_ANSWER_SUMMARY_CHARS) };
  }
}

export const gradeStep = llmStep(async (ctx) => {
  const input = gradeJobInput(ctx.input);
  // "I don't know" is a verdict without a call: nothing to grade.
  if (input.idk) {
    const verdict: Verdict = { result: 'wrong', feedback: IDK_FEEDBACK, model_answer_summary: truncate(input.card.answer, MODEL_ANSWER_SUMMARY_CHARS) };
    return { value: verdict };
  }
  const text = await ctx.agent.complete({ system: '', user: buildGradePrompt(input.card, input.userAnswer) });
  return { value: parseVerdict(text, input.card.answer) };
});

/**
 * The review row and the FSRS advance, under the job id. The grading ledger is
 * checked first and written beside the row, so a redelivered step answers from
 * the state it already scheduled instead of reviewing the card twice.
 */
export const gradeRecord = async (ctx: WriteStepContext): Promise<StepOutput> => {
  const input = gradeJobInput(ctx.input);
  const verdict = coerceVerdict(ctx.outputs['grade'], input.card.answer);
  let state: SrsState | null = ctx.repos.idempotency.findGrading(ctx.stepKey);
  if (state === null) {
    state = ctx.repos.reviews.record(input.questionId, verdict.result, input.userAnswer, verdict.feedback);
    ctx.repos.idempotency.recordGrading(ctx.stepKey, input.questionId, state);
  }
  const result: GradeAnswerResult = { question_id: input.questionId, user_answer: input.userAnswer, idk: input.idk, verdict, state };
  return { value: result, progress: gradeResult(result) };
};
