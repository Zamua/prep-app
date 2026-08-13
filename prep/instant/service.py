"""Use cases for the instant bounded context.

One synchronous generation call: build the prompt from the topic,
run it through the deploy's free-tier adapter, parse and validate
the output into wire-shaped cards.

Free-tier only, by construction: the adapter comes from
`free_tier_agent()` directly, never `agent_for_user()`. An anonymous
internet endpoint must never be able to spend a BYOK key or the
operator's subscription pool under any deploy shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from prep.agent.port import AgentBusy, AgentPort, AgentTimeout, AgentUnavailable
from prep.agent.selector import free_tier_agent
from prep.domain import grading
from prep.domain.qa_extract import parse_qa_pairs

logger = logging.getLogger(__name__)

TOPIC_MAX_CHARS = 500
DISPLAY_NAME_MAX_CHARS = 60

# Response hygiene caps: over-cap items are skipped, an over-cap list
# is truncated, and fewer than MIN_CARDS survivors is degenerate.
# MAX_CARDS is the free-tier per-generation card cap (docs/INSTANT-START.md
# section 1), not just hygiene: the prompt asks for exactly this many.
MAX_CARDS = 5
MIN_CARDS = 3
CARD_PROMPT_MAX_CHARS = 2000
CARD_ANSWER_MAX_CHARS = 500

# The service owns the deadline (wait_for below) so a stall always
# classifies as spend. The adapter's own transport timeout sits above
# it as a backstop; the shared adapter maps ITS timeout to AgentBusy,
# which would misclassify the spend as a free refusal.
#
# Deploy-tunable because the ceiling is a property of whichever shared
# endpoint funds the free tier, not of this code: the same prompt has
# measured 7s and 130s on the same provider hours apart. Raise it to
# trade a longer wait for fewer refusals when the upstream is slow.
_ENV_TIMEOUT_S = "PREP_INSTANT_TIMEOUT_S"
DEFAULT_GENERATION_TIMEOUT_S = 60.0
_ADAPTER_TIMEOUT_HEADROOM_S = 15.0


def generation_timeout_s() -> float:
    raw = (os.environ.get(_ENV_TIMEOUT_S) or "").strip()
    if not raw:
        return DEFAULT_GENERATION_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.error(
            "%s=%r is not a number; using %.0f", _ENV_TIMEOUT_S, raw, DEFAULT_GENERATION_TIMEOUT_S
        )
        return DEFAULT_GENERATION_TIMEOUT_S
    if value <= 0:
        logger.error(
            "%s=%r must be positive; using %.0f", _ENV_TIMEOUT_S, raw, DEFAULT_GENERATION_TIMEOUT_S
        )
        return DEFAULT_GENERATION_TIMEOUT_S
    return value


_ENV_MAX_OUTPUT_TOKENS = "PREP_INSTANT_MAX_OUTPUT_TOKENS"
DEFAULT_MAX_OUTPUT_TOKENS = 1024


class InstantBusy(RuntimeError):
    """Refused with no upstream spend: concurrency-cap contention or
    shared-capacity contention upstream."""


class InstantTimedOut(RuntimeError):
    """The upstream took longer than the deadline. The call WAS issued,
    so it counts as spend, but the honest cause is a shared endpoint
    under load rather than a broken one."""


class InstantGenerationFailed(RuntimeError):
    """The upstream call was issued and produced no usable deck:
    timeout, provider failure, unparseable or degenerate output.
    Counts as spend."""


# Two concurrent generations bound the endpoint's own resource use;
# contention beyond them refuses immediately rather than queueing.
_semaphore = asyncio.Semaphore(2)


# ---- adapter resolution ----------------------------------------------------

_factory_override: Callable[..., AgentPort | None] | None = None


def set_instant_agent_factory(fn: Callable[..., AgentPort | None] | None) -> None:
    """Test seam - replace the free-tier factory for the rest of the
    process. The factory is called with `max_output_tokens=<int>`.
    Pass `None` to restore the default."""
    global _factory_override
    _factory_override = fn


def _max_output_tokens() -> int:
    raw = (os.environ.get(_ENV_MAX_OUTPUT_TOKENS) or "").strip()
    if not raw:
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        value = int(raw)
    except ValueError:
        logger.error(
            "%s=%r is not an integer; using %d",
            _ENV_MAX_OUTPUT_TOKENS,
            raw,
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
        return DEFAULT_MAX_OUTPUT_TOKENS
    if value <= 0:
        # A non-positive cap would 4xx upstream on every call and
        # charge visitors for the failures.
        logger.error(
            "%s=%r must be positive; using %d",
            _ENV_MAX_OUTPUT_TOKENS,
            raw,
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
        return DEFAULT_MAX_OUTPUT_TOKENS
    return value


def resolve_agent() -> AgentPort | None:
    """The free-tier adapter with the instant output cap, or None when
    the deploy has no free tier. Resolved per request; the factory is
    cheap and never raises by contract."""
    factory = _factory_override or free_tier_agent
    return factory(max_output_tokens=_max_output_tokens())


# ---- topic hygiene ---------------------------------------------------------


def sanitize_topic(raw: object) -> str | None:
    """Cleaned topic text, or None when unusable (not a string, empty
    after cleaning, or over the cap). Control characters are removed;
    tabs and newlines collapse to spaces."""
    if not isinstance(raw, str):
        return None
    if len(raw) > TOPIC_MAX_CHARS * 2:
        # Cleaning removes or 1:1-maps characters, never grows the
        # string, so anything needing more slack than this cannot come
        # back under the cap; bail before the per-char pass.
        return None
    chars = []
    for ch in raw:
        if ch in "\t\r\n":
            chars.append(" ")
        elif unicodedata.category(ch) != "Cc":
            chars.append(ch)
    cleaned = "".join(chars).strip()
    if not cleaned or len(cleaned) > TOPIC_MAX_CHARS:
        return None
    return cleaned


def display_name_for(topic: str) -> str:
    return re.sub(r"\s+", " ", topic).strip()[:DISPLAY_NAME_MAX_CHARS].strip()


# ---- prompt ----------------------------------------------------------------

# Derived from the trivia generation prompt, minus the explanation
# field and the existing-questions block. The regex guidance states
# the offline grader's real contract: there is no fallback grade path,
# a null regex means reveal + self-verdict, so the model should emit a
# regex whenever the answer shape allows one.
_PROMPT_TEMPLATE = """\
You are generating short-answer flashcards for a spaced-repetition
study app. Each card has a Q (the prompt), an A (the short answer),
and an R (a regex that grades the user's typed answer).

Generate **exactly 5** cards on the topic: the 5 most essential
facts or skills a beginner should lock in first. Make each one
count; no filler, no near-duplicates.

Topic: %(topic)s

Constraints:
- Question (q): default to <= 140 chars. When the topic naturally
  calls for a snippet or multi-line formatted content the q MAY be
  longer and include markdown fenced code blocks - keep the FIRST
  line a short plain-language summary.
- Answer (a): short enough to type on a phone in a few seconds. A few
  words, a number, an identifier, a brief phrase, a small expression.
  Not full sentences.
- Cover varied sub-areas of the topic AND vary the recall shape
  across cards - different facets, angles, or skill probes the topic
  naturally supports. Aim for several distinct shapes across the
  batch and don't let any single shape dominate.

REGEX GUIDANCE (the `r` field):
- The regex grades the user's typed answer. Applied with re.IGNORECASE
  and re.fullmatch - the whole user input must match.
- The regex MUST match the canonical answer `a` exactly (case-insensitive).
  After generating, mentally check: does re.fullmatch(r, a) succeed?
- Accept obvious legitimate alternative forms a user might type:
  abbreviations, common synonyms, equivalent number formats, etc.
  Examples:
    a: "write-ahead log"     r: "(write[- ]?ahead log|wal)"
    a: "31.5 million"        r: "(31\\.5 ?(million|m|mil)|thirty[- ]one(?: and a half| point five)? million)"
    a: "Isaac Newton"        r: "(isaac )?newton"
- There is no fallback grader. A null `r` means the card reveals the
  answer and the user grades themselves. Emit a regex whenever the
  answer shape allows one; return null only when no reasonable regex
  can cover the answer.
- Keep regexes reasonably short (under 200 chars).

Return ONLY valid JSON, no prose, no code fences. Format:

[
  {"q": "Question text?", "a": "Short answer", "r": "regex|alternatives"},
  ...
]
"""


def build_prompt(topic: str) -> str:
    return _PROMPT_TEMPLATE % {"topic": topic}


# ---- generation ------------------------------------------------------------


@dataclass(frozen=True)
class InstantDeck:
    display_name: str
    cards: list[dict]


async def generate(agent: AgentPort, topic: str) -> InstantDeck:
    """One upstream call, parsed and validated into the wire deck.

    Raises `InstantBusy` (no spend) or `InstantGenerationFailed`
    (spend); the caller maps those to the ledger's outcome classes.
    """
    prompt = build_prompt(topic)
    deadline = generation_timeout_s()
    if _semaphore.locked():
        raise InstantBusy("generation concurrency cap reached")
    async with _semaphore:
        try:
            result = await asyncio.wait_for(
                agent.run(prompt, timeout_s=deadline + _ADAPTER_TIMEOUT_HEADROOM_S),
                timeout=deadline,
            )
        except asyncio.TimeoutError as e:
            raise InstantTimedOut(f"generation timed out after {deadline:.0f}s") from e
        except AgentTimeout as e:
            # The adapter's transport timeout: the call was issued and
            # spent upstream tokens, so it must not class as a free
            # refusal.
            raise InstantGenerationFailed(str(e)) from e
        except AgentBusy as e:
            raise InstantBusy(str(e)) from e
        except AgentUnavailable as e:
            raise InstantGenerationFailed(str(e)) from e
    return InstantDeck(display_name=display_name_for(topic), cards=_extract_cards(result.text))


def _extract_cards(text: str) -> list[dict]:
    """Parse + per-item hygiene. Model output never touches server
    state: it is validated and returned to the requester's own device."""
    try:
        items = parse_qa_pairs(text)
    except ValueError as e:
        raise InstantGenerationFailed(f"unparseable output: {e}") from e

    cards: list[dict] = []
    for raw in items:
        if len(cards) == MAX_CARDS:
            break
        if not isinstance(raw, dict):
            continue
        q = raw.get("q")
        a = raw.get("a")
        q = q.strip() if isinstance(q, str) else ""
        a = a.strip() if isinstance(a, str) else ""
        if not q or not a:
            continue
        if len(q) > CARD_PROMPT_MAX_CHARS or len(a) > CARD_ANSWER_MAX_CHARS:
            continue
        r_raw = raw.get("r")
        r = grading.validate_regex_update(r_raw, expected_literal=a) if r_raw else None
        cards.append({"prompt": q, "answer": a, "answer_regex": r})
    if len(cards) < MIN_CARDS:
        raise InstantGenerationFailed(f"degenerate output: {len(cards)} usable cards")
    return cards
