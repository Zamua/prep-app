// Grading dispatch for trivia: stored regex, then string similarity, then
// the model. The model is a port, so this stays a policy, not a client.
import { matchRegex, validateRegexUpdate } from '../../domain/grading/index.js';
import { classifyGrading, gradeAnswer, looksLikeParaphrase } from '../../domain/triviaGrading.js';
import type { Question } from '../entities.js';
import { AgentBusy, AgentUnavailable, type AgentPort } from '../ports.js';

/** Shared-tier contention, not a broken grader: its fallback line says so. */
export { AgentBusy };

/** A grader that has not answered by now loses to the string match, which
 * is instant. */
export const GRADE_TIMEOUT_MS = 12_000;

export interface Verdict {
  correct: boolean;
  feedback: string | null;
  regex_update: string | null;
}

export const AI_GRADE_PROMPT = `You are grading a single short-answer trivia question. As part of
the verdict, you also decide whether the regex used to grade this
card should evolve to accept the user's answer next time.

Question:
%(prompt)s

Expected answer (what we're looking for):
%(expected)s

Current grading regex (or null):
%(current_regex)s

User's answer:
%(given)s

VERDICT:
A correct answer conveys the same fact as the expected answer. Minor
variations in phrasing, casing, or word order are fine. Mark wrong
if the user's answer contradicts, is too vague, or is unrelated.

REGEX UPDATE (only when verdict=right):
Decide whether the user typed a LEGITIMATE ALTERNATIVE FORM of the
expected answer that the regex should accept going forward — for
example a synonym, abbreviation, equivalent number format, or
common alias. Examples:
  expected "write-ahead log"   given "wal"            → update regex
  expected "31.5 million"      given "31.5m"          → update regex
  expected "Isaac Newton"      given "Sir Newton"     → update regex

Do NOT propose a regex update for orthographic typos or spelling
errors — those are forgiven by the grader but should not pollute
the regex. Examples:
  expected "write-ahead log"   given "right-ahead log"   → NO update (typo)
  expected "Crash recovery"    given "crsh recovry"      → NO update (typo)

When proposing a regex_update:
- It must be a case-insensitive full-match pattern.
- It must match BOTH the expected answer AND the user's answer
  (case-insensitive fullmatch).
- Keep it under 200 chars.
- Prefer extending the existing regex with an alternation rather
  than rewriting from scratch (so prior accepted forms still match).

If verdict=wrong, regex_update MUST be null.
If verdict=right but the user's form is a typo (or already accepted
by the current regex), regex_update MUST be null.

Respond with ONLY a JSON object, no prose, no fences:

{"verdict": "right"|"wrong", "feedback": "1-2 sentences explaining why", "regex_update": "regex|alternatives" or null}
`;

/** Strip fences and leading prose, then parse the outermost object. */
export function parseGradeJson(out: string): Record<string, unknown> {
  let text = out.trim().replace(/^```(?:json)?\s*/i, '');
  text = text.replace(/\s*```\s*$/, '');
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end < 0 || end < start) throw new Error('no JSON object');
  return JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

export async function aiGrade(
  agent: AgentPort,
  input: { prompt: string; expected: string; given: string; currentRegex: string | null },
): Promise<Verdict> {
  if (!input.given.trim()) return { correct: false, feedback: 'No answer given.', regex_update: null };
  const fill = (t: string, key: string, value: string) => t.replace(key, () => value);
  let text = fill(AI_GRADE_PROMPT, '%(prompt)s', input.prompt.trim());
  text = fill(text, '%(expected)s', input.expected.trim());
  text = fill(text, '%(current_regex)s', input.currentRegex || 'null');
  text = fill(text, '%(given)s', input.given.trim());

  let out: string;
  try {
    out = await agent.complete({ system: '', user: text, signal: AbortSignal.timeout(GRADE_TIMEOUT_MS) });
  } catch (e) {
    if (!(e instanceof AgentUnavailable)) throw e;
    const feedback =
      e instanceof AgentBusy
        ? '(graded by string similarity - free AI was busy)'
        : '(graded by string similarity - the AI grader was unreachable)';
    return { correct: gradeAnswer(input.expected, input.given), feedback, regex_update: null };
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = parseGradeJson(out);
  } catch {
    return {
      correct: gradeAnswer(input.expected, input.given),
      feedback: '(graded by string similarity - the AI grader returned malformed output)',
      regex_update: null,
    };
  }
  const correct = str(parsed['verdict']).trim().toLowerCase() === 'right';
  let regexUpdate: string | null = null;
  if (correct && str(parsed['regex_update']).trim()) {
    regexUpdate = validateRegexUpdate(parsed['regex_update'], input.expected, input.given);
  }
  return { correct, feedback: str(parsed['feedback']).trim(), regex_update: regexUpdate };
}

/** The regrade path is the same call; the name keeps the dispute explicit. */
export const aiRegrade = aiGrade;

export async function gradeWithFallback(agent: AgentPort, q: Question, userAnswer: string): Promise<Verdict> {
  const regexVerdict = matchRegex(q.answer_regex, userAnswer);
  if (regexVerdict === true) return { correct: true, feedback: null, regex_update: null };

  const ask = () => aiGrade(agent, { prompt: q.prompt, expected: q.answer, given: userAnswer, currentRegex: q.answer_regex });
  if (classifyGrading(q.answer) === 'ai') return ask();

  if (gradeAnswer(q.answer, userAnswer)) return { correct: true, feedback: null, regex_update: null };
  // Deterministic said wrong. A stored regex that missed means the user is
  // engaged with regex-graded content: let the model judge alt-form vs typo.
  const hasRegex = Boolean(q.answer_regex) && regexVerdict === false;
  if (hasRegex || looksLikeParaphrase(q.answer, userAnswer)) return ask();
  return { correct: false, feedback: null, regex_update: null };
}
