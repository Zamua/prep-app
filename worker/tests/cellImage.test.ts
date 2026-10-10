import { describe, expect, it } from 'vitest';
import type { CellImage } from '../domain/cellImage.js';
import { DirectoryCell } from '../runtime/cells/DirectoryCell.js';
import { InstantLimiterCell } from '../runtime/cells/InstantLimiterCell.js';
import { UserCell } from '../runtime/cells/UserCell.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { fakeEnv } from './helpers.js';

const USER = 'seed@example.com';
const AT = '2026-03-14T15:00:00+00:00';
/** The image crosses a bucket as JSON between the two runtimes. */
const overTheWire = (img: CellImage): unknown => JSON.parse(JSON.stringify(img));

async function seededUser() {
  const state = fakeCellState();
  const cell = new UserCell(state, fakeEnv());
  await state.ready();
  await cell.wipe('study');
  await cell.seed('study', USER, null);
  return { cell, state };
}

async function blank<T>(make: (s: ReturnType<typeof fakeCellState>) => T): Promise<T> {
  const state = fakeCellState();
  const cell = make(state);
  await state.ready();
  return cell;
}

describe('a user cell image', () => {
  it('restores into a blank cell as the same image', async () => {
    const { cell } = await seededUser();
    const img = await cell.image();
    expect(Object.values(img.tables).some((t) => t.rows.length > 0)).toBe(true);
    const target = await blank((s) => new UserCell(s, fakeEnv()));
    expect(await target.restoreImage(overTheWire(img))).toMatchObject({ restored: true });
    expect(await target.image()).toEqual(img);
  });

  it('carries a blob byte for byte', async () => {
    const { cell, state } = await seededUser();
    state.storage.sql.exec('CREATE TABLE IF NOT EXISTS blob_probe (b BLOB)');
    state.storage.sql.exec('INSERT INTO blob_probe (b) VALUES (?)', new Uint8Array([0, 255, 7]));
    const img = await cell.image();
    expect(img.tables['blob_probe']!.rows).toEqual([[1, { b64: 'AP8H' }]]);
  });

  it('refuses a cell that already holds rows, and leaves it as it was', async () => {
    const { cell } = await seededUser();
    const img = await cell.image();
    expect(await cell.restoreImage(overTheWire(img))).toEqual({ restored: false, reason: 'not-blank' });
    expect(await cell.image()).toEqual(img);
  });

  it('refuses an image whose tables are not this build’s', async () => {
    const { cell } = await seededUser();
    const img = await cell.image();
    delete img.tables['cards'];
    const target = await blank((s) => new UserCell(s, fakeEnv()));
    expect(await target.restoreImage(overTheWire(img))).toEqual({ restored: false, reason: 'schema' });
  });

  it('lists every job the user started', async () => {
    const { cell, state } = await seededUser();
    state.storage.sql.exec(
      "INSERT INTO active_workflows (workflow_id, workflow_type, status, started_at, url_path) VALUES ('wf-b', 'PlanGenerate', 'done', ?, '/plan/wf-b'), ('wf-a', 'Transform', 'computing', ?, '/transform/wf-a')",
      AT,
      AT,
    );
    expect(await cell.jobIds()).toEqual(expect.arrayContaining(['wf-a', 'wf-b']));
  });
});

describe('a directory image', () => {
  it('restores the registry, the tombstones and the key-value state', async () => {
    const source = await blank((s) => new DirectoryCell(s, fakeEnv()));
    await source.register('user_a', false, AT);
    await source.register('anon:' + 'ab'.repeat(16), true, AT);
    await source.register('user_gone', false, AT);
    await source.tombstone('user_gone', 'deleted', AT);
    await source.remove('user_gone');
    const img = await source.image();
    const target = await blank((s) => new DirectoryCell(s, fakeEnv()));
    expect(await target.restoreImage(overTheWire(img))).toMatchObject({ restored: true });
    expect(await target.image()).toEqual(img);
    expect(await target.cellNames()).toEqual(['anon:' + 'ab'.repeat(16), 'user_a', 'user_gone']);
  });
});

describe('an instant limiter image', () => {
  it('restores the ledger', async () => {
    const source = await blank((s) => new InstantLimiterCell(s, fakeEnv()));
    await source.reserve({ ip: '203.0.113.7', topicChars: 12, userId: null, userIsAnonymous: null, at: AT });
    const img = await source.image();
    expect(Object.values(img.tables).some((t) => t.rows.length > 0)).toBe(true);
    const target = await blank((s) => new InstantLimiterCell(s, fakeEnv()));
    expect(await target.restoreImage(overTheWire(img))).toMatchObject({ restored: true });
    expect(await target.image()).toEqual(img);
  });
});
