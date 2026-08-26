// The ports the app layer speaks through. Adapters live in runtime/adapters
// and meet these interfaces at the composition root only. Repositories
// mirror the Python repos method for method, the user parameter dropped:
// a cell holds one user.
import type { ScheduledReview } from '../domain/fsrs/index.js';
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

/** One recorded Python response (docs/PHASE-1.md A7). A page either names
 * the template and the context the route passed, or carries a body. `sets`
 * are the flags the request leaves behind; `state` is the flag the recording
 * depended on, or null. */
export interface FixturePage {
  method: string;
  path: string;
  status: number;
  headers: { 'content-type': string; location?: string };
  template?: string;
  context?: Record<string, unknown>;
  body?: string;
  sets: string[];
  state: string | null;
}

/** Phase 1 stand-in for the routes lanes C and D port: the pages a profile's
 * routes rendered, replayed by state. */
export interface FixturePages {
  seed(profile: string): Record<string, unknown> | null;
  resolve(profile: string, method: string, path: string, flags: readonly string[]): FixturePage | null;
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
  /** Throws `QuestionNotFound`. SRS state survives the edit. */
  update(qid: number, q: NewQuestion): void;
  setAnswerRegex(qid: number, regex: string | null): boolean;
  get(qid: number): Question | null;
  moveToDeck(questionIds: readonly number[], destDeckId: number): number;
  listInDeck(deckId: number): DeckCard[];
  promptsInDeck(deckId: number): string[];
  setSuspended(qid: number, suspended: boolean): void;
  delete(qid: number): boolean;
}

/** The card-state reads and writes of Python's ReviewRepo. */
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
}

export interface PrefsRepo {
  /** The profile as Python's user dict, or null before the first upsert. */
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
  /** Inserts rows the cell lacks by primary key; user columns are dropped. Returns rows inserted per table. */
  importRows(snapshot: CellSnapshot, opts: { idempotentBy: 'id' }): Record<string, number>;
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

/** The column pins the parity seed profiles write past the repositories. */
export interface ParityPins {
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
  offline: OfflineRepo;
  export: ExportRepo;
  instant: InstantRepo;
  tombstone: TombstoneRepo;
  tx: Transactions;
  pins: ParityPins;
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

export interface AgentRequest {
  system: string;
  user: string;
  maxTokens?: number;
}

/** Throws `AgentUnavailable` when no provider can answer. */
export interface AgentPort {
  complete(request: AgentRequest): Promise<string>;
}

export class RunnerUnavailable extends Error {}

export interface WorkflowRunner {
  /** Throws `RunnerUnavailable`. */
  start(workflowType: string, input: Record<string, unknown>): Promise<{ workflowId: string }>;
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

/** The parity seed's reset of the global instant ledger. Not part of
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
}

export interface UserCells {
  cell(id: string): UserCellRpc;
}

/** A port's methods with the RPC promise removed: the cell-local adapter shape. */
export type Sync<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => Promise<infer R> ? (...args: A) => R : T[K];
};
