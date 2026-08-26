// One user cell's repositories over its storage, built by the composition
// root for the cell.
import type { Clock, Random, SessionIds, UserRepos } from '../../../app/ports.js';
import type { Fuzz } from '../../../domain/fsrs/index.js';
import { SqlByokRepo } from './byokRepo.js';
import { SqlCardRepo } from './cardRepo.js';
import { SqlDeckRepo } from './deckRepo.js';
import { SqlExportRepo, SqlTombstoneRepo } from './exportRepo.js';
import { SqlIdempotencyRepo } from './idempotencyRepo.js';
import { SqlInstantRepo } from './instantRepo.js';
import { SqlJobProgressRepo } from './jobProgressRepo.js';
import { SqlJobStatusRepo } from './jobStatusRepo.js';
import { SqlNotifyRepo, SqlPushSubRepo } from './notifyRepo.js';
import { SqlOfflineRepo } from './offlineRepo.js';
import { SqlParityPins } from './parityPins.js';
import { SqlPrefsRepo } from './prefsRepo.js';
import { SqlQuestionRepo } from './questionRepo.js';
import { SqlReviewRepo } from './reviewRepo.js';
import { SqlSessionRepo } from './sessionRepo.js';
import type { CellStorage } from './storage.js';
import { SqlTokenRepo } from './tokenRepo.js';
import { SqlTriviaRepo } from './triviaRepo.js';

export interface RepoDeps {
  clock: Clock;
  sessionIds: SessionIds;
  random: Random;
  fuzz: Fuzz;
}

export function userRepos(storage: CellStorage, deps: RepoDeps): UserRepos {
  const { clock } = deps;
  const decks = new SqlDeckRepo(storage, clock);
  const cards = new SqlCardRepo(storage, clock);
  return {
    decks,
    questions: new SqlQuestionRepo(storage, clock),
    cards,
    reviews: new SqlReviewRepo(storage, clock, cards, deps.fuzz),
    sessions: new SqlSessionRepo(storage, clock, deps.sessionIds),
    trivia: new SqlTriviaRepo(storage, clock, deps.sessionIds, deps.random),
    notify: new SqlNotifyRepo(storage, clock),
    pushSubs: new SqlPushSubRepo(storage, clock),
    byok: new SqlByokRepo(storage, clock),
    tokens: new SqlTokenRepo(storage, clock),
    idempotency: new SqlIdempotencyRepo(storage, clock),
    prefs: new SqlPrefsRepo(storage, clock),
    jobs: new SqlJobStatusRepo(storage, clock),
    jobProgress: new SqlJobProgressRepo(storage, clock),
    offline: new SqlOfflineRepo(storage, clock, decks, cards, deps.fuzz),
    export: new SqlExportRepo(storage),
    instant: new SqlInstantRepo(storage, clock, deps.random),
    tombstone: new SqlTombstoneRepo(storage),
    tx: { sync: <T>(fn: () => T) => storage.transactionSync(fn) },
    pins: new SqlParityPins(storage),
  };
}

export { migrate, seedSequences, resetSequences, USER_MIGRATIONS, DIRECTORY_MIGRATIONS, JOB_MIGRATIONS, LIMITER_MIGRATIONS } from './migrate.js';
export { SqlDirectoryRepo } from './directoryRepo.js';
export { SqlJobLedger } from './jobLedgerRepo.js';
export { SqlLimiterRepo } from './limiterRepo.js';
export type { CellStorage } from './storage.js';
