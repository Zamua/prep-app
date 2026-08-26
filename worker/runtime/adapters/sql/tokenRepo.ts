// `api_tokens`, transcribed from prep/api/repo.py. Hashing and masking are
// the PAT adapter's; the repo stores what it is given.
import type { Clock, TokenRepo } from '../../../app/ports.js';
import type { ApiTokenMetadata } from '../../../app/entities.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow } from './time.js';

const toMeta = (r: Row): ApiTokenMetadata => ({
  id: Number(r['id']),
  label: (r['label'] as string | null) ?? null,
  key_prefix: String(r['key_prefix']),
  created_at: String(r['created_at']),
  last_used_at: (r['last_used_at'] as string | null) ?? null,
});

export class SqlTokenRepo implements TokenRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  insert(tokenHash: string, keyPrefix: string, label: string | null): ApiTokenMetadata {
    const ts = isoNow(this.clock);
    const id = this.db.insert(
      'INSERT INTO api_tokens (token_hash, label, key_prefix, created_at, last_used_at) VALUES (?, ?, ?, ?, NULL)',
      tokenHash,
      label,
      keyPrefix,
      ts,
    );
    return { id, label, key_prefix: keyPrefix, created_at: ts, last_used_at: null };
  }

  list(): ApiTokenMetadata[] {
    return this.db.all('SELECT id, label, key_prefix, created_at, last_used_at FROM api_tokens ORDER BY created_at DESC').map(toMeta);
  }

  delete(tokenId: number): boolean {
    return this.db.run('DELETE FROM api_tokens WHERE id = ?', tokenId) > 0;
  }

  lookup(tokenHash: string): { id: number } | null {
    const row = this.db.first<{ id: number }>('SELECT id FROM api_tokens WHERE token_hash = ?', tokenHash);
    if (!row) return null;
    this.db.run('UPDATE api_tokens SET last_used_at = ? WHERE id = ?', isoNow(this.clock), row.id);
    return { id: Number(row.id) };
  }
}
