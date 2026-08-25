// Verification variant: every runtime compile-from-bytes path throws, so a
// successful query proves the module import alone fed sql.js.
import { DurableObject } from 'cloudflare:workers';
import initSqlJs from 'sql.js/dist/sql-wasm-browser.js';
import sqlWasm from '../../../sqljs/src/sql-wasm.wasm';

interface Env { ANKI: DurableObjectNamespace; }

const calls: Record<string, number> = {};
const trapped: string[] = [];
const WA = WebAssembly as unknown as Record<string, unknown>;
for (const name of ['compile', 'instantiate', 'compileStreaming', 'instantiateStreaming', 'validate']) {
  try {
    WA[name] = (..._a: unknown[]) => { calls[name] = (calls[name] ?? 0) + 1; throw new Error(`TRAP: WebAssembly.${name} called at runtime`); };
    trapped.push(name);
  } catch (e) { trapped.push(`${name}:not-writable:${String(e)}`); }
}
const RealModule = WebAssembly.Module;
try {
  WA.Module = new Proxy(RealModule, { construct() { calls.Module = (calls.Module ?? 0) + 1; throw new Error('TRAP: new WebAssembly.Module(bytes) at runtime'); } });
  trapped.push('Module');
} catch (e) { trapped.push(`Module:not-writable:${String(e)}`); }
const RealInstance = WebAssembly.Instance;
WA.Instance = new Proxy(RealInstance, { construct(t, args) { calls.Instance = (calls.Instance ?? 0) + 1; return Reflect.construct(t, args); } });

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
  const importInfo = {
    typeofImport: typeof sqlWasm,
    isModule: sqlWasm instanceof RealModule,
    exportsCount: sqlWasm instanceof RealModule ? RealModule.exports(sqlWasm).length : -1,
    tag: Object.prototype.toString.call(sqlWasm),
  };
  try {
    const SQL = await loadSql();
    const tInit = Date.now() - t0;
    const db = new SQL.Database();
    db.run('CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT)');
    db.run("INSERT INTO notes (flds) VALUES ('frontback'), ('q2a2')");
    const rows = db.exec('SELECT id, flds FROM notes ORDER BY id');
    const bytes = db.export().length;
    db.close();
    return { ok: true, initMs: tInit, totalMs: Date.now() - t0, rows: rows[0]?.values ?? [], exportBytes: bytes, importInfo, trapped, calls };
  } catch (e) {
    return { ok: false, error: String(e), importInfo, trapped, calls };
  }
}

export class AnkiCell extends DurableObject<Env> {
  async query() { return roundTrip(); }
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
