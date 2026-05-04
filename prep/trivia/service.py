"""Use cases for the trivia bounded context.

`generate_batch` — calls the agent for N short-Q-short-A pairs on a
free-text topic, dedupes against the deck's existing prompts, inserts
the survivors via `prep.decks.QuestionRepo.add`, and appends them to
the trivia queue.

`grade_answer` — deterministic case/punctuation/whitespace-tolerant
equivalence check. Free-text trivia answers are short by design, so a
normalized-string compare is ~good-enough for the MVP. We can swap to
a claude-graded path later by routing through the existing
`GradeAnswer` Temporal workflow without touching this seam.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from prep import chat_handoff
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import QuestionRepo
from prep.domain import grading
from prep.trivia.agent_client import AgentUnavailable, run_prompt
from prep.trivia.repo import TriviaQueueRepo

logger = logging.getLogger(__name__)


DEFAULT_BATCH_SIZE = 25


@dataclass(frozen=True)
class GenerateOutcome:
    """Result of a generate_batch call. `inserted` is the count of new
    questions actually written; some entries from claude get rejected
    if they're duplicates or malformed.
    """

    inserted: int
    skipped_duplicates: int
    skipped_invalid: int


_GEN_PROMPT_TEMPLATE = """\
You are generating short-answer trivia questions for a notification-driven
flashcard app. Each card has a Q (the prompt), an A (the short answer),
an E (a deeper explanation that gets revealed when the user taps to
expand "Deep dive"), and an R (a regex that grades user answers).

Generate exactly %(batch_size)d questions on the topic:

%(topic)s

Constraints:
- Each question (q) fits in a phone notification body — <= 140 characters.
- Each answer (a) is 1-5 words. Names, numbers, short phrases. Not sentences.
- Each explanation (e) is 2-4 sentences. Surface the WHY: context,
  causation, why this matters, common misconception, or a memorable
  hook. Treat the user as smart and curious — go beyond restating the
  answer. ~300 characters is a good target.
- Cover varied sub-areas of the topic; don't all be the same flavor.
- Don't repeat any of these existing questions:

%(existing)s

REGEX GUIDANCE (the `r` field):
- The regex grades the user's typed answer. Applied with re.IGNORECASE
  and re.fullmatch — the whole user input must match.
- The regex MUST match the canonical answer `a` exactly (case-insensitive).
  After generating, mentally check: does re.fullmatch(r, a) succeed?
- Accept obvious legitimate alternative forms a user might type:
  abbreviations, common synonyms, equivalent number formats, etc.
  Examples:
    a: "write-ahead log"     r: "(write[- ]?ahead log|wal)"
    a: "31.5 million"        r: "(31\\.5 ?(million|m|mil)|thirty[- ]one(?: and a half| point five)? million)"
    a: "Isaac Newton"        r: "(isaac )?newton"
- Don't try to anticipate typos — the grader has a separate path for
  paraphrase / typo-tolerant matching. The regex is for SEMANTIC
  alternatives, not orthographic ones.
- Keep regexes reasonably short (under 200 chars). If you can't write
  a good regex, return null for `r` — the grader has fallbacks.

Return ONLY valid JSON, no prose, no code fences. Format:

[
  {"q": "Question text?", "a": "Short answer", "e": "2-4 sentence explanation.", "r": "regex|alternatives"},
  ...
]
"""


def _build_prompt(topic: str, batch_size: int, existing: list[str]) -> str:
    if existing:
        existing_block = "\n".join(f"- {p}" for p in existing[:200])
    else:
        existing_block = "(none yet — this is the first batch)"
    return _GEN_PROMPT_TEMPLATE % {
        "batch_size": batch_size,
        "topic": topic.strip(),
        "existing": existing_block,
    }


def _parse_qa_pairs(stdout: str) -> list[dict]:
    """Tolerant parse: claude sometimes wraps JSON in code fences or
    adds a leading note even when told not to. Strip those, then try
    `json.loads` on the bracket-bounded chunk.
    """
    text = stdout.strip()
    # Strip common code-fence wrappers.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    # Find the first [ and last ] — claude occasionally adds a leading
    # "Here are 25 questions:" line despite our explicit instruction.
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        raise ValueError("agent output contained no JSON array")
    chunk = text[start : end + 1]
    parsed = json.loads(chunk)
    if not isinstance(parsed, list):
        raise ValueError("agent JSON was not a list")
    return parsed


def generate_batch(
    *,
    user_id: str,
    deck_id: int,
    topic: str,
    questions_repo: QuestionRepo,
    trivia_repo: TriviaQueueRepo,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> GenerateOutcome:
    """Ask the agent for `batch_size` Q-A pairs on `topic`, insert the
    new ones, and append them to the trivia queue.

    Raises `AgentUnavailable` so the scheduler can decide whether to
    retry on the next tick or surface the error.
    """
    existing = trivia_repo.existing_prompts(deck_id)
    prompt = _build_prompt(topic, batch_size, existing)
    stdout = run_prompt(prompt)

    try:
        pairs = _parse_qa_pairs(stdout)
    except (ValueError, json.JSONDecodeError) as e:
        raise AgentUnavailable(
            f"agent returned unparseable output: {e}; head={stdout[:300]!r}"
        ) from e

    existing_lc = {p.strip().lower() for p in existing}
    inserted = 0
    skipped_dup = 0
    skipped_invalid = 0
    for raw in pairs:
        if not isinstance(raw, dict):
            skipped_invalid += 1
            continue
        q = (raw.get("q") or "").strip()
        a = (raw.get("a") or "").strip()
        # Explanation is optional — if claude omits it the card still
        # works, the Deep dive section just stays hidden.
        e = (raw.get("e") or "").strip() or None
        # Regex is optional + validated. If claude returned something
        # that doesn't compile or doesn't match the canonical answer,
        # store None — the grader falls through to its legacy path.
        r_raw = raw.get("r")
        r = grading.validate_regex_update(r_raw, expected_literal=a) if r_raw else None
        if not q or not a:
            skipped_invalid += 1
            continue
        if q.lower() in existing_lc:
            skipped_dup += 1
            continue
        existing_lc.add(q.lower())
        qid = questions_repo.add(
            user_id,
            deck_id,
            NewQuestion(
                type=QuestionType.SHORT,
                topic=topic,
                prompt=q,
                answer=a,
                explanation=e,
                answer_regex=r,
            ),
        )
        trivia_repo.append_card(qid, deck_id)
        inserted += 1

    logger.info(
        "trivia gen for deck=%s: inserted=%d duplicates=%d invalid=%d",
        deck_id,
        inserted,
        skipped_dup,
        skipped_invalid,
    )
    return GenerateOutcome(
        inserted=inserted,
        skipped_duplicates=skipped_dup,
        skipped_invalid=skipped_invalid,
    )


# ---- grading -----------------------------------------------------------

_NORMALIZE_RE = re.compile(r"[^\w\s]")


def _normalize_for_grading(s: str) -> str:
    """Lowercase + strip leading/trailing whitespace + collapse runs of
    whitespace + drop punctuation. So `"U.S.A."` → `"usa"` and
    `" THE   beatles "` → `"the beatles"`.
    """
    s = s.lower().strip()
    s = _NORMALIZE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def looks_like_paraphrase(*, expected: str, given: str) -> bool:
    """True if the user's answer looks substantive enough that
    deterministic-said-wrong might be a false negative. Used by the
    routes layer as a tie-breaker to escalate to claude.

    Signals (any one is enough):
      - given has 2+ more tokens than expected (user wrote a longer
        explanation rather than the canonical short answer)
      - given is empty/whitespace → NO (clearly didn't try)

    Cheap; doesn't compare semantic content — that's claude's job.
    """
    g = given.strip()
    if not g:
        return False
    e = expected.strip()
    e_tokens = len(e.split())
    g_tokens = len(g.split())
    return g_tokens >= e_tokens + 2


def classify_grading(expected: str) -> str:
    """Decide whether a trivia answer can be reliably graded by
    string similarity, or needs claude.

    Returns "deterministic" or "claude". Conservative: when in doubt,
    use claude. False-deterministic-positives ("you got it wrong"
    when you actually got it right) feel terrible; a false claude
    call costs ~5-10s, which is fine.

    Rules (checked top-down on the EXPECTED answer):
      - empty / whitespace → deterministic (nothing useful to grade)
      - pure numeric (digits + . , - %) → deterministic
      - <= 3 tokens AND no sentence punctuation → deterministic
        (handles "Bobby Prince", "id Software", "Leonardo da Vinci",
        etc.; the existing token-subset matcher in grade_answer
        accepts "Newton" for "Isaac Newton")
      - everything else → claude
    """
    s = expected.strip()
    if not s:
        return "deterministic"
    if re.fullmatch(r"[\d.,%\-+]+", s):
        return "deterministic"
    tokens = s.split()
    if len(tokens) <= 3 and not re.search(r"[.!?,;:]", s):
        return "deterministic"
    return "claude"


_CLAUDE_GRADE_PROMPT = """\
You are grading a single short-answer trivia question. As part of
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
- It must compile under Python's `re` with re.IGNORECASE.
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
"""


def _parse_grade_json(out: str) -> dict:
    """Strip code fences / leading prose, return parsed JSON object.
    Raises ValueError if no JSON object can be extracted."""
    text = out.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0 or end < start:
        raise ValueError("no JSON object")
    return json.loads(text[start : end + 1])


def claude_grade(
    *, prompt: str, expected: str, given: str, current_regex: str | None = None
) -> dict:
    """Synchronous claude-graded verdict. Returns
    `{"correct": bool, "feedback": str, "regex_update": str | None}`.

    `regex_update` is a validated regex the caller should persist on
    the question (claude proposes one only when the user's answer is
    a legitimate alternative form, not a typo). None when the verdict
    is wrong, when the user's form is a typo, when claude didn't
    propose one, or when the proposed regex failed validation (must
    compile, match BOTH the canonical answer AND the user's form).

    Falls back to deterministic match on agent error; regex_update
    is always None on the fallback path.
    """
    if not given.strip():
        return {"correct": False, "feedback": "No answer given.", "regex_update": None}
    prompt_text = _CLAUDE_GRADE_PROMPT % {
        "prompt": prompt.strip(),
        "expected": expected.strip(),
        "given": given.strip(),
        "current_regex": current_regex or "null",
    }
    try:
        out = run_prompt(prompt_text, timeout_s=30.0)
    except AgentUnavailable as e:
        logger.warning("claude_grade: agent unavailable, falling back to string match: %s", e)
        return {
            "correct": grade_answer(expected=expected, given=given),
            "feedback": "(graded by string similarity — claude was unreachable)",
            "regex_update": None,
        }
    try:
        parsed = _parse_grade_json(out)
        verdict = (parsed.get("verdict") or "").strip().lower()
        feedback = (parsed.get("feedback") or "").strip()
        correct = verdict == "right"
        regex_update = None
        if correct:
            proposed = parsed.get("regex_update")
            if isinstance(proposed, str) and proposed.strip():
                regex_update = grading.validate_regex_update(
                    proposed, expected_literal=expected, prior_given=given
                )
        return {"correct": correct, "feedback": feedback, "regex_update": regex_update}
    except (ValueError, json.JSONDecodeError, KeyError) as e:
        logger.warning("claude_grade: bad JSON, falling back to string match: %s", e)
        return {
            "correct": grade_answer(expected=expected, given=given),
            "feedback": "(graded by string similarity — claude returned malformed JSON)",
            "regex_update": None,
        }


# claude_regrade is now an alias for claude_grade — same prompt,
# same return shape. Kept as a name so existing callers + tests
# that read "regrade" remain explicit about the dispute path.
claude_regrade = claude_grade


def grade_answer(*, expected: str, given: str) -> bool:
    """True iff `given` matches `expected` after normalization. Liberal
    enough to handle "us" / "U.S." / "United States" the user proposed
    if claude wrote the expected answer in any of those forms — the
    user types the equivalent variant.

    Strict enough that "newton" doesn't grade as "isaac newton" — for
    multi-word expected answers, the given must contain the same
    tokens. We lean conservative here; false-negative is recoverable
    (user reads the correct answer, dismisses), false-positive is
    learning poison.
    """
    norm_e = _normalize_for_grading(expected)
    norm_g = _normalize_for_grading(given)
    if not norm_e or not norm_g:
        return False
    if norm_e == norm_g:
        return True
    # Compare with whitespace fully removed — handles abbreviations
    # like "U.S.A." (norm → "u s a") vs "usa" (norm → "usa").
    if norm_e.replace(" ", "") == norm_g.replace(" ", ""):
        return True
    # Allow case where expected is multi-word and given includes all
    # tokens (handles "Lincoln" → "Abraham Lincoln" matches).
    e_tokens = set(norm_e.split())
    g_tokens = set(norm_g.split())
    if e_tokens and e_tokens.issubset(g_tokens):
        return True
    return False


# ---- Grading dispatch (deterministic + claude tie-breaker) -------------


def grade_with_fallback(q, user_answer: str) -> dict:
    """Dispatch through three layers, fastest first:

    1. **Stored regex** — if `q.answer_regex` is set and matches
       (case-insensitive fullmatch), instant correct verdict.
    2. **Deterministic string** — case/punctuation/token-subset
       compare via `grade_answer`. Cheap; covers the canonical-form
       case when the regex is missing or hasn't been taught the
       form yet.
    3. **Claude** — fires when:
       - classify_grading says the answer is complex enough to need
         semantic judgment, OR
       - deterministic said wrong AND it looks like a paraphrase, OR
       - the card has a regex that missed (implies the user is
         engaged with regex-graded content; let claude judge whether
         their form is a legit alt and propose a regex update).

    Returns `{"correct": bool, "feedback": str | None,
              "regex_update": str | None}`. regex_update is non-None
    only when the claude path took AND claude proposed a validated
    regex update (callers should persist via QuestionRepo).
    """
    regex_verdict = grading.match_regex(q.answer_regex, user_answer)
    if regex_verdict is True:
        return {"correct": True, "feedback": None, "regex_update": None}

    mode = classify_grading(q.answer)
    if mode == "claude":
        return claude_grade(
            prompt=q.prompt,
            expected=q.answer,
            given=user_answer,
            current_regex=q.answer_regex,
        )

    det_correct = grade_answer(expected=q.answer, given=user_answer)
    if det_correct:
        return {"correct": True, "feedback": None, "regex_update": None}
    # Deterministic said wrong — escalate to claude if (a) the user's
    # answer looks substantive enough to be a paraphrase, or (b) the
    # card has a stored regex that missed (claude can judge alt-form
    # vs typo and propose a regex_update accordingly).
    has_regex = bool(q.answer_regex) and regex_verdict is False
    if has_regex or looks_like_paraphrase(expected=q.answer, given=user_answer):
        return claude_grade(
            prompt=q.prompt,
            expected=q.answer,
            given=user_answer,
            current_regex=q.answer_regex,
        )
    return {"correct": False, "feedback": None, "regex_update": None}


# ---- Explore-with-AI handoff context ----------------------------------


def build_explore_ctx(
    *,
    deck_name: str,
    q,
    user_answer: str,
    correct: bool,
    expected: str,
    idk: bool = False,
) -> dict:
    """Build the "Explore further" template context for a trivia
    card's post-answer state: AI-chat handoff URLs (Claude/ChatGPT)
    plus a Google search link (target=_blank → native browser on
    iOS PWA, escapes the in-app webview).

    `idk=True` flags the prefilled chat message so the AI knows the
    user skipped (vs. an empty user_answer that came from a real
    answer like "")."""
    msg = chat_handoff.build_message(
        deck_name=deck_name,
        q={"type": "short", "prompt": q.prompt, "answer": q.answer},
        user_answer=user_answer,
        verdict={"result": "right" if correct else "wrong"},
        idk=idk,
    )
    return {
        "handoff_urls": chat_handoff.provider_urls(msg),
        "handoff_providers": chat_handoff.CHAT_PROVIDERS,
        "handoff_default_provider": chat_handoff.DEFAULT_PROVIDER,
        "google_search_url": chat_handoff.google_search_url(q.prompt),
    }
