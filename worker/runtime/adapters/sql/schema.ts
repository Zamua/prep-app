// The UserCell schema. A cell holds one user, so no table carries a user
// column; every other column name is what a migrated row already uses, so
// an import copies rows verbatim.

export const PROFILE_TABLE = 'profile';

/** Tables whose ids come from the cell's id block (docs/PHASE-3.md 0). */
export const AUTOINCREMENT_TABLES = ['decks', 'questions', 'reviews', 'notifications_log', 'api_tokens'] as const;

/** Data tables in parent-before-child order: the order `importRows` inserts. */
export const DATA_TABLES = [
  'decks',
  'questions',
  'cards',
  'reviews',
  'grading_idempotency',
  'questions_idempotency',
  'steps_idempotency',
  'offline_sync_idempotency',
  'study_sessions',
  'study_session_answers',
  'trivia_sessions',
  'trivia_queue',
  'notifications_log',
  'push_subscriptions',
  'byok_credentials',
  'api_tokens',
  'active_workflows',
  'job_progress',
] as const;
export type DataTable = (typeof DATA_TABLES)[number];

export const USER_SCHEMA = `
CREATE TABLE IF NOT EXISTS profile (
  id                   TEXT PRIMARY KEY,
  display_name         TEXT,
  profile_pic_url      TEXT,
  email                TEXT,
  created_at           TEXT NOT NULL,
  last_seen_at         TEXT NOT NULL,
  is_anonymous         INTEGER NOT NULL DEFAULT 0,
  notification_prefs   TEXT,
  editor_input_mode    TEXT,
  active_byok_provider TEXT,
  desired_retention    REAL,
  id_base              INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS decks (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  name                        TEXT NOT NULL UNIQUE,
  created_at                  TEXT NOT NULL,
  context_prompt              TEXT,
  deck_type                   TEXT NOT NULL DEFAULT 'srs',
  notification_interval_minutes INTEGER,
  last_notified_at            TEXT,
  notifications_enabled       INTEGER NOT NULL DEFAULT 1,
  notification_ignored_streak INTEGER NOT NULL DEFAULT 0,
  trivia_session_size         INTEGER NOT NULL DEFAULT 3,
  pinned_at                   TEXT,
  notifications_muted_until   TEXT,
  desired_retention           REAL,
  display_name                TEXT
);

CREATE TABLE IF NOT EXISTS questions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deck_id      INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
  type         TEXT NOT NULL,
  topic        TEXT,
  prompt       TEXT NOT NULL,
  choices      TEXT,
  answer       TEXT NOT NULL,
  rubric       TEXT,
  created_at   TEXT NOT NULL,
  suspended    INTEGER NOT NULL DEFAULT 0,
  skeleton     TEXT,
  language     TEXT,
  explanation  TEXT,
  answer_regex TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_deck ON questions(deck_id);

CREATE TABLE IF NOT EXISTS cards (
  question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  step        INTEGER NOT NULL DEFAULT 0,
  next_due    TEXT NOT NULL,
  last_review TEXT,
  stability   REAL,
  difficulty  REAL,
  fsrs_state  INTEGER NOT NULL DEFAULT 1,
  learning_steps INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(next_due);

CREATE TABLE IF NOT EXISTS reviews (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  ts           TEXT NOT NULL,
  result       TEXT NOT NULL,
  user_answer  TEXT,
  grader_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_q ON reviews(question_id);

CREATE TABLE IF NOT EXISTS grading_idempotency (
  idempotency_key  TEXT PRIMARY KEY,
  question_id      INTEGER NOT NULL,
  step             INTEGER NOT NULL,
  next_due         TEXT NOT NULL,
  interval_minutes INTEGER NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  question_id     INTEGER NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  result          TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offline_sync_idempotency (
  client_id   TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL,
  question_id INTEGER,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_sessions (
  id                          TEXT PRIMARY KEY,
  deck_id                     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
  created_at                  TEXT NOT NULL,
  last_active                 TEXT NOT NULL,
  status                      TEXT NOT NULL DEFAULT 'active',
  state                       TEXT NOT NULL DEFAULT 'awaiting-answer',
  current_question_id         INTEGER REFERENCES questions(id),
  current_draft               TEXT,
  current_grading_workflow_id TEXT,
  last_answered_qid           INTEGER,
  last_answered_verdict       TEXT,
  last_answered_state         TEXT,
  version                     INTEGER NOT NULL DEFAULT 1,
  device_label                TEXT,
  snoozed_until               TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON study_sessions(status, last_active);
CREATE INDEX IF NOT EXISTS idx_sessions_deck ON study_sessions(deck_id, status);

CREATE TABLE IF NOT EXISTS study_session_answers (
  session_id  TEXT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  answered_at TEXT NOT NULL,
  result      TEXT NOT NULL,
  workflow_id TEXT,
  PRIMARY KEY (session_id, question_id)
);

CREATE TABLE IF NOT EXISTS trivia_sessions (
  id            TEXT PRIMARY KEY,
  deck_id       INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
  started_at    TEXT NOT NULL,
  last_active   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active',
  queue         TEXT NOT NULL DEFAULT '',
  done          TEXT NOT NULL DEFAULT '',
  snoozed_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_trivia_sessions_status ON trivia_sessions(status, last_active DESC);
CREATE INDEX IF NOT EXISTS idx_trivia_sessions_deck_status ON trivia_sessions(deck_id, status);

CREATE TABLE IF NOT EXISTS trivia_queue (
  question_id             INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  queue_position          INTEGER NOT NULL,
  last_answered_at        TEXT,
  last_answered_correctly INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trivia_queue_pos ON trivia_queue(queue_position);

CREATE TABLE IF NOT EXISTS notifications_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  sent_at TEXT NOT NULL,
  title   TEXT NOT NULL,
  body    TEXT NOT NULL,
  url     TEXT NOT NULL,
  source  TEXT NOT NULL,
  seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_log_sent ON notifications_log(sent_at DESC);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint     TEXT PRIMARY KEY,
  p256dh       TEXT NOT NULL,
  auth         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS byok_credentials (
  provider     TEXT PRIMARY KEY,
  ciphertext   TEXT NOT NULL,
  key_prefix   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS api_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash   TEXT NOT NULL UNIQUE,
  label        TEXT,
  key_prefix   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS active_workflows (
  workflow_id          TEXT PRIMARY KEY,
  workflow_type        TEXT NOT NULL,
  deck_id              INTEGER,
  deck_name            TEXT,
  status               TEXT NOT NULL,
  started_at           TEXT NOT NULL,
  terminal_at          TEXT,
  url_path             TEXT NOT NULL,
  notified_action_at   TEXT,
  notified_terminal_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_active_workflows_terminal ON active_workflows(terminal_at);

CREATE TABLE IF NOT EXISTS job_progress (
  workflow_id TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  transition  INTEGER NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tombstone (
  reason       TEXT NOT NULL,
  at           TEXT NOT NULL,
  scrubbed_at  TEXT,
  former_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);
`;

export const DIRECTORY_SCHEMA = `
CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  is_anonymous INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  idx          INTEGER NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_users_anon ON users(created_at) WHERE is_anonymous = 1;

CREATE TABLE IF NOT EXISTS account_merges (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  anon_user_id   TEXT NOT NULL,
  target_user_id TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  completed_at   TEXT,
  status         TEXT NOT NULL,
  counts         TEXT,
  error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_merges_anon ON account_merges(anon_user_id);
CREATE INDEX IF NOT EXISTS idx_account_merges_target ON account_merges(target_user_id, status);

CREATE TABLE IF NOT EXISTS merge_markers (
  anon_id    TEXT PRIMARY KEY,
  target_id  TEXT NOT NULL,
  audit_id   INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tombstones (
  id     TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);
`;

export const LIMITER_SCHEMA = `
CREATE TABLE IF NOT EXISTS instant_generations (
  id          INTEGER PRIMARY KEY,
  ip          TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  outcome     TEXT NOT NULL DEFAULT 'pending',
  cards       INTEGER,
  topic_chars INTEGER,
  user_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_instant_generations_ip_created ON instant_generations (ip, created_at);
CREATE INDEX IF NOT EXISTS idx_instant_generations_created ON instant_generations (created_at);
CREATE INDEX IF NOT EXISTS idx_instant_generations_user_created ON instant_generations (user_id, created_at);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);
`;

/**
 * One job's cell: the step ledger. `steps` is the idempotency ledger, one row
 * per unit of work; `events` is the signal inbox; `outbox` is the status
 * write, one row per transition. `job.cursor` is the node the next activation
 * resumes at, and `job.state` is written, never inferred.
 */
export const JOB_SCHEMA = `
CREATE TABLE IF NOT EXISTS job (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  owner         TEXT NOT NULL,
  input         TEXT NOT NULL,
  state         TEXT NOT NULL,
  cursor        INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  deadline_at   TEXT,
  deadline_kind TEXT,
  terminal_at   TEXT,
  terminal_status TEXT,
  error         TEXT,
  transition    INTEGER NOT NULL DEFAULT 0,
  url_path      TEXT NOT NULL DEFAULT '',
  workflow_type TEXT NOT NULL DEFAULT '',
  deck_id       INTEGER,
  deck_name     TEXT
);

CREATE TABLE IF NOT EXISTS steps (
  step_key        TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  idx             INTEGER NOT NULL,
  item            INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL,
  attempt         INTEGER NOT NULL DEFAULT 0,
  refusals        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  output          TEXT,
  error           TEXT,
  started_at      TEXT,
  finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_idx ON steps(idx, item);

CREATE TABLE IF NOT EXISTS events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  payload     TEXT,
  at          TEXT NOT NULL,
  consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
  transition      INTEGER PRIMARY KEY,
  status          TEXT NOT NULL,
  payload         TEXT NOT NULL,
  at              TEXT NOT NULL,
  delivered_at    TEXT,
  attempt         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);
`;
