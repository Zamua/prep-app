// `byok_credentials`. The crypto is not here: the `Cipher` port wraps this
// repo, so a plaintext key never reaches SQL.
import type { ByokRepo, Clock } from '../../../app/ports.js';
import type { CredentialMetadata } from '../../../app/entities.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow } from './time.js';

export class SqlByokRepo implements ByokRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  store(provider: string, ciphertext: string, keyPrefix: string): CredentialMetadata {
    const ts = isoNow(this.clock);
    this.db.run(
      `INSERT INTO byok_credentials (provider, ciphertext, key_prefix, created_at, last_used_at) VALUES (?, ?, ?, ?, NULL)
       ON CONFLICT (provider) DO UPDATE SET ciphertext = excluded.ciphertext, key_prefix = excluded.key_prefix,
                                           created_at = excluded.created_at, last_used_at = NULL`,
      provider,
      ciphertext,
      keyPrefix,
      ts,
    );
    return { provider, key_prefix: keyPrefix, created_at: ts, last_used_at: null };
  }

  delete(provider: string): boolean {
    return this.db.run('DELETE FROM byok_credentials WHERE provider = ?', provider) > 0;
  }

  touchLastUsed(provider: string): void {
    this.db.run('UPDATE byok_credentials SET last_used_at = ? WHERE provider = ?', isoNow(this.clock), provider);
  }

  getCiphertext(provider: string): string | null {
    const row = this.db.first<{ ciphertext: string }>('SELECT ciphertext FROM byok_credentials WHERE provider = ?', provider);
    return row ? row.ciphertext : null;
  }

  metadata(provider: string): CredentialMetadata | null {
    const row = this.db.first('SELECT key_prefix, created_at, last_used_at FROM byok_credentials WHERE provider = ?', provider);
    if (!row) return null;
    return {
      provider,
      key_prefix: String(row['key_prefix']),
      created_at: String(row['created_at']),
      last_used_at: (row['last_used_at'] as string | null) ?? null,
    };
  }

  listProviders(): string[] {
    return this.db.all<{ provider: string }>('SELECT provider FROM byok_credentials ORDER BY provider').map((r) => r.provider);
  }
}
