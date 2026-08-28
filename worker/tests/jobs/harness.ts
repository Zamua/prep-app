// Real cells over the SqlStorage fake, with the scheduler a test drives.
// The JobCell and the UserCell are the shipped classes; only storage, the
// clock, the step handlers and the graphs are the test's.
import { StepRegistry } from '../../app/jobs/registry.js';
import type { PushSubscription } from '../../app/entities.js';
import type { AgentPort, Clock, JobStatusWrite, JobTransition, PushOutcome, UserCellRpc, UserCells, UserRepos, WorkflowRunner } from '../../app/ports.js';
import type { StepGraph } from '../../domain/jobs/graph.js';
import { AlarmLedgerRunner } from '../../runtime/adapters/alarmLedgerRunner.js';
import { SeededRandom } from '../../runtime/adapters/random.js';
import { composeWith } from '../../runtime/compose.js';
import { JobCell } from '../../runtime/cells/JobCell.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import type { Env } from '../../runtime/env.js';
import { AlarmBus } from '../fakes/alarms.js';
import { fakeCellState, type FakeCellStorage } from '../fakes/sqlStorage.js';
import { MutableClock, TEST_NOW, USER } from '../repos/setup.js';

export interface SentPush {
  endpoint: string;
  payload: string;
}

export interface JobHarness {
  env: Env;
  clock: MutableClock;
  bus: AlarmBus;
  registry: StepRegistry;
  pushes: SentPush[];
  runner(owner?: string): WorkflowRunner;
  userStorage(id: string): FakeCellStorage;
  jobStorage(id: string): FakeCellStorage;
  jobCell(id: string): JobCell;
  /** The job's whole ledger, for the assertions a status write cannot make. */
  ledger(id: string): { job: Record<string, unknown>; steps: Record<string, unknown>[]; outbox: Record<string, unknown>[]; events: Record<string, unknown>[] };
  peek(id: string): Promise<JobTransition | null>;
  repos(owner?: string): UserRepos;
  /** Every status write the JobCell handed its owner, in the order it made
   * them: what a monotonic-transition assertion needs. */
  statusWrites: JobStatusWrite[];
  /** Stands between the JobCell and its owner. Throwing here is how a test
   * stages the post-restart window, in which the owner is briefly out of
   * reach and every call to it is a refusal. */
  interfere(hook: ((call: { method: string; args: unknown[] }) => void) | null): void;
  /** Drops the cell object and rebuilds it over the same storage: a node
   * restart, which is the only crash an in-process test can stage. */
  restart(id: string): Promise<void>;
  /** Fires this job's alarm once if one is due, moving the clock to it.
   * Returns false when nothing was armed. */
  tick(id: string): Promise<boolean>;
  /** Fires every due alarm until nothing is due. */
  settle(): Promise<number>;
  /** Moves the clock forward through every armed alarm inside the window. */
  settleThrough(ms: number): Promise<number>;
}

export function jobHarness(opts: { graphs: Readonly<Record<string, StepGraph>>; agent?: AgentPort; at?: Date; wallClock?: Clock } = { graphs: {} }): JobHarness {
  const clock = new MutableClock(opts.at ?? TEST_NOW);
  const bus = new AlarmBus(clock);
  const registry = new StepRegistry();
  const pushes: SentPush[] = [];

  const users = new Map<string, { cell: UserCell; storage: FakeCellStorage }>();
  const jobs = new Map<string, { cell: JobCell; storage: FakeCellStorage }>();

  const userEntry = (id: string) => {
    let e = users.get(id);
    if (!e) {
      const state = fakeCellState();
      e = { cell: new UserCell(state, env), storage: state.fake };
      users.set(id, e);
    }
    return e;
  };
  const jobEntry = (id: string) => {
    let e = jobs.get(id);
    if (!e) {
      const state = fakeCellState();
      const cell = new JobCell(state, env);
      e = { cell, storage: state.fake };
      jobs.set(id, e);
      bus.register(state.fake, () => cell.alarm());
    }
    return e;
  };

  const namespace = <T>(entry: (id: string) => { cell: T }): DurableObjectNamespace =>
    ({
      idFromName: (name: string) => ({ name, toString: () => name }),
      get: (id: { name: string }) => entry(id.name).cell,
    }) as unknown as DurableObjectNamespace;

  const env: Env = {
    USER: namespace(userEntry),
    DIRECTORY: namespace(userEntry),
    INSTANT_LIMITER: namespace(userEntry),
    JOB: namespace(jobEntry),
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) } as unknown as Fetcher,
    PREP_ENV: 'dev',
    PREP_TEST_MODE: '1',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_INTERNAL_TOKEN: 'test-internal-token',
  };

  const statusWrites: JobStatusWrite[] = [];
  let hook: ((call: { method: string; args: unknown[] }) => void) | null = null;
  /** The owner's cell with no inline retry and a record of what it was told. */
  const direct: UserCells = {
    cell: (id: string) => {
      const target = userEntry(id).cell as unknown as Record<string, (...a: unknown[]) => unknown>;
      return new Proxy({} as UserCellRpc, {
        get: (_t, prop) => {
          const name = String(prop);
          return (...args: unknown[]) => {
            if (name === 'jobStatus') statusWrites.push(structuredClone(args[0]) as JobStatusWrite);
            hook?.({ method: name, args });
            return target[name]!.apply(target, args);
          };
        },
      });
    },
  };

  const webPush = {
    async send(sub: PushSubscription, payload: string): Promise<PushOutcome> {
      pushes.push({ endpoint: sub.endpoint, payload });
      return 'ok';
    },
  };

  const c = composeWith(env, {
    clock,
    // The bus fires against this clock, so it is what wall time is here
    // unless a test is about the two coming apart.
    wallClock: opts.wallClock ?? clock,
    stepRegistry: registry,
    jobGraphs: opts.graphs,
    userCellsDirect: direct,
    webPush,
    randoms: { instant: new SeededRandom(1), merge: new SeededRandom(2), tokens: new SeededRandom(3) },
    ...(opts.agent ? { agent: opts.agent } : {}),
  });

  const rows = (storage: FakeCellStorage, table: string) => storage.rows(table);

  return {
    env,
    clock,
    bus,
    registry,
    pushes,
    runner: (owner = USER) =>
      new AlarmLedgerRunner({
        jobs: c.jobCells,
        notify: { repos: c.userRepos(userEntry(owner).storage, clock), webPush, vapidPublicKey: '' },
        owner,
        random: c.randoms.tokens,
        clock,
      }),
    userStorage: (id) => userEntry(id).storage,
    jobStorage: (id) => jobEntry(id).storage,
    jobCell: (id) => jobEntry(id).cell,
    ledger: (id) => {
      const s = jobEntry(id).storage;
      return { job: rows(s, 'job')[0] ?? {}, steps: rows(s, 'steps'), outbox: rows(s, 'outbox'), events: rows(s, 'events') };
    },
    peek: (id) => jobEntry(id).cell.peek(),
    statusWrites,
    interfere: (fn) => void (hook = fn),
    restart: async (id) => {
      const entry = jobEntry(id);
      const cell = new JobCell(fakeCellState(entry.storage), env);
      jobs.set(id, { cell, storage: entry.storage });
      bus.register(entry.storage, () => cell.alarm());
      // The constructor's blockConcurrencyWhile is not awaited by the class;
      // give it the turn the runtime would.
      await new Promise((r) => setTimeout(r, 0));
    },
    tick: async (id) => {
      const entry = jobEntry(id);
      const at = entry.storage.alarmAt;
      if (at === null) return false;
      if (at > clock.now().getTime()) clock.set(new Date(at));
      entry.storage.alarmAt = null;
      await entry.cell.alarm();
      return true;
    },
    repos: (owner = USER) => c.userRepos(userEntry(owner).storage, clock),
    settle: () => bus.settle(),
    settleThrough: (ms) => bus.settleThrough(clock.now().getTime() + ms),
  };
}

/** A user cell with a profile and a push subscription: what a status write
 * needs before it can register a badge row or fire a notification. */
export function seedOwner(h: JobHarness, owner = USER, opts: { push?: boolean } = {}): void {
  const repos = h.repos(owner);
  repos.prefs.upsert(owner, { email: owner, displayName: 'Seed' });
  if (opts.push !== false) repos.pushSubs.upsert('https://push.example/1', 'p256dh', 'auth');
}
