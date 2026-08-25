"""Merge oracle: an anonymous account holding a row in every
user-scoped table merged into a target that already holds colliding
deck slugs and more rows than any anonymous cap allows.

`before.json` and `after.json` hold the rows per table for both
accounts, the `account_merges` audit row, the `MergeResult`, and the
`previous_ids` the offline snapshot reports afterwards.
"""

from __future__ import annotations

from tests.parity.oracles import PARITY_NOW, dump_json, write_corpus
from tests.parity.oracles.harness import Harness, all_rows, jsonable, scratch_app, table_rows
from tests.parity.oracles.seed import ANON_ID, upsert_parity_user

NAME = "merge"
NOW = PARITY_NOW.isoformat()


def seed_anonymous_everywhere(anon: str) -> None:
    """One row per user-scoped table for the anonymous account, plus a
    row in each derived table, both preference columns set, and the
    two colliding deck slugs."""
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute(
            "INSERT INTO users (tailscale_login, display_name, email, created_at, last_seen_at,"
            " is_anonymous, desired_retention, editor_input_mode)"
            " VALUES (?, 'Guest', NULL, ?, ?, 1, 0.8, 'emacs')",
            (anon, NOW, NOW),
        )
        decks = {}
        for slug, display in (("capitals", "World Capitals"), ("x", "X"), ("solo", "Solo")):
            decks[slug] = c.execute(
                "INSERT INTO decks (user_id, name, display_name, created_at) VALUES (?, ?, ?, ?)",
                (anon, slug, display, NOW),
            ).lastrowid
        qid = c.execute(
            "INSERT INTO questions (user_id, deck_id, type, prompt, answer, created_at)"
            " VALUES (?, ?, 'short', 'Capital of France?', 'Paris', ?)",
            (anon, decks["capitals"], NOW),
        ).lastrowid
        c.execute(
            "INSERT INTO cards (question_id, step, next_due, stability, difficulty, fsrs_state,"
            " last_review) VALUES (?, 2, ?, 7.5, 5.0, 2, ?)",
            (qid, NOW, NOW),
        )
        c.execute(
            "INSERT INTO reviews (question_id, ts, result, user_answer) VALUES (?, ?, 'right', 'Paris')",
            (qid, NOW),
        )
        c.execute(
            "INSERT INTO study_sessions (id, user_id, deck_id, created_at, last_active)"
            " VALUES ('anonsession000001', ?, ?, ?, ?)",
            (anon, decks["capitals"], NOW, NOW),
        )
        c.execute(
            "INSERT INTO study_session_answers (session_id, question_id, answered_at, result)"
            " VALUES ('anonsession000001', ?, ?, 'right')",
            (qid, NOW),
        )
        c.execute(
            "INSERT INTO trivia_sessions (id, user_id, deck_id, started_at, last_active)"
            " VALUES ('anontrivia0000001', ?, ?, ?, ?)",
            (anon, decks["solo"], NOW, NOW),
        )
        c.execute("INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, 1)", (qid,))
        c.execute(
            "INSERT INTO notifications_log (user_id, sent_at, title, body, url, source)"
            " VALUES (?, ?, 'due', '1 card', '/', 'srs-digest')",
            (anon, NOW),
        )
        c.execute(
            "INSERT INTO active_workflows"
            " (workflow_id, user_login, workflow_type, status, started_at, url_path)"
            " VALUES ('anon-plan-1', ?, 'plan', 'planning', ?, '/plan/anon-plan-1')",
            (anon, NOW),
        )
        for client_id in ("shared-client", "anon-only-client"):
            c.execute(
                "INSERT INTO offline_sync_idempotency (user_id, client_id, kind, status, created_at)"
                " VALUES (?, ?, 'review', 'applied', ?)",
                (anon, client_id, NOW),
            )
        c.execute(
            "INSERT INTO instant_generations (ip, created_at, outcome, user_id)"
            " VALUES ('203.0.113.5', ?, 'ok', ?)",
            (NOW, anon),
        )
        c.execute(
            "INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at,"
            " last_seen_at) VALUES ('https://push.example/anon', ?, 'p', 'a', ?, ?)",
            (anon, NOW, NOW),
        )
        c.execute(
            "INSERT INTO byok_credentials (user_id, provider, ciphertext, key_prefix, created_at)"
            " VALUES (?, 'anthropic-api', 'ct', 'sk-ant-…', ?)",
            (anon, NOW),
        )
        c.execute(
            "INSERT INTO api_tokens (user_id, token_hash, key_prefix, created_at)"
            " VALUES (?, 'anon-hash', 'prep_pat_…', ?)",
            (anon, NOW),
        )


def seed_target_everywhere(target: str) -> None:
    """The target holds the colliding slugs (`capitals`, and `x` with
    every numbered suffix taken) and more decks and questions than the
    anonymous caps allow, plus its own preferences."""
    from prep.auth.limits import ANON_MAX_QUESTIONS
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute(
            "UPDATE users SET desired_retention = 0.9, editor_input_mode = NULL"
            " WHERE tailscale_login = ?",
            (target,),
        )
        capitals = c.execute(
            "INSERT INTO decks (user_id, name, display_name, created_at)"
            " VALUES (?, 'capitals', 'Capitals', ?)",
            (target, NOW),
        ).lastrowid
        c.execute("INSERT INTO decks (user_id, name, created_at) VALUES (?, 'x', ?)", (target, NOW))
        for n in range(2, 101):
            c.execute(
                "INSERT INTO decks (user_id, name, created_at) VALUES (?, ?, ?)",
                (target, f"x-{n}", NOW),
            )
        for i in range(ANON_MAX_QUESTIONS):
            qid = c.execute(
                "INSERT INTO questions (user_id, deck_id, type, prompt, answer, created_at)"
                " VALUES (?, ?, 'short', ?, ?, ?)",
                (target, capitals, f"Target question {i}?", f"answer {i}", NOW),
            ).lastrowid
            c.execute(
                "INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)", (qid, NOW)
            )
        c.execute(
            "INSERT INTO offline_sync_idempotency (user_id, client_id, kind, status, created_at)"
            " VALUES (?, 'shared-client', 'review', 'applied', ?)",
            (target, NOW),
        )


def snapshot_rows(h: Harness, scoped: dict[str, set[str]], users: tuple[str, ...]) -> dict:
    out: dict = {"users": {}, "tables": {}}
    for login in users:
        rows = table_rows(h.db_path, "users", "tailscale_login", login)
        out["users"][login] = rows[0] if rows else None
    for table, columns in sorted(scoped.items()):
        out["tables"][table] = {
            column: {login: table_rows(h.db_path, table, column, login) for login in users}
            for column in sorted(columns)
        }
    return out


def extract() -> dict[str, str]:
    from prep.auth.merge import discover_user_scoped_tables, merge_anonymous_into
    from prep.infrastructure.db import cursor

    with scratch_app() as h:
        target = upsert_parity_user()["tailscale_login"]
        anon = ANON_ID
        seed_anonymous_everywhere(anon)
        seed_target_everywhere(target)
        with cursor() as c:
            scoped = discover_user_scoped_tables(c)
        populated = {
            table
            for table, columns in scoped.items()
            for column in columns
            if table_rows(h.db_path, table, column, anon)
        }
        missing = sorted(set(scoped) - populated)
        assert not missing, f"anonymous account has no row in {missing}"

        before = snapshot_rows(h, scoped, (anon, target))
        result = merge_anonymous_into(anon, target)
        after = snapshot_rows(h, scoped, (anon, target))
        snapshot = h.client.get("/api/offline/snapshot", headers=h.headers(target))
        assert snapshot.status_code == 200, snapshot.text
        after["account_merges"] = all_rows(h.db_path, "account_merges")
        after["result"] = jsonable(result)
        after["previous_ids"] = snapshot.json()["user"]["previous_ids"]
        after["target_deck_slugs"] = sorted(
            r["name"] for r in table_rows(h.db_path, "decks", "user_id", target)
        )
    header = {
        "anon": anon,
        "target": target,
        "user_scoped_tables": {t: sorted(c) for t, c in sorted(scoped.items())},
    }
    return {
        "before.json": dump_json({"header": header, **jsonable(before)}),
        "after.json": dump_json({"header": header, **jsonable(after)}),
    }


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
