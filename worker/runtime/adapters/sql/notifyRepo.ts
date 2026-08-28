// `notifications_log` and `push_subscriptions`.
import type { Clock, NotifyRepo, PushSubRepo } from '../../../app/ports.js';
import type { NotificationLogEntry, PushSubscription } from '../../../app/entities.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow, isoSeconds } from './time.js';

export class SqlNotifyRepo implements NotifyRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  append(entry: { title: string; body: string; url: string; source: string }): number {
    return this.db.insert(
      'INSERT INTO notifications_log (sent_at, title, body, url, source) VALUES (?, ?, ?, ?, ?)',
      isoSeconds(this.clock.now()),
      entry.title,
      entry.body,
      entry.url,
      entry.source,
    );
  }

  listRecent(limit = 50): NotificationLogEntry[] {
    return this.db
      .all('SELECT id, sent_at, title, body, url, source, seen_at FROM notifications_log ORDER BY sent_at DESC LIMIT ?', limit)
      .map((r) => ({
        id: Number(r['id']),
        sent_at: String(r['sent_at']),
        title: String(r['title']),
        body: String(r['body']),
        url: String(r['url']),
        source: String(r['source']),
        seen_at: (r['seen_at'] as string | null) ?? null,
      }));
  }

  countUnseen(): number {
    const row = this.db.first<{ n: number }>('SELECT COUNT(*) AS n FROM notifications_log WHERE seen_at IS NULL');
    return Number(row?.n ?? 0);
  }

  markAllSeen(): void {
    this.db.run('UPDATE notifications_log SET seen_at = ? WHERE seen_at IS NULL', isoSeconds(this.clock.now()));
  }
}

export class SqlPushSubRepo implements PushSubRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  upsert(endpoint: string, p256dh: string, auth: string): void {
    const ts = isoNow(this.clock);
    this.db.run(
      `INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, last_seen_at = excluded.last_seen_at`,
      endpoint,
      p256dh,
      auth,
      ts,
      ts,
    );
  }

  list(): PushSubscription[] {
    return this.db
      .all<{ endpoint: string; p256dh: string; auth: string }>('SELECT endpoint, p256dh, auth FROM push_subscriptions')
      .map((r) => ({ endpoint: r.endpoint, p256dh: r.p256dh, auth: r.auth }));
  }

  count(): number {
    const row = this.db.first<{ n: number }>('SELECT COUNT(*) AS n FROM push_subscriptions');
    return Number(row?.n ?? 0);
  }

  deleteByEndpoint(endpoint: string): void {
    this.db.run('DELETE FROM push_subscriptions WHERE endpoint = ?', endpoint);
  }
}
