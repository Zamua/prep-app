// `decks`, transcribed from prep/decks/repo.py: DeckRepo.
import type { Clock, DeckRepo } from '../../../app/ports.js';
import { DeckNameTaken } from '../../../app/ports.js';
import type { Deck, DeckMeta, DeckSummary, DeckType, TriviaSourceMeta } from '../../../app/entities.js';
import { refuseOverRowCap } from './caps.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow } from './time.js';

const DECK_COLUMNS =
  'id, name, display_name, created_at, context_prompt, deck_type, notification_interval_minutes, last_notified_at, ' +
  'notifications_enabled, notification_ignored_streak, trivia_session_size, notifications_muted_until';

export function rowToDeck(r: Row): Deck {
  return {
    id: Number(r['id']),
    name: String(r['name']),
    display_name: (r['display_name'] as string | null) ?? null,
    created_at: String(r['created_at']),
    context_prompt: (r['context_prompt'] as string | null) ?? null,
    deck_type: ((r['deck_type'] as string | null) || 'srs') as DeckType,
    notification_interval_minutes: r['notification_interval_minutes'] == null ? null : Number(r['notification_interval_minutes']),
    last_notified_at: (r['last_notified_at'] as string | null) ?? null,
    notifications_enabled: Boolean(r['notifications_enabled'] ?? 1),
    notification_ignored_streak: Number(r['notification_ignored_streak'] ?? 0),
    trivia_session_size: Number(r['trivia_session_size'] || 3),
    notifications_muted_until: (r['notifications_muted_until'] as string | null) ?? null,
  };
}

const isUniqueViolation = (e: unknown) => e instanceof Error && /UNIQUE constraint failed/.test(e.message);

export class SqlDeckRepo implements DeckRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  getOrCreate(name: string): number {
    return this.storage.transactionSync(() => {
      const row = this.db.first<{ id: number }>('SELECT id FROM decks WHERE name = ?', name);
      if (row) return Number(row.id);
      refuseOverRowCap(this.db, { newDecks: 1 });
      return this.db.insert('INSERT INTO decks (name, created_at) VALUES (?, ?)', name, isoNow(this.clock));
    });
  }

  findId(name: string): number | null {
    const row = this.db.first<{ id: number }>('SELECT id FROM decks WHERE name = ?', name);
    return row ? Number(row.id) : null;
  }

  findName(deckId: number): string | null {
    const row = this.db.first<{ name: string }>('SELECT name FROM decks WHERE id = ?', deckId);
    return row ? row.name : null;
  }

  getType(deckId: number): DeckType | null {
    const row = this.db.first<{ deck_type: string }>('SELECT deck_type FROM decks WHERE id = ?', deckId);
    return row ? (row.deck_type as DeckType) : null;
  }

  getMeta(deckId: number): DeckMeta {
    const row = this.db.first(
      'SELECT notifications_enabled, notification_interval_minutes, trivia_session_size, context_prompt, pinned_at, display_name FROM decks WHERE id = ?',
      deckId,
    );
    if (!row) {
      return { deck_id: deckId, notifications_enabled: true, interval_minutes: null, session_size: 3, context_prompt: '', pinned: false, display_name: null };
    }
    return {
      deck_id: deckId,
      notifications_enabled: Boolean(row['notifications_enabled']),
      interval_minutes: row['notification_interval_minutes'] == null ? null : Number(row['notification_interval_minutes']),
      session_size: Number(row['trivia_session_size'] || 3),
      context_prompt: (row['context_prompt'] as string | null) || '',
      pinned: row['pinned_at'] != null,
      display_name: (row['display_name'] as string | null) ?? null,
    };
  }

  getTriviaSourceMeta(deckId: number): TriviaSourceMeta | null {
    const row = this.db.first('SELECT notification_interval_minutes, context_prompt FROM decks WHERE id = ?', deckId);
    if (!row) return null;
    return {
      notification_interval_minutes: row['notification_interval_minutes'] == null ? null : Number(row['notification_interval_minutes']),
      context_prompt: (row['context_prompt'] as string | null) ?? null,
    };
  }

  create(name: string, opts: { contextPrompt?: string | null; displayName?: string | null } = {}): number {
    return this.storage.transactionSync(() => {
      refuseOverRowCap(this.db, { newDecks: 1 });
      try {
        return this.db.insert(
          'INSERT INTO decks (name, display_name, created_at, context_prompt) VALUES (?, ?, ?, ?)',
          name,
          opts.displayName ?? null,
          isoNow(this.clock),
          opts.contextPrompt ?? null,
        );
      } catch (e) {
        if (isUniqueViolation(e)) throw new DeckNameTaken(name);
        throw e;
      }
    });
  }

  updateDisplayName(name: string, displayName: string): boolean {
    return this.db.run('UPDATE decks SET display_name = ? WHERE name = ?', displayName, name) > 0;
  }

  getContextPrompt(name: string): string | null {
    const row = this.db.first<{ context_prompt: string | null }>('SELECT context_prompt FROM decks WHERE name = ?', name);
    return row ? row.context_prompt : null;
  }

  updateContextPrompt(name: string, contextPrompt: string): void {
    this.db.run('UPDATE decks SET context_prompt = ? WHERE name = ?', contextPrompt, name);
  }

  rename(oldName: string, newName: string): boolean {
    try {
      return this.db.run('UPDATE decks SET name = ? WHERE name = ?', newName, oldName) > 0;
    } catch (e) {
      if (isUniqueViolation(e)) return false;
      throw e;
    }
  }

  delete(name: string): number {
    return this.db.run('DELETE FROM decks WHERE name = ?', name);
  }

  listSummaries(): DeckSummary[] {
    const rows = this.db.all(
      `SELECT d.id, d.name, d.display_name, d.deck_type, d.pinned_at,
              COUNT(q.id) AS total,
              SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                        AND COALESCE(d.deck_type,'srs') = 'srs'
                       THEN 1 ELSE 0 END) AS due
         FROM decks d
         LEFT JOIN questions q ON q.deck_id = d.id
         LEFT JOIN cards ON cards.question_id = q.id
        GROUP BY d.id
        ORDER BY (d.pinned_at IS NULL), d.pinned_at DESC, COALESCE(d.display_name, d.name)`,
      isoNow(this.clock),
    );
    return rows.map((r) => ({
      id: Number(r['id']),
      name: String(r['name']),
      display_name: (r['display_name'] as string | null) ?? null,
      total: Number(r['total'] ?? 0),
      due: Number(r['due'] ?? 0),
      deck_type: ((r['deck_type'] as string | null) || 'srs') as DeckType,
      pinned: r['pinned_at'] != null,
    }));
  }

  dueBreakdown(): [string, number][] {
    const rows = this.db.all<{ name: string; n: number }>(
      `SELECT d.name, COUNT(c.question_id) AS n
         FROM decks d
         LEFT JOIN questions q ON q.deck_id = d.id
         LEFT JOIN cards c ON c.question_id = q.id AND c.next_due <= ? AND COALESCE(q.suspended, 0) = 0
        WHERE COALESCE(d.notifications_enabled, 1) = 1
          AND COALESCE(d.deck_type, 'srs') = 'srs'
        GROUP BY d.id
       HAVING n > 0
        ORDER BY n DESC`,
      isoNow(this.clock),
    );
    return rows.map((r) => [r.name, Number(r.n)]);
  }

  createTrivia(name: string, opts: { topic: string; intervalMinutes: number; displayName?: string | null }): number {
    return this.storage.transactionSync(() => {
      refuseOverRowCap(this.db, { newDecks: 1 });
      try {
        return this.db.insert(
          `INSERT INTO decks (name, display_name, created_at, context_prompt, deck_type, notification_interval_minutes)
           VALUES (?, ?, ?, ?, 'trivia', ?)`,
          name,
          opts.displayName ?? null,
          isoNow(this.clock),
          opts.topic,
          opts.intervalMinutes,
        );
      } catch (e) {
        if (isUniqueViolation(e)) throw new DeckNameTaken(name);
        throw e;
      }
    });
  }

  listTriviaDecks(): Deck[] {
    return this.db.all(`SELECT ${DECK_COLUMNS} FROM decks WHERE deck_type = 'trivia'`).map(rowToDeck);
  }

  recordNotificationFire(deckId: number, ts: string, ignoredStreak: number): void {
    this.db.run('UPDATE decks SET last_notified_at = ?, notification_ignored_streak = ? WHERE id = ?', ts, ignoredStreak, deckId);
  }

  resetIgnoredStreakForDeck(deckId: number): void {
    this.db.run('UPDATE decks SET notification_ignored_streak = 0 WHERE id = ?', deckId);
  }

  setNotificationInterval(deckId: number, minutes: number): boolean {
    if (minutes < 1 || minutes > 720) throw new RangeError(`interval out of range: ${minutes}`);
    return (
      this.db.run(
        `UPDATE decks SET notification_interval_minutes = ?, notification_ignored_streak = 0 WHERE id = ? AND deck_type = 'trivia'`,
        minutes,
        deckId,
      ) > 0
    );
  }

  getTriviaSessionSize(deckId: number): number {
    const row = this.db.first<{ trivia_session_size: number }>(`SELECT trivia_session_size FROM decks WHERE id = ? AND deck_type = 'trivia'`, deckId);
    return row ? Number(row.trivia_session_size || 3) : 3;
  }

  setTriviaSessionSize(deckId: number, size: number): boolean {
    if (size < 1 || size > 20) throw new RangeError(`session size out of range: ${size}`);
    return this.db.run(`UPDATE decks SET trivia_session_size = ? WHERE id = ? AND deck_type = 'trivia'`, size, deckId) > 0;
  }

  setNotificationsEnabled(deckId: number, enabled: boolean): boolean {
    return this.db.run('UPDATE decks SET notifications_enabled = ? WHERE id = ?', enabled ? 1 : 0, deckId) > 0;
  }

  muteNotificationsUntil(deckId: number, untilIso: string | null): boolean {
    return this.db.run('UPDATE decks SET notifications_muted_until = ? WHERE id = ?', untilIso, deckId) > 0;
  }

  setPinned(deckId: number, pinned: boolean): boolean {
    return this.db.run('UPDATE decks SET pinned_at = ? WHERE id = ?', pinned ? isoNow(this.clock) : null, deckId) > 0;
  }

  getDesiredRetention(deckId: number): number | null {
    const row = this.db.first<{ desired_retention: number | null }>('SELECT desired_retention FROM decks WHERE id = ?', deckId);
    return row && row.desired_retention != null ? Number(row.desired_retention) : null;
  }

  setDesiredRetention(deckId: number, retention: number | null): boolean {
    return this.db.run('UPDATE decks SET desired_retention = ? WHERE id = ?', retention, deckId) > 0;
  }
}
