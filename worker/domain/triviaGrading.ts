// How a trivia answer is judged before any model is asked: normalize,
// compare, and decide whether the expected answer is simple enough that
// string similarity can be trusted at all.

// Everything that is neither a word character nor whitespace, Unicode-wide.
const PUNCTUATION = /[^\p{L}\p{N}\p{M}_\s]/gu;

/** Lowercase, trim, collapse whitespace runs, drop punctuation:
 * `"U.S.A."` becomes `"usa"`. */
export function normalizeForGrading(s: string): string {
  return s.toLowerCase().trim().replace(PUNCTUATION, ' ').replace(/\s+/gu, ' ').trim();
}

const tokens = (s: string): string[] => (s ? s.split(/\s+/u).filter(Boolean) : []);

/** Substantive enough that a deterministic "wrong" might be a false
 * negative: the user wrote an explanation, not the canonical short form. */
export function looksLikeParaphrase(expected: string, given: string): boolean {
  const g = given.trim();
  if (!g) return false;
  return tokens(g).length >= tokens(expected.trim()).length + 2;
}

const NUMERIC = /^[\d.,%\-+]+$/;
const SENTENCE_PUNCTUATION = /[.!?,;:]/;

/** Whether string similarity can grade this expected answer, or a model
 * must. Conservative: a false "you got it wrong" feels terrible, a
 * needless model call only costs seconds. */
export function classifyGrading(expected: string): 'deterministic' | 'ai' {
  const s = expected.trim();
  if (!s) return 'deterministic';
  if (NUMERIC.test(s)) return 'deterministic';
  if (tokens(s).length <= 3 && !SENTENCE_PUNCTUATION.test(s)) return 'deterministic';
  return 'ai';
}

/** True iff `given` matches `expected` after normalization. Liberal about
 * form, strict about content: a false positive is learning poison, a false
 * negative is recoverable. */
export function gradeAnswer(expected: string, given: string): boolean {
  const e = normalizeForGrading(expected);
  const g = normalizeForGrading(given);
  if (!e || !g) return false;
  if (e === g) return true;
  // Whitespace fully removed: "U.S.A." normalizes to "u s a", "usa" to "usa".
  if (e.replace(/ /g, '') === g.replace(/ /g, '')) return true;
  const eTokens = new Set(tokens(e));
  const gTokens = new Set(tokens(g));
  return eTokens.size > 0 && [...eTokens].every((t) => gTokens.has(t));
}
