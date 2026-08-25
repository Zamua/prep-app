"""Grading oracle: every branch of `grade`, `match_regex`, and
`validate_regex_update` as `(call, result)` rows.

A raised `ValueError` is a row too: the free-text types are refused by
the synchronous grader, and another implementation must refuse them
the same way.
"""

from __future__ import annotations

import json

from prep.domain.grading import MAX_REGEX_LEN, grade, match_regex, validate_regex_update
from tests.parity.oracles import dump_json, write_corpus

NAME = "grading"

_MCQ = {"type": "mcq", "answer": "Paris"}
_MULTI = {"type": "multi", "answer": json.dumps(["Paris", "Lyon"])}
_SHORT = {"type": "short", "answer": "Paris", "answer_regex": r"paris|par(is)?"}
_CODE = {"type": "code", "answer": "return a + b"}
_LONG_PATTERN = "a" * (MAX_REGEX_LEN + 1)

GRADE_CASES: list[tuple[str, dict, str, bool]] = [
    ("mcq-right", _MCQ, "Paris", False),
    ("mcq-right-surrounding-space", _MCQ, "  Paris  ", False),
    ("mcq-wrong", _MCQ, "Lyon", False),
    ("mcq-wrong-case", _MCQ, "paris", False),
    ("mcq-empty", _MCQ, "", False),
    ("mcq-idk", _MCQ, "Lyon", True),
    ("multi-exact", _MULTI, json.dumps(["Lyon", "Paris"]), False),
    ("multi-partial", _MULTI, json.dumps(["Paris"]), False),
    ("multi-extra", _MULTI, json.dumps(["Paris", "Lyon", "Nice"]), False),
    ("multi-missing-and-extra", _MULTI, json.dumps(["Nice"]), False),
    ("multi-empty", _MULTI, "", False),
    ("multi-malformed-json", _MULTI, "not json", False),
    ("multi-idk", _MULTI, json.dumps(["Paris"]), True),
    ("short-refused", _SHORT, "Paris", False),
    ("short-idk", _SHORT, "", True),
    ("code-refused", _CODE, "return a + b", False),
    ("code-idk", _CODE, "", True),
    ("idk-truncates-long-answer", {"type": "short", "answer": "x" * 500}, "", True),
    ("idk-missing-answer", {"type": "short"}, "", True),
]

MATCH_CASES: list[tuple[str, str | None, str]] = [
    ("no-pattern-none", None, "Paris"),
    ("no-pattern-empty", "", "Paris"),
    ("invalid-pattern", "(", "Paris"),
    ("over-length", _LONG_PATTERN, "a"),
    ("match-exact", r"paris", "Paris"),
    ("match-case-insensitive", r"PARIS", "paris"),
    ("match-alternation", r"paris|par", "par"),
    ("match-fullmatch-only", r"par", "Paris"),
    ("match-strips-whitespace", r"paris", "  paris \n"),
    ("miss", r"paris", "Lyon"),
    ("match-dotall", r"a.b", "a\nb"),
    ("miss-partial-prefix", r"pa", "paris"),
]

VALIDATE_CASES: list[tuple[str, object, str, str | None]] = [
    ("non-str", 42, "Paris", None),
    ("none", None, "Paris", None),
    ("empty", "", "Paris", None),
    ("whitespace-only", "   ", "Paris", None),
    ("over-length", _LONG_PATTERN, "a", None),
    ("invalid", "(", "Paris", None),
    ("misses-expected", r"lyon", "Paris", None),
    ("misses-prior", r"paris", "Paris", "Lyon"),
    ("accepted", r"paris|lyon", "Paris", None),
    ("accepted-with-prior", r"paris|lyon", "Paris", "lyon"),
    ("accepted-strips", r"  paris  ", "Paris", None),
    ("accepted-expected-none", r".*", "", None),
]


def _call(fn, *args, **kwargs) -> dict:
    try:
        return {"result": fn(*args, **kwargs)}
    except ValueError as e:
        return {"error": {"type": "ValueError", "message": str(e)}}


def build() -> dict:
    return {
        "grade": [
            {
                "id": cid,
                "question": question,
                "user_answer": answer,
                "idk": idk,
                **_call(grade, question, answer, idk=idk),
            }
            for cid, question, answer, idk in GRADE_CASES
        ],
        "match_regex": [
            {
                "id": cid,
                "pattern": pattern,
                "given": given,
                "result": match_regex(pattern, given),
            }
            for cid, pattern, given in MATCH_CASES
        ],
        "validate_regex_update": [
            {
                "id": cid,
                "pattern": pattern,
                "expected_literal": expected,
                "prior_given": prior,
                "result": validate_regex_update(
                    pattern, expected_literal=expected, prior_given=prior
                ),
            }
            for cid, pattern, expected, prior in VALIDATE_CASES
        ],
        "header": {"max_regex_len": MAX_REGEX_LEN},
    }


def extract() -> dict[str, str]:
    return {"corpus.json": dump_json(build())}


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
