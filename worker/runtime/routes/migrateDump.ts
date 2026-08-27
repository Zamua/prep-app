// The verifier's read of the cell side: `GET /_migrate/dump`.
//
// Same prefix, same gate and the same seal as the rest of the family, so
// the route that proves a migration landed closes with it. Read-only: it
// is the only `/_migrate/*` route that writes nothing.
//
// Paged by rowid and capped at the same 2,000 rows the import is, so one
// argument bounds both directions and the heaviest account is compared a
// bounded page at a time. `columns` narrows a page to the fields a check
// actually reads: tier 1 wants keys, not 50,000 review bodies.
import type { Composition } from '../compose.js';
import { UnknownTable, type DumpPage } from '../storage.js';
import { migrationGate, MIGRATE_PREFIX } from './migrate.js';

export const DUMP_PATH = `${MIGRATE_PREFIX}/dump`;
export const MAX_DUMP_ROWS = 2000;

/** The paged read every cell class answers; no port declares it because
 * nothing in the app reads it. */
interface MigrationDump {
  dumpPage(table: string, after: number | null, limit: number, columns: readonly string[] | null): Promise<DumpPage>;
}

const DIRECTORY_CELL = 'directory';
const LIMITER_CELL = 'limiter';

/** Null when the path is not ours, so the caller falls through. */
export async function serveMigrateDump(request: Request, url: URL, c: Composition): Promise<Response | null> {
  if (url.pathname !== DUMP_PATH) return null;
  if (request.method !== 'GET') return Response.json({ detail: 'Method Not Allowed' }, { status: 405 });
  const refused = await migrationGate(request, c);
  if (refused) return refused;

  const table = url.searchParams.get('table');
  if (!table) return Response.json({ detail: 'table is required' }, { status: 422 });
  const cell = url.searchParams.get('cell');
  const user = url.searchParams.get('user');
  if (!cell && !user) return Response.json({ detail: 'user or cell is required' }, { status: 422 });

  const after = numeric(url.searchParams.get('after'));
  if (after === undefined) return Response.json({ detail: 'after must be a non-negative integer' }, { status: 400 });
  // Zero is refused rather than served: an empty page reads as an empty
  // table, and a verifier that believed one would call a lost table clean.
  const limit = numeric(url.searchParams.get('limit'));
  if (limit === undefined || limit === 0) return Response.json({ detail: 'limit must be a positive integer' }, { status: 400 });
  const columns = url.searchParams.get('columns');

  const target = cellFor(c, cell, user);
  if (!target) return Response.json({ detail: `unknown cell ${cell}` }, { status: 422 });
  try {
    const page = await target.dumpPage(
      table,
      after,
      Math.min(limit ?? MAX_DUMP_ROWS, MAX_DUMP_ROWS),
      columns ? columns.split(',').filter(Boolean) : null,
    );
    return Response.json({ table, rows: page.rows, next: page.next });
  } catch (e) {
    // A name the cell does not have is the caller's mistake, not a 500:
    // the verifier and the cell schema have drifted, and which name it was
    // is the whole diagnosis.
    if (e instanceof UnknownTable) return Response.json({ detail: e.message }, { status: 422 });
    throw e;
  }
}

function cellFor(c: Composition, cell: string | null, user: string | null): MigrationDump | null {
  if (cell === DIRECTORY_CELL) return c.directory as unknown as MigrationDump;
  if (cell === LIMITER_CELL) return c.limiter as unknown as MigrationDump;
  if (cell) return null;
  return c.userCells.cell(user!) as unknown as MigrationDump;
}

/** `undefined` for a value that is present and unusable, `null` for absent:
 * a typo in a cursor must not silently read from the start. */
function numeric(raw: string | null): number | null | undefined {
  if (raw === null || raw === '') return null;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : undefined;
}
