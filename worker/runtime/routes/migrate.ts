// The import door: one chunk in, the resume point out, and the seal that
// closes it.
//
// Gated on `X-Internal-Token` exactly as `/_test/seed` is, but not on test
// mode: this has to run where the data goes, which is production. The seal
// is what keeps that safe: once set, every route here answers 410.
import { importUserChunk } from '../../app/migrate/import.js';
import { ChunkRejected, isChunkRefusal, parseChunk, CHUNK_TOO_LARGE, GLOBAL_TABLES, MAX_CHUNK_BYTES, MIGRATION_SEALED } from '../../domain/migrate.js';
import { readCapped } from '../cells/routes/adapt.js';
import type { Composition } from '../compose.js';

export const MIGRATE_PREFIX = '/_migrate';
const INTERNAL_TOKEN_HEADER = 'x-internal-token';

/** The migration-only surface of a global cell. No port declares any of it
 * because nothing in the app reads it: the seal is the entry worker's gate,
 * and the two copies are a straight row move with no policy in them. */
interface GlobalMigration {
  importMigrationRows(table: string, rows: readonly Record<string, unknown>[]): Promise<number>;
  migrationCounts(tables: readonly string[]): Promise<Record<string, number>>;
}

interface MigrationRun {
  snapshot: string;
  openedAt: string;
}

interface MigrationSeal extends GlobalMigration {
  sealMigration(): Promise<void>;
  migrationSealed(): Promise<boolean>;
  beginMigrationRun(snapshot: string): Promise<MigrationRun>;
  migrationRun(): Promise<MigrationRun | null>;
}

/**
 * The refusal every `/_migrate/*` route owes before it does anything, or
 * null to carry on. Exported so a route that lands in another module still
 * answers 503, 401 and 410 the same way.
 */
export async function migrationGate(request: Request, c: Composition): Promise<Response | null> {
  if (!c.internalToken) return Response.json({ detail: 'PREP_INTERNAL_TOKEN not configured' }, { status: 503 });
  if (request.headers.get(INTERNAL_TOKEN_HEADER) !== c.internalToken) return Response.json({ detail: 'invalid X-Internal-Token' }, { status: 401 });
  // After the token, so an unauthenticated caller learns nothing about the
  // fleet's state. A second seal answers 410 too, which is the answer a
  // retried seal wants anyway.
  if (await (c.directory as unknown as MigrationSeal).migrationSealed()) return Response.json({ detail: MIGRATION_SEALED }, { status: 410 });
  return null;
}

/** Null when the path is not one of ours, so the caller falls through: the
 * prefix is shared, and the gate above is what a sibling route reuses. */
export async function serveMigrate(request: Request, url: URL, c: Composition): Promise<Response | null> {
  const rest = url.pathname.startsWith(MIGRATE_PREFIX) ? url.pathname.slice(MIGRATE_PREFIX.length) : null;
  const mine = rest !== null && ((request.method === 'POST' && (rest === '/import' || rest === '/seal')) || (request.method === 'GET' && rest === '/status'));
  if (!mine) return null;
  const refused = await migrationGate(request, c);
  if (refused) return refused;

  if (rest === '/import') return serveImport(request, c);
  if (rest === '/status') return serveStatus(url, c);
  await (c.directory as unknown as MigrationSeal).sealMigration();
  return Response.json({ sealed: true });
}

async function serveImport(request: Request, c: Composition): Promise<Response> {
  // Before any parsing: `Content-Length` decides first and a chunked body is
  // counted as it arrives, so nothing past the cap is ever held.
  const raw = await readCapped(request, MAX_CHUNK_BYTES);
  if (raw === null) return Response.json({ detail: CHUNK_TOO_LARGE }, { status: 413 });
  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return Response.json({ detail: 'bad json' }, { status: 400 });
  }
  const chunk = parseChunk(body, c.dataTables);
  if (isChunkRefusal(chunk)) return Response.json({ detail: chunk.detail }, { status: chunk.status });
  try {
    if (chunk.kind === 'run') {
      const run = await (c.directory as unknown as MigrationSeal).beginMigrationRun(chunk.snapshot);
      return Response.json({ run });
    }
    if (chunk.kind === 'global') {
      const inserted = await globalCell(c, chunk.cell).importMigrationRows(chunk.table, chunk.rows);
      return Response.json({ inserted: inserted ? { [chunk.table]: inserted } : {}, dropped: 0 });
    }
    return Response.json(await importUserChunk({ directory: c.directory, cells: c.userCells }, chunk));
  } catch (e) {
    // A parent-before-child ordering mistake arrives here as the foreign key
    // failure, and the operator needs to read which one rather than a page.
    if (e instanceof ChunkRejected) return Response.json({ detail: e.message }, { status: 422 });
    throw e;
  }
}

/** `?user=` for a cell, `?cell=` for one of the two globals. */
async function serveStatus(url: URL, c: Composition): Promise<Response> {
  const cell = url.searchParams.get('cell');
  if (cell !== null) {
    if (!(cell in GLOBAL_TABLES)) return Response.json({ detail: `unknown cell ${JSON.stringify(cell)}` }, { status: 422 });
    // `users` rides along for the directory: it is what the per-user register
    // writes, so a run resuming the globals still reads its own progress.
    const tables = cell === 'directory' ? ['users', ...GLOBAL_TABLES[cell]!] : [...GLOBAL_TABLES[cell]!];
    const counts = await globalCell(c, cell).migrationCounts(tables);
    // Which snapshot this fleet was built from rides with the directory's
    // counts: the verifier reads it before it compares anything, so a run
    // against the wrong snapshot aborts instead of reading clean.
    if (cell !== 'directory') return Response.json({ tables: counts });
    return Response.json({ tables: counts, run: await (c.directory as unknown as MigrationSeal).migrationRun() });
  }
  const user = url.searchParams.get('user');
  if (!user) return Response.json({ detail: 'user or cell is required' }, { status: 422 });
  return Response.json(await c.userCells.cell(user).migrationStatus());
}

const globalCell = (c: Composition, name: string): GlobalMigration =>
  (name === 'directory' ? c.directory : c.limiter) as unknown as GlobalMigration;
