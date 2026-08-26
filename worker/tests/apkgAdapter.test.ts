// The two adapters behind the codec ports: the zip container and the sql.js
// collection. The load path is the part worth pinning - a cell may only take
// WASM through the module import, and a fallback that quietly compiles from
// bytes would pass every other test here and fail on the fleet.
import { describe, expect, it } from 'vitest';
import { zipSync } from 'fflate';
import { NotAnApkg, NotAZip, ZipEntryTooLarge } from '../app/ports.js';
import { SqlJsApkg, sqlEngine } from '../runtime/adapters/apkg.js';
import { FflateZip } from '../runtime/adapters/zip.js';

const zip = new FflateZip();
const enc = new TextEncoder();

const COL = { id: 1, crt: 1, mod: 1, scm: 1, ver: 11, dty: 0, usn: -1, ls: 0, conf: {}, models: {}, decks: {}, dconf: {}, tags: {} };
const note = (id: number, flds: string) => ({ id, guid: `g${id}`, mid: 1, mod: 1, usn: -1, tags: '', flds, sfld: flds.split('\x1f')[0]!, csum: id, flags: 0, data: '' });

/** Every path a runtime WASM compile could take, trapped. Returns the undo. */
function trapRuntimeCompilation(): { calls: string[]; restore(): void } {
  const calls: string[] = [];
  const WA = WebAssembly as unknown as Record<string, unknown>;
  const names = ['compile', 'instantiate', 'compileStreaming', 'instantiateStreaming', 'validate'];
  const saved = new Map<string, unknown>(names.map((n) => [n, WA[n]]));
  for (const name of names) {
    WA[name] = () => {
      calls.push(name);
      throw new Error(`WebAssembly.${name} called at runtime`);
    };
  }
  const RealModule = WebAssembly.Module;
  saved.set('Module', RealModule);
  WA['Module'] = new Proxy(RealModule, {
    construct() {
      calls.push('new Module');
      throw new Error('new WebAssembly.Module(bytes) at runtime');
    },
  });
  return {
    calls,
    restore() {
      for (const [name, value] of saved) WA[name] = value;
    },
  };
}

describe('sql.js loads through the module import', () => {
  // First in the file on purpose: the compiled module is memoised per isolate,
  // so a later test would find the work already done and prove nothing.
  it('compiles no WASM at runtime, and still answers a query', async () => {
    const trap = trapRuntimeCompilation();
    try {
      const built = await new SqlJsApkg().build(COL, [note(7, 'front\x1fback')], []);
      expect(await new SqlJsApkg().notes(built)).toEqual([{ id: 7, flds: 'front\x1fback' }]);
      expect(trap.calls).toEqual([]);
    } finally {
      trap.restore();
    }
  });

  it('compiles once and shares the module with every later caller', async () => {
    expect(await sqlEngine()).toBe(await sqlEngine());
  });
});

describe('SqlJsApkg', () => {
  it('writes the collection and the empty media map', async () => {
    const built = await new SqlJsApkg().build(COL, [note(1, 'a\x1fb')], []);
    const entries = zip.read(built);
    expect(entries.map((e) => e.name).sort()).toEqual(['collection.anki21', 'media']);
    expect(new TextDecoder().decode(entries.find((e) => e.name === 'media')!.bytes)).toBe('{}');
  });

  it('reads notes by id whatever order they were written in', async () => {
    const built = await new SqlJsApkg().build(COL, [note(9, 'i\x1fix'), note(2, 'ii\x1fiix')], []);
    expect((await new SqlJsApkg().notes(built)).map((n) => n.id)).toEqual([2, 9]);
  });

  it('opens and closes a database per call, so many calls in a row still answer', async () => {
    const reader = new SqlJsApkg();
    const built = await reader.build(COL, [note(1, 'a\x1fb')], []);
    for (let i = 0; i < 25; i++) expect(await reader.notes(built)).toHaveLength(1);
  });

  it('refuses bytes that are not a zip, naming the format', async () => {
    await expect(new SqlJsApkg().notes(enc.encode('nope'))).rejects.toThrow(NotAnApkg);
    await expect(new SqlJsApkg().notes(enc.encode('nope'))).rejects.toThrow(/not a valid \.apkg \(zip parse failed\)/);
  });

  it('refuses a zip with no collection inside, in the words Python uses', async () => {
    const blob = zip.write([{ name: 'media', bytes: enc.encode('{}') }]);
    await expect(new SqlJsApkg().notes(blob)).rejects.toThrow('not a valid .apkg — no collection.anki2 / collection.anki21 inside');
  });

  it('refuses a collection that is not a sqlite database', async () => {
    const blob = zip.write([{ name: 'collection.anki21', bytes: enc.encode('SQLite format 3 is what this is not') }]);
    await expect(new SqlJsApkg().notes(blob)).rejects.toThrow(NotAnApkg);
  });

  it('refuses a sqlite database with no notes table', async () => {
    const SQL = await sqlEngine();
    const db = new SQL.Database();
    db.run('CREATE TABLE something (id INTEGER)');
    const bytes = db.export();
    db.close();
    await expect(new SqlJsApkg().notes(zip.write([{ name: 'collection.anki21', bytes }]))).rejects.toThrow(/no readable notes table/);
  });

  it('answers an empty list for a collection whose notes table is empty', async () => {
    const built = await new SqlJsApkg().build(COL, [], []);
    expect(await new SqlJsApkg().notes(built)).toEqual([]);
  });
});

describe('FflateZip', () => {
  it('round-trips entries in the order they were written', () => {
    const entries = [
      { name: 'meta.json', bytes: enc.encode('{}') },
      { name: 'cards.csv', bytes: enc.encode('a,b\r\n') },
      { name: 'reviews.csv', bytes: new Uint8Array(0) },
    ];
    expect(zip.read(zip.write(entries))).toEqual(entries);
  });

  it('writes the same bytes for the same entries', () => {
    const entries = [{ name: 'a', bytes: enc.encode('x') }];
    expect(zip.write(entries)).toEqual(zip.write(entries));
  });

  it('rejects bytes that are not a zip', () => {
    expect(() => zip.read(enc.encode('not a zip'))).toThrow(NotAZip);
  });

  it('refuses an entry that declares more inflated bytes than the cap, without inflating it', () => {
    // 40 MiB of zeros deflates to a few kilobytes: the declared size in the
    // central directory is the only thing that can stop this cheaply.
    const bomb = zipSync({ 'big.bin': new Uint8Array(40 * 1024 * 1024) }, { level: 9 });
    expect(bomb.length).toBeLessThan(200 * 1024);
    expect(() => zip.read(bomb, { maxEntryBytes: 32 * 1024 * 1024 })).toThrow(ZipEntryTooLarge);
  });

  it('lets an entry inside the cap through', () => {
    const entries = [{ name: 'small', bytes: enc.encode('x'.repeat(1000)) }];
    expect(zip.read(zip.write(entries), { maxEntryBytes: 32 * 1024 * 1024 })).toEqual(entries);
  });
});
