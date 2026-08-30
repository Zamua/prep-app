// The entities the ports exchange: typed views over the rows. Field names
// are the column names; a cell holds one user, so no row carries a user
// column.

export type QuestionType = 'code' | 'mcq' | 'multi' | 'short';
export type DeckType = 'srs' | 'trivia';
export type ReviewResult = 'right' | 'wrong';

export const SLUG_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';
export const SLUG_LENGTH = 8;

/** The account row. `login` is the cell id, whatever provider minted it. */
export interface Profile {
  login: string;
  display_name: string | null;
  profile_pic_url: string | null;
  email: string | null;
  created_at: string;
  last_seen_at: string;
  is_anonymous: number;
  notification_prefs: string | null;
  editor_input_mode: string | null;
  active_byok_provider: string | null;
  desired_retention: number | null;
}

export interface ProfileClaims {
  email?: string | null;
  displayName?: string | null;
  profilePicUrl?: string | null;
}

export interface Deck {
  id: number;
  name: string;
  display_name: string | null;
  created_at: string;
  context_prompt: string | null;
  deck_type: DeckType;
  notification_interval_minutes: number | null;
  last_notified_at: string | null;
  notifications_enabled: boolean;
  notification_ignored_streak: number;
  trivia_session_size: number;
  notifications_muted_until: string | null;
}

export interface DeckSummary {
  id: number;
  name: string;
  display_name: string | null;
  total: number;
  due: number;
  deck_type: DeckType;
  pinned: boolean;
}

export interface DeckMeta {
  deck_id: number;
  notifications_enabled: boolean;
  interval_minutes: number | null;
  session_size: number;
  context_prompt: string;
  pinned: boolean;
  display_name: string | null;
}

export interface TriviaSourceMeta {
  notification_interval_minutes: number | null;
  context_prompt: string | null;
}

export interface NewQuestion {
  type: QuestionType;
  prompt: string;
  /** A list for `multi` is stored as its JSON. */
  answer: string | string[];
  topic?: string | null;
  choices?: string[] | null;
  /** A list is stored as `- item` lines. */
  rubric?: string | string[] | null;
  skeleton?: string | null;
  language?: string | null;
  explanation?: string | null;
  answer_regex?: string | null;
}

export interface Question {
  id: number;
  deck_id: number;
  type: QuestionType;
  topic: string | null;
  prompt: string;
  choices: string[] | null;
  answer: string;
  rubric: string | null;
  created_at: string;
  suspended: boolean;
  skeleton: string | null;
  language: string | null;
  explanation: string | null;
  answer_regex: string | null;
}

export interface DeckCard {
  id: number;
  type: QuestionType;
  topic: string | null;
  prompt: string;
  choices: string[] | null;
  answer: string;
  rubric: string | null;
  suspended: boolean;
  skeleton: string | null;
  language: string | null;
  answer_regex: string | null;
  step: number;
  next_due: string | null;
  last_review: string | null;
  rights: number;
  attempts: number;
}

export interface CardState {
  step: number;
  next_due: string;
  interval_minutes: number;
}

export interface CardRow {
  question_id: number;
  step: number;
  next_due: string;
  last_review: string | null;
  stability: number | null;
  difficulty: number | null;
  fsrs_state: number;
  learning_steps: number;
}

export type SessionStatus = 'active' | 'completed' | 'abandoned';
export type SessionState = 'awaiting-answer' | 'grading' | 'showing-result';

export interface StudySession {
  id: string;
  deck_id: number;
  created_at: string;
  last_active: string;
  status: SessionStatus;
  state: SessionState;
  current_question_id: number | null;
  current_draft: string | null;
  current_grading_workflow_id: string | null;
  last_answered_qid: number | null;
  last_answered_verdict: Record<string, unknown> | null;
  last_answered_state: Record<string, unknown> | null;
  version: number;
  device_label: string | null;
}

export interface RecentSession {
  id: string;
  deck_id: number;
  deck_name: string;
  deck_display_name: string | null;
  last_active: string;
  status: SessionStatus;
  state: SessionState;
  device_label: string | null;
  current_question_id: number | null;
  current_prompt: string | null;
  current_type: string | null;
  snoozed_until: string | null;
}

export interface ReviewRow {
  prompt: string;
  ts: string;
  result: string;
  user_answer: string | null;
  grader_notes: string | null;
}

export interface TriviaQueueEntry {
  question_id: number;
  queue_position: number;
  last_answered_at: string | null;
  last_answered_correctly: boolean | null;
}

export interface NextCard {
  question_id: number;
  deck_id: number;
  prompt: string;
  is_fresh: boolean;
}

export type DoneItem = readonly [qid: number, verdict: string];

export interface TriviaSession {
  id: string;
  deck_id: number;
  started_at: string;
  last_active: string;
  status: string;
  queue: number[];
  done: DoneItem[];
}

export interface ActiveTriviaSession {
  deck_name: string;
  deck_display_name: string | null;
  deck_id: number;
  last_active: string;
  queue: number[];
  done: DoneItem[];
  snoozed_until: string | null;
}

export interface TriviaDeckStats {
  total: number;
  unanswered: number;
  wrong: number;
  mastered: number;
}

export interface NotificationLogEntry {
  id: number;
  sent_at: string;
  title: string;
  body: string;
  url: string;
  source: string;
  seen_at: string | null;
}

export interface PushSubscription {
  endpoint: string;
  p256dh: string;
  auth: string;
}

export interface CredentialMetadata {
  provider: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiTokenMetadata {
  id: number;
  label: string | null;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ActiveWorkflow {
  workflow_id: string;
  workflow_type: string;
  deck_id: number | null;
  deck_name: string | null;
  deck_display_name: string | null;
  status: string;
  started_at: string;
  terminal_at: string | null;
  url_path: string;
  notified_action_at: string | null;
  notified_terminal_at: string | null;
}

export interface SnapshotDeck {
  id: number;
  name: string;
  display_name: string | null;
  pinned_at: string | null;
  total: number;
}

export interface SnapshotCard {
  question_id: number;
  deck_id: number;
  type: string;
  prompt: string;
  choices: string[] | null;
  answer: string;
  answer_regex: string | null;
  rubric: string | null;
  skeleton: string | null;
  explanation: string | null;
  step: number;
  next_due: string;
}

export type SyncOutcome = { kind: string; status: string; question_id: number | null };

export interface NotificationPrefs {
  mode: string;
  digest_hour: number;
  tz: string;
  threshold: number;
  quiet_hours_enabled: boolean;
  quiet_start_hour: number;
  quiet_end_hour: number;
  last_digest_date: string | null;
  last_when_ready_at: string | null;
}

export const DEFAULT_NOTIFICATION_PREFS: NotificationPrefs = {
  mode: 'off',
  digest_hour: 9,
  tz: 'America/New_York',
  threshold: 3,
  quiet_hours_enabled: false,
  quiet_start_hour: 22,
  quiet_end_hour: 8,
  last_digest_date: null,
  last_when_ready_at: null,
};

export const EDITOR_INPUT_MODES = ['vanilla', 'vim', 'emacs'] as const;
export const DEFAULT_EDITOR_INPUT_MODE = 'vanilla';

/** A cell's rows by table, for the merge and the migration importer. */
export interface CellSnapshot {
  profile: Record<string, unknown> | null;
  tables: Record<string, Record<string, unknown>[]>;
}

/** One migration chunk as the cell applies it: one table's rows, plus the
 * profile on the chunk that opens a user. */
export interface MigrationWrite {
  idx: number;
  table: string | null;
  rows: readonly Record<string, unknown>[];
  profile: Record<string, unknown> | null;
}

/** What a cell already holds, which is what a killed run resumes from. */
export interface MigrationStatus {
  profile: boolean;
  /** The cell's id block; 0 until a profile chunk lands. */
  idx: number;
  tables: Record<string, number>;
}

export interface DirectoryUser {
  id: string;
  is_anonymous: boolean;
  created_at: string;
  idx: number;
}

export interface MergeAudit {
  id: number;
  anon_user_id: string;
  target_user_id: string;
  started_at: string;
  completed_at: string | null;
  status: string;
  counts: Record<string, number> | null;
  error: string | null;
}

export interface MergeMarker {
  anon_id: string;
  target_id: string;
  audit_id: number;
  started_at: string;
}

export type TombstoneReason = 'merged' | 'reaped' | 'deleted';

export interface Tombstone {
  reason: TombstoneReason;
  at: string;
  scrubbed_at: string | null;
  former_bytes: number;
}

export interface InstantCard {
  prompt: string;
  answer: string;
  answer_regex: string | null;
}

export interface InstantDeckResult {
  slug: string;
  deck_id: number;
}
