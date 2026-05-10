"""Repo tests for the notify bounded context.

Three repositories live here:
- NotifyPrefsRepo  → user-level prefs (delegates to UserRepo)
- PushSubsRepo     → per-device push subscriptions
- NotificationLogRepo → append-only fired-push history

Tests run against the per-test temp sqlite via the `initialized_db`
fixture and exercise the entity-shaped reads the routes/scheduler
depend on.
"""

from __future__ import annotations

from prep.notify.entities import (
    NotificationPrefs,
    NotifyMode,
)
from prep.notify.repo import (
    NotificationLogRepo,
    NotifyPrefsRepo,
    PushSubsRepo,
)

# ----- NotifyPrefsRepo -----------------------------------------------------


def test_prefs_get_returns_defaults_for_new_user(initialized_db: str):
    """Fresh users with no saved prefs see the canonical defaults."""
    prefs = NotifyPrefsRepo().get(initialized_db)
    assert isinstance(prefs, NotificationPrefs)
    assert prefs.mode == NotifyMode.OFF
    assert prefs.digest_hour == 9


def test_prefs_set_then_get_round_trips(initialized_db: str):
    repo = NotifyPrefsRepo()
    new = NotificationPrefs(mode=NotifyMode.DIGEST, digest_hour=7, threshold=5)
    repo.set(initialized_db, new)
    loaded = repo.get(initialized_db)
    assert loaded.mode == NotifyMode.DIGEST
    assert loaded.digest_hour == 7
    assert loaded.threshold == 5


# ----- PushSubsRepo --------------------------------------------------------


def test_subs_upsert_and_list(initialized_db: str):
    """Upsert one subscription; the raw list call (which is what the
    push sender uses) returns it back as a dict."""
    repo = PushSubsRepo()
    repo.upsert(initialized_db, "https://push/1", "pk1", "ak1")
    subs = repo.list_for_user_raw(initialized_db)
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push/1"
    assert subs[0]["p256dh"] == "pk1"


def test_subs_upsert_replaces_keys_for_same_endpoint(initialized_db: str):
    """Same endpoint upserted twice with different keys → one row,
    latest keys win. Browsers rotate p256dh/auth periodically."""
    repo = PushSubsRepo()
    repo.upsert(initialized_db, "https://push/1", "old-pk", "old-auth")
    repo.upsert(initialized_db, "https://push/1", "new-pk", "new-auth")
    subs = repo.list_for_user_raw(initialized_db)
    assert len(subs) == 1
    assert subs[0]["p256dh"] == "new-pk"
    assert subs[0]["auth"] == "new-auth"


def test_subs_count_and_delete(initialized_db: str):
    repo = PushSubsRepo()
    repo.upsert(initialized_db, "https://push/1", "p", "a")
    repo.upsert(initialized_db, "https://push/2", "p", "a")
    assert repo.count_for_user(initialized_db) == 2
    repo.delete_by_endpoint("https://push/1")
    assert repo.count_for_user(initialized_db) == 1


def test_subs_list_users_with_subs(initialized_db: str):
    """Scheduler iterates only users who have at least one device."""
    PushSubsRepo().upsert(initialized_db, "https://push/x", "p", "a")
    users = PushSubsRepo().list_users_with_subs()
    assert initialized_db in users


# ----- NotificationLogRepo -------------------------------------------------


def test_log_append_returns_id_and_lists_recent(initialized_db: str):
    repo = NotificationLogRepo()
    rid = repo.append(
        user_id=initialized_db,
        title="hi",
        body="body",
        url="/notify",
        source="manual",
    )
    assert isinstance(rid, int) and rid > 0
    entries = repo.list_recent(initialized_db, limit=5)
    assert len(entries) == 1
    assert entries[0].title == "hi"
    assert entries[0].source == "manual"


def test_log_count_unseen_and_mark_all_seen(initialized_db: str):
    repo = NotificationLogRepo()
    for i in range(3):
        repo.append(
            user_id=initialized_db,
            title=f"t{i}",
            body="b",
            url="/notify",
            source="trivia",
        )
    assert repo.count_unseen(initialized_db) == 3
    repo.mark_all_seen(initialized_db)
    assert repo.count_unseen(initialized_db) == 0
