import { DurableObject } from 'cloudflare:workers';

interface Env {
  USER: DurableObjectNamespace;
}

export class UserCell extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, body TEXT)');
  }

  fill(n: number) {
    const body = 'x'.repeat(1024);
    for (let i = 0; i < n; i++) this.ctx.storage.sql.exec('INSERT INTO cards (body) VALUES (?)', body);
    return this.count();
  }

  count() {
    const tables = [...this.ctx.storage.sql.exec("SELECT name FROM sqlite_master WHERE type='table'")].map(
      (r) => r.name,
    );
    const rows = tables.includes('cards')
      ? Number([...this.ctx.storage.sql.exec('SELECT count(*) AS n FROM cards')][0]!.n)
      : -1;
    return { tables, rows, dbBytes: this.ctx.storage.sql.databaseSize };
  }

  async wipe() {
    await this.ctx.storage.deleteAll();
    return this.count();
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const m = url.pathname.match(/^\/([^/]+)\/(fill|count|wipe)$/);
    if (!m) return new Response('use /<cell>/{fill,count,wipe}', { status: 404 });
    const stub = env.USER.get(env.USER.idFromName(m[1]!)) as unknown as {
      fill(n: number): Promise<object>;
      count(): Promise<object>;
      wipe(): Promise<object>;
    };
    const n = Number(url.searchParams.get('n') ?? '500');
    const out = m[2] === 'fill' ? await stub.fill(n) : m[2] === 'wipe' ? await stub.wipe() : await stub.count();
    return Response.json({ cell: m[1], op: m[2], ...out });
  },
};
