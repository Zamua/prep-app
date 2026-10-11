// A lived-in account: SRS decks that arrived a week apart, a month of daily
// sessions run through the same calls the study API makes, a trivia deck, and
// the notifications and job rows that use leaves behind.
//
// `history` adds what only an older account holds: learned cards scheduled
// months out, the reviews behind them, answered trivia, older notifications
// and finished jobs. None of it is due, so two accounts that differ only in
// `history` owe the same study work and differ only in how much they hold.
import type { Clock, UserRepos } from '../../app/ports.js';
import { compose } from '../../runtime/compose.js';
import type { Env } from '../../runtime/env.js';
import type { FakeCellStorage } from '../fakes/sqlStorage.js';
import { mintToken, seed, SEED_USER, type WorkerEnv } from '../api/harness.js';

export const NOW = '2026-03-14T15:00:00Z';
const DAY = 86_400_000;

export interface AccountShape {
  /** Cards per SRS deck, each deck's arriving in one batch. */
  srsDecks: readonly number[];
  triviaCards: number;
  days: number;
  reviewsPerDay: number;
  notifications: number;
  jobs: number;
  /** Multiples of the above held as settled history; see the file comment. */
  history: number;
}

export const REALISTIC: AccountShape = { srsDecks: [120, 90, 60, 30], triviaCards: 24, days: 30, reviewsPerDay: 24, notifications: 40, jobs: 6, history: 0 };

export const DECKS = ['world-capitals', 'world-history', 'distributed-systems', 'databases'];
export const TRIVIA_DECK = 'trivia-mix';

export interface Account {
  env: Env;
  storage: FakeCellStorage;
  token: string;
  /** How to answer each SRS card: its mcq choice, or `self` for a self-graded one. */
  answers: Map<number, { answer: string; type: string }>;
  deckIds: Map<string, number>;
}

class MovingClock implements Clock {
  constructor(public at: number) {}
  now(): Date {
    return new Date(this.at);
  }
}

/** Deterministic, so two builds of one shape hold identical rows. */
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

function cardFor(deck: string, i: number): { type: 'mcq' | 'short'; prompt: string; answer: string; choices?: string[]; answer_regex?: string } {
  const answer = `answer-${deck}-${i}`;
  if (i % 4 === 3) return { type: 'short', prompt: `${deck} recall ${i}: name the thing`, answer, answer_regex: `^${answer}$` };
  if (i % 4 === 2) return { type: 'short', prompt: `${deck} explain ${i}: in a sentence`, answer };
  return { type: 'mcq', prompt: `${deck} question ${i}: pick one`, answer, choices: [answer, `wrong-a-${i}`, `wrong-b-${i}`] };
}

const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', '+00:00');

export async function buildAccount(state: WorkerEnv, shape: AccountShape = REALISTIC): Promise<Account> {
  const { env } = state;
  await seed(env, 'empty', SEED_USER);
  const storage = state.userStorage(SEED_USER);
  const now = Date.parse(NOW);
  const clock = new MovingClock(now - shape.days * DAY - DAY);
  const repos: UserRepos = compose(env).userRepos(storage, clock);
  const rand = lcg(20260314);
  const answers = new Map<number, { answer: string; type: string }>();

  const prefs = repos.prefs.getNotificationPrefs();
  prefs.mode = 'when-ready';
  repos.prefs.setNotificationPrefs(prefs);
  repos.pushSubs.upsert('https://push.example.test/sub/1', 'p256dh-key', 'auth-key');

  const triviaId = repos.decks.createTrivia(TRIVIA_DECK, { topic: 'general knowledge', intervalMinutes: 60 });
  for (let i = 0; i < shape.triviaCards; i++) {
    const qid = repos.questions.add(triviaId, { type: 'short', prompt: `trivia ${i}`, answer: `trivia-answer-${i}` });
    repos.trivia.appendCard(qid, triviaId);
  }

  const deckIds: number[] = [];
  const byName = new Map<string, number>([[TRIVIA_DECK, triviaId]]);
  const createdOn = (d: number) => shape.days - d * Math.floor(shape.days / (shape.srsDecks.length + 1));
  for (let day = shape.days; day >= 1; day--) {
    clock.at = now - day * DAY + 3_600_000 * (day % 5);
    shape.srsDecks.forEach((count, d) => {
      if (createdOn(d) !== day) return;
      const name = DECKS[d]!;
      const id = repos.decks.create(name, { displayName: name.replace(/-/g, ' ') });
      deckIds.push(id);
      byName.set(name, id);
      for (let i = 0; i < count; i++) {
        const card = cardFor(name, i);
        const qid = repos.questions.add(id, card);
        answers.set(qid, { answer: card.answer, type: card.type === 'short' ? 'self' : card.type });
      }
    });
    clock.at += 600_000;
    const deckId = deckIds[day % deckIds.length]!;
    const sid = await repos.sessions.create(deckId, 'iPhone');
    let session = repos.sessions.get(sid)!;
    for (let n = 0; n < shape.reviewsPerDay && session.current_question_id !== null; n++) {
      clock.at += 30_000;
      const qid = session.current_question_id;
      const result = rand() < 0.8 ? 'right' : 'wrong';
      repos.tx.sync(() => {
        const recorded = repos.reviews.record(qid, result, result === 'right' ? (answers.get(qid)?.answer ?? '') : 'nope');
        repos.sessions.recordAnswerSync(sid, qid, session.version, '', { result }, recorded as unknown as Record<string, unknown>);
      });
      session = repos.sessions.get(sid)!;
      repos.sessions.advance(sid, session.version);
      session = repos.sessions.get(sid)!;
    }
    if (day % 3 !== 0) repos.sessions.markCompleted(sid);
  }

  clock.at = now - 2 * DAY;
  for (let i = 0; i < shape.notifications; i++) {
    clock.at += 60_000 * 30;
    repos.notify.append({ title: 'cards ready', body: `${i} cards due`, url: '/', source: 'srs-when-ready' });
  }
  repos.notify.markAllSeen();
  repos.notify.append({ title: 'cards ready', body: 'unseen', url: '/', source: 'srs-when-ready' });

  for (let i = 0; i < shape.jobs * (1 + shape.history); i++) {
    clock.at = now - 60_000 * (i + 1);
    const wid = `job-${i}`;
    repos.jobs.register({ workflowId: wid, workflowType: 'grading', deckId: null, deckName: DECKS[0]!, urlPath: `/grading/${wid}`, initialStatus: 'grading' });
    repos.jobProgress.upsert({ workflowId: wid, transition: 1, status: 'completed', progress: {} });
    repos.jobs.setTerminalAt(wid);
  }

  if (shape.history > 0) settledHistory(repos, clock, shape, byName, triviaId);

  const token = await mintToken(storage, SEED_USER, 'mcp');
  return { env, storage, token, answers, deckIds: byName };
}

/** Learned cards, their reviews, answered trivia and old notifications: rows an
 * older account holds that owe nothing today. */
function settledHistory(repos: UserRepos, clock: MovingClock, shape: AccountShape, byName: Map<string, number>, triviaId: number): void {
  const now = Date.parse(NOW);
  clock.at = now - 400 * DAY;
  shape.srsDecks.forEach((count, d) => {
    const name = DECKS[d]!;
    const deckId = byName.get(name)!;
    for (let i = 0; i < count * shape.history; i++) {
      const qid = repos.questions.add(deckId, { type: 'mcq', prompt: `${name} learned ${i}`, answer: 'a', choices: ['a', 'b'] });
      for (let r = 0; r < 5; r++) repos.reviews.importReview(qid, iso(now - (300 - r * 50) * DAY), r === 1 ? 'wrong' : 'right', 'a');
      repos.cards.restoreCardState(qid, { next_due: iso(now + (60 + (i % 90)) * DAY), step: 5, last_review: iso(now - 50 * DAY), stability: 120, difficulty: 4 });
    }
  });
  for (let i = 0; i < shape.triviaCards * shape.history; i++) {
    const qid = repos.questions.add(triviaId, { type: 'short', prompt: `trivia answered ${i}`, answer: `a-${i}` });
    repos.trivia.importEntry(qid, 1000 + i, { lastAnsweredAt: iso(now - (i + 1) * DAY), lastAnsweredCorrectly: i % 3 === 0 ? 0 : 1 });
  }
  for (let i = 0; i < shape.notifications * shape.history; i++) {
    const id = repos.notify.append({ title: 'cards ready', body: `old ${i}`, url: '/', source: 'srs-when-ready' });
    repos.pins.notificationSentAt(id, iso(now - (10 + i) * DAY).replace('+00:00', 'Z'));
  }
  repos.notify.markAllSeen();
  repos.notify.append({ title: 'cards ready', body: 'unseen', url: '/', source: 'srs-when-ready' });
}
