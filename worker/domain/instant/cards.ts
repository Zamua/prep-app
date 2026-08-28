// Topic hygiene, the prompt, and the parse of model output into wire
// cards. Lengths are code points.

export const TOPIC_MAX_CHARS = 500;
export const DISPLAY_NAME_MAX_CHARS = 60;

// Response hygiene caps: over-cap items are skipped, an over-cap list is
// truncated, and fewer than MIN_CARDS survivors is degenerate. MAX_CARDS
// is also what the prompt asks for.
export const MAX_CARDS = 5;
export const MIN_CARDS = 3;
export const CARD_PROMPT_MAX_CHARS = 2000;
export const CARD_ANSWER_MAX_CHARS = 500;

export class QaParseError extends Error {}
export class DegenerateOutput extends Error {}

export interface InstantCard {
  prompt: string;
  answer: string;
  answer_regex: string | null;
}

/** `validateRegexUpdate(pattern, expectedLiteral)` of `domain/grading`. */
export type RegexValidator = (pattern: unknown, expectedLiteral: string) => string | null;

const SPACES = /\s+/g;
const LEADING_FENCE = /^```(?:json)?\s*/i;
const TRAILING_FENCE = /\s*```\s*$/;

/**
 * Cleaned topic text, or null when unusable. Tabs and newlines become
 * spaces, other control characters are dropped.
 */
export function sanitizeTopic(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const chars = [...raw];
  // Cleaning never grows the string: past twice the cap it cannot come back.
  if (chars.length > TOPIC_MAX_CHARS * 2) return null;
  let out = '';
  for (const ch of chars) {
    if (ch === '\t' || ch === '\r' || ch === '\n') out += ' ';
    else if (!/\p{Cc}/u.test(ch)) out += ch;
  }
  const cleaned = out.trim();
  if (!cleaned || [...cleaned].length > TOPIC_MAX_CHARS) return null;
  return cleaned;
}

export function displayNameFor(topic: string): string {
  const collapsed = topic.replace(SPACES, ' ').trim();
  return [...collapsed].slice(0, DISPLAY_NAME_MAX_CHARS).join('').trim();
}

// Derived from the trivia generation prompt, minus the explanation field
// and the existing-questions block.
const PROMPT_LINES = [
  'You are generating short-answer flashcards for a spaced-repetition',
  'study app. Each card has a Q (the prompt), an A (the short answer),',
  'and an R (a regex that grades the user\'s typed answer).',
  '',
  'Generate **exactly 5** cards on the topic: the 5 most essential',
  'facts or skills a beginner should lock in first. Make each one',
  'count; no filler, no near-duplicates.',
  '',
  'Topic: %TOPIC%',
  '',
  'Constraints:',
  '- Question (q): default to <= 140 chars. When the topic naturally',
  '  calls for a snippet or multi-line formatted content the q MAY be',
  '  longer and include markdown fenced code blocks - keep the FIRST',
  '  line a short plain-language summary.',
  '- Answer (a): short enough to type on a phone in a few seconds. A few',
  '  words, a number, an identifier, a brief phrase, a small expression.',
  '  Not full sentences.',
  '- Cover varied sub-areas of the topic AND vary the recall shape',
  '  across cards - different facets, angles, or skill probes the topic',
  '  naturally supports. Aim for several distinct shapes across the',
  "  batch and don't let any single shape dominate.",
  '',
  'REGEX GUIDANCE (the `r` field):',
  '- The regex grades the user\'s typed answer. It is applied',
  '  case-insensitively and must match the WHOLE input, not a substring.',
  '- The regex MUST match the canonical answer `a` exactly (case-insensitive).',
  '  After generating, mentally check: does `a` match `r` end to end?',
  '- Accept obvious legitimate alternative forms a user might type:',
  '  abbreviations, common synonyms, equivalent number formats, etc.',
  '  Examples:',
  '    a: "write-ahead log"     r: "(write[- ]?ahead log|wal)"',
  '    a: "31.5 million"        r: "(31\\.5 ?(million|m|mil)|thirty[- ]one(?: and a half| point five)? million)"',
  '    a: "Isaac Newton"        r: "(isaac )?newton"',
  '- There is no fallback grader. A null `r` means the card reveals the',
  '  answer and the user grades themselves. Emit a regex whenever the',
  '  answer shape allows one; return null only when no reasonable regex',
  '  can cover the answer.',
  '- Keep regexes reasonably short (under 200 chars).',
  '',
  'Return ONLY valid JSON, no prose, no code fences. Format:',
  '',
  '[',
  '  {"q": "Question text?", "a": "Short answer", "r": "regex|alternatives"},',
  '  ...',
  ']',
  '',
];

export const PROMPT_TEMPLATE = PROMPT_LINES.join('\n');

export function buildPrompt(topic: string): string {
  return PROMPT_TEMPLATE.replace('%TOPIC%', () => topic);
}

/**
 * The JSON array in raw model output, tolerating code fences and a
 * leading note. Throws `QaParseError` when there is none.
 */
export function parseQaPairs(stdout: string): unknown[] {
  let text = stdout.trim();
  text = text.replace(LEADING_FENCE, '');
  text = text.replace(TRAILING_FENCE, '');
  const start = text.indexOf('[');
  const end = text.lastIndexOf(']');
  if (start < 0 || end < 0 || end < start) throw new QaParseError('agent output contained no JSON array');
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.slice(start, end + 1));
  } catch (e) {
    throw new QaParseError(e instanceof Error ? e.message : String(e));
  }
  if (!Array.isArray(parsed)) throw new QaParseError('agent JSON was not a list');
  return parsed;
}

const isDict = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Empty JSON values are falsey, containers included. */
function truthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === '') return false;
  if (Array.isArray(v)) return v.length > 0;
  if (isDict(v)) return Object.keys(v).length > 0;
  return true;
}

/**
 * Parse plus per-item hygiene. Throws `QaParseError` on unparseable
 * output and `DegenerateOutput` under MIN_CARDS survivors.
 */
export function extractCards(text: string, validateRegex: RegexValidator): InstantCard[] {
  const items = parseQaPairs(text);
  const cards: InstantCard[] = [];
  for (const raw of items) {
    if (cards.length === MAX_CARDS) break;
    if (!isDict(raw)) continue;
    const q = typeof raw['q'] === 'string' ? raw['q'].trim() : '';
    const a = typeof raw['a'] === 'string' ? raw['a'].trim() : '';
    if (!q || !a) continue;
    if ([...q].length > CARD_PROMPT_MAX_CHARS || [...a].length > CARD_ANSWER_MAX_CHARS) continue;
    const r = raw['r'];
    cards.push({ prompt: q, answer: a, answer_regex: truthy(r) ? validateRegex(r, a) : null });
  }
  if (cards.length < MIN_CARDS) throw new DegenerateOutput(`degenerate output: ${cards.length} usable cards`);
  return cards;
}
