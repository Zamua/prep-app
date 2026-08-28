// The per-user alarm: the pure plan, and the cell that arms and runs it.
// Every assertion here is about state on disk, because that is the only thing
// an evicted cell has when its alarm fires.
import { describe, expect, it } from 'vitest';
import type { NotificationPrefs, PushSubscription } from '../app/entities.js';
import type { JobInput, JobKind, PushOutcome, UserRepos, WorkflowRunner } from '../app/ports.js';
import { RunnerUnavailable } from '../app/ports.js';
import { readWakeInputs } from '../app/notify/wake.js';
import {
  DEFAULT_TZ,
  effectiveIntervalMinutes,
  inQuietHours,
  nextLocalHour,
  partsIn,
  planWake,
  type TriviaDeckState,
  type WakeInputs,
} from '../domain/notify/wake.js';
import { isoUtc } from '../domain/time.js';
import { composeWith } from '../runtime/compose.js';
import { UserCell } from '../runtime/cells/UserCell.js';
import type { Env } from '../runtime/env.js';
import { AlarmBus } from './fakes/alarms.js';
import { fakeCellState, type FakeCellStorage } from './fakes/sqlStorage.js';
import { MutableClock, USER } from './repos/setup.js';

/** 2026-06-15 12:00Z is 08:00 America/New_York: inside the default quiet
 * window's last hour is 07:00, so the fixture sits just outside it. */
const NOW = new Date('2026-06-15T12:00:00Z');
const M = 60_000;
const H = 3_600_000;
const D = 86_400_000;

// ---- the pure plan ---------------------------------------------------------

const PREFS = (over: Partial<NotificationPrefs> = {}): WakeInputs['prefs'] => ({
  mode: 'off',
  digest_hour: 9,
  tz: DEFAULT_TZ,
  threshold: 3,
  quiet_hours_enabled: false,
  quiet_start_hour: 22,
  quiet_end_hour: 8,
  last_digest_date: null,
  last_when_ready_at: null,
  ...over,
});

const DECK = (over: Partial<TriviaDeckState> = {}): TriviaDeckState => ({
  id: 1,
  notificationsEnabled: true,
  mutedUntil: null,
  intervalMinutes: 30,
  ignoredStreak: 0,
  lastNotifiedAt: null,
  sessionSize: 3,
  unanswered: 5,
  queued: 5,
  topic: 'World history',
  lastRefillAt: null,
  activeSince: null,
  ...over,
});

const INPUTS = (over: Partial<WakeInputs> = {}): WakeInputs => ({
  prefs: PREFS(),
  canGenerate: true,
  hasPushDevice: true,
  dueTotal: 0,
  nextDueAt: null,
  decks: [],
  earliestTerminalAt: null,
  ...over,
});

describe('local time', () => {
  it('reads the wall clock of a zone, and falls back on a zone that is not one', () => {
    expect(partsIn('America/New_York', NOW)).toMatchObject({ year: 2026, month: 6, day: 15, hour: 8 });
    expect(partsIn('Europe/Berlin', NOW).hour).toBe(14);
    expect(partsIn('Mars/Olympus', NOW)).toEqual(partsIn(DEFAULT_TZ, NOW));
  });

  it('finds the next local hour across a spring-forward, without repeating one', () => {
    // 2026-03-08: America/New_York jumps 02:00 to 03:00.
    const before = new Date('2026-03-08T05:30:00Z'); // 00:30 local
    const at = nextLocalHour('America/New_York', 4, before);
    expect(partsIn('America/New_York', at).hour).toBe(4);
    expect(at.getTime()).toBeGreaterThan(before.getTime());
    // Asked again from the instant it returned, it moves on rather than
    // handing back the same hour.
    expect(nextLocalHour('America/New_York', 4, at).getTime()).toBe(at.getTime() + D);
  });
});

describe('quiet hours', () => {
  it('wraps midnight and treats an equal pair as no window at all', () => {
    expect(inQuietHours(23, 22, 8)).toBe(true);
    expect(inQuietHours(7, 22, 8)).toBe(true);
    expect(inQuietHours(8, 22, 8)).toBe(false);
    expect(inQuietHours(13, 9, 17)).toBe(true);
    expect(inQuietHours(5, 5, 5)).toBe(false);
  });
});

describe('the trivia backoff', () => {
  it('doubles per ignored fire and caps at five doublings', () => {
    expect([0, 1, 2, 5, 9].map((s) => effectiveIntervalMinutes(30, s))).toEqual([30, 60, 120, 960, 960]);
  });
});

describe('planWake', () => {
  it('asks for nothing when nothing is configured', () => {
    expect(planWake(INPUTS(), NOW)).toEqual({ tasks: [], wakeAt: null });
  });

  it('never names an instant that has already passed', () => {
    const cases: WakeInputs[] = [
      INPUTS({ prefs: PREFS({ mode: 'digest' }), dueTotal: 4 }),
      INPUTS({ prefs: PREFS({ mode: 'when-ready', threshold: 9 }), dueTotal: 1, nextDueAt: isoUtc(new Date(NOW.getTime() + M)) }),
      INPUTS({ decks: [DECK({ lastNotifiedAt: isoUtc(NOW) })] }),
      INPUTS({ earliestTerminalAt: isoUtc(new Date(NOW.getTime() - H)) }),
    ];
    for (const input of cases) {
      const plan = planWake(input, NOW);
      if (plan.tasks.length) expect(plan.wakeAt).toBe(isoUtc(NOW));
      else expect(new Date(plan.wakeAt!).getTime()).toBeGreaterThan(NOW.getTime());
    }
  });

  it('fires the digest inside its local hour, once per local date', () => {
    // 12:00Z is 08:00 in New York.
    const at8 = INPUTS({ prefs: PREFS({ mode: 'digest', digest_hour: 8 }), dueTotal: 4 });
    expect(planWake(at8, NOW).tasks).toEqual([{ kind: 'digest', localDate: '2026-06-15' }]);

    const sent = planWake({ ...at8, prefs: PREFS({ mode: 'digest', digest_hour: 8, last_digest_date: '2026-06-15' }) }, NOW);
    expect(sent.tasks).toEqual([]);
    expect(partsIn(DEFAULT_TZ, new Date(sent.wakeAt!))).toMatchObject({ day: 16, hour: 8 });
  });

  it('waits for the hour when nothing is due, and for a card that lands inside it', () => {
    const base = PREFS({ mode: 'digest', digest_hour: 8 });
    const empty = planWake(INPUTS({ prefs: base, dueTotal: 0 }), NOW);
    expect(empty.tasks).toEqual([]);
    expect(partsIn(DEFAULT_TZ, new Date(empty.wakeAt!))).toMatchObject({ day: 16, hour: 8 });

    const soon = isoUtc(new Date(NOW.getTime() + 20 * M));
    expect(planWake(INPUTS({ prefs: base, dueTotal: 0, nextDueAt: soon }), NOW).wakeAt).toBe(soon);
  });

  it('holds when-ready behind the threshold, the debounce and the quiet window', () => {
    const ready = PREFS({ mode: 'when-ready', threshold: 3 });
    expect(planWake(INPUTS({ prefs: ready, dueTotal: 3 }), NOW).tasks).toEqual([{ kind: 'when-ready' }]);
    expect(planWake(INPUTS({ prefs: ready, dueTotal: 2 }), NOW)).toEqual({ tasks: [], wakeAt: null });

    const debounced = PREFS({ mode: 'when-ready', threshold: 3, last_when_ready_at: isoUtc(new Date(NOW.getTime() - H)) });
    const held = planWake(INPUTS({ prefs: debounced, dueTotal: 5 }), NOW);
    expect(held.tasks).toEqual([]);
    expect(held.wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 3 * H)));

    // 08:00 local with a window that ends at 09:00.
    const quiet = PREFS({ mode: 'when-ready', threshold: 1, quiet_hours_enabled: true, quiet_start_hour: 22, quiet_end_hour: 9 });
    const silenced = planWake(INPUTS({ prefs: quiet, dueTotal: 5 }), NOW);
    expect(silenced.tasks).toEqual([]);
    expect(partsIn(DEFAULT_TZ, new Date(silenced.wakeAt!)).hour).toBe(9);
  });

  it('gives the SRS pair no wake at all without a device to push to', () => {
    const input = INPUTS({ prefs: PREFS({ mode: 'when-ready', threshold: 1 }), dueTotal: 5, hasPushDevice: false });
    expect(planWake(input, NOW)).toEqual({ tasks: [], wakeAt: null });
  });

  it('refills a short deck and notifies it in the same pass', () => {
    const plan = planWake(INPUTS({ decks: [DECK({ unanswered: 1, sessionSize: 3 })] }), NOW);
    expect(plan.tasks).toEqual([
      { kind: 'trivia-refill', deckId: 1 },
      { kind: 'trivia-notify', deckId: 1 },
    ]);
  });

  it('does not dispatch a second refill inside the deck\'s own interval', () => {
    const recent = DECK({ unanswered: 1, lastRefillAt: isoUtc(new Date(NOW.getTime() - M)) });
    expect(planWake(INPUTS({ decks: [recent] }), NOW).tasks).toEqual([{ kind: 'trivia-notify', deckId: 1 }]);
  });

  it('plans no refill at all where nothing would fund or run one', () => {
    const starved = INPUTS({ canGenerate: false, decks: [DECK({ unanswered: 0, queued: 0 })] });
    // Not merely skipped for this pass: a deck that asks and is never answered
    // re-plans on every wake, which is a dispatch attempt every tick forever.
    expect(planWake(starved, NOW)).toEqual({ tasks: [], wakeAt: null });
  });

  it('refills during quiet hours but leaves the fire and the stamp alone', () => {
    const quiet = PREFS({ quiet_hours_enabled: true, quiet_start_hour: 22, quiet_end_hour: 9 });
    const plan = planWake(INPUTS({ prefs: quiet, decks: [DECK({ unanswered: 0 })] }), NOW);
    expect(plan.tasks).toEqual([{ kind: 'trivia-refill', deckId: 1 }]);
    // Once that dispatch is on the books the deck waits out the window, with
    // no notification and no stamp behind it.
    const after = planWake(INPUTS({ prefs: quiet, decks: [DECK({ unanswered: 0, lastRefillAt: isoUtc(NOW) })] }), NOW);
    expect(after.tasks).toEqual([]);
    expect(after.wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 30 * M)));
    // A deck the refill filled waits for the window instead: only the
    // notification is what quiet hours hold back.
    const filled = planWake(INPUTS({ prefs: quiet, decks: [DECK({ lastRefillAt: isoUtc(NOW) })] }), NOW);
    expect(filled.tasks).toEqual([]);
    expect(partsIn(DEFAULT_TZ, new Date(filled.wakeAt!)).hour).toBe(9);
  });

  it('waits out the backed-off interval, the mute and a mid-session user', () => {
    const backedOff = DECK({ lastNotifiedAt: isoUtc(new Date(NOW.getTime() - 30 * M)), intervalMinutes: 30, ignoredStreak: 2 });
    // 30 minutes at two doublings is two hours, so 90 minutes are left.
    expect(planWake(INPUTS({ decks: [backedOff] }), NOW).wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 90 * M)));

    const muted = DECK({ mutedUntil: isoUtc(new Date(NOW.getTime() + 3 * H)) });
    expect(planWake(INPUTS({ decks: [muted] }), NOW).wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 3 * H)));

    const midSession = DECK({ activeSince: isoUtc(new Date(NOW.getTime() - 2 * M)) });
    const plan = planWake(INPUTS({ decks: [midSession] }), NOW);
    expect(plan.tasks).toEqual([]);
    expect(plan.wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 3 * M)));

    expect(planWake(INPUTS({ decks: [DECK({ notificationsEnabled: false })] }), NOW)).toEqual({ tasks: [], wakeAt: null });
  });

  it('leaves an empty deck without a wake: the refill it dispatched brings the cell back', () => {
    const plan = planWake(INPUTS({ decks: [DECK({ unanswered: 0, queued: 0 })] }), NOW);
    expect(plan.tasks).toEqual([{ kind: 'trivia-refill', deckId: 1 }]);

    // The dispatch on the books is what the deck waits on; the interval past
    // it is the soonest a second one could help.
    const waiting = planWake(INPUTS({ decks: [DECK({ unanswered: 0, queued: 0, lastRefillAt: isoUtc(NOW) })] }), NOW);
    expect(waiting).toEqual({ tasks: [], wakeAt: isoUtc(new Date(NOW.getTime() + 30 * M)) });
  });

  it('prunes a day after the oldest terminal row, not before', () => {
    const fresh = isoUtc(new Date(NOW.getTime() - H));
    expect(planWake(INPUTS({ earliestTerminalAt: fresh }), NOW).wakeAt).toBe(isoUtc(new Date(NOW.getTime() + 23 * H)));
    expect(planWake(INPUTS({ earliestTerminalAt: isoUtc(new Date(NOW.getTime() - 25 * H)) }), NOW).tasks).toEqual([{ kind: 'prune' }]);
  });
});

// ---- the cell ---------------------------------------------------------------

interface Started {
  kind: JobKind;
  input: JobInput;
}

interface Harness {
  clock: MutableClock;
  bus: AlarmBus;
  storage: FakeCellStorage;
  cell(): UserCell;
  repos(): UserRepos;
  pushes: { payload: string; source: string }[];
  started: Started[];
  logSources(): string[];
  prefs(): NotificationPrefs;
  setPrefs(over: Partial<NotificationPrefs>): void;
  /** Re-arms the way a write does, without inventing a request. */
  arm(): Promise<void>;
  /** Drops the cell and rebuilds it over the same storage: an eviction. */
  evict(): Promise<UserCell>;
  runnerFails: { at: null | Error };
  /** Whether the stub runner registers the badge row a real start writes. */
  tracesRefills: { on: boolean };
}

function harness(opts: { at?: Date } = {}): Harness {
  const clock = new MutableClock(opts.at ?? NOW);
  const bus = new AlarmBus(clock);
  const pushes: { payload: string; source: string }[] = [];
  const started: Started[] = [];
  const runnerFails: { at: null | Error } = { at: null };
  const tracesRefills = { on: true };
  const state = fakeCellState();
  const storage = state.fake;

  const runner: WorkflowRunner = {
    async start(kind, input) {
      if (runnerFails.at) throw runnerFails.at;
      started.push({ kind, input });
      const workflowId = `${kind}-stub-${started.length}`;
      // The badge row the real runner registers with its first transition:
      // it is what tells the next plan a refill is already in flight.
      const record = input as unknown as Record<string, unknown>;
      if (!tracesRefills.on) return { workflowId };
      repos().jobs.register({
        workflowId,
        workflowType: 'trivia_gen',
        deckId: (record['deckId'] as number | undefined) ?? null,
        deckName: (record['deckName'] as string | undefined) ?? null,
        urlPath: `/trivia/gen/${workflowId}`,
        initialStatus: 'generating',
      });
      return { workflowId };
    },
    async signal() {
      return null;
    },
    async status() {
      return null;
    },
    async terminate() {},
  };

  const env: Env = {
    USER: unreachableNamespace(),
    DIRECTORY: unreachableNamespace(),
    INSTANT_LIMITER: unreachableNamespace(),
    JOB: unreachableNamespace(),
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) } as unknown as Fetcher,
    PREP_ENV: 'dev',
    PREP_TEST_MODE: '1',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_INTERNAL_TOKEN: 'test-internal-token',
    // A funded tier: without one the plan declines to dispatch a refill at
    // all, which is right and is its own test.
    PREP_FREE_INFERENCE_BASE_URL: 'http://127.0.0.1:9/v1',
    PREP_FREE_INFERENCE_API_KEY: 'test-free-tier-key',
    PREP_FREE_INFERENCE_MODEL: 'test-model',
  };

  const c = composeWith(env, {
    clock,
    runner: () => runner,
    webPush: {
      async send(_sub: PushSubscription, payload: string): Promise<PushOutcome> {
        pushes.push({ payload, source: '' });
        return 'ok';
      },
    },
  });

  let cell = new UserCell(state, env);
  bus.register(storage, () => cell.alarm());

  const repos = () => c.userRepos(storage, clock);
  return {
    clock,
    bus,
    storage,
    pushes,
    started,
    runnerFails,
    tracesRefills,
    cell: () => cell,
    repos,
    logSources: () => repos().notify.listRecent(50).map((e) => e.source),
    prefs: () => repos().prefs.getNotificationPrefs(),
    setPrefs: (over) => repos().prefs.setNotificationPrefs({ ...repos().prefs.getNotificationPrefs(), ...over }),
    arm: async () => {
      // The write path a cell takes on the way out of any non-read request.
      await cell.fetch(new Request('https://cell.internal/nothing', { method: 'POST', headers: { 'x-prep-subject': USER } }));
    },
    evict: async () => {
      cell = new UserCell(fakeCellState(storage), env);
      bus.register(storage, () => cell.alarm());
      // The constructor's blockConcurrencyWhile is not awaited by the class.
      await new Promise((r) => setTimeout(r, 0));
      return cell;
    },
  };
}

function unreachableNamespace(): DurableObjectNamespace {
  return {
    idFromName: (name: string) => ({ name, toString: () => name }),
    get: () => {
      throw new Error('namespace must not be reached');
    },
  } as unknown as DurableObjectNamespace;
}

/** A profile with a device, the state every SRS notification needs. */
function seedUser(h: Harness, opts: { push?: boolean } = {}): void {
  const repos = h.repos();
  repos.prefs.upsert(USER, { email: USER, displayName: 'Seed' });
  if (opts.push !== false) repos.pushSubs.upsert('https://push.example/1', 'p256dh', 'auth');
}

/** An SRS deck with `count` cards already due. */
function seedDue(h: Harness, count: number): number {
  const repos = h.repos();
  const deck = repos.decks.create('geography', { contextPrompt: 'Capitals.' });
  for (let i = 0; i < count; i++) {
    const qid = repos.questions.add(deck, { type: 'short', prompt: `q${i}`, answer: 'a' });
    repos.cards.restoreCardState(qid, { next_due: isoUtc(new Date(h.clock.now().getTime() - H)) });
  }
  return deck;
}

function seedTrivia(h: Harness, opts: { cards?: number; interval?: number } = {}): number {
  const repos = h.repos();
  const deck = repos.decks.createTrivia('world-history', { topic: 'World history.', intervalMinutes: opts.interval ?? 30 });
  for (let i = 0; i < (opts.cards ?? 5); i++) {
    const qid = repos.questions.add(deck, { type: 'short', prompt: `trivia ${i}`, answer: 'a' });
    repos.trivia.appendCard(qid, deck);
  }
  return deck;
}

describe('the user cell alarm', () => {
  it('arms nothing for a cell with nothing to schedule', async () => {
    const h = harness();
    seedUser(h);
    await h.arm();
    expect(h.storage.alarmAt).toBeNull();
  });

  it('re-derives the same wake on an eviction, holding nothing in memory', async () => {
    const h = harness();
    seedUser(h);
    seedDue(h, 5);
    h.setPrefs({ mode: 'when-ready', threshold: 1, last_when_ready_at: isoUtc(new Date(NOW.getTime() - H)) });
    await h.arm();
    const armed = h.storage.alarmAt;
    expect(armed).toBe(NOW.getTime() + 3 * H);

    h.storage.alarmAt = null;
    await h.evict();
    expect(h.storage.alarmAt).toBe(armed);
  });

  it('sends the digest once per local date and stamps the date it used', async () => {
    // 13:00Z is 09:00 in New York, the default digest hour.
    const h = harness({ at: new Date('2026-06-15T13:00:00Z') });
    seedUser(h);
    seedDue(h, 4);
    h.setPrefs({ mode: 'digest', digest_hour: 9 });
    await h.arm();
    expect(await h.bus.settle()).toBe(1);

    expect(h.prefs().last_digest_date).toBe('2026-06-15');
    expect(h.logSources()).toEqual(['srs-digest']);
    expect(h.pushes).toHaveLength(1);
    expect(JSON.parse(h.pushes[0]!.payload)['body']).toBe('4 cards due in geography.');

    // A second fire inside the same hour writes nothing more.
    h.clock.advance(10 * M);
    await h.cell().alarm();
    expect(h.logSources()).toEqual(['srs-digest']);
  });

  it('sends when-ready once, then holds the four-hour debounce', async () => {
    const h = harness();
    seedUser(h);
    seedDue(h, 5);
    h.setPrefs({ mode: 'when-ready', threshold: 3 });
    await h.arm();
    expect(await h.bus.settle()).toBe(1);

    expect(h.logSources()).toEqual(['srs-when-ready']);
    const fired = h.clock.now().getTime();
    expect(h.prefs().last_when_ready_at).toBe(isoUtc(new Date(fired)));
    expect(h.storage.alarmAt).toBe(fired + 4 * H);

    await h.bus.settleThrough(fired + 3 * H);
    expect(h.logSources()).toEqual(['srs-when-ready']);
    await h.bus.settleThrough(fired + 4 * H + 1);
    expect(h.logSources()).toEqual(['srs-when-ready', 'srs-when-ready']);
  });

  it('says nothing during quiet hours and fires when the window reopens', async () => {
    // 04:00Z is 00:00 in New York, inside the default 22 to 08 window.
    const h = harness({ at: new Date('2026-06-15T04:00:00Z') });
    seedUser(h);
    seedDue(h, 5);
    h.setPrefs({ mode: 'when-ready', threshold: 1, quiet_hours_enabled: true });
    await h.arm();
    expect(h.logSources()).toEqual([]);
    expect(partsIn(DEFAULT_TZ, new Date(h.storage.alarmAt!)).hour).toBe(8);

    await h.bus.settleThrough(h.storage.alarmAt! + 1);
    expect(h.logSources()).toEqual(['srs-when-ready']);
  });

  it('dispatches a refill job for a short deck rather than calling the model', async () => {
    const h = harness();
    seedUser(h);
    const deck = seedTrivia(h, { cards: 1 });
    await h.arm();
    await h.bus.settle();

    // The free tier's per-call cap and the deck's prompts travel with it: the
    // generate step runs in a cell that can read neither.
    expect(h.started).toEqual([
      { kind: 'TriviaGenerate', input: { deckId: deck, deckName: 'world-history', topic: 'World history.', batchSize: 5, existing: ['trivia 0'] } },
    ]);
    // The deck still had a card, so the notification went out beside the dispatch.
    expect(h.logSources()).toEqual(['trivia']);
    expect(h.repos().decks.listTriviaDecks()[0]!.last_notified_at).toBe(isoUtc(NOW));
  });

  it('keeps serving a deck whose refill cannot start', async () => {
    const h = harness();
    seedUser(h);
    seedTrivia(h, { cards: 1 });
    h.runnerFails.at = new RunnerUnavailable('jobs are off');
    await h.arm();
    await h.bus.settle();

    expect(h.started).toEqual([]);
    expect(h.logSources()).toEqual(['trivia']);
  });

  it('backs a deck off when nobody answers, and resets when somebody does', async () => {
    const h = harness();
    seedUser(h);
    const deck = seedTrivia(h, { cards: 6, interval: 30 });
    const streak = () => h.repos().decks.listTriviaDecks()[0]!.notification_ignored_streak;
    await h.arm();
    await h.bus.settle();
    expect(streak()).toBe(1);
    // One doubling: the next fire is an hour out, not half of one.
    expect(h.storage.alarmAt).toBe(NOW.getTime() + H);

    await h.bus.settleThrough(NOW.getTime() + H + 1);
    expect(streak()).toBe(2);
    // Two doublings on a thirty-minute deck: two hours.
    expect(h.storage.alarmAt).toBe(NOW.getTime() + 3 * H);

    // Answering is itself the reset, and the fire that follows keeps it there
    // rather than counting the push nobody had seen yet as ignored.
    h.clock.advance(M);
    h.repos().trivia.markAnswered(h.repos().trivia.listQueueForDeck(deck)[0]!.question_id, true);
    expect(streak()).toBe(0);
    await h.arm();
    expect(h.storage.alarmAt).toBe(NOW.getTime() + H + 30 * M);
    await h.bus.settleThrough(NOW.getTime() + H + 30 * M + 1);
    expect(streak()).toBe(0);
  });

  it('asks for a refill no more often than the scheduler period when one leaves no trace', async () => {
    const h = harness();
    seedUser(h);
    // 04:00Z is midnight in New York: quiet, so nothing stamps the deck.
    h.clock.set(new Date('2026-06-15T04:00:00Z'));
    seedTrivia(h, { cards: 1 });
    h.setPrefs({ quiet_hours_enabled: true });
    h.tracesRefills.on = false;
    await h.arm();
    await h.bus.settleThrough(h.clock.now().getTime() + 21 * M);
    expect(h.started.length).toBe(5);
  });

  it('prunes terminal workflow rows a day after they landed', async () => {
    const h = harness();
    seedUser(h);
    const repos = h.repos();
    repos.jobs.register({ workflowId: 'plan-a-1', workflowType: 'plan', deckId: null, deckName: 'a', urlPath: '/plan/plan-a-1' });
    repos.jobs.setTerminalAt('plan-a-1', isoUtc(new Date(NOW.getTime() - 2 * D)));
    repos.jobProgress.upsert({ workflowId: 'plan-a-1', transition: 3, status: 'done', progress: {} });
    await h.arm();
    await h.bus.settle();

    expect(h.repos().jobs.get('plan-a-1')).toBeNull();
    expect(h.repos().jobProgress.get('plan-a-1')).toBeNull();
    expect(h.storage.alarmAt).toBeNull();
  });

  it('drops the alarm on a tombstoned cell', async () => {
    const h = harness();
    seedUser(h);
    seedDue(h, 5);
    h.setPrefs({ mode: 'when-ready', threshold: 1 });
    await h.arm();
    expect(h.storage.alarmAt).not.toBeNull();

    await h.cell().destroy('reaped', isoUtc(NOW));
    await h.cell().alarm();
    expect(h.storage.alarmAt).toBeNull();
  });

  it('settles rather than spinning, whatever is configured', async () => {
    const h = harness();
    seedUser(h);
    seedDue(h, 5);
    seedTrivia(h, { cards: 2, interval: 30 });
    h.setPrefs({ mode: 'when-ready', threshold: 1, quiet_hours_enabled: true });
    const repos = h.repos();
    repos.jobs.register({ workflowId: 'x', workflowType: 'plan', deckId: null, deckName: null, urlPath: '/plan/x' });
    repos.jobs.setTerminalAt('x', isoUtc(new Date(NOW.getTime() - 2 * D)));
    await h.arm();
    // A full week of wall time in one pass: the bus throws if the alarms
    // never settle, which is the spin this asserts against.
    await h.bus.settleThrough(NOW.getTime() + 7 * D);
    expect(h.storage.alarmAt === null || h.storage.alarmAt > h.clock.now().getTime()).toBe(true);
  });
});

describe('what the cell reads for its plan', () => {
  it('names the deck it would refill and when its last refill was asked for', async () => {
    const h = harness();
    seedUser(h);
    const deck = seedTrivia(h, { cards: 2 });
    const repos = h.repos();
    expect(readWakeInputs(repos, h.clock, true).decks[0]).toMatchObject({ id: deck, queued: 2, unanswered: 2, topic: 'World history.', lastRefillAt: null });

    repos.jobs.register({ workflowId: 'trivia-world-history-1', workflowType: 'trivia_gen', deckId: deck, deckName: 'world-history', urlPath: '/trivia/gen/1' });
    expect(readWakeInputs(repos, h.clock, true).decks[0]).toMatchObject({ lastRefillAt: isoUtc(NOW) });
    // A job that has since finished still holds the deck off: the guard is
    // that a refill was asked for, not that one is still running.
    repos.jobs.setTerminalAt('trivia-world-history-1');
    expect(readWakeInputs(repos, h.clock, true).decks[0]).toMatchObject({ lastRefillAt: isoUtc(NOW) });
  });
});
