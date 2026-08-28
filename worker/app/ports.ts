// The ports the app layer speaks through. Adapters live in runtime/adapters
// and meet these interfaces at the composition root only. No repository
// method takes a user: a cell holds one.
import type { ScheduledReview } from '../domain/fsrs/index.js';
import type { LedgerCommit, LedgerRows, StepOutput } from '../domain/jobs/ledger.js';
import type { GradeCard, TransformCard, TransformDeck, TransformScope } from '../domain/jobs/snapshot.js';
import type { Refusal } from '../domain/instant/limiter.js';
import type { AccountRows } from '../domain/limits.js';
import type {
  ActiveTriviaSession,
  ActiveWorkflow,
  ApiTokenMetadata,
  CardRow,
  CardState,
  CellSnapshot,
  CredentialMetadata,
  Deck,
  DeckCard,
  DeckMeta,
  DeckSummary,
  DeckType,
  DirectoryUser,
  DoneItem,
  InstantCard,
  InstantDeckResult,
  MergeAudit,
  MergeMarker,
  MigrationStatus,
  MigrationWrite,
  NewQuestion,
  NextCard,
  NotificationLogEntry,
  NotificationPrefs,
  Profile,
  ProfileClaims,
  PushSubscription,
  Question,
  RecentSession,
  ReviewResult,
  ReviewRow,
  SnapshotCard,
  SnapshotDeck,
  StudySession,
  SyncOutcome,
  Tombstone,
  TombstoneReason,
  TriviaDeckStats,
  TriviaQueueEntry,
  TriviaSession,
  TriviaSourceMeta,
} from './entities.js';

export interface Clock {
  now(): Date;
}

/** Where a subject came from; the cell's gates branch on it. */
export type IdentityKind = 'clerk' | 'fake' | 'anon' | 'pat';

export interface Identity {
  subject: string;
  kind: IdentityKind;
  displayName: string | null;
  email: string | null;
  profilePicUrl: string | null;
}

/** `null` for any flow a provider does not have; the template hides it. */
export interface SignInUrls {
  sign_in: string | null;
  sign_up: string | null;
  sign_out: string | null;
  account: string | null;
}

export interface IdentityProvider {
  readonly name: string;
  /** Never throws: a credential that fails verification is an unauthenticated request. */
  identify(request: Request): Promise<Identity | null>;
  /** The browser holds evidence of a session these credentials no longer prove. */
  hasDormantSession(request: Request): boolean;
  urls(): SignInUrls;
}

/** Why a signed webhook was not accepted; the route maps it to a status. */
export type WebhookFailure = 'no_secret' | 'missing_headers' | 'bad_timestamp' | 'bad_signature';

export interface WebhookVerifier {
  /** null when the request is authentic. */
  verify(request: Request, body: string, now: Date): Promise<WebhookFailure | null>;
}

export interface Renderer {
  render(template: string, context: Record<string, unknown>): string;
}

// ---- randomness -----------------------------------------------------------

export interface Random {
  bytes(n: number): Uint8Array;
  choice<T>(seq: readonly T[]): T;
}

export interface SessionIds {
  next(): Promise<string>;
}

// ---- errors the ports raise ----------------------------------------------

/** A version-checked session mutation lost to another device. */
export class StaleVersionError extends Error {
  constructor(readonly currentVersion: number) {
    super(`stale session version (current is ${currentVersion})`);
  }
}

export class SessionNotFound extends Error {}
export class QuestionNotFound extends Error {}
/** UNIQUE(name) on decks. */
export class DeckNameTaken extends Error {}
/** A sync item failed validation against server state; the batch continues. */
export class SyncItemRejected extends Error {}

// ---- repositories ---------------------------------------------------------

export interface DeckRepo {
  /** Creates with no context prompt when missing; the cap gates the create only. */
  getOrCreate(name: string): number;
  findId(name: string): number | null;
  findName(deckId: number): string | null;
  getType(deckId: number): DeckType | null;
  getMeta(deckId: number): DeckMeta;
  getTriviaSourceMeta(deckId: number): TriviaSourceMeta | null;
  /** Throws `DeckNameTaken` on a slug collision. */
  create(name: string, opts?: { contextPrompt?: string | null; displayName?: string | null }): number;
  updateDisplayName(name: string, displayName: string): boolean;
  getContextPrompt(name: string): string | null;
  updateContextPrompt(name: string, contextPrompt: string): void;
  /** False when the source is missing or the new slug is taken. */
  rename(oldName: string, newName: string): boolean;
  /** Rows removed (0 or 1); the subtree cascades. */
  delete(name: string): number;
  listSummaries(): DeckSummary[];
  /** Every deck for the reorganize prompt, by name, cards excluded. */
  listForTransform(): Omit<TransformDeck, 'cards'>[];
  dueBreakdown(): [name: string, due: number][];
  createTrivia(name: string, opts: { topic: string; intervalMinutes: number; displayName?: string | null }): number;
  listTriviaDecks(): Deck[];
  recordNotificationFire(deckId: number, ts: string, ignoredStreak: number): void;
  resetIgnoredStreakForDeck(deckId: number): void;
  /** Throws `RangeError` outside 1..720. */
  setNotificationInterval(deckId: number, minutes: number): boolean;
  getTriviaSessionSize(deckId: number): number;
  /** Throws `RangeError` outside 1..20. */
  setTriviaSessionSize(deckId: number, size: number): boolean;
  setNotificationsEnabled(deckId: number, enabled: boolean): boolean;
  muteNotificationsUntil(deckId: number, untilIso: string | null): boolean;
  setPinned(deckId: number, pinned: boolean): boolean;
  getDesiredRetention(deckId: number): number | null;
  setDesiredRetention(deckId: number, retention: number | null): boolean;
}

export interface QuestionRepo {
  /** Seeds the cards row for an SRS deck; trivia decks queue instead. */
  add(deckId: number, q: NewQuestion): number;
  /** Throws `QuestionNotFound`. SRS state survives the edit. The card form
   * carries no explanation, so that column stays as it stands. */
  update(qid: number, q: NewQuestion): void;
  /** `update` plus the explanation: a transform returns the card's whole new
   * shape, so the field the form cannot reach moves with the rest. */
  replace(qid: number, q: NewQuestion): void;
  setAnswerRegex(qid: number, regex: string | null): boolean;
  get(qid: number): Question | null;
  moveToDeck(questionIds: readonly number[], destDeckId: number): number;
  listInDeck(deckId: number): DeckCard[];
  /** One card as the transform prompt shows it; the Go loader took no
   * suspended filter here, since the user named this card. */
  cardForTransform(qid: number): TransformCard | null;
  /** A deck's cards for the transform prompt: unsuspended, by id. */
  cardsForTransform(deckId: number): TransformCard[];
  promptsInDeck(deckId: number): string[];
  /** The deck's card with this prompt under Go's `LOWER(TRIM(prompt))`
   * compare, or null: the trivia insert's dedupe, one indexed lookup. */
  findByPrompt(deckId: number, prompt: string): number | null;
  setSuspended(qid: number, suspended: boolean): void;
  delete(qid: number): boolean;
}

/** The scheduler state of a card, and the reviews behind it. */
export interface CardRepo {
  srsState(qid: number): CardRow | null;
  /** The retention the scheduler runs at: deck override, else the profile's. */
  effectiveRetention(qid: number): number | null;
  /** The cards row as `ReviewRepo.record` writes it. */
  writeScheduled(qid: number, scheduled: ScheduledReview, reviewedAt: string): void;
  listCardStateForDeck(deckId: number): (CardRow & { prompt: string })[];
  restoreCardState(qid: number, fields: Partial<Omit<CardRow, 'question_id'>>): void;
  countDue(): number;
  nextDueMinutes(deckId?: number | null): number | null;
  dueQuestions(deckId: number, limit?: number): Question[];
}

export interface ReviewRepo {
  /** Records a review and advances the SRS state; the canonical grade path. */
  record(qid: number, result: ReviewResult, userAnswer: string, notes?: string): CardState;
  listReviewsForDeck(deckId: number): ReviewRow[];
  importReview(qid: number, ts: string, result: ReviewResult, userAnswer?: string, graderNotes?: string | null): void;
  getLastUserAnswer(qid: number): string | null;
}

export interface SessionRepo {
  create(deckId: number, deviceLabel: string): Promise<string>;
  get(sid: string): StudySession | null;
  findActiveForDeck(deckId: number): StudySession | null;
  /** Ages idle sessions out first; snoozed ones are hidden. */
  listRecent(limit?: number): RecentSession[];
  snooze(sid: string, untilIso: string | null): void;
  listSnoozed(): RecentSession[];
  updateDraft(sid: string, draft: string, expectedVersion: number): number;
  recordAnswerSync(
    sid: string,
    questionId: number,
    expectedVersion: number,
    userAnswer: string,
    verdict: Record<string, unknown>,
    state: Record<string, unknown>,
  ): number;
  setGrading(sid: string, questionId: number, workflowId: string, expectedVersion: number): number;
  gradingCompleted(sid: string, questionId: number, verdict: Record<string, unknown>, state: Record<string, unknown>, workflowId: string): void;
  gradingAbandoned(sid: string, workflowId: string): void;
  advance(sid: string, expectedVersion: number): number;
  markCompleted(sid: string): void;
  abandon(sid: string): void;
  abandonAllForDeck(deckId: number): number;
}

export interface TriviaRepo {
  appendCard(questionId: number, deckId: number): TriviaQueueEntry;
  pickNextForDeck(deckId: number): NextCard | null;
  listQueueForDeck(deckId: number): (TriviaQueueEntry & { prompt: string })[];
  importEntry(questionId: number, queuePosition: number, opts?: { lastAnsweredAt?: string | null; lastAnsweredCorrectly?: number | null }): void;
  markAnswered(questionId: number, correct: boolean): void;
  setLastCorrectness(questionId: number, correct: boolean): void;
  countUnanswered(deckId: number): number;
  deckStats(deckId: number): TriviaDeckStats;
  hasAnswerSince(deckId: number, ts: string | null): boolean;
  countPendingReview(deckId: number): number;
  pickSessionForDeck(deckId: number, opts?: { targetSize?: number; freshTarget?: number }): NextCard[];
  promptForQuestion(questionId: number): string | null;
  existingPrompts(deckId: number): string[];

  getActiveSessionForDeck(deckId: number): TriviaSession | null;
  listActiveSessions(): ActiveTriviaSession[];
  snoozeActiveForDeck(deckId: number, untilIso: string | null): number;
  listSnoozedSessions(): ActiveTriviaSession[];
  startOrResume(deckId: number, state: { queue: readonly number[]; done: readonly DoneItem[] }): Promise<TriviaSession>;
  replaceActive(deckId: number, state: { queue: readonly number[] }): Promise<TriviaSession>;
  persistState(deckId: number, state: { queue: readonly number[]; done: readonly DoneItem[] }): void;
  completeSession(deckId: number): void;
  abandonAllSessionsForDeck(deckId: number): number;
}

export interface NotifyRepo {
  append(entry: { title: string; body: string; url: string; source: string }): number;
  listRecent(limit?: number): NotificationLogEntry[];
  countUnseen(): number;
  markAllSeen(): void;
}

export interface PushSubRepo {
  upsert(endpoint: string, p256dh: string, auth: string): void;
  list(): PushSubscription[];
  count(): number;
  deleteByEndpoint(endpoint: string): void;
}

/** Ciphertext in, ciphertext out: the `Cipher` port wraps it. */
export interface ByokRepo {
  store(provider: string, ciphertext: string, keyPrefix: string): CredentialMetadata;
  delete(provider: string): boolean;
  touchLastUsed(provider: string): void;
  getCiphertext(provider: string): string | null;
  metadata(provider: string): CredentialMetadata | null;
  listProviders(): string[];
}

export interface TokenRepo {
  insert(tokenHash: string, keyPrefix: string, label: string | null): ApiTokenMetadata;
  list(): ApiTokenMetadata[];
  delete(tokenId: number): boolean;
  /** Touches `last_used_at`; null when no row matches the hash. */
  lookup(tokenHash: string): { id: number } | null;
}

export interface IdempotencyRepo {
  findGrading(key: string): CardState | null;
  recordGrading(key: string, questionId: number, state: CardState): void;
  findSync(clientId: string): SyncOutcome | null;
  recordSync(clientId: string, kind: 'card' | 'review', status: string, questionId: number | null): void;
  findQuestion(key: string): number | null;
  recordQuestion(key: string, questionId: number): void;
  /** A whole write step's result, for a step whose ops are not each keyed on
   * their own: a redelivery answers from here instead of re-deriving it from
   * rows its first run already wrote. */
  findStepResult(key: string): unknown | null;
  recordStepResult(key: string, result: unknown): void;
}

export interface PrefsRepo {
  /** The account row, or null before the first upsert. */
  get(): Profile | null;
  /** Creates or refreshes the row and bumps `last_seen_at`; COALESCE keeps set claims. */
  upsert(id: string, claims?: ProfileClaims): Profile;
  /** Bumps `last_seen_at` only; inserts nothing on a miss. */
  touch(): void;
  createAnonymous(id: string, displayName: string): Profile;
  accountRows(): AccountRows;
  getEditorInputMode(): string;
  setEditorInputMode(mode: string): void;
  getNotificationPrefs(): NotificationPrefs;
  setNotificationPrefs(prefs: NotificationPrefs): void;
  getActiveByokProvider(): string | null;
  setActiveByokProvider(provider: string | null): void;
  getDesiredRetention(): number | null;
  setDesiredRetention(retention: number | null): void;
  getIdBase(): number;
  setIdBase(base: number): void;
}

export interface JobStatusRepo {
  register(job: {
    workflowId: string;
    workflowType: string;
    deckId: number | null;
    deckName: string | null;
    urlPath: string;
    initialStatus?: string;
  }): void;
  get(workflowId: string): ActiveWorkflow | null;
  updateStatus(workflowId: string, status: string): void;
  setTerminalAt(workflowId: string, terminalAt?: string | null): void;
  markNotified(workflowId: string, kind: 'action' | 'terminal'): void;
  /** Active plus recently terminal rows, newest first; the bucket sort is the caller's. */
  listForUser(opts?: { recentTerminalWindowSeconds?: number }): ActiveWorkflow[];
  cleanupStaleTerminal(opts?: { windowSeconds?: number }): number;
  listNonTerminal(): ActiveWorkflow[];
  pruneTerminalOlderThan(opts?: { windowSeconds?: number }): number;
}

export interface OfflineRepo {
  snapshotDecks(): SnapshotDeck[];
  snapshotCards(): SnapshotCard[];
  resolveCardClientId(cardClientId: string): number | null;
  findSrsDeckByLabel(label: string): number | null;
  /** The SRS inbox, or whether the name is taken by a non-SRS deck. */
  findSrsInbox(): { id: number } | { taken: boolean };
  /** The inbox for deck-less cards, created on demand, suffixed past a non-SRS `inbox`. */
  resolveSrsInbox(): number;
  /** The SRS deck labelled `deckName`, created past taken slugs; the inbox when the slug space is exhausted. */
  resolveNamedSrsDeck(deckName: string): number;
  /** Throws `SyncItemRejected` for an unknown or non-SRS deck. */
  createCard(clientId: string, deckId: number, prompt: string, answer: string, answerRegex: string | null): number;
  /** 'applied' or 'logged_no_reschedule'; throws `SyncItemRejected` for an unknown question. */
  applyReview(clientId: string, questionId: number, verdict: ReviewResult, userAnswer: string, reviewedAt: Date, notes: string): string;
}

export interface ExportRepo {
  dump(): CellSnapshot;
  /** The named columns of the named tables, plus the profile: the merge's
   * read of an account that is not moving. */
  project(columns: Readonly<Record<string, readonly string[]>>): CellSnapshot;
  /**
   * Writes rows keyed by their primary key; user columns are dropped.
   * Returns rows written per table.
   *
   * `conflict: 'ignore'` keeps the row the cell already holds, which is the
   * merge's rule: two cells mint from disjoint id blocks, so a collision is
   * a bug. `conflict: 'update'` overwrites a row that differs, which is the
   * migration's: the second pass exists to carry what the window changed.
   */
  importRows(snapshot: CellSnapshot, opts: { idempotentBy: 'id'; conflict: 'ignore' | 'update' }): Record<string, number>;
  /** The migrated `profile` row, columns verbatim. `last_seen_at` is the
   * anonymous reaper's only input, so the clock-stamped `prefs.upsert`
   * cannot stand in for this. Keyed by `id`: a replay writes the same row. */
  importProfile(row: Readonly<Record<string, unknown>>): void;
  /** Rows per data table and whether the profile is there: the resume point. */
  counts(): { profile: boolean; tables: Record<string, number> };
  /** Every data row, the profile kept. */
  wipe(): void;
}

/** The account, deck and cards of an instant generation in one transaction. */
export interface InstantRepo {
  createInstantDeck(displayName: string, cards: readonly InstantCard[], mint: { id: string; displayName: string } | null): InstantDeckResult;
}

export interface Transactions {
  sync<T>(fn: () => T): T;
}

/** The column pins the seed profiles write past the repositories. */
export interface TestPins {
  session(sid: string, lastActive: string, createdAt?: string | null): void;
  answerInSession(sid: string, qid: number, answeredAt: string, result: string): void;
  pinnedAt(deckId: number, pinnedAt: string): void;
  notificationSentAt(noteId: number, sentAt: string): void;
  workflowStartedAt(workflowId: string, startedAt: string): void;
  tokenCreatedAt(tokenId: number, createdAt: string): void;
}

export interface Hasher {
  sha256Hex(text: string): Promise<string>;
}

export interface TombstoneRepo {
  get(): Tombstone | null;
  /** Written right after `deleteAll`; creates its own table. */
  write(reason: TombstoneReason, at: string, formerBytes: number): void;
  stampScrubbed(at: string): void;
  databaseSize(): number;
  /** The zero-fill scrub to `former_bytes`, its own RPC; idempotent. */
  scrub(at: string): void;
}

/** Every repository of one user cell, over its storage. */
export interface UserRepos {
  decks: DeckRepo;
  questions: QuestionRepo;
  cards: CardRepo;
  reviews: ReviewRepo;
  sessions: SessionRepo;
  trivia: TriviaRepo;
  notify: NotifyRepo;
  pushSubs: PushSubRepo;
  byok: ByokRepo;
  tokens: TokenRepo;
  idempotency: IdempotencyRepo;
  prefs: PrefsRepo;
  jobs: JobStatusRepo;
  jobProgress: JobProgressRepo;
  offline: OfflineRepo;
  export: ExportRepo;
  instant: InstantRepo;
  tombstone: TombstoneRepo;
  tx: Transactions;
  pins: TestPins;
}

// ---- deck interchange codecs ----------------------------------------------

export interface ZipEntry {
  name: string;
  bytes: Uint8Array;
}

export interface ZipReadOptions {
  /** Inflate only these names. Everything else is skipped in the central
   * directory, so an archive's unread bulk costs nothing. */
  only?: readonly string[];
  /** Ceiling on one inflated entry. */
  maxEntryBytes?: number;
  /** Ceiling on the sum of the inflated entries, which is what the heap
   * actually holds; `maxEntryBytes` alone bounds nothing when an archive
   * carries many entries, or many under one name. */
  maxTotalBytes?: number;
}

/** The archive container. `.prepdeck` is written through it, `.apkg` is read
 * through it, and both are bounded before anything inflates. */
export interface ZipCodec {
  /** Entries in central-directory order, restricted to `only` when given.
   * Throws `NotAZip`, or `ZipEntryTooLarge` when an entry or the selection's
   * total declares more than its ceiling - the declarations are read first,
   * so a bomb never expands. */
  read(blob: Uint8Array, opts?: ZipReadOptions): ZipEntry[];
  /** Stored, no compression, fixed 1980 stamp: two exports of an unchanged
   * deck are byte-identical. */
  write(entries: readonly ZipEntry[]): Uint8Array;
}

export class NotAZip extends Error {}
export class ZipEntryTooLarge extends Error {}
/** The blob is not an Anki package: unreadable zip, or no collection inside. */
export class NotAnApkg extends Error {}

export interface ApkgNote {
  id: number;
  flds: string;
}

export interface ApkgReader {
  /** Every `notes` row of the packaged collection, by id. Only the collection
   * entry is inflated; the media an `.apkg` carries is never read. Throws
   * `NotAnApkg`. */
  notes(blob: Uint8Array, opts?: { maxEntryBytes?: number; maxTotalBytes?: number }): Promise<ApkgNote[]>;
}

/** The `col` row. The four JSON columns arrive as values and are serialized
 * by the adapter, so the app layer never names a wire encoding. */
export interface ApkgCollection {
  id: number;
  crt: number;
  mod: number;
  scm: number;
  ver: number;
  dty: number;
  usn: number;
  ls: number;
  conf: unknown;
  models: unknown;
  decks: unknown;
  dconf: unknown;
  tags: unknown;
}

export interface ApkgNoteRow {
  id: number;
  guid: string;
  mid: number;
  mod: number;
  usn: number;
  tags: string;
  flds: string;
  sfld: string;
  csum: number;
  flags: number;
  data: string;
}

export interface ApkgCard {
  id: number;
  nid: number;
  did: number;
  ord: number;
  mod: number;
  usn: number;
  type: number;
  queue: number;
  due: number;
  ivl: number;
  factor: number;
  reps: number;
  lapses: number;
  left: number;
  odue: number;
  odid: number;
  flags: number;
  data: string;
}

export interface ApkgWriter {
  build(col: ApkgCollection, notes: readonly ApkgNoteRow[], cards: readonly ApkgCard[]): Promise<Uint8Array>;
}

// ---- crypto, push, agents, jobs -------------------------------------------

export interface Signer {
  sign(payload: string): Promise<Uint8Array>;
}

export class DecryptionError extends Error {}

export interface Cipher {
  encrypt(plaintext: string): Promise<string>;
  /** Throws `DecryptionError`. */
  decrypt(ciphertext: string): Promise<string>;
}

export type PushOutcome = 'ok' | 'gone' | 'fail';

export interface WebPush {
  send(subscription: PushSubscription, payload: string): Promise<PushOutcome>;
}

export class AgentUnavailable extends Error {}

/** Shared, deploy-funded capacity is saturated. Not the caller's fault and
 * not their budget: the remedy is to retry, or bring a key. */
export class AgentBusy extends AgentUnavailable {}

/** A shared-capacity call that timed out in transport. Unlike the other busy
 * shapes the request WAS issued, so a caller metering upstream spend counts
 * it as spent. */
export class AgentTimeout extends AgentBusy {}

/** The credential's own quota or credit pool is gone; only its owner can
 * refill it. */
export class AgentBudgetExhausted extends AgentUnavailable {}

export interface AgentRequest {
  system: string;
  user: string;
  maxTokens?: number;
  /** The caller's deadline; the adapter aborts on the earlier of it and its
   * own transport timeout. */
  signal?: AbortSignal;
}

/** Throws `AgentUnavailable` when no provider can answer. */
export interface AgentPort {
  complete(request: AgentRequest): Promise<string>;
}

/** Which credential funds one owner's call, resolved from their rows alone.
 * The ciphertext travels, never the key: the caller decrypts in its own
 * isolate with the same master key. */
export type AgentConfig =
  | { tier: 'byok'; provider: string; ciphertext: string }
  | { tier: 'free' }
  | { tier: 'none'; reason: string };

/** What `funding_tier_for_user` answers, minus the retired subscription. */
export type FundingTier = 'byok' | 'free' | 'none';

export class RunnerUnavailable extends Error {}

/** The tier cap is not optional either: a start that omits it hands the free
 * tier's shared credential an uncapped call. Zero means uncapped. */
export interface PlanGenerateInput {
  deckId: number;
  deckName: string;
  prompt: string;
  maxCards: number;
}

/** The snapshot fields are not optional: an LLM step has no repositories, so
 * a start that omits them shows the model an empty library. */
export interface TransformJobInput {
  scope: TransformScope;
  targetId: number;
  prompt: string;
  /** Null for reorganize, which spans decks. */
  deckName: string | null;
  /** The owning deck's standing description, '' when it has none. */
  deckContextPrompt: string;
  /** The card (card scope) or the deck's unsuspended cards (deck scope). */
  cards: TransformCard[];
  /** Every deck with its cards; reorganize only, empty otherwise. */
  decks: TransformDeck[];
}

export interface TriviaGenerateInput {
  deckId: number;
  deckName: string;
  topic: string;
  /** The tier's per-call ceiling; zero takes `DEFAULT_BATCH_SIZE`. */
  batchSize: number;
  /** The deck's current prompts, read here because the generate step holds no
   * repositories: an empty list tells the model the deck is empty. */
  existing: string[];
}

export interface GradeAnswerInput {
  questionId: number;
  deckName: string;
  userAnswer: string;
  idk: boolean;
  sessionId?: string;
  /** The question as the prompt shows it, read before the job starts. */
  card: GradeCard;
}

/** One entry per job kind; `JobKind` is its key set, so a kind added without
 * an input shape is a compile error rather than an untyped record. */
export interface JobInputs {
  PlanGenerate: PlanGenerateInput;
  Transform: TransformJobInput;
  TriviaGenerate: TriviaGenerateInput;
  GradeAnswer: GradeAnswerInput;
}

export type JobKind = keyof JobInputs;
export type JobInput = JobInputs[JobKind];

/** What a partial renders: the status literal and the progress keys under it. */
export interface JobStatus {
  status: string;
  progress: Record<string, unknown>;
}

/** A status with the transition that produced it: what a JobCell answers, so
 * the caller can apply the write itself instead of waiting for the alarm. */
export interface JobTransition extends JobStatus {
  transition: number;
}

export interface WorkflowRunner {
  /** Throws `RunnerUnavailable` on a deploy with jobs off. */
  start<K extends JobKind>(kind: K, input: JobInputs[K]): Promise<{ workflowId: string }>;
  /** The post-signal status, so a route renders the transient fragment
   * without a second read. Null when no job owns the id. */
  signal(id: string, event: { name: string; payload?: unknown }): Promise<JobStatus | null>;
  /** Reads `job_progress` in the calling cell; null renders `gone`. */
  status(id: string): Promise<JobStatus | null>;
  terminate(id: string, reason: string): Promise<void>;
}

/** One transition, as the JobCell hands it to its owner. Idempotent by
 * `(jobId, transition)`: a re-delivery whose number is not above the stored
 * one is dropped before any side effect. */
export interface JobStatusWrite {
  jobId: string;
  transition: number;
  status: string;
  progress: Record<string, unknown>;
  urlPath: string;
  /** The badge's `workflow_type`. */
  kind: string;
  deckId: number | null;
  deckName: string | null;
}

/** A write step, run in the owner's cell so it reaches the repositories and
 * the idempotency ledgers directly. `stepKey` is the key it writes under. */
export interface JobStepRequest {
  jobId: string;
  jobKind: string;
  name: string;
  stepKey: string;
  idx: number;
  item: number;
  input: Record<string, unknown>;
  outputs: Record<string, unknown>;
  itemInput: unknown;
  at: string;
}

/** One job cell's step ledger. Everything a decision rests on is read
 * through `read`, and everything an activation concluded lands through one
 * `commit`, so a crash leaves either the whole transition or none of it. */
export interface JobLedger {
  /** Null before `create`: an id nobody started. */
  read(): LedgerRows | null;
  /** False when the job already exists; a repeated start is not a new job. */
  create(job: {
    id: string;
    kind: string;
    owner: string;
    input: Record<string, unknown>;
    createdAt: string;
    urlPath: string;
    workflowType: string;
    deckId: number | null;
    deckName: string | null;
  }): boolean;
  /** The job's route and badge columns, carried so the status write can
   * register the row without a second source. */
  route(): { urlPath: string; workflowType: string; deckId: number | null; deckName: string | null };
  appendEvent(event: { name: string; payload: unknown; at: string }): void;
  commit(commit: LedgerCommit): void;
  markDelivered(transition: number, at: string): void;
  deferDelivery(transition: number, attempt: number, nextAt: string): void;
}

/** The `job_progress` read model: what `WorkflowRunner.status` answers from,
 * written by the same transaction that moves `active_workflows`. */
export interface JobProgressRepo {
  get(workflowId: string): JobStatus | null;
  /** The stored transition number, or null when no row exists. */
  transitionOf(workflowId: string): number | null;
  upsert(row: { workflowId: string; transition: number; status: string; progress: Record<string, unknown> }): void;
  /** Rows whose workflow is gone from `active_workflows`; the per-user prune. */
  pruneOrphans(): number;
  /** One row, dropped while its badge row stands: stands in for an execution
   * deleted out from under a status read. */
  remove(workflowId: string): boolean;
}

// ---- the global cells, as the user cell and the router see them ------------

export interface Directory {
  /** Idempotent: an existing id keeps its idx. */
  register(id: string, isAnonymous: boolean, at: string, opts?: { idx?: number }): Promise<{ idx: number }>;
  lookup(id: string): Promise<DirectoryUser | null>;
  beginMerge(anonId: string, targetId: string, at: string): Promise<{ auditId: number; marker: MergeMarker }>;
  completeMerge(auditId: number, counts: Record<string, number>, at: string): Promise<void>;
  failMerge(auditId: number, error: string, at: string): Promise<void>;
  /** Counts this attempt against the marker and returns the count, so a merge
   * that fails the same way every time gives up instead of retrying forever. */
  noteMergeAttempt(anonId: string): Promise<number>;
  marker(anonId: string): Promise<MergeMarker | null>;
  clearMarker(anonId: string): Promise<void>;
  previousIds(targetId: string): Promise<string[]>;
  audit(auditId: number): Promise<MergeAudit | null>;
  tombstone(id: string, reason: TombstoneReason, at: string): Promise<void>;
  tombstoneOf(id: string): Promise<{ reason: TombstoneReason; at: string } | null>;
  remove(id: string): Promise<void>;
  listAnonymous(after: string | null, limit: number): Promise<DirectoryUser[]>;
}

export interface Reservation {
  id: number;
}

export type ReserveResult = { reservation: Reservation } | { refusal: Refusal };

export interface Limiter {
  reserve(req: { ip: string; topicChars: number; userId: string | null; userIsAnonymous: boolean | null; at: string }): Promise<ReserveResult>;
  resolve(id: number, outcome: 'ok' | 'failed_spent' | 'failed_free', cards: number | null, userId: string | null): Promise<void>;
  /** The merge's reassign rule over the ledger this cell owns, not a user
   * cell's; returns the rows moved. */
  reassign(fromId: string, toId: string): Promise<number>;
}

/** The seed's reset of the global instant ledger. Not part of
 * serving: a durable limiter cell would otherwise carry one run's spend
 * into the next against a clock that never advances. */
export interface LedgerReset {
  wipe(): void;
}

export interface Precheck {
  exists: boolean;
  isAnonymous: boolean;
  tombstoned: TombstoneReason | null;
}

/** The two profile columns an anonymous account may have set. */
export interface CarriedPreferences {
  desired_retention: number | null;
  editor_input_mode: string | null;
}

/** The RPC surface of one user cell as another cell or the router sees it. */
export interface UserCellRpc {
  precheck(): Promise<Precheck>;
  /** `idx` seeds the id block on the first contact; later calls keep the row's. */
  upsert(id: string, claims: ProfileClaims, at: string, idx?: number): Promise<Profile>;
  dump(): Promise<CellSnapshot>;
  /** What a merge into this cell reads of it: the policy's target columns. */
  mergeView(): Promise<CellSnapshot>;
  importRows(snapshot: CellSnapshot): Promise<Record<string, number>>;
  /** One migration chunk, in one transaction: it lands whole or not at all,
   * so a run killed mid-user leaves no half-row. Returns rows inserted. */
  importChunk(write: MigrationWrite): Promise<Record<string, number>>;
  /** What this cell already holds, for the importer's resume point. */
  migrationStatus(): Promise<MigrationStatus>;
  /** COPY-IF-NULL of the merge's carried profile columns; counts what moved. */
  carryPreferences(carried: CarriedPreferences): Promise<Record<string, number>>;
  destroy(reason: TombstoneReason, at: string): Promise<void>;
  scrub(at: string): Promise<void>;
  createInstantDeck(input: {
    displayName: string;
    cards: readonly InstantCard[];
    mint: { id: string; displayName: string; idx: number } | null;
    at: string;
  }): Promise<InstantDeckResult>;
  lastSeenAt(): Promise<string | null>;
  /** Which credential funds this owner's next LLM step. Read once per step,
   * never cached: a revoked key must stop the step after it. */
  agentConfig(): Promise<AgentConfig>;
  /** One transaction: `active_workflows`, `job_progress` and the
   * notification rules. Idempotent by `(jobId, transition)`. */
  jobStatus(write: JobStatusWrite): Promise<void>;
  /** Runs a write step here, where the repositories and the idempotency
   * ledgers are, so a step row and a data row cannot disagree. */
  applyJobStep(step: JobStepRequest): Promise<StepOutput>;
  /** This owner's jobs that have not reached a terminal status, read before a
   * deletion: they have to be stopped before the cell is emptied. */
  activeJobIds(): Promise<string[]>;
}

export interface UserCells {
  cell(id: string): UserCellRpc;
}

/** The RPC surface of one job's cell. Every method is idempotent: a retry
 * after an unreachable node repeats the decision, never the work. */
export interface JobCellRpc {
  start(job: {
    id: string;
    kind: string;
    owner: string;
    input: Record<string, unknown>;
    urlPath: string;
    workflowType: string;
    deckId: number | null;
    deckName: string | null;
    at: string;
  }): Promise<JobTransition>;
  signal(event: { name: string; payload?: unknown; at: string }): Promise<JobTransition | null>;
  terminate(reason: string, at: string): Promise<void>;
  /** The current status without driving anything; null before `start`. */
  peek(): Promise<JobTransition | null>;
}

export interface JobCells {
  cell(id: string): JobCellRpc;
}

/** A port's methods with the RPC promise removed: the cell-local adapter shape. */
export type Sync<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => Promise<infer R> ? (...args: A) => R : T[K];
};
