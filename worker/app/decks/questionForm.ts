// The new-question and edit-question forms take the same fields in the
// same shape, so one parse serves both: the entity when it validates, the
// raw dict the template re-renders so typed input survives an error.
import { pyJsonDumps, pyStrip, type JsonValue } from '../../domain/py.js';
import type { NewQuestion, Question, QuestionType } from '../entities.js';

const VALID_TYPES: readonly string[] = ['code', 'mcq', 'multi', 'short'];

/** Python's `str.splitlines`: its line-boundary set, not just `\n`. */
export function splitLines(s: string): string[] {
  return s.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
}

export type QuestionFormRaw = Record<string, string>;

export interface ParsedQuestionForm {
  question: NewQuestion | null;
  raw: QuestionFormRaw;
  error: string | null;
}

const field = (form: URLSearchParams, name: string): string => pyStrip(form.get(name) ?? '');

export function parseQuestionForm(form: URLSearchParams): ParsedQuestionForm {
  const qtype = field(form, 'type');
  const prompt = field(form, 'prompt');
  const answerRaw = field(form, 'answer');
  const topic = field(form, 'topic') || null;
  const skeleton = field(form, 'skeleton') || null;
  const language = field(form, 'language') || null;
  const rubric = field(form, 'rubric') || null;
  const answerRegex = field(form, 'answer_regex') || null;
  const choicesRaw = field(form, 'choices');
  const lines = splitLines(choicesRaw).map(pyStrip).filter(Boolean);
  const choices = lines.length ? lines : null;

  const raw: QuestionFormRaw = {
    type: qtype,
    prompt,
    answer: answerRaw,
    topic: topic ?? '',
    skeleton: skeleton ?? '',
    language: language ?? '',
    rubric: rubric ?? '',
    answer_regex: answerRegex ?? '',
    choices: choicesRaw,
  };

  let error: string | null = null;
  if (!VALID_TYPES.includes(qtype)) error = `Type must be one of: ${VALID_TYPES.join(', ')}.`;
  else if (!prompt) error = 'Prompt is required.';
  else if (!answerRaw) error = 'Answer is required.';
  else if ((qtype === 'mcq' || qtype === 'multi') && !choices) {
    error = `${qtype.toUpperCase()} questions need at least one choice (one per line).`;
  } else if (qtype === 'code' && !language) error = 'Code questions need a language.';

  if (error) return { question: null, raw, error };

  // `multi` answers store a JSON array; accept either a JSON literal or a
  // newline-separated list.
  let answer = answerRaw;
  if (qtype === 'multi') {
    let parsed: unknown;
    try {
      parsed = JSON.parse(answerRaw);
    } catch {
      answer = pyJsonDumps(splitLines(answerRaw).map(pyStrip).filter(Boolean));
      parsed = undefined;
    }
    if (Array.isArray(parsed)) answer = pyJsonDumps(parsed as JsonValue);
  }

  return {
    question: {
      type: qtype as QuestionType,
      prompt,
      answer,
      topic,
      choices,
      rubric,
      skeleton,
      language,
      answer_regex: answerRegex,
    },
    raw,
    error: null,
  };
}

/** The stored question as the form block expects it: list fields
 * newline-joined, a multi answer unwrapped from its JSON. */
export function questionFormFromEntity(q: Question): QuestionFormRaw {
  let answer = q.answer || '';
  if (q.type === 'multi') {
    try {
      const parsed: unknown = answer ? JSON.parse(answer) : [];
      if (Array.isArray(parsed)) answer = parsed.join('\n');
    } catch {
      // A hand-written answer that is not JSON renders verbatim.
    }
  }
  return {
    type: q.type,
    topic: q.topic ?? '',
    prompt: q.prompt,
    choices: q.choices ? q.choices.join('\n') : '',
    answer,
    rubric: q.rubric ?? '',
    skeleton: q.skeleton ?? '',
    language: q.language ?? '',
    answer_regex: q.answer_regex ?? '',
  };
}
