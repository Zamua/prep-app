// A real celld node the test can kill. `scripts/run-node.sh` builds, deploys
// and starts it; the kill is SIGKILL on that process, and the restart is the
// same script with the build and the deploy skipped.
//
// The deploy config is generated rather than committed: the dev one pins the
// clock (`PREP_FAKE_NOW`), and a job whose alarm never comes due proves
// nothing about alarms.
import { spawnSync } from 'node:child_process';
import { createServer, type Server } from 'node:http';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const WORKER = new URL('../..', import.meta.url).pathname.replace(/\/$/, '');
export const STATE = process.env['PREP_CRASH_STATE_DIR'] ?? '/private/tmp/prep-crash-state';
export const PORT = Number(process.env['PREP_CRASH_PORT'] ?? 8795);
export const CONTROL_PORT = Number(process.env['PREP_CRASH_CONTROL_PORT'] ?? 8796);
export const BASE = `http://127.0.0.1:${PORT}`;
export const CONTROL = `http://127.0.0.1:${CONTROL_PORT}/step`;
export const INTERNAL_TOKEN = 'test-internal-token';
export const OWNER = 'crash@example.test';

/** The scratch MinIO's root credential, which run-node.sh refuses to default. */
function minioCredentials(): { AWS_ACCESS_KEY_ID: string; AWS_SECRET_ACCESS_KEY: string } {
  if (process.env['AWS_ACCESS_KEY_ID'] && process.env['AWS_SECRET_ACCESS_KEY']) {
    return { AWS_ACCESS_KEY_ID: process.env['AWS_ACCESS_KEY_ID'], AWS_SECRET_ACCESS_KEY: process.env['AWS_SECRET_ACCESS_KEY'] };
  }
  const inspect = spawnSync('docker', ['inspect', 'celld-scratch-minio', '--format', '{{range .Config.Env}}{{println .}}{{end}}'], { encoding: 'utf8' });
  const env = Object.fromEntries(
    inspect.stdout
      .split('\n')
      .filter((l) => l.includes('='))
      .map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]),
  ) as Record<string, string>;
  const id = env['MINIO_ROOT_USER'];
  const secret = env['MINIO_ROOT_PASSWORD'];
  if (!id || !secret) throw new Error('no scratch MinIO credential: start celld-scratch-minio or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY');
  return { AWS_ACCESS_KEY_ID: id, AWS_SECRET_ACCESS_KEY: secret };
}

/** The dev config minus the pinned clock. It is written into the worker tree
 * because celld refuses a `main` outside the project, and it is generated
 * rather than committed because a deploy file is an operator contract. */
function writeConfig(): string {
  mkdirSync(STATE, { recursive: true });
  const dev = JSON.parse(readFileSync(join(WORKER, 'wrangler.dev.jsonc'), 'utf8').replace(/^\s*\/\/.*$/gm, '')) as { vars: Record<string, string> };
  const vars = { ...dev.vars };
  delete vars['PREP_FAKE_NOW'];
  // Same reason: a scheduler held still for the corpus proves nothing about
  // the alarm these suites exist to drive.
  delete vars['PREP_TEST_NO_PERIODIC'];
  // The deploy name stays the dev one: a second name in the same bucket is a
  // second deployment, and which one a node serves is not the test's to guess.
  const config = { ...dev, vars };
  const path = join(WORKER, 'wrangler.crash.json');
  writeFileSync(path, JSON.stringify(config, null, 2));
  return path;
}

function runNode(args: string[], extra: Record<string, string>): void {
  const r = spawnSync('bash', [join(WORKER, 'scripts', 'run-node.sh'), ...args], {
    cwd: WORKER,
    encoding: 'utf8',
    env: { ...process.env, ...minioCredentials(), PREP_DEV_PORT: String(PORT), PREP_DEV_STATE_DIR: STATE, ...extra },
  });
  if (r.status !== 0) throw new Error(`run-node.sh ${args.join(' ')} failed:\n${r.stdout}\n${r.stderr}`);
}

let configPath: string | null = null;

export function startNode(opts: { build?: boolean } = {}): void {
  configPath ??= writeConfig();
  runNode([], { PREP_RUN_CONFIG: configPath, ...(opts.build === false ? { SKIP_BUILD: '1', SKIP_DEPLOY: '1' } : {}) });
}

export function stopNode(): void {
  if (configPath === null) return;
  runNode(['stop'], { PREP_RUN_CONFIG: configPath });
}

/** SIGKILL, so nothing flushes on the way out: the crash the ledger is for. */
export function killNode(): void {
  const pid = Number(readFileSync(join(STATE, 'node.pid'), 'utf8').trim());
  process.kill(pid, 'SIGKILL');
}

export function restartNode(): void {
  startNode({ build: false });
}

export interface ProbeReply {
  items?: unknown[];
  refuse?: boolean;
  fail?: string;
}

export interface ControlServer {
  /** Every step key the probe's LLM step asked about, in order. */
  seen: string[];
  /** What the next call for a key gets; the default is two items. */
  reply(key: string, reply: ProbeReply): void;
  /** Holds the next call for `key` open until `release` is called. */
  hold(key: string): Promise<void>;
  release(key: string): void;
  close(): Promise<void>;
}

/** Stands in for the LLM: counts calls per step key and can hold one open,
 * which is how a kill lands mid-step. */
export async function startControl(): Promise<ControlServer> {
  const seen: string[] = [];
  const replies = new Map<string, ProbeReply>();
  const holds = new Map<string, { release: () => void; arrived: () => void }>();
  const server: Server = createServer((req, res) => {
    const url = new URL(req.url ?? '/', CONTROL);
    const key = url.searchParams.get('key') ?? '';
    seen.push(key);
    const answer = () => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(replies.get(key) ?? { items: ['one', 'two'] }));
    };
    const held = holds.get(key);
    if (held) {
      held.arrived();
      holds.set(key, { ...held, release: answer });
      return;
    }
    answer();
  });
  await new Promise<void>((resolve) => server.listen(CONTROL_PORT, '127.0.0.1', resolve));
  return {
    seen,
    reply: (key, reply) => void replies.set(key, reply),
    hold: (key) =>
      new Promise<void>((arrived) => {
        holds.set(key, { release: () => {}, arrived });
      }),
    release: (key) => {
      const held = holds.get(key);
      holds.delete(key);
      held?.release();
    },
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

const headers = { 'content-type': 'application/json', 'x-internal-token': INTERNAL_TOKEN };

export interface JobView {
  status: string;
  progress: Record<string, unknown>;
  transition: number;
}

/** A cell is out of reach for a few seconds after a node restart; that is a
 * retryable answer, not a failure, so every read here rides it out. */
async function retrying<T>(fn: () => Promise<T>, seconds = 30): Promise<T> {
  const until = Date.now() + seconds * 1000;
  let last: unknown;
  for (;;) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      if (Date.now() > until) throw last;
      await sleep(250);
    }
  }
}

export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export async function startJob(id: string, kind: string, input: Record<string, unknown>): Promise<JobView> {
  return retrying(async () => {
    const res = await fetch(`${BASE}/_test/job/start`, { method: 'POST', headers, body: JSON.stringify({ id, kind, owner: OWNER, input }) });
    if (!res.ok) throw new Error(`start ${id}: ${res.status} ${await res.text()}`);
    return (await res.json()) as JobView;
  });
}

export async function signalJob(id: string, name: string): Promise<JobView | null> {
  return retrying(async () => {
    const res = await fetch(`${BASE}/_test/job/signal`, { method: 'POST', headers, body: JSON.stringify({ id, name }) });
    if (!res.ok) throw new Error(`signal ${id}: ${res.status} ${await res.text()}`);
    return (await res.json()) as JobView | null;
  });
}

export async function readJob(id: string): Promise<JobView | null> {
  return retrying(async () => {
    const res = await fetch(`${BASE}/_test/job/${id}`, { headers });
    if (!res.ok) throw new Error(`read ${id}: ${res.status} ${await res.text()}`);
    return (await res.json()) as JobView | null;
  });
}

/** Polls until the job's status is one of `wanted`, collecting what it saw. */
export async function until(id: string, wanted: readonly string[], seconds = 60): Promise<{ view: JobView; seenStatuses: string[]; polls: number }> {
  const seenStatuses: string[] = [];
  const deadline = Date.now() + seconds * 1000;
  let polls = 0;
  for (;;) {
    polls++;
    const view = await readJob(id);
    if (view && seenStatuses.at(-1) !== view.status) seenStatuses.push(view.status);
    if (view && wanted.includes(view.status)) return { view, seenStatuses, polls };
    if (Date.now() > deadline) throw new Error(`${id} never reached ${wanted.join('|')}; saw ${seenStatuses.join(' -> ')}`);
    await sleep(100);
  }
}
