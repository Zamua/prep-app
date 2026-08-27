// The Python snapshot into the cells, one chunk at a time.
//
// Nothing here is keyed by a run id. Every step keys on something the data
// already carries, so a replay converges instead of duplicating: `register`
// hands back the idx a user already has, the id block only ever rises, the
// profile write is the same row twice, and the insert ignores a primary key
// the cell already holds. Two runs of the same export therefore converge,
// and so do two concurrent ones.
import { applyDispositions, directoryEntry, profileForImport, type MigrationChunk } from '../../domain/migrate.js';
import type { Directory, UserCells } from '../ports.js';

export interface ImportDeps {
  directory: Directory;
  cells: UserCells;
}

export interface ImportResult {
  /** The directory's answer, which wins: a user already registered keeps the
   * idx its rows were minted against. */
  idx: number;
  inserted: Record<string, number>;
  /** Rows a disposition took out. Counted so a run can report them rather
   * than the operator inferring the gap from a count mismatch. */
  dropped: number;
}

export async function importChunk(deps: ImportDeps, chunk: MigrationChunk): Promise<ImportResult> {
  const profile = chunk.profile ? profileForImport(chunk.profile) : null;
  let idx = chunk.idx;
  if (profile) {
    const entry = directoryEntry(profile);
    idx = (await deps.directory.register(chunk.user, entry.isAnonymous, entry.createdAt, { idx: chunk.idx })).idx;
  }
  const disposed = chunk.table ? applyDispositions(chunk.table, chunk.rows) : { rows: [], dropped: 0 };
  const inserted = await deps.cells.cell(chunk.user).importChunk({ idx, table: chunk.table, rows: disposed.rows, profile });
  return { idx, inserted, dropped: disposed.dropped };
}
