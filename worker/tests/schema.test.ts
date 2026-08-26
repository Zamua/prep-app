import { describe, expect, it } from 'vitest';
import { AUTOINCREMENT_TABLES, DATA_TABLES } from '../runtime/adapters/sql/schema.js';
import { migrate, USER_MIGRATIONS, DIRECTORY_MIGRATIONS, LIMITER_MIGRATIONS } from '../runtime/adapters/sql/index.js';
import { Db } from '../runtime/adapters/sql/storage.js';
import { FakeCellStorage } from './fakes/sqlStorage.js';
import { pythonJson } from './pyoracle.js';

// Python's schema after `db.init()`: every table's columns and indexes.
const PY_SCHEMA = `
import os, tempfile, json
d = tempfile.mkdtemp()
os.environ["PREP_DB_PATH"] = os.path.join(d, "x.sqlite")
from prep.infrastructure import db
db.init()
out = {"columns": {}, "indexes": {}}
with db.cursor() as c:
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        out["columns"][t] = [r["name"] for r in c.execute(f'PRAGMA table_info("{t}")')]
        out["indexes"][t] = sorted(r["name"] for r in c.execute(f'PRAGMA index_list("{t}")') if not r["name"].startswith("sqlite_"))
print(json.dumps(out))
`;

const USER_COLUMNS = new Set(['user_id', 'user_login']);
/** Tables that live in a global cell or nowhere. */
const NOT_IN_USER_CELL = new Set(['users', 'instant_generations', 'account_merges']);

function columnsOf(db: Db): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const t of db.tables()) out[t] = [...db.columns(t)];
  return out;
}

describe('the user cell schema', () => {
  const storage = new FakeCellStorage();
  migrate(storage.sql, USER_MIGRATIONS);
  const db = new Db(storage.sql);
  const py = pythonJson<{ columns: Record<string, string[]>; indexes: Record<string, string[]> }>(PY_SCHEMA);
  const ts = columnsOf(db);

  it("carries every Python table minus its user column, columns unchanged", () => {
    for (const [table, cols] of Object.entries(py.columns)) {
      if (NOT_IN_USER_CELL.has(table)) continue;
      expect.soft(ts[table], `table ${table}`).toEqual(cols.filter((c) => !USER_COLUMNS.has(c)));
    }
  });

  it("names the profile row's columns after users' plus id_base", () => {
    const users = py.columns['users']!.map((c) => (c === 'tailscale_login' ? 'id' : c));
    expect([...ts['profile']!].sort()).toEqual([...users, 'id_base'].sort());
  });

  it('has exactly the tables the phase names', () => {
    const expected = [...DATA_TABLES, 'profile', 'questions_idempotency', 'tombstone', 'schema_version'];
    expect(db.tables().sort()).toEqual([...new Set(expected)].sort());
  });

  it('keeps the indexes that carry a query', () => {
    const names = db.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'").map((r) => r.name);
    for (const idx of ['idx_questions_deck', 'idx_cards_due', 'idx_reviews_q', 'idx_sessions_status', 'idx_sessions_deck', 'idx_trivia_queue_pos']) {
      expect(names).toContain(idx);
    }
  });

  it('autoincrements exactly the tables the id block covers', () => {
    const auto = db.all<{ sql: string; name: string }>("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE '%AUTOINCREMENT%'").map((r) => r.name);
    expect(auto.sort()).toEqual([...AUTOINCREMENT_TABLES].sort());
  });

  it('cascades a deck delete through its subtree', () => {
    db.run("INSERT INTO profile (id, created_at, last_seen_at) VALUES ('u', 't', 't')");
    const deck = db.insert("INSERT INTO decks (name, created_at) VALUES ('d', 't')");
    const q = db.insert("INSERT INTO questions (deck_id, type, prompt, answer, created_at) VALUES (?, 'short', 'p', 'a', 't')", deck);
    db.run("INSERT INTO cards (question_id, next_due) VALUES (?, 't')", q);
    db.run("INSERT INTO reviews (question_id, ts, result) VALUES (?, 't', 'right')", q);
    db.run("INSERT INTO study_sessions (id, deck_id, created_at, last_active) VALUES ('s', ?, 't', 't')", deck);
    db.run("INSERT INTO study_session_answers (session_id, question_id, answered_at, result) VALUES ('s', ?, 't', 'right')", q);
    db.run("INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, 1)", q);
    db.run("DELETE FROM decks WHERE id = ?", deck);
    for (const t of ['questions', 'cards', 'reviews', 'study_sessions', 'study_session_answers', 'trivia_queue']) {
      expect(db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM ${t}`)?.n, t).toBe(0);
    }
  });
});

describe('the global cells', () => {
  it('directory: users, merges, markers, tombstones', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, DIRECTORY_MIGRATIONS);
    const db = new Db(storage.sql);
    expect(db.tables()).toEqual(['account_merges', 'merge_markers', 'schema_version', 'tombstones', 'users']);
    expect([...db.columns('users')]).toEqual(['id', 'is_anonymous', 'created_at', 'idx']);
  });

  it('limiter: the ledger as Python keeps it, both indexes', () => {
    const storage = new FakeCellStorage();
    migrate(storage.sql, LIMITER_MIGRATIONS);
    const db = new Db(storage.sql);
    expect([...db.columns('instant_generations')]).toEqual(['id', 'ip', 'created_at', 'outcome', 'cards', 'topic_chars', 'user_id']);
    const idx = db.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'instant_generations'").map((r) => r.name);
    expect(idx.sort()).toEqual(['idx_instant_generations_created', 'idx_instant_generations_ip_created', 'idx_instant_generations_user_created']);
  });
});
