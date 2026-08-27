"""A synthetic prod-shaped snapshot.

    python -m migrate.synth --out <path> --users 40 --seed 7

There is no real snapshot to build against until the operator authorises
one, so this generates a file with the same shape: many users, one heavy
enough to force the importer's chunking, one mid-merge, one anonymous
with nothing at all, push subscriptions, a `claude-subscription` BYOK
row, a PAT holder, `desired_retention` at both clamp ends, and cards in
every FSRS state.

The schema comes from `migrate.legacy_schema`, the frozen copy of what
the snapshot was written by. Rows go in by raw SQL:
the repositories would refuse the states this deliberately produces, a
merge left half-run among them.

The output is produced by `VACUUM INTO`, which is how the operator makes
a real one, so the file the exporter sees is a genuine snapshot with no
`-wal` beside it.

Same `--seed` and `--now` produce a byte-identical database.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from migrate import clock, layout, legacy_schema

# The retention clamp ends, restated rather than imported: this fixture
# pins the values a migrated card was actually scheduled under, and must
# keep pinning them if the domain later widens the range.
RETENTION_MIN = 0.70
RETENTION_MAX = 0.97

QUESTION_TYPES = ("short", "mcq", "code")
LANGUAGES = (None, "python", "typescript")
# A non-ASCII name and an embedded quote plus newline: the export writes
# pure ASCII, and both classes have to survive the round trip.
AWKWARD_TEXT = 'quantum "spin" ½\nsecond line\ttab'


@dataclass
class Ids:
    """Every id is assigned here, so a seed fixes them and they stay far
    below ID_BLOCK (2^32) the way real Python ids are."""

    deck: int = 0
    question: int = 0
    review: int = 0
    notification: int = 0
    token: int = 0
    merge: int = 0
    generation: int = 0

    def next(self, name: str) -> int:
        value = getattr(self, name) + 1
        setattr(self, name, value)
        return value


@dataclass
class Plan:
    """What the generator decided to build, for the tests to assert on
    without re-deriving it."""

    users: list[str] = field(default_factory=list)
    heavy: str = ""
    anonymous: list[str] = field(default_factory=list)
    empty_anonymous: str = ""
    mid_merge: tuple[str, str] = ("", "")
    completed_merge: tuple[str, str] = ("", "")
    push_user: str = ""
    pat_user: str = ""
    subscription_user: str = ""
    retention_low: str = ""
    retention_high: str = ""
    grading_user: str = ""


def _at(base: datetime, **delta) -> str:
    return (base + timedelta(**delta)).isoformat()


def build_schema(path: Path) -> None:
    """Materialises the frozen pre-cutover schema at `path`."""
    legacy_schema.build(Path(path))


# ---- row writers ----------------------------------------------------------


def _user(
    c: sqlite3.Connection,
    uid: str,
    *,
    created_at: str,
    last_seen_at: str,
    is_anonymous: int = 0,
    display_name: str | None = None,
    email: str | None = None,
    desired_retention: float | None = None,
    active_byok_provider: str | None = None,
    editor_input_mode: str | None = None,
    notification_prefs: str | None = None,
    profile_pic_url: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO users (tailscale_login, display_name, profile_pic_url, created_at,"
        " last_seen_at, notification_prefs, editor_input_mode, email, active_byok_provider,"
        " desired_retention, is_anonymous)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            display_name,
            profile_pic_url,
            created_at,
            last_seen_at,
            notification_prefs,
            editor_input_mode,
            email,
            active_byok_provider,
            desired_retention,
            is_anonymous,
        ),
    )


def _deck(
    c: sqlite3.Connection,
    ids: Ids,
    uid: str,
    name: str,
    created_at: str,
    *,
    deck_type: str = "srs",
    desired_retention: float | None = None,
    display_name: str | None = None,
    interval: int | None = None,
) -> int:
    did = ids.next("deck")
    c.execute(
        "INSERT INTO decks (id, user_id, name, created_at, context_prompt, deck_type,"
        " notification_interval_minutes, last_notified_at, notifications_enabled,"
        " notification_ignored_streak, trivia_session_size, pinned_at,"
        " notifications_muted_until, desired_retention, display_name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 3, NULL, NULL, ?, ?)",
        (
            did,
            uid,
            name,
            created_at,
            f"everything worth knowing about {name}",
            deck_type,
            interval,
            None,
            desired_retention,
            display_name,
        ),
    )
    return did


def _questions(
    c: sqlite3.Connection,
    ids: Ids,
    uid: str,
    deck_id: int,
    count: int,
    base: datetime,
    *,
    awkward: bool = False,
) -> list[int]:
    rows = []
    made = []
    for n in range(count):
        qid = ids.next("question")
        made.append(qid)
        qtype = QUESTION_TYPES[n % len(QUESTION_TYPES)]
        prompt = AWKWARD_TEXT if awkward and n == 0 else f"question {qid}: what is {n}?"
        rows.append(
            (
                qid,
                uid,
                deck_id,
                qtype,
                f"topic-{n % 7}",
                prompt,
                json.dumps([f"a{n}", f"b{n}"]) if qtype == "mcq" else None,
                f"answer {n}",
                "grade generously" if qtype == "code" else None,
                _at(base, minutes=-n),
                1 if n % 23 == 0 else 0,
                "def solve():\n    ..." if qtype == "code" else None,
                LANGUAGES[n % len(LANGUAGES)],
                f"because {n}" if n % 3 == 0 else None,
                rf"^\s*answer\s+{n}\s*$" if qtype == "short" else None,
            )
        )
    c.executemany(
        "INSERT INTO questions (id, user_id, deck_id, type, topic, prompt, choices, answer,"
        " rubric, created_at, suspended, skeleton, language, explanation, answer_regex)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return made


def _cards(c: sqlite3.Connection, rng: random.Random, qids: Sequence[int], base: datetime) -> None:
    """Every FSRS state appears, and a fresh card keeps NULL stability.
    Values come off `random()` so they carry full 17-digit doubles: a
    float that only round-trips through `repr` is the one that catches a
    lossy encoder."""
    rows = []
    for n, qid in enumerate(qids):
        state = (n % 3) + 1
        fresh = state == 1 and n % 6 == 0
        rows.append(
            (
                qid,
                0 if fresh else (n % 6),
                _at(base, hours=n % 97, minutes=n % 59),
                None if fresh else _at(base, days=-(n % 31) - 1),
                None if fresh else rng.random() * 400.0,
                None if fresh else 1.0 + rng.random() * 9.0,
                state,
            )
        )
    c.executemany(
        "INSERT INTO cards (question_id, step, next_due, last_review, stability, difficulty,"
        " fsrs_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _reviews(
    c: sqlite3.Connection,
    ids: Ids,
    rng: random.Random,
    qids: Sequence[int],
    count: int,
    base: datetime,
) -> None:
    rows = []
    for n in range(count):
        rows.append(
            (
                ids.next("review"),
                qids[n % len(qids)],
                _at(base, minutes=-n * 7),
                "right" if rng.random() < 0.7 else "wrong",
                f"typed answer {n}",
                "close enough" if n % 11 == 0 else None,
            )
        )
        if len(rows) >= 5000:
            c.executemany(
                "INSERT INTO reviews (id, question_id, ts, result, user_answer, grader_notes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            rows = []
    if rows:
        c.executemany(
            "INSERT INTO reviews (id, question_id, ts, result, user_answer, grader_notes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _study_session(
    c: sqlite3.Connection,
    uid: str,
    deck_id: int,
    sid: str,
    qids: Sequence[int],
    base: datetime,
    *,
    state: str = "awaiting-answer",
    workflow_id: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO study_sessions (id, user_id, deck_id, created_at, last_active, status,"
        " state, current_question_id, current_draft, current_grading_workflow_id,"
        " last_answered_qid, last_answered_verdict, last_answered_state, version, device_label,"
        " snoozed_until) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, 3, 'iPhone', NULL)",
        (
            sid,
            uid,
            deck_id,
            _at(base, hours=-4),
            _at(base, minutes=-9),
            state,
            qids[0],
            "half an answer",
            workflow_id,
            qids[min(1, len(qids) - 1)],
            json.dumps({"correct": True, "notes": "ok"}),
            json.dumps({"step": 2, "next_due": _at(base, days=3)}),
        ),
    )
    for n, qid in enumerate(qids[:3]):
        c.execute(
            "INSERT INTO study_session_answers (session_id, question_id, answered_at, result,"
            " workflow_id) VALUES (?, ?, ?, ?, ?)",
            (sid, qid, _at(base, minutes=-30 + n), "right" if n % 2 else "wrong", f"wf-{sid}-{n}"),
        )


def _trivia(
    c: sqlite3.Connection, uid: str, deck_id: int, qids: Sequence[int], base: datetime, tag: str
) -> None:
    for n, qid in enumerate(qids):
        c.execute(
            "INSERT INTO trivia_queue (question_id, queue_position, last_answered_at,"
            " last_answered_correctly) VALUES (?, ?, ?, ?)",
            (
                qid,
                n,
                None if n == 0 else _at(base, hours=-n),
                None if n == 0 else (n % 2),
            ),
        )
    c.execute(
        "INSERT INTO trivia_sessions (id, user_id, deck_id, started_at, last_active, status,"
        " queue, done, snoozed_until) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL)",
        (
            f"ts-{tag}",
            uid,
            deck_id,
            _at(base, hours=-2),
            _at(base, minutes=-12),
            ",".join(str(q) for q in qids[1:]),
            f"{qids[0]}r",
        ),
    )


# ---- the generator --------------------------------------------------------


def populate(
    conn: sqlite3.Connection,
    *,
    users: int,
    seed: int,
    base: datetime,
    anonymous: int,
    heavy_questions: int,
    heavy_reviews: int,
) -> Plan:
    rng = random.Random(seed)
    ids = Ids()
    plan = Plan()
    c = conn

    provider_count = max(1, users - anonymous)

    # created_at is strictly decreasing with n, and idx ranks by it: the
    # numbering is then a property of the fixture, not of insertion order.
    def born(n: int) -> str:
        return _at(base, days=-400 + n, seconds=n)

    provider_ids = [f"user_2{seed:02d}{n:04d}synthetic" for n in range(provider_count)]
    anon_ids = [f"anon:{rng.getrandbits(128):032x}" for _ in range(anonymous)]

    plan.heavy = provider_ids[0]
    plan.push_user = provider_ids[min(1, provider_count - 1)]
    plan.pat_user = provider_ids[min(2, provider_count - 1)]
    plan.subscription_user = provider_ids[min(3, provider_count - 1)]
    plan.retention_low = provider_ids[min(4, provider_count - 1)]
    plan.retention_high = provider_ids[min(5, provider_count - 1)]
    plan.grading_user = provider_ids[min(6, provider_count - 1)]
    plan.anonymous = list(anon_ids)
    plan.empty_anonymous = anon_ids[0] if anon_ids else ""

    retention = {plan.retention_low: RETENTION_MIN, plan.retention_high: RETENTION_MAX}
    for n, uid in enumerate(provider_ids):
        _user(
            c,
            uid,
            created_at=born(n),
            last_seen_at=_at(base, hours=-(n % 72)),
            display_name=f"Synthetic {n}",
            email=f"synthetic{n}@example.test",
            desired_retention=retention.get(uid),
            active_byok_provider=("claude-subscription" if uid == plan.subscription_user else None),
            editor_input_mode="vim" if n % 5 == 0 else None,
            notification_prefs=json.dumps({"tz": "America/New_York", "quiet_hours": n % 4 == 0}),
            profile_pic_url=f"https://img.example.test/{n}.png" if n % 3 == 0 else None,
        )
    for n, uid in enumerate(anon_ids):
        _user(
            c,
            uid,
            created_at=born(provider_count + n),
            last_seen_at=_at(base, days=-(n % 40)),
            is_anonymous=1,
            display_name=f"Guest {n}",
            desired_retention=RETENTION_MIN if n == 1 else None,
        )
    plan.users = provider_ids + anon_ids

    # The heavy user: one deck big enough that the importer has to chunk.
    heavy_deck = _deck(
        c, ids, plan.heavy, "distributed-systems", born(0), display_name="Distributed Systems"
    )
    heavy_qids = _questions(c, ids, plan.heavy, heavy_deck, heavy_questions, base, awkward=True)
    _cards(c, rng, heavy_qids, base)
    _reviews(c, ids, rng, heavy_qids, heavy_reviews, base)
    _study_session(c, plan.heavy, heavy_deck, "sess-heavy", heavy_qids, base)

    for n, uid in enumerate(provider_ids[1:], start=1):
        srs = _deck(
            c,
            ids,
            uid,
            f"world-capitals-{n}",
            born(n),
            desired_retention=(
                RETENTION_MAX
                if uid == plan.retention_high
                else RETENTION_MIN
                if uid == plan.retention_low
                else None
            ),
        )
        qids = _questions(c, ids, uid, srs, 6 + (n % 5), base)
        _cards(c, rng, qids, base)
        _reviews(c, ids, rng, qids, 3 * len(qids), base)
        _study_session(
            c,
            uid,
            srs,
            f"sess-{n}",
            qids,
            base,
            state="grading" if uid == plan.grading_user else "awaiting-answer",
            workflow_id=f"grade-{uid}-{n}" if uid == plan.grading_user else None,
        )
        if n % 3 == 0:
            trivia = _deck(c, ids, uid, f"trivia-{n}", born(n), deck_type="trivia", interval=180)
            tq = _questions(c, ids, uid, trivia, 4, base)
            _trivia(c, uid, trivia, tq, base, tag=str(n))
        for k in range(n % 4):
            c.execute(
                "INSERT INTO notifications_log (id, user_id, sent_at, title, body, url, source,"
                " seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ids.next("notification"),
                    uid,
                    _at(base, hours=-k - 1),
                    f"{k} cards due",
                    "tap to study",
                    "/decks",
                    "scheduler",
                    _at(base, hours=-k) if k % 2 else None,
                ),
            )
        c.execute(
            "INSERT INTO grading_idempotency (idempotency_key, question_id, step, next_due,"
            " interval_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"grade-{uid}-key", qids[0], 2, _at(base, days=3), 4320, _at(base, hours=-6)),
        )
        c.execute(
            "INSERT INTO offline_sync_idempotency (user_id, client_id, kind, status, question_id,"
            " created_at) VALUES (?, ?, 'review', 'applied', ?, ?)",
            (uid, f"client-{uid}-1", qids[0], _at(base, hours=-5)),
        )
        c.execute(
            "INSERT INTO active_workflows (workflow_id, user_login, workflow_type, deck_id,"
            " deck_name, status, started_at, terminal_at, url_path, notified_action_at,"
            " notified_terminal_at) VALUES (?, ?, 'GenerateDeck', ?, ?, 'running', ?, NULL, ?,"
            " NULL, NULL)",
            (f"wf-{uid}", uid, srs, f"world-capitals-{n}", _at(base, minutes=-3), "/decks"),
        )

    # Two anonymous users with data, the rest bare. The first stays empty:
    # a user with no rows at all still needs a directory entry and a profile.
    for n, uid in enumerate(anon_ids[1:3], start=1):
        deck = _deck(c, ids, uid, f"guest-notes-{n}", born(provider_count + n))
        qids = _questions(c, ids, uid, deck, 3, base)
        _cards(c, rng, qids, base)
        _reviews(c, ids, rng, qids, 4, base)

    _push(c, plan.push_user, base)
    _tokens(c, ids, plan.pat_user, base)
    _byok(c, plan.subscription_user, plan.pat_user, base)
    _merges(c, ids, plan, base, anon_ids)
    _generations(c, ids, rng, plan, base)
    return plan


def _push(c: sqlite3.Connection, uid: str, base: datetime) -> None:
    for n in range(2):
        c.execute(
            "INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at,"
            " last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"https://push.example.test/{uid}/{n}",
                uid,
                f"BP{'x' * 20}{n}",
                f"auth{n}{'y' * 12}",
                _at(base, days=-30 + n),
                _at(base, hours=-n),
            ),
        )


def _tokens(c: sqlite3.Connection, ids: Ids, uid: str, base: datetime) -> None:
    c.execute(
        "INSERT INTO api_tokens (id, user_id, token_hash, label, key_prefix, created_at,"
        " last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ids.next("token"),
            uid,
            "0" * 64,
            "mcp",
            "prep_pat_Aa…x9zT",
            _at(base, days=-60),
            _at(base, hours=-2),
        ),
    )


def _byok(c: sqlite3.Connection, subscriber: str, other: str, base: datetime) -> None:
    rows = [
        (
            subscriber,
            "claude-subscription",
            "enc:subscription",
            "sk-ant-oat01",
            _at(base, days=-90),
        ),
        (subscriber, "anthropic", "enc:anthropic", "sk-ant-api03", _at(base, days=-45)),
        (other, "openai", "enc:openai", "sk-proj", _at(base, days=-12)),
    ]
    for uid, provider, ciphertext, prefix, created in rows:
        c.execute(
            "INSERT INTO byok_credentials (user_id, provider, ciphertext, key_prefix, created_at,"
            " last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, provider, ciphertext, prefix, created, _at(base, hours=-1)),
        )


def _merges(
    c: sqlite3.Connection, ids: Ids, plan: Plan, base: datetime, anon_ids: Sequence[str]
) -> None:
    """One completed merge (the source of `previous_ids`) and one still
    `started` (the runbook's abort criterion)."""
    if len(anon_ids) < 2:
        return
    done = (anon_ids[-1], plan.pat_user)
    started = (anon_ids[-2], plan.push_user)
    plan.completed_merge = done
    plan.mid_merge = started
    c.execute(
        "INSERT INTO account_merges (id, anon_user_id, target_user_id, started_at, completed_at,"
        " status, counts, error) VALUES (?, ?, ?, ?, ?, 'completed', ?, NULL)",
        (
            ids.next("merge"),
            done[0],
            done[1],
            _at(base, days=-7),
            _at(base, days=-7, seconds=2),
            json.dumps({"decks": 1, "questions": 3}),
        ),
    )
    c.execute(
        "INSERT INTO account_merges (id, anon_user_id, target_user_id, started_at, completed_at,"
        " status, counts, error) VALUES (?, ?, ?, ?, NULL, 'started', NULL, NULL)",
        (ids.next("merge"), started[0], started[1], _at(base, minutes=-2)),
    )


def _generations(
    c: sqlite3.Connection, ids: Ids, rng: random.Random, plan: Plan, base: datetime
) -> None:
    """Rows inside and outside the 48 h window, with and without an owner.
    The dropped ones are what makes the limiter filter observable."""
    outcomes = ("ok", "failed_spent", "failed_free", "pending")
    for n in range(60):
        hours = -(n * 3)
        owner = None if n % 4 == 0 else plan.users[n % len(plan.users)]
        c.execute(
            "INSERT INTO instant_generations (id, ip, created_at, outcome, cards, topic_chars,"
            " user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ids.next("generation"),
                f"203.0.113.{n % 200}",
                _at(base, hours=hours),
                outcomes[n % len(outcomes)],
                rng.randint(3, 12),
                rng.randint(10, 200),
                owner,
            ),
        )


def generate(
    out: Path | str,
    *,
    users: int = 40,
    seed: int = 7,
    anonymous: int = 30,
    heavy_questions: int = 5000,
    heavy_reviews: int = 50000,
    now: datetime | None = None,
) -> Plan:
    """Builds a working database, then `VACUUM INTO` the output so what
    lands is a real snapshot with no sidecar."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    base = layout.to_utc(now) if now else clock.now()
    with tempfile.TemporaryDirectory(prefix="prep-synth-") as tmp:
        working = Path(tmp) / "build.sqlite"
        build_schema(working)
        conn = sqlite3.connect(working)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            plan = populate(
                conn,
                users=users,
                seed=seed,
                base=base,
                anonymous=anonymous,
                heavy_questions=heavy_questions,
                heavy_reviews=heavy_reviews,
            )
            conn.commit()
            conn.execute("VACUUM INTO ?", (str(out),))
        finally:
            conn.close()
    return plan


# ---- cli ------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m migrate.synth",
        description="Write a synthetic prod-shaped snapshot for the migration tools.",
    )
    parser.add_argument("--out", required=True, type=Path, help="snapshot file to write")
    parser.add_argument("--users", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--anonymous", type=int, default=30)
    parser.add_argument("--heavy-questions", type=int, default=5000)
    parser.add_argument("--heavy-reviews", type=int, default=50000)
    parser.add_argument("--now", default=None, help="ISO-8601 instant the fixture is built around")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = generate(
        args.out,
        users=args.users,
        seed=args.seed,
        anonymous=args.anonymous,
        heavy_questions=args.heavy_questions,
        heavy_reviews=args.heavy_reviews,
        now=layout.parse_instant(args.now, flag="--now") if args.now else None,
    )
    print(f"{args.out}  {args.out.stat().st_size} bytes")
    print(f"users            {len(plan.users)} ({len(plan.anonymous)} anonymous)")
    print(f"heavy            {plan.heavy}  {args.heavy_questions}q / {args.heavy_reviews}r")
    print(f"mid-merge        {plan.mid_merge[0]} -> {plan.mid_merge[1]}")
    print(f"empty anonymous  {plan.empty_anonymous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
