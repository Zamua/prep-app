"""Offline sync oracle: `{name, request, response}` pairs over
`/api/offline/snapshot` and `/api/offline/sync` against the reader
profile, plus an anonymous account at its question cap for the cap
rejection.

Every item outcome the service can produce appears: created cards
(inbox, named deck, explicit deck), rejected cards (cap, bad deck,
missing prompt), applied and logged-without-reschedule reviews, the
per-item rejections (unknown card, missing client id, bad timestamp,
naive timestamp), a future timestamp clamped to server time, the
whole batch replayed, and the two parse-level 422s.
"""

from __future__ import annotations

from datetime import timedelta

from tests.parity.oracles import PARITY_NOW, PARITY_USER, dump_json, write_corpus
from tests.parity.oracles.harness import jsonable, scratch_app
from tests.parity.oracles.seed import ANON_ID, seed_anonymous

NAME = "offline"
SYNC = "/api/offline/sync"


def _at(**delta) -> str:
    return (PARITY_NOW - timedelta(**delta)).isoformat()


def build_batch(ids: dict) -> dict:
    q = ids["questions"]["srs_a"]
    return {
        "device_id": "parity-device",
        "new_cards": [
            {
                "client_id": "card-inbox",
                "deck_id": None,
                "deck_name": None,
                "prompt": "Capital of Peru?",
                "answer": "Lima",
                "answer_regex": r"lima",
                "created_at": _at(hours=3),
            },
            {
                "client_id": "card-named",
                "deck_id": None,
                "deck_name": "World Capitals",
                "prompt": "Capital of Chile?",
                "answer": "Santiago",
                "answer_regex": "(",
                "created_at": _at(hours=3),
            },
            {
                "client_id": "card-new-named-deck",
                "deck_id": None,
                "deck_name": "Offline Notes",
                "prompt": "Capital of Kenya?",
                "answer": "Nairobi",
                "answer_regex": None,
                "created_at": _at(hours=2),
            },
            {
                "client_id": "card-explicit-deck",
                "deck_id": ids["decks"]["srs_b"]["id"],
                "deck_name": None,
                "prompt": "Paxos phase count?",
                "answer": "2",
                "answer_regex": r"2|two",
                "created_at": _at(hours=2),
            },
            {
                "client_id": "card-bad-deck",
                "deck_id": 999999,
                "prompt": "Orphan?",
                "answer": "yes",
            },
            {
                "client_id": "card-trivia-deck",
                "deck_id": ids["decks"]["trivia"]["id"],
                "prompt": "Not for trivia",
                "answer": "no",
            },
            {"client_id": "card-no-prompt", "prompt": "   ", "answer": "x"},
            {"client_id": "card-no-answer", "prompt": "x", "answer": ""},
            {"client_id": "", "prompt": "x", "answer": "y"},
            {"client_id": 42, "prompt": "x", "answer": "y"},
        ],
        "reviews": [
            {
                "client_id": "review-nairobi-right",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "Nairobi",
                "graded_by": "auto",
                "reviewed_at": _at(hours=1),
            },
            {
                "client_id": "review-nairobi-older",
                "question_id": q["short_regex"],
                "verdict": "wrong",
                "user_answer": "Mombasa",
                "graded_by": "self",
                "reviewed_at": _at(hours=2),
            },
            {
                "client_id": "review-canberra-self",
                "question_id": q["mcq"],
                "verdict": "wrong",
                "user_answer": "Sydney",
                "graded_by": "self",
                "reviewed_at": _at(minutes=30),
            },
            {
                "client_id": "review-offline-card",
                "card_client_id": "card-inbox",
                "verdict": "right",
                "user_answer": "Lima",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=20),
            },
            {
                "client_id": "review-future",
                "question_id": q["multi"],
                "verdict": "right",
                "user_answer": '["Ottawa", "Lima"]',
                "graded_by": "self",
                "reviewed_at": "2099-01-01T00:00:00+00:00",
            },
            {
                "client_id": "review-unknown-card",
                "question_id": 999999,
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-unknown-client-card",
                "card_client_id": "never-sent",
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-both-targets",
                "question_id": q["short_regex"],
                "card_client_id": "card-inbox",
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-no-target",
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-bad-timestamp",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": "yesterday",
            },
            {
                "client_id": "review-naive-timestamp",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": "2026-03-14T14:00:00",
            },
            {
                "client_id": "review-missing-timestamp",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
            },
            {
                "client_id": "review-bad-verdict",
                "question_id": q["short_regex"],
                "verdict": "meh",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-bad-grader",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "friend",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "card-inbox",
                "question_id": q["short_regex"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=10),
            },
            {
                "client_id": "review-suspended",
                "question_id": q["suspended"],
                "verdict": "right",
                "user_answer": "",
                "graded_by": "auto",
                "reviewed_at": _at(minutes=5),
            },
        ],
    }


def extract() -> dict[str, str]:
    with scratch_app() as h:
        from prep.dev.parity_seed import seed

        ids = seed(PARITY_USER, "reader")
        headers = {**h.headers(), "accept": "application/json"}
        h.call("snapshot", "GET", "/api/offline/snapshot", headers=headers)
        h.call("snapshot-unauthenticated", "GET", "/api/offline/snapshot")

        batch = build_batch(ids)
        h.call("sync-batch", "POST", SYNC, headers=headers, json_body=batch)
        h.call("sync-batch-replayed", "POST", SYNC, headers=headers, json_body=batch)
        h.call(
            "snapshot-after-sync",
            "GET",
            "/api/offline/snapshot",
            headers=headers,
            note="new cards and rescheduled reviews visible",
        )
        h.call(
            "sync-empty",
            "POST",
            SYNC,
            headers=headers,
            json_body={"device_id": "parity-device", "new_cards": [], "reviews": []},
        )
        h.call("sync-empty-object", "POST", SYNC, headers=headers, json_body={})
        h.call(
            "sync-over-cap-cards",
            "POST",
            SYNC,
            headers=headers,
            json_body={
                "new_cards": [
                    {"client_id": f"c{i}", "prompt": "p", "answer": "a"} for i in range(101)
                ],
                "reviews": [],
            },
        )
        h.call(
            "sync-over-cap-reviews",
            "POST",
            SYNC,
            headers=headers,
            json_body={
                "new_cards": [],
                "reviews": [
                    {
                        "client_id": f"r{i}",
                        "question_id": ids["questions"]["srs_a"]["short_regex"],
                        "verdict": "right",
                        "user_answer": "",
                        "graded_by": "auto",
                        "reviewed_at": _at(minutes=1),
                    }
                    for i in range(501)
                ],
            },
        )
        h.call(
            "sync-malformed-items",
            "POST",
            SYNC,
            headers=headers,
            json_body={"new_cards": ["not an object"], "reviews": [1]},
        )
        h.call(
            "sync-malformed-body",
            "POST",
            SYNC,
            headers={**headers, "content-type": "application/json"},
            content=b"{not json",
        )
        h.call("sync-unauthenticated", "POST", SYNC, json_body=batch)

        from prep.auth import anon_cookie
        from prep.auth.limits import ANON_MAX_QUESTIONS

        seed_anonymous(ANON_ID, questions=ANON_MAX_QUESTIONS)
        cookie = {"cookie": f"{anon_cookie.COOKIE_NAME}={anon_cookie.mint_cookie(ANON_ID)}"}
        h.call(
            "sync-anonymous-at-cap",
            "POST",
            SYNC,
            headers=cookie,
            json_body={
                "device_id": "anon-device",
                "new_cards": [
                    {"client_id": "anon-card", "prompt": "One more?", "answer": "no"},
                ],
                "reviews": [],
            },
            note="the cap rejects the item; the batch itself is 200",
        )
        recorded = h.recorded
    return {
        "pairs.json": dump_json(
            {
                "header": {
                    "profile": "reader",
                    "ids": ids,
                    "comparison": "JSON responses compared as parsed values; Set-Cookie by regex",
                },
                "pairs": jsonable(recorded),
            }
        )
    }


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
