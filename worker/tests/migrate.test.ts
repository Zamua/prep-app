import { describe, expect, it } from 'vitest';
import { currentVersion, ID_BLOCK, migrate, resetSequences, seedSequences, USER_MIGRATIONS, type Migration } from '../runtime/adapters/sql/migrate.js';
import { AUTOINCREMENT_TABLES } from '../runtime/adapters/sql/schema.js';
import { Db } from '../runtime/adapters/sql/storage.js';
import { FakeCellStorage } from './fakes/sqlStorage.js';

describe('migrate', () => {
  it('takes a fresh cell from version 0 to the latest and is a no-op the second time', () => {
    const storage = new FakeCellStorage();
    const db = new Db(storage.sql);
    expect(currentVersion(db)).toBe(0);
    expect(migrate(storage.sql, USER_MIGRATIONS)).toBe(USER_MIGRATIONS.at(-1)!.version);
    const before = db.all("SELECT name, sql FROM sqlite_master ORDER BY name");
    expect(migrate(storage.sql, USER_MIGRATIONS)).toBe(USER_MIGRATIONS.at(-1)!.version);
    expect(db.all("SELECT name, sql FROM sqlite_master ORDER BY name")).toEqual(before);
    expect(db.all('SELECT version FROM schema_version')).toEqual([{ version: USER_MIGRATIONS.at(-1)!.version }]);
  });

  it('applies only the steps above the stored version, each once', () => {
    const storage = new FakeCellStorage();
    const applied: number[] = [];
    const steps: Migration[] = [
      { version: 1, apply: (db) => { applied.push(1); db.script('CREATE TABLE IF NOT EXISTS a (x); CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)'); } },
      { version: 2, apply: (db) => { applied.push(2); db.script('CREATE TABLE IF NOT EXISTS b (y)'); } },
    ];
    migrate(storage.sql, steps.slice(0, 1));
    migrate(storage.sql, steps);
    migrate(storage.sql, steps);
    expect(applied).toEqual([1, 2]);
  });

  it('re-migrates a wiped cell on its next activation', async () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, USER_MIGRATIONS);
    await storage.deleteAll();
    expect(currentVersion(new Db(storage.sql))).toBe(0);
    expect(migrate(storage.sql, USER_MIGRATIONS)).toBe(USER_MIGRATIONS.at(-1)!.version);
    expect(new Db(storage.sql).tables()).toContain('decks');
  });

  it('turns foreign keys on', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, USER_MIGRATIONS);
    expect(() => new Db(storage.sql).run("INSERT INTO questions (deck_id, type, prompt, answer, created_at) VALUES (999, 'short', 'p', 'a', 't')")).toThrow(/FOREIGN KEY/);
  });
});

describe('the id block', () => {
  it('seeds every autoincrement counter to idx * 2^32 and never lowers one', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, USER_MIGRATIONS);
    const db = new Db(storage.sql);
    seedSequences(storage.sql, 3);
    const deck = db.insert("INSERT INTO decks (name, created_at) VALUES ('d', 't')");
    expect(deck).toBe(3 * ID_BLOCK + 1);
    for (const t of AUTOINCREMENT_TABLES) {
      expect(Number(db.first<{ seq: number }>('SELECT seq FROM sqlite_sequence WHERE name = ?', t)?.seq)).toBeGreaterThanOrEqual(3 * ID_BLOCK);
    }
    seedSequences(storage.sql, 1);
    expect(db.insert("INSERT INTO decks (name, created_at) VALUES ('e', 't')")).toBe(3 * ID_BLOCK + 2);
    expect(3 * ID_BLOCK + 2).toBeLessThan(Number.MAX_SAFE_INTEGER);
  });

  it('block 0 is the reset: ids restart at 1', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, USER_MIGRATIONS);
    const db = new Db(storage.sql);
    seedSequences(storage.sql, 2);
    db.insert("INSERT INTO decks (name, created_at) VALUES ('d', 't')");
    db.run('DELETE FROM decks');
    resetSequences(storage.sql);
    expect(db.insert("INSERT INTO decks (name, created_at) VALUES ('d', 't')")).toBe(1);
  });

  it('two million users stay below 2^53', () => {
    expect(2_000_000 * ID_BLOCK + ID_BLOCK).toBeLessThan(2 ** 53);
  });
});
