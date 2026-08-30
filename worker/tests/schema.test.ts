import { describe, expect, it } from 'vitest';
import { AUTOINCREMENT_TABLES, DATA_TABLES } from '../runtime/adapters/sql/schema.js';
import { migrate, USER_MIGRATIONS, DIRECTORY_MIGRATIONS, LIMITER_MIGRATIONS } from '../runtime/adapters/sql/index.js';
import { Db } from '../runtime/adapters/sql/storage.js';
import { FakeCellStorage } from './fakes/sqlStorage.js';

/** Every column of every user-cell table, in declaration order. A repo maps
 * rows by name, so a dropped or renamed column is a read that returns
 * undefined rather than an error. */
const COLUMNS: Record<string, string[]> = {
  active_workflows: ["workflow_id", "workflow_type", "deck_id", "deck_name", "status", "started_at", "terminal_at", "url_path", "notified_action_at", "notified_terminal_at"],
  api_tokens: ["id", "token_hash", "label", "key_prefix", "created_at", "last_used_at"],
  byok_credentials: ["provider", "ciphertext", "key_prefix", "created_at", "last_used_at"],
  cards: ["question_id", "step", "next_due", "last_review", "stability", "difficulty", "fsrs_state", "learning_steps"],
  decks: ["id", "name", "created_at", "context_prompt", "deck_type", "notification_interval_minutes", "last_notified_at", "notifications_enabled", "notification_ignored_streak", "trivia_session_size", "pinned_at", "notifications_muted_until", "desired_retention", "display_name"],
  grading_idempotency: ["idempotency_key", "question_id", "step", "next_due", "interval_minutes", "created_at"],
  job_progress: ["workflow_id", "payload", "transition", "updated_at"],
  notifications_log: ["id", "sent_at", "title", "body", "url", "source", "seen_at"],
  offline_sync_idempotency: ["client_id", "kind", "status", "question_id", "created_at"],
  profile: ["id", "display_name", "profile_pic_url", "email", "created_at", "last_seen_at", "is_anonymous", "notification_prefs", "editor_input_mode", "active_byok_provider", "desired_retention", "id_base"],
  push_subscriptions: ["endpoint", "p256dh", "auth", "created_at", "last_seen_at"],
  questions: ["id", "deck_id", "type", "topic", "prompt", "choices", "answer", "rubric", "created_at", "suspended", "skeleton", "language", "explanation", "answer_regex"],
  questions_idempotency: ["idempotency_key", "question_id", "created_at"],
  reviews: ["id", "question_id", "ts", "result", "user_answer", "grader_notes"],
  schema_version: ["version"],
  steps_idempotency: ["idempotency_key", "result", "created_at"],
  study_session_answers: ["session_id", "question_id", "answered_at", "result", "workflow_id"],
  study_sessions: ["id", "deck_id", "created_at", "last_active", "status", "state", "current_question_id", "current_draft", "current_grading_workflow_id", "last_answered_qid", "last_answered_verdict", "last_answered_state", "version", "device_label", "snoozed_until"],
  tombstone: ["reason", "at", "scrubbed_at", "former_bytes"],
  trivia_queue: ["question_id", "queue_position", "last_answered_at", "last_answered_correctly"],
  trivia_sessions: ["id", "deck_id", "started_at", "last_active", "status", "queue", "done", "snoozed_until"],
};

function columnsOf(db: Db): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const t of db.tables()) out[t] = [...db.columns(t)];
  return out;
}

describe('the user cell schema', () => {
  const storage = new FakeCellStorage();
  migrate(storage.sql, USER_MIGRATIONS);
  const db = new Db(storage.sql);
  const ts = columnsOf(db);

  it('carries every table with the columns it declares', () => {
    for (const [table, cols] of Object.entries(COLUMNS)) {
      expect.soft(ts[table], `table ${table}`).toEqual(cols);
    }
    expect(Object.keys(ts).sort()).toEqual(Object.keys(COLUMNS).sort());
  });

  it('has exactly the tables the phase names', () => {
    const expected = [...DATA_TABLES, 'profile', 'questions_idempotency', 'tombstone', 'schema_version'];
    expect(db.tables().sort()).toEqual([...new Set(expected)].sort());
  });

  it('keeps the indexes that carry a query', () => {
    const names = db.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'").map((r) => r.name);
    for (const idx of ['idx_questions_deck', 'idx_cards_due', 'idx_reviews_q', 'idx_sessions_status', 'idx_sessions_deck', 'idx_trivia_queue_pos']) {
      expect(names).toContain(idx);
    }
  });

  it('autoincrements exactly the tables the id block covers', () => {
    const auto = db.all<{ sql: string; name: string }>("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE '%AUTOINCREMENT%'").map((r) => r.name);
    expect(auto.sort()).toEqual([...AUTOINCREMENT_TABLES].sort());
  });

  it('cascades a deck delete through its subtree', () => {
    db.run("INSERT INTO profile (id, created_at, last_seen_at) VALUES ('u', 't', 't')");
    const deck = db.insert("INSERT INTO decks (name, created_at) VALUES ('d', 't')");
    const q = db.insert("INSERT INTO questions (deck_id, type, prompt, answer, created_at) VALUES (?, 'short', 'p', 'a', 't')", deck);
    db.run("INSERT INTO cards (question_id, next_due) VALUES (?, 't')", q);
    db.run("INSERT INTO reviews (question_id, ts, result) VALUES (?, 't', 'right')", q);
    db.run("INSERT INTO study_sessions (id, deck_id, created_at, last_active) VALUES ('s', ?, 't', 't')", deck);
    db.run("INSERT INTO study_session_answers (session_id, question_id, answered_at, result) VALUES ('s', ?, 't', 'right')", q);
    db.run("INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, 1)", q);
    db.run("DELETE FROM decks WHERE id = ?", deck);
    for (const t of ['questions', 'cards', 'reviews', 'study_sessions', 'study_session_answers', 'trivia_queue']) {
      expect(db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM ${t}`)?.n, t).toBe(0);
    }
  });
});

describe('the global cells', () => {
  it('directory: users, merges, markers, tombstones', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, DIRECTORY_MIGRATIONS);
    const db = new Db(storage.sql);
    expect(db.tables()).toEqual(['account_merges', 'merge_markers', 'schema_version', 'tombstones', 'users']);
    expect([...db.columns('users')]).toEqual(['id', 'is_anonymous', 'created_at', 'idx']);
  });

  it('limiter: the ledger and its three indexes', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, LIMITER_MIGRATIONS);
    const db = new Db(storage.sql);
    expect([...db.columns('instant_generations')]).toEqual(['id', 'ip', 'created_at', 'outcome', 'cards', 'topic_chars', 'user_id']);
    const idx = db.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'instant_generations'").map((r) => r.name);
    expect(idx.sort()).toEqual(['idx_instant_generations_created', 'idx_instant_generations_ip_created', 'idx_instant_generations_user_created']);
  });
});
