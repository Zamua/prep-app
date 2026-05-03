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

from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import QuestionRepo
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
and an E (a deeper explanation that gets revealed when the user taps to
expand "Deep dive").

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

Return ONLY valid JSON, no prose, no code fences. Format:

[
  {"q": "Question text?", "a": "Short answer", "e": "2-4 sentence explanation."},
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
