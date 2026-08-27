// The import's rules: what one chunk may weigh, the dispositions that are
// not a straight copy, and the shape a chunk has to arrive in.
//
// A cell holds one chunk at a time - the body string, the parsed array, the
// insert - so these caps are what keeps the whole import inside a 128 MB
// isolate. They belong to the endpoint rather than to the tool that calls
// it: a hand-rolled request must not be able to OOM a cell either.

export const MAX_CHUNK_BYTES = 4 * 1024 * 1024;
export const MAX_CHUNK_ROWS = 2000;

export const CHUNK_TOO_LARGE = 'chunk over 4 MiB';
export const CHUNK_TOO_MANY_ROWS = `chunk over ${MAX_CHUNK_ROWS} rows`;
export const MIGRATION_SEALED = 'migration sealed';

/**
 * Decision 7.4. The subscription credential spent the operator's own token
 * and has no counterpart in the fleet, where every user brings a key. The
 * export stays a faithful copy of the snapshot; the drop is here, so the
 * policy has one home.
 */
export const DROPPED_BYOK_PROVIDERS: readonly string[] = ['claude-subscription'];

/**
 * Tables no chunk may carry. Every `active_workflows` row names a Temporal
 * execution that stops existing with the Go worker, and a non-terminal one
 * would leave the badge polling a job cell with no ledger; `job_progress`
 * is that badge's read model. Refused rather than ignored, so an exporter
 * that started emitting them fails loudly.
 */
export const RESET_TABLES: readonly string[] = ['active_workflows', 'job_progress'];

/**
 * The global tables a chunk may carry, by the cell that owns them.
 * `DirectoryCell.users` is deliberately absent: those rows are written by
 * the per-user register, which is also what hands out the id block, so a
 * second writer could hand the same user two.
 */
export const GLOBAL_TABLES: Readonly<Record<string, readonly string[]>> = {
  directory: ['account_merges'],
  limiter: ['instant_generations'],
};

/** Python's primary key for the row the cell keys by `id`. */
const PY_PROFILE_KEY = 'tailscale_login';

/** One user, one table, the rows of it that fit under the caps. */
export interface UserChunk {
  kind: 'user';
  user: string;
  /** The exporter's rank, which seeds this cell's id block. Never 0: block 0
   * is the parity seed's. */
  idx: number;
  /** Null on the chunk that carries only the profile, which is how a user
   * with no rows at all still gets one. */
  table: string | null;
  rows: readonly Record<string, unknown>[];
  profile: Record<string, unknown> | null;
}

/** One global cell's table: a straight copy, ids preserved. */
export interface GlobalChunk {
  kind: 'global';
  cell: string;
  table: string;
  rows: readonly Record<string, unknown>[];
}

/**
 * The run's own header, sent once before any user: the sha256 of the
 * snapshot being replayed. It is what ties a fleet to a snapshot, so the
 * verifier cannot read a fleet as clean against a file the fleet was never
 * built from, and it is what holds the retention sweep off for the length of
 * the cutover.
 */
export interface RunChunk {
  kind: 'run';
  snapshot: string;
}

export type MigrationChunk = UserChunk | GlobalChunk | RunChunk;

/** A snapshot digest, as `sha256_file` writes it. */
const SHA256 = /^[0-9a-f]{64}$/;

export interface ChunkRefusal {
  status: number;
  detail: string;
}

/**
 * A chunk the cell refuses on its own rows: a constraint they violate, or an
 * account that no longer exists. The same chunk fails the same way, so this
 * must not spend the RPC layer's backoff the way an unreachable cell does.
 */
export class ChunkRejected extends Error {
  override readonly name = 'ChunkRejected';
}

export const isChunkRefusal = (value: MigrationChunk | ChunkRefusal): value is ChunkRefusal => 'status' in value;

const refuse = (status: number, detail: string): ChunkRefusal => ({ status, detail });

/** JSON carries no bytes, and this schema has no BLOB column; anything else
 * is a caller inventing a shape the insert would coerce silently. */
function badValue(row: Record<string, unknown>): string | null {
  for (const [key, value] of Object.entries(row)) {
    if (value === null) continue;
    const t = typeof value;
    if (t !== 'string' && t !== 'number' && t !== 'boolean') return key;
  }
  return null;
}

/**
 * The body as a chunk, or the refusal to answer with. The row cap is checked
 * here because it is only knowable after the parse; the byte cap is the
 * caller's, enforced against `Content-Length` before any of this runs.
 */
export function parseChunk(body: unknown, knownTables: readonly string[]): MigrationChunk | ChunkRefusal {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return refuse(400, 'bad json');
  const b = body as Record<string, unknown>;
  // The whole-snapshot shape. One chunk carries one table so the cell never
  // holds a second table's rows while inserting the first.
  if ('tables' in b) return refuse(422, 'one table per chunk');
  if ('snapshot' in b) {
    const snapshot = b['snapshot'];
    if (typeof snapshot !== 'string' || !SHA256.test(snapshot)) return refuse(422, 'snapshot must be a sha256 hex digest');
    return { kind: 'run', snapshot };
  }
  if ('cell' in b) return parseGlobal(b);
  if (typeof b['user'] !== 'string' || !b['user']) return refuse(422, 'user is required');
  const idx = b['idx'];
  if (typeof idx !== 'number' || !Number.isInteger(idx) || idx < 1) return refuse(422, 'idx must be an integer above 0');

  let profile: Record<string, unknown> | null = null;
  const rawProfile = b['profile'];
  if (rawProfile !== undefined && rawProfile !== null) {
    if (typeof rawProfile !== 'object' || Array.isArray(rawProfile)) return refuse(422, 'profile must be an object');
    profile = rawProfile as Record<string, unknown>;
    const bad = badValue(profile);
    if (bad) return refuse(422, `profile.${bad} is not a column value`);
    const id = profile['id'] ?? profile[PY_PROFILE_KEY];
    if (id !== b['user']) return refuse(422, 'profile names a different user');
  }

  const rawTable = b['table'];
  let table: string | null = null;
  if (rawTable !== undefined && rawTable !== null) {
    if (typeof rawTable !== 'string') return refuse(422, 'table must be a string');
    if (RESET_TABLES.includes(rawTable)) return refuse(422, `${rawTable} is not migrated`);
    if (!knownTables.includes(rawTable)) return refuse(422, `unknown table ${JSON.stringify(rawTable)}`);
    table = rawTable;
  }

  const rows = parseRows(b['rows']);
  if ('status' in rows) return rows;
  if (table === null && rows.rows.length > 0) return refuse(422, 'rows without a table');
  if (table === null && profile === null) return refuse(422, 'a chunk carries a table or a profile');
  return { kind: 'user', user: b['user'], idx, table, rows: rows.rows, profile };
}

function parseGlobal(b: Record<string, unknown>): GlobalChunk | ChunkRefusal {
  const cell = b['cell'];
  if (typeof cell !== 'string' || !(cell in GLOBAL_TABLES)) return refuse(422, `unknown cell ${JSON.stringify(cell)}`);
  const table = b['table'];
  if (typeof table !== 'string' || !GLOBAL_TABLES[cell]!.includes(table)) {
    return refuse(422, `${JSON.stringify(table ?? null)} is not a table the ${cell} cell takes`);
  }
  const rows = parseRows(b['rows']);
  return 'status' in rows ? rows : { kind: 'global', cell, table, rows: rows.rows };
}

function parseRows(raw: unknown): { rows: Record<string, unknown>[] } | ChunkRefusal {
  const list = raw ?? [];
  if (!Array.isArray(list)) return refuse(422, 'rows must be an array');
  if (list.length > MAX_CHUNK_ROWS) return refuse(413, CHUNK_TOO_MANY_ROWS);
  const rows: Record<string, unknown>[] = [];
  for (const [i, item] of list.entries()) {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return refuse(422, `rows[${i}] is not an object`);
    const row = item as Record<string, unknown>;
    const bad = badValue(row);
    if (bad) return refuse(422, `rows[${i}].${bad} is not a column value`);
    rows.push(row);
  }
  return { rows };
}

/** Rows the cell may hold, and how many the policy took out. */
export function applyDispositions(table: string, rows: readonly Record<string, unknown>[]): { rows: Record<string, unknown>[]; dropped: number } {
  if (table !== 'byok_credentials') return { rows: [...rows], dropped: 0 };
  const kept = rows.filter((row) => !DROPPED_BYOK_PROVIDERS.includes(String(row['provider'])));
  return { rows: kept, dropped: rows.length - kept.length };
}

/**
 * Python's `users` row as the cell's `profile` row. The key is renamed, and
 * a provider the import drops cannot be left named as the active one or the
 * settings page offers a credential that is no longer there.
 */
export function profileForImport(row: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...row };
  if (out['id'] === undefined) out['id'] = out[PY_PROFILE_KEY];
  delete out[PY_PROFILE_KEY];
  if (DROPPED_BYOK_PROVIDERS.includes(String(out['active_byok_provider']))) out['active_byok_provider'] = null;
  return out;
}

/** What the directory needs from the profile row it is splitting. */
export function directoryEntry(profile: Readonly<Record<string, unknown>>): { isAnonymous: boolean; createdAt: string } {
  return { isAnonymous: Boolean(Number(profile['is_anonymous'] ?? 0)), createdAt: String(profile['created_at']) };
}
