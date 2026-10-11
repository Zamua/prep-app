// workerd, the runtime the cells deploy to, replaying a recording into a
// SQLite-backed Durable Object so the rows each action read are workerd's own
// `rowsRead`, not an estimate. A checkout without a workerd binary has no
// measurement; `locateWorkerd` says so and the caller skips.
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Snapshot, Statement } from './recorder.js';

/** `PREP_WORKERD`, else the `workerd` package when one is installed. */
export function locateWorkerd(): string | null {
  if (process.env['PREP_WORKERD']) return process.env['PREP_WORKERD'];
  try {
    return (createRequire(import.meta.url)('workerd') as { default: string }).default;
  } catch {
    return null;
  }
}

const REPLAY_WORKER = `
import { DurableObject } from 'cloudflare:workers';
const decode = (v) => (v && typeof v === 'object' && 'b64' in v ? Uint8Array.from(atob(v.b64), (c) => c.charCodeAt(0)).buffer : v);
export class Replay extends DurableObject {
  run(statements) {
    return statements.map((s) => {
      try {
        const cursor = this.ctx.storage.sql.exec(s.sql, ...(s.bindings || []).map(decode));
        cursor.toArray();
        return [cursor.rowsRead, cursor.rowsWritten, null];
      } catch (e) {
        return [0, 0, String((e && e.message) || e)];
      }
    });
  }
}
export default {
  async fetch(request, env) {
    const cell = new URL(request.url).searchParams.get('cell');
    return Response.json(await env.REPLAY.get(env.REPLAY.idFromName(cell)).run(await request.json()));
  },
};`;

const config = (port: number, dir: string) => `using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [ (name = "main", worker = .replay), (name = "disk", disk = (path = "${join(dir, 'do')}", writable = true)) ],
  sockets = [ (name = "http", address = "127.0.0.1:${port}", http = (), service = "main") ],
);
const replay :Workerd.Worker = (
  modules = [ (name = "replay", esModule = embed "replay.js") ],
  compatibilityDate = "2025-09-01",
  durableObjectNamespaces = [ (className = "Replay", uniqueKey = "prep-rows-replay", enableSql = true) ],
  durableObjectStorage = (localDisk = "disk"),
  bindings = [ (name = "REPLAY", durableObjectNamespace = "Replay") ],
);
`;

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as { port: number }).port;
      server.close(() => resolve(port));
    });
  });
}

export interface Tally {
  read: number;
  written: number;
  requests: number;
}

export class Workerd {
  private cells = 0;

  private constructor(
    private readonly child: ChildProcess,
    private readonly port: number,
    private readonly dir: string,
  ) {}

  static async start(bin: string): Promise<Workerd> {
    const dir = mkdtempSync(join(tmpdir(), 'prep-rows-'));
    mkdirSync(join(dir, 'do'));
    const port = await freePort();
    writeFileSync(join(dir, 'replay.js'), REPLAY_WORKER);
    writeFileSync(join(dir, 'config.capnp'), config(port, dir));
    const child = spawn(bin, ['serve', join(dir, 'config.capnp'), '--experimental'], { cwd: dir, stdio: 'ignore' });
    const w = new Workerd(child, port, dir);
    for (let i = 0; i < 100; i++) {
      try {
        await w.run('probe', [{ label: '', sql: 'SELECT 1', bindings: [] }]);
        return w;
      } catch {
        await new Promise((r) => setTimeout(r, 50));
      }
    }
    w.stop();
    throw new Error('workerd did not come up');
  }

  private async run(cell: string, statements: Statement[]): Promise<[number, number, string | null][]> {
    const out: [number, number, string | null][] = [];
    for (let i = 0; i < statements.length; i += 500) {
      const res = await fetch(`http://127.0.0.1:${this.port}/?cell=${cell}`, { method: 'POST', body: JSON.stringify(statements.slice(i, i + 500)) });
      out.push(...((await res.json()) as [number, number, string | null][]));
    }
    return out;
  }

  /** Loads the snapshot into a fresh cell, replays the statements in order and
   * sums rowsRead and rowsWritten per label. Throws if any statement failed:
   * a replay that diverged from the recording would measure something else. */
  async replay(snap: Snapshot, statements: Statement[], counts: Map<string, number>): Promise<Map<string, Tally>> {
    const cell = `cell-${++this.cells}`;
    const load: Statement[] = [];
    for (const t of snap.tables) load.push({ label: '', sql: t.sql, bindings: [] });
    for (const t of snap.tables) {
      const insert = `INSERT INTO "${t.name}" (${t.columns.map((c) => `"${c}"`).join(', ')}) VALUES (${t.columns.map(() => '?').join(', ')})`;
      for (const row of t.rows) load.push({ label: '', sql: insert, bindings: row });
    }
    for (const s of snap.sequences) load.push({ label: '', sql: 'INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', bindings: [s.name, s.seq] });
    for (const sql of snap.indexes) load.push({ label: '', sql, bindings: [] });
    const results = await this.run(cell, [...load, ...statements]);
    const failed = results.map((r, i) => (r[2] ? `${r[2]}: ${[...load, ...statements][i]!.sql.slice(0, 120)}` : null)).filter((e) => e !== null);
    if (failed.length) throw new Error(`replay diverged:\n${failed.slice(0, 5).join('\n')}`);
    const tally = new Map<string, Tally>();
    results.slice(load.length).forEach(([read, written], i) => {
      const label = statements[i]!.label;
      if (!label) return;
      const t = tally.get(label) ?? { read: 0, written: 0, requests: counts.get(label) ?? 0 };
      t.read += read;
      t.written += written;
      tally.set(label, t);
    });
    return tally;
  }

  stop(): void {
    this.child.kill();
    rmSync(this.dir, { recursive: true, force: true });
  }
}
