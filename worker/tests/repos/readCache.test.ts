// The read cache in front of a user cell's storage: what it keeps, what drops
// it, and the facts about the schema and SQLite it rests on.
import Database from 'better-sqlite3';
import { describe, expect, it } from 'vitest';
import { keepsFloor } from '../../runtime/adapters/sql/dueFloor.js';
import { windowHolds } from '../../runtime/adapters/sql/dueWindow.js';
import { migrate, USER_MIGRATIONS } from '../../runtime/adapters/sql/index.js';
import { WRITE_SCHEDULED } from '../../runtime/adapters/sql/cardRepo.js';
import { CASCADES, classify, ReadCachingStorage, VOLATILE_FUNCTIONS } from '../../runtime/adapters/sql/readCache.js';
import { RECORD_ANSWER } from '../../runtime/adapters/sql/sessionRepo.js';
import { FakeCellStorage } from '../fakes/sqlStorage.js';

/** A migrated cell behind the cache, counting the statements that reach the rows. */
function cached() {
  const raw = new FakeCellStorage();
  migrate(raw.sql, USER_MIGRATIONS);
  const sent: string[] = [];
  const exec = raw.sql.exec.bind(raw.sql);
  raw.sql.exec = ((query: string, ...bindings: unknown[]) => {
    sent.push(query);
    return exec(query, ...bindings);
  }) as typeof raw.sql.exec;
  const storage = new ReadCachingStorage(raw);
  const reads = () => sent.filter((q) => !q.includes('total_changes()'));
  return { raw, storage, sent, reads, reset: () => void (sent.length = 0) };
}

const DECK = "INSERT INTO decks (name, created_at) VALUES ('d', '2026-03-14T15:00:00+00:00')";
const PUSH = "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, last_seen_at) VALUES ('e', 'p', 'a', 'x', 'x')";

describe('which statements the cache can keep', () => {
  it('keeps a plain read, keyed by every word it names', () => {
    expect(classify('SELECT * FROM profile LIMIT 1')).toEqual({ kind: 'read', tables: ['select', 'from', 'profile', 'limit'] });
  });

  it('never keeps a read whose answer can move with no write', () => {
    for (const sql of [
      'SELECT q.id FROM questions q ORDER BY RANDOM() LIMIT 1',
      'SELECT changes() AS n',
      'SELECT last_insert_rowid() AS id',
      "SELECT datetime('now')",
      'SELECT name FROM sqlite_master',
      'SELECT name FROM pragma_table_info(?)',
      'SELECT seq FROM sqlite_sequence',
    ]) {
      expect(classify(sql)).toEqual({ kind: 'read', tables: null });
    }
  });

  it('names the table a write lands on, and the children a delete reaches', () => {
    expect(classify('UPDATE study_sessions SET state = ? WHERE id = ?')).toEqual({ kind: 'write', tables: ['study_sessions'] });
    expect(classify('INSERT INTO profile (id) VALUES (?) ON CONFLICT(id) DO UPDATE SET id = excluded.id')).toEqual({ kind: 'write', tables: ['profile'] });
    expect(classify('INSERT INTO "cards" (question_id) VALUES (?)')).toEqual({ kind: 'write', tables: ['cards'] });
    expect(classify(RECORD_ANSWER)).toEqual({ kind: 'write', tables: ['study_session_answers'] });
    const written = (sql: string) => {
      const s = classify(sql);
      return new Set(s.kind === 'write' ? s.tables : []);
    };
    expect(written('DELETE FROM decks WHERE name = ?')).toEqual(
      new Set(['decks', 'questions', 'study_sessions', 'trivia_sessions', 'cards', 'reviews', 'study_session_answers', 'trivia_queue']),
    );
    expect(written('INSERT OR REPLACE INTO questions (id) VALUES (?)')).toContain('cards');
    expect(written('UPDATE OR REPLACE study_sessions SET id = ?')).toContain('study_session_answers');
  });

  it('cannot attribute a script, a schema change, a pragma or a transaction statement', () => {
    for (const sql of ['CREATE TABLE t (a)', 'ALTER TABLE profile ADD COLUMN x TEXT', 'PRAGMA foreign_keys = ON', 'BEGIN', 'ROLLBACK', 'DELETE FROM a; DELETE FROM b', 'WITH x AS (SELECT 1) DELETE FROM cards']) {
      expect(classify(sql)).toEqual({ kind: 'opaque' });
    }
  });
});

describe('what the cache rests on', () => {
  const schema = () => {
    const raw = new FakeCellStorage();
    migrate(raw.sql, USER_MIGRATIONS);
    return raw.db;
  };

  it('CASCADES is every foreign key that writes a child, and none acts on update', () => {
    const db = schema();
    const tables = (db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").all() as { name: string }[]).map((t) => t.name);
    const found: Record<string, string[]> = {};
    for (const child of tables) {
      for (const fk of db.prepare('SELECT "table" AS parent, on_delete, on_update FROM pragma_foreign_key_list(?)').all(child) as { parent: string; on_delete: string; on_update: string }[]) {
        expect([child, fk.on_update]).toEqual([child, 'NO ACTION']);
        if (fk.on_delete === 'NO ACTION' || fk.on_delete === 'RESTRICT') continue;
        (found[fk.parent] ??= []).push(child);
      }
    }
    const sorted = (r: Readonly<Record<string, readonly string[]>>) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, [...v].sort()]));
    expect(sorted(found)).toEqual(sorted(CASCADES));
  });

  it('holds no trigger, no view and no table that replaces on a plain insert', () => {
    const db = schema();
    expect(db.prepare("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')").all()).toEqual([]);
    for (const { sql } of db.prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL").all() as { sql: string }[]) {
      expect(sql).not.toMatch(/ON\s+CONFLICT\s+REPLACE/i);
    }
  });

  it("names every nondeterministic scalar SQLite ships, or catches it by the sqlite_ prefix", () => {
    const db = new Database(':memory:');
    const volatile = (db.prepare("SELECT DISTINCT name FROM pragma_function_list WHERE builtin = 1 AND type = 's' AND (flags & 0x800) = 0").all() as { name: string }[]).map((r) => r.name);
    expect(volatile.length).toBeGreaterThan(0);
    for (const name of volatile) if (!name.startsWith('sqlite_')) expect(VOLATILE_FUNCTIONS).toContain(name);
    for (const name of ['date', 'time', 'datetime', 'julianday', 'strftime', 'unixepoch']) expect(VOLATILE_FUNCTIONS).toContain(name);
  });
});

describe('a cell behind the cache', () => {
  it('answers a repeated read without reaching the rows', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    expect(c.storage.sql.exec('SELECT name FROM decks WHERE id = ?', 1).toArray()).toEqual([{ name: 'd' }]);
    c.reset();
    expect(c.storage.sql.exec('SELECT name FROM decks WHERE id = ?', 1).toArray()).toEqual([{ name: 'd' }]);
    expect(c.reads()).toEqual([]);
    c.storage.sql.exec('SELECT name FROM decks WHERE id = ?', 2).toArray();
    expect(c.reads()).toEqual(['SELECT name FROM decks WHERE id = ?']);
  });

  it('reads again after a write to a table the read names, and only then', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    const read = () => c.storage.sql.exec('SELECT name FROM decks WHERE id = ?', 1).toArray();
    read();
    c.storage.sql.exec(PUSH);
    c.reset();
    read();
    expect(c.reads()).toEqual([]);
    c.storage.sql.exec("UPDATE decks SET name = 'e' WHERE id = 1");
    c.reset();
    expect(read()).toEqual([{ name: 'e' }]);
    expect(c.reads()).toHaveLength(1);
  });

  it('reads a child again after a delete cascades into it', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    c.storage.sql.exec("INSERT INTO questions (deck_id, type, prompt, answer, created_at) VALUES (1, 'short', 'p', 'a', 'x')");
    const count = () => c.storage.sql.exec<{ n: number }>('SELECT COUNT(*) AS n FROM trivia_queue').toArray()[0]!.n;
    c.storage.sql.exec("INSERT INTO trivia_queue (question_id, queue_position) VALUES (1, 1)");
    expect(count()).toBe(1);
    c.storage.sql.exec('DELETE FROM decks WHERE id = 1');
    expect(count()).toBe(0);
  });

  it('drops what a rolled-back transaction read', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    expect(() =>
      c.storage.transactionSync(() => {
        c.storage.sql.exec("UPDATE decks SET name = 'gone' WHERE id = 1");
        expect(c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray()).toEqual([{ name: 'gone' }]);
        throw new Error('roll back');
      }),
    ).toThrow('roll back');
    expect(c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray()).toEqual([{ name: 'd' }]);
  });

  it('drops everything for a statement it cannot attribute, and for deleteAll', async () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    const read = () => c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray();
    read();
    c.storage.sql.exec('PRAGMA foreign_keys = ON');
    c.reset();
    read();
    expect(c.reads()).toHaveLength(1);
    await c.storage.deleteAll();
    migrate(c.storage.sql, USER_MIGRATIONS);
    expect(read()).toEqual([]);
  });

  it('notices a row changed behind its back', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    const read = () => c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray();
    read();
    c.raw.db.prepare("UPDATE decks SET name = 'behind' WHERE id = 1").run();
    expect(read()).toEqual([{ name: 'behind' }]);
  });

  it('notices a change behind its back that lands just before a write of its own', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    const read = () => c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray();
    read();
    c.raw.db.prepare("UPDATE decks SET name = 'behind' WHERE id = 1").run();
    c.storage.sql.exec(PUSH);
    expect(read()).toEqual([{ name: 'behind' }]);
  });

  it('hands out copies, so a caller that changes a row changes its own', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    const first = c.storage.sql.exec<{ name: string }>('SELECT name FROM decks WHERE id = 1').toArray();
    first[0]!.name = 'mutated';
    const second = c.storage.sql.exec<{ name: string }>('SELECT name FROM decks WHERE id = 1').toArray();
    second[0]!.name = 'again';
    expect(c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray()).toEqual([{ name: 'd' }]);
  });

  it('keeps neither a blob nor a long list', () => {
    const c = cached();
    c.storage.sql.exec("INSERT INTO byok_credentials (provider, ciphertext, key_prefix, created_at) VALUES ('p', x'00ff', 'k', 'x')");
    for (let i = 0; i < 40; i++) c.storage.sql.exec(`INSERT INTO decks (name, created_at) VALUES ('d${i}', 'x')`);
    c.storage.sql.exec('SELECT ciphertext FROM byok_credentials').toArray();
    c.storage.sql.exec('SELECT name FROM decks').toArray();
    c.reset();
    c.storage.sql.exec('SELECT ciphertext FROM byok_credentials').toArray();
    c.storage.sql.exec('SELECT name FROM decks').toArray();
    expect(c.reads()).toHaveLength(2);
  });

  it('holds a bounded number of bytes, dropping the least recently used first', () => {
    const c = cached();
    c.storage.sql.exec(`INSERT INTO decks (name, created_at, context_prompt) VALUES ('big', 'x', '${'x'.repeat(40_000)}')`);
    for (let i = 0; i < 2000; i++) c.storage.sql.exec(`INSERT INTO decks (name, created_at, context_prompt) VALUES ('d${i}', 'x', '${'y'.repeat(200)}')`);
    const read = (id: number) => c.storage.sql.exec('SELECT * FROM decks WHERE id = ?', id).toArray();
    read(1);
    for (let id = 2; id <= 2001; id++) read(id);
    c.reset();
    read(1);
    read(2);
    read(2001);
    expect(c.reads()).toHaveLength(2);
  });

  it('leaves a cursor read one row at a time, or by one(), to the rows', () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    c.storage.sql.exec('SELECT name FROM decks').one();
    [...c.storage.sql.exec('SELECT id FROM decks')];
    c.reset();
    c.storage.sql.exec('SELECT name FROM decks').toArray();
    c.storage.sql.exec('SELECT id FROM decks').toArray();
    expect(c.reads()).toHaveLength(2);
    expect(() => c.storage.sql.exec('SELECT name FROM decks WHERE id = 9').one()).toThrow();
  });

  it('absorbs its own key-value and alarm writes', async () => {
    const c = cached();
    c.storage.sql.exec(DECK);
    c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray();
    await c.storage.put('k', 1);
    await c.storage.setAlarm(5);
    c.reset();
    c.storage.sql.exec('SELECT name FROM decks WHERE id = 1').toArray();
    expect(c.reads()).toEqual([]);
  });
});

describe('the due window', () => {
  const at = '2026-03-14T15:00:00+00:00';
  it('holds from where it was read until the next card comes due', () => {
    expect(windowHolds({ at, next: '2026-03-14T16:00:00+00:00' }, at)).toBe(true);
    expect(windowHolds({ at, next: '2026-03-14T16:00:00+00:00' }, '2026-03-14T15:59:59.999000+00:00')).toBe(true);
    expect(windowHolds({ at, next: '2026-03-14T16:00:00+00:00' }, '2026-03-14T16:00:00+00:00')).toBe(false);
    expect(windowHolds({ at, next: null }, '2027-01-01T00:00:00+00:00')).toBe(true);
    expect(windowHolds({ at, next: null }, '2026-03-14T14:59:59+00:00')).toBe(false);
  });
});

describe('the pick floor', () => {
  const floor = '2026-03-14T12:00:00+00:00';
  const scheduled = (due: unknown) => ({ sql: WRITE_SCHEDULED, bindings: [3, due, 'x', 1, 1, 2, 0, 9] as (string | number | null)[] });
  it('stands through an answer and a review that schedules no earlier than it', () => {
    expect(keepsFloor({ sql: RECORD_ANSWER, bindings: null }, floor)).toBe(true);
    expect(keepsFloor(scheduled('2026-03-14T15:01:00+00:00'), floor)).toBe(true);
    expect(keepsFloor(scheduled(floor), floor)).toBe(true);
  });

  it('falls to a review scheduled earlier, and to any other write', () => {
    expect(keepsFloor(scheduled('2026-03-14T11:59:59+00:00'), floor)).toBe(false);
    expect(keepsFloor({ sql: WRITE_SCHEDULED, bindings: null }, floor)).toBe(false);
    expect(keepsFloor({ sql: 'UPDATE cards SET next_due = ? WHERE question_id = ?', bindings: ['2027-01-01T00:00:00+00:00', 1] }, floor)).toBe(false);
    expect(keepsFloor({ sql: 'UPDATE questions SET suspended = ? WHERE id = ?', bindings: [0, 1] }, floor)).toBe(false);
  });
});
