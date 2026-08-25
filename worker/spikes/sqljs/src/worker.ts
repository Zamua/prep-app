// sql.js's emscripten glue takes an instantiateWasm hook, which is how the
// bundler-resolved compiled module reaches it without any fetch or
// WebAssembly.compile at runtime.
import { DurableObject } from 'cloudflare:workers';
// The browser glue: the node glue requires node:fs at module init because
// celld exposes a process global, and esbuild cannot bundle that require.
import initSqlJs from 'sql.js/dist/sql-wasm-browser.js';
import sqlWasm from './sql-wasm.wasm';

interface Env {
  ANKI: DurableObjectNamespace;
}

let sqlReady: Promise<any> | null = null;
function loadSql() {
  if (!sqlReady) {
    sqlReady = initSqlJs({
      instantiateWasm(imports: WebAssembly.Imports, cb: (i: WebAssembly.Instance) => void) {
        const inst = new WebAssembly.Instance(sqlWasm, imports);
        cb(inst);
        return inst.exports;
      },
    });
  }
  return sqlReady;
}

async function roundTrip() {
  const t0 = Date.now();
  const SQL = await loadSql();
  const tInit = Date.now() - t0;
  const db = new SQL.Database();
  db.run('CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT)');
  db.run("INSERT INTO notes (flds) VALUES ('frontback'), ('q2a2')");
  const rows = db.exec('SELECT id, flds FROM notes ORDER BY id');
  const bytes = db.export().length;
  db.close();
  return { initMs: tInit, totalMs: Date.now() - t0, rows: rows[0]?.values ?? [], exportBytes: bytes };
}

export class AnkiCell extends DurableObject<Env> {
  async query() {
    return roundTrip();
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    if (url.pathname === '/entry') return Response.json({ where: 'entry', ...(await roundTrip()) });
    const name = url.searchParams.get('cell') ?? 'anki';
    const stub = env.ANKI.get(env.ANKI.idFromName(name)) as unknown as { query(): Promise<object> };
    return Response.json({ where: 'cell', ...(await stub.query()) });
  },
};
