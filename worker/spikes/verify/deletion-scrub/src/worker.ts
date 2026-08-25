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
    const sql = this.ctx.storage.sql;
    const tables = [...sql.exec("SELECT name FROM sqlite_master WHERE type='table'")].map((r) => r.name);
    const rows = tables.includes('cards') ? Number([...sql.exec('SELECT count(*) AS n FROM cards')][0]!.n) : -1;
    const pragma = (p: string) => {
      try {
        return [...sql.exec(`PRAGMA ${p}`)][0] ?? null;
      } catch (e) {
        return `ERR ${String(e).slice(0, 80)}`;
      }
    };
    return { tables, rows, dbBytes: sql.databaseSize, page_count: pragma('page_count'), freelist_count: pragma('freelist_count') };
  }

  async wipe(mode: string, n: number) {
    const sql = this.ctx.storage.sql;
    const steps: string[] = [];
    const attempt = (label: string, fn: () => void) => {
      try {
        fn();
        steps.push(`${label}: ok`);
      } catch (e) {
        steps.push(`${label}: ${String(e).slice(0, 120)}`);
      }
    };
    if (mode === 'secure') attempt('PRAGMA secure_delete=ON', () => sql.exec('PRAGMA secure_delete = ON'));
    await this.ctx.storage.deleteAll();
    steps.push('deleteAll: ok');
    if (mode === 'vacuum') attempt('VACUUM', () => sql.exec('VACUUM'));
    if (mode === 'zero') attempt('zero-fill', () => {
      // Overwrite freed pages by writing then dropping a same-size table of zero bytes.
      sql.exec('CREATE TABLE scrub (b BLOB)');
      for (let i = 0; i < n; i++) sql.exec('INSERT INTO scrub (b) VALUES (zeroblob(1024))');
      sql.exec('DROP TABLE scrub');
    });
    if (mode === 'one') attempt('one tiny insert', () => {
      sql.exec('CREATE TABLE IF NOT EXISTS marker (v TEXT)');
      sql.exec("INSERT INTO marker (v) VALUES ('deleted')");
    });
    return { mode, steps, ...this.count() };
  }

  // Separate RPC after deleteAll: reuse the freed pages with zeros, then free them again.
  scrub(n: number) {
    const sql = this.ctx.storage.sql;
    sql.exec('CREATE TABLE IF NOT EXISTS scrub (b BLOB)');
    for (let i = 0; i < n; i++) sql.exec('INSERT INTO scrub (b) VALUES (zeroblob(1024))');
    sql.exec('DROP TABLE scrub');
    return this.count();
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const m = url.pathname.match(/^\/([^/]+)\/(fill|count|wipe|scrub)$/);
    if (!m) return new Response('use /<cell>/{fill,count,wipe?mode=plain|secure|vacuum|zero}', { status: 404 });
    const stub = env.USER.get(env.USER.idFromName(m[1]!)) as unknown as {
      fill(n: number): Promise<object>;
      count(): Promise<object>;
      wipe(mode: string, n: number): Promise<object>;
      scrub(n: number): Promise<object>;
    };
    const n = Number(url.searchParams.get('n') ?? '500');
    const mode = url.searchParams.get('mode') ?? 'plain';
    const out =
      m[2] === 'fill' ? await stub.fill(n) : m[2] === 'wipe' ? await stub.wipe(mode, n) : m[2] === 'scrub' ? await stub.scrub(n) : await stub.count();
    return Response.json({ cell: m[1], op: m[2], ...out });
  },
};
