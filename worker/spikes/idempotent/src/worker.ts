import { DurableObject } from 'cloudflare:workers';

interface Env {
  USER: DurableObjectNamespace;
  JOB: DurableObjectNamespace;
}

interface UserApi {
  append(key: string, body: string): Promise<{ inserted: boolean; rows: number }>;
  rows(): Promise<{ rows: number; keys: string[] }>;
}

// The idempotency key is the UNIQUE column: a retry of a landed write is
// a no-op by construction, not by a read-then-write check.
export class UserCell extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(
      'CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, idem TEXT UNIQUE NOT NULL, body TEXT NOT NULL)',
    );
  }

  append(key: string, body: string) {
    const r = this.ctx.storage.sql.exec('INSERT OR IGNORE INTO log (idem, body) VALUES (?, ?)', key, body);
    return { inserted: r.rowsWritten > 0, rows: this.rows().rows };
  }

  rows() {
    const keys = [...this.ctx.storage.sql.exec('SELECT idem FROM log ORDER BY id')].map((r) => String(r.idem));
    return { rows: keys.length, keys };
  }
}

// JobCell records the attempt in its own storage, then RPCs into the
// user's cell. `hangMs` delays the RPC so a kill lands before the user
// write; `hangAfterMs` delays the completion record so a kill lands after
// the user write but before the job knows it landed.
export class JobCell extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(
      'CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, idem TEXT NOT NULL, phase TEXT NOT NULL)',
    );
  }

  async run(user: string, key: string, hangMs: number, hangAfterMs: number) {
    this.ctx.storage.sql.exec("INSERT INTO attempts (idem, phase) VALUES (?, 'started')", key);
    if (hangMs > 0) await new Promise((r) => setTimeout(r, hangMs));
    const stub = this.env.USER.get(this.env.USER.idFromName(user)) as unknown as UserApi;
    const out = await stub.append(key, `job-${key}`);
    if (hangAfterMs > 0) await new Promise((r) => setTimeout(r, hangAfterMs));
    this.ctx.storage.sql.exec("INSERT INTO attempts (idem, phase) VALUES (?, 'landed')", key);
    return out;
  }

  attempts() {
    return [...this.ctx.storage.sql.exec('SELECT idem, phase FROM attempts ORDER BY id')].map(
      (r) => `${r.idem}:${r.phase}`,
    );
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const user = url.searchParams.get('user') ?? 'u1';
    const key = url.searchParams.get('key') ?? 'k1';
    const hangMs = Number(url.searchParams.get('hangMs') ?? '0');
    const hangAfterMs = Number(url.searchParams.get('hangAfterMs') ?? '0');
    if (url.pathname === '/job/run') {
      const job = env.JOB.get(env.JOB.idFromName('job-' + user)) as unknown as {
        run(u: string, k: string, h: number, ha: number): Promise<object>;
      };
      return Response.json(await job.run(user, key, hangMs, hangAfterMs));
    }
    if (url.pathname === '/job/attempts') {
      const job = env.JOB.get(env.JOB.idFromName('job-' + user)) as unknown as { attempts(): Promise<string[]> };
      return Response.json(await job.attempts());
    }
    if (url.pathname === '/user/rows') {
      const u = env.USER.get(env.USER.idFromName(user)) as unknown as UserApi;
      return Response.json(await u.rows());
    }
    return new Response('use /job/run /job/attempts /user/rows', { status: 404 });
  },
};
