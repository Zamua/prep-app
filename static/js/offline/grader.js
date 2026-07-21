// grader.js: the deterministic-grader port for offline study
// (docs/OFFLINE.md section 2, per-type table). Pure functions: no
// I/O, no DOM, no store access, mirroring the shape discipline of
// prep/domain/.
//
// This is a parity port of prep/domain/grading.py for the card types
// that grade without an LLM: mcq (exact match), multi (set
// equality), and short with a usable answer_regex. Everything else
// returns null, which the caller reads as "reveal + self-verdict".
// The two implementations are pinned to each other through shared
// fixtures (tests/offline/fixtures/grader_cases.json): Python runs
// them in tests/offline/test_parity_fixtures.py, the browser suite
// runs them against this module. Change semantics here and the pin
// breaks loudly on both sides.
//
// A locally recorded "auto" verdict replays server-side as a
// verdict, never re-graded, so a cross-engine regex difference can
// only ever change which grading UI the user saw, not corrupt state
// (docs/OFFLINE.md section 6). Still, the regex path prefers null
// over a possible misgrade: see matchRegex.

// Mirror of grading.MAX_REGEX_LEN: anything longer is almost
// certainly a hallucination; answers are 1-5 words, the regex
// shouldn't be a novel.
export const MAX_REGEX_LEN = 500;

// Python-only spellings with direct JS equivalents, normalized
// before compiling: (?P<name> -> (?<name> and (?P=name) -> \k<name>.
// The replacement is textual, so a literal "(?P<" inside a character
// class would be corrupted; answer regexes are short alternations in
// practice, and the worst case is a wrong local verdict on a card
// the server replays by verdict anyway.
function normalizePattern(pattern) {
  return pattern.replace(/\(\?P</g, "(?<").replace(/\(\?P=(\w+)\)/g, "\\k<$1>");
}

// Port of grading.match_regex: case-insensitive, dot-all, fullmatch
// with whitespace tolerance at the boundaries.
//
//   true  -> the pattern matched
//   false -> the pattern compiled but did not match
//   null  -> no pattern, oversized pattern, or not safely compilable
//            in this engine (caller falls back to self-verdict)
//
// Compiled with the "u" flag on purpose: without it, JS silently
// treats Python-only escapes as identity literals (\A becomes the
// letter A) and would misgrade where Python grades fine. With "u"
// those patterns throw instead, and a throw means null, which is the
// self-verdict UI: when in doubt, never misgrade. The bare pattern
// is compiled first so an unbalanced pattern (which Python rejects)
// cannot be accidentally repaired by the ^(?:...)$ fullmatch
// wrapper.
export function matchRegex(pattern, given) {
  if (!pattern || typeof pattern !== "string") return null;
  if (pattern.length > MAX_REGEX_LEN) return null;
  const answer = String(given ?? "").trim();
  // Shorthand classes compile in BOTH engines with different
  // semantics: JS \\w \\d \\b stay ASCII-only even under the u flag,
  // Python's are Unicode. "caf\\w+" fullmatches "café" in Python but
  // fails here, and unlike a compile failure that is a recorded
  // auto-verdict misgrade. When shorthand meets non-ASCII on either
  // side, fall to self-verdict instead of guessing.
  if (/\\[wWbBdD]/.test(pattern) && (!isAscii(pattern) || !isAscii(answer))) {
    return null;
  }
  const normalized = normalizePattern(pattern);
  let compiled;
  try {
    new RegExp(normalized, "isu"); // validity probe, result unused
    compiled = new RegExp("^(?:" + normalized + ")$", "isu");
  } catch (e) {
    return null;
  }
  return compiled.test(answer);
}

function isAscii(s) {
  for (let i = 0; i < s.length; i++) {
    if (s.charCodeAt(i) > 127) return false;
  }
  return true;
}

// Port of grading._grade_mcq: strip both sides, exact comparison.
// Deliberately case-SENSITIVE, same as the online grader.
function gradeMcq(card, userAnswer) {
  const correct = String(userAnswer ?? "").trim() === String(card.answer ?? "").trim();
  return {verdict: correct ? "right" : "wrong"};
}

// gradeMulti support: the Python grader calls set(json.loads(...));
// only a JSON array is a shape the checkbox UI can produce, so
// anything else is treated like a parse failure. (Python's set()
// would iterate a JSON string's characters; that corner is
// unreachable from the UI and deliberately not mirrored.)
function toSet(parsed) {
  if (!Array.isArray(parsed)) throw new TypeError("expected a JSON array");
  return new Set(parsed);
}

// Port of grading._grade_multi: user answer and card answer are
// JSON-encoded arrays; order-independent set equality. The Python
// grader resets BOTH sides to the empty set when either fails to
// parse (and empty == empty, so a broken pair grades right); that
// quirk is mirrored and fixture-pinned so the implementations cannot
// drift apart silently.
function gradeMulti(card, userAnswer) {
  let picked;
  let expected;
  try {
    picked = userAnswer ? toSet(JSON.parse(userAnswer)) : new Set();
    expected = toSet(JSON.parse(card.answer));
  } catch (e) {
    picked = new Set();
    expected = new Set();
  }
  const correct =
    picked.size === expected.size && [...picked].every((item) => expected.has(item));
  return {verdict: correct ? "right" : "wrong"};
}

// The one-call entry point. Returns {verdict: "right"|"wrong"} when
// the card grades deterministically, null when it needs the reveal +
// self-verdict flow (short without a usable regex, code, locally
// authored cards, anything unknown). idk mirrors the online path: a
// wrong verdict regardless of card type, recorded by the caller with
// an empty answer.
export function grade(card, userAnswer, idk = false) {
  if (idk) return {verdict: "wrong"};
  const type = card ? card.type : null;
  if (type === "mcq") return gradeMcq(card, userAnswer);
  if (type === "multi") return gradeMulti(card, userAnswer);
  if (type === "short") {
    const matched = matchRegex(card.answer_regex, userAnswer);
    if (matched === null) return null;
    return {verdict: matched ? "right" : "wrong"};
  }
  return null;
}
