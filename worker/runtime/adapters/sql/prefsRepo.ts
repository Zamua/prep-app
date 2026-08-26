// The `profile` row, transcribed from prep/auth/repo.py: UserRepo. Read as
// the `user` dict with Python's key names.
import type { Clock, PrefsRepo } from '../../../app/ports.js';
import {
  DEFAULT_EDITOR_INPUT_MODE,
  DEFAULT_NOTIFICATION_PREFS,
  EDITOR_INPUT_MODES,
  type NotificationPrefs,
  type Profile,
  type ProfileClaims,
} from '../../../app/entities.js';
import { pyJsonDumps, type JsonValue } from '../../../domain/py.js';
import { accountRows } from './caps.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow } from './time.js';
import type { AccountRows } from '../../../domain/limits.js';

/** Python's column order, `tailscale_login` for the id. */
export function rowToProfile(r: Row): Profile {
  return {
    tailscale_login: String(r['id']),
    display_name: (r['display_name'] as string | null) ?? null,
    profile_pic_url: (r['profile_pic_url'] as string | null) ?? null,
    created_at: String(r['created_at']),
    last_seen_at: String(r['last_seen_at']),
    notification_prefs: (r['notification_prefs'] as string | null) ?? null,
    editor_input_mode: (r['editor_input_mode'] as string | null) ?? null,
    email: (r['email'] as string | null) ?? null,
    active_byok_provider: (r['active_byok_provider'] as string | null) ?? null,
    desired_retention: r['desired_retention'] == null ? null : Number(r['desired_retention']),
    is_anonymous: Number(r['is_anonymous'] ?? 0),
  };
}

export class SqlPrefsRepo implements PrefsRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  get(): Profile | null {
    const row = this.db.first('SELECT * FROM profile LIMIT 1');
    return row ? rowToProfile(row) : null;
  }

  upsert(id: string, claims: ProfileClaims = {}): Profile {
    const ts = isoNow(this.clock);
    const email = claims.email ?? null;
    const name = claims.displayName ?? null;
    const pic = claims.profilePicUrl ?? null;
    this.db.run(
      `INSERT INTO profile (id, email, display_name, profile_pic_url, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET email = COALESCE(?, profile.email), display_name = COALESCE(?, profile.display_name),
                                     profile_pic_url = COALESCE(?, profile.profile_pic_url), last_seen_at = ?`,
      id,
      email,
      name,
      pic,
      ts,
      ts,
      email,
      name,
      pic,
      ts,
    );
    return this.get()!;
  }

  touch(): void {
    this.db.run('UPDATE profile SET last_seen_at = ?', isoNow(this.clock));
  }

  createAnonymous(id: string, displayName: string): Profile {
    const ts = isoNow(this.clock);
    this.db.run(
      'INSERT INTO profile (id, display_name, email, created_at, last_seen_at, is_anonymous) VALUES (?, ?, NULL, ?, ?, 1)',
      id,
      displayName,
      ts,
      ts,
    );
    return this.get()!;
  }

  accountRows(): AccountRows {
    return accountRows(this.db);
  }

  getEditorInputMode(): string {
    const row = this.db.first<{ editor_input_mode: string | null }>('SELECT editor_input_mode FROM profile LIMIT 1');
    const v = row?.editor_input_mode;
    return v && (EDITOR_INPUT_MODES as readonly string[]).includes(v) ? v : DEFAULT_EDITOR_INPUT_MODE;
  }

  setEditorInputMode(mode: string): void {
    if (!(EDITOR_INPUT_MODES as readonly string[]).includes(mode)) throw new RangeError(`unknown editor input mode ${JSON.stringify(mode)}`);
    this.db.run('UPDATE profile SET editor_input_mode = ?', mode);
  }

  getNotificationPrefs(): NotificationPrefs {
    const row = this.db.first<{ notification_prefs: string | null }>('SELECT notification_prefs FROM profile LIMIT 1');
    const raw = row?.notification_prefs;
    const saved = raw ? (JSON.parse(raw) as Partial<NotificationPrefs>) : {};
    return { ...DEFAULT_NOTIFICATION_PREFS, ...saved };
  }

  setNotificationPrefs(prefs: NotificationPrefs): void {
    this.db.run('UPDATE profile SET notification_prefs = ?', pyJsonDumps(prefs as unknown as JsonValue));
  }

  getActiveByokProvider(): string | null {
    const row = this.db.first<{ active_byok_provider: string | null }>('SELECT active_byok_provider FROM profile LIMIT 1');
    return row?.active_byok_provider || null;
  }

  setActiveByokProvider(provider: string | null): void {
    this.db.run('UPDATE profile SET active_byok_provider = ?', provider);
  }

  getDesiredRetention(): number | null {
    const row = this.db.first<{ desired_retention: number | null }>('SELECT desired_retention FROM profile LIMIT 1');
    return row && row.desired_retention != null ? Number(row.desired_retention) : null;
  }

  setDesiredRetention(retention: number | null): void {
    this.db.run('UPDATE profile SET desired_retention = ?', retention);
  }

  getIdBase(): number {
    const row = this.db.first<{ id_base: number }>('SELECT id_base FROM profile LIMIT 1');
    return Number(row?.id_base ?? 0);
  }

  setIdBase(base: number): void {
    this.db.run('UPDATE profile SET id_base = ?', base);
  }
}
