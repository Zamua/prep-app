// The synchronous grader: mcq, multi and "I don't know", plus the regex
// half of short-answer grading. Free text is graded elsewhere.
import { AnswerJsonError, AnswerShapeError, parseJson, sameSet, toSet, type Scalar } from './answerJson';
import { GradingError, literalList, sortedValues } from './literal';
import { matchRegex } from './regex';

export { MAX_REGEX_LEN, matchRegex, translatePattern, validateRegexUpdate } from './regex';
export { GradingError } from './literal';

export type Verdict = 'right' | 'wrong';
export type Question = Record<string, unknown>;
export interface GradeResult {
  result: Verdict;
  feedback: string;
  /** mcq echoes the stored answer, which may be null. */
  model_answer_summary: string | null;
}

export class UnsupportedQuestionType extends Error {}

const IDK_FEEDBACK = "Marked as 'I don't know' — see again soon.";

/** `question.get("answer") or ""`, with a non-text answer refused. */
function answerText(question: Question): string {
  const a = question.answer;
  if (a == null || a === '') return '';
  if (typeof a !== 'string') throw new GradingError('answer is not text');
  return a;
}

export function grade(question: Question, userAnswer: string, idk = false): GradeResult {
  if (idk) {
    return {
      result: 'wrong',
      feedback: IDK_FEEDBACK,
      model_answer_summary: [...answerText(question)].slice(0, 400).join(''),
    };
  }
  const qtype = question.type;
  if (qtype === 'mcq') return gradeMcq(question, userAnswer);
  if (qtype === 'multi') return gradeMulti(question, userAnswer);
  throw new UnsupportedQuestionType(`grade() called with type=${JSON.stringify(qtype)}`);
}

function gradeMcq(question: Question, userAnswer: string): GradeResult {
  const correct = (userAnswer || '').trim() === answerText(question).trim();
  return {
    result: correct ? 'right' : 'wrong',
    feedback: correct ? 'Correct.' : 'Wrong choice.',
    model_answer_summary: (question.answer as string | null | undefined) ?? null,
  };
}

/** The answer parsed and reduced to a set; a non-string cannot be one. */
function loadSet(x: unknown): Scalar[] {
  if (typeof x !== 'string') throw new AnswerShapeError('the stored answer is not text');
  return toSet(parseJson(x));
}

// A parse or type failure on either side empties both sets, so a broken
// pair grades right rather than blaming the learner for stored damage.
function gradeMulti(question: Question, userAnswer: string): GradeResult {
  let picked: Scalar[];
  let expected: Scalar[];
  try {
    picked = userAnswer ? loadSet(userAnswer) : [];
    expected = loadSet(question.answer);
  } catch (e) {
    if (!(e instanceof AnswerJsonError || e instanceof AnswerShapeError)) throw e;
    picked = [];
    expected = [];
  }
  const correct = sameSet(picked, expected);
  const summary = literalList(sortedValues(expected));
  return {
    result: correct ? 'right' : 'wrong',
    feedback: correct ? 'Correct.' : `Expected: ${summary}; you picked: ${literalList(sortedValues(picked))}.`,
    model_answer_summary: summary,
  };
}

/**
 * The browser grader: a verdict where the card grades deterministically,
 * null where it needs reveal and self-verdict.
 */
export function gradeOffline(card: Question | null, userAnswer: unknown, idk = false): { verdict: Verdict } | null {
  if (idk) return { verdict: 'wrong' };
  if (!card) return null;
  const type = card.type;
  if (type === 'mcq' || type === 'multi') {
    return { verdict: grade(card, userAnswer == null ? '' : String(userAnswer)).result };
  }
  if (type === 'short') {
    const matched = matchRegex(card.answer_regex, userAnswer);
    return matched === null ? null : { verdict: matched ? 'right' : 'wrong' };
  }
  return null;
}
