// The activity log over an analytics dataset. The actor is written only as a
// truncated HMAC under a deploy secret, and a write never rejects: a deploy
// with no dataset or no salt logs nothing.
import type { ActivityLog } from '../../app/ports.js';
import { actorMessage, dataPoint, type ActivityDataPoint, type ActivityEvent } from '../../domain/activity.js';

export interface ActivityDataset {
  writeDataPoint(point: ActivityDataPoint): void;
}

const HASH_HEX_CHARS = 16;

export const NO_ACTIVITY: ActivityLog = { record: async () => {} };

class DatasetActivityLog implements ActivityLog {
  private key: Promise<CryptoKey> | null = null;
  // A cell serves one user, so this holds one entry for its whole life.
  private readonly hashes = new Map<string, string>();

  constructor(
    private readonly dataset: ActivityDataset,
    private readonly salt: string,
  ) {}

  async record(actorId: string, events: readonly ActivityEvent[]): Promise<void> {
    if (events.length === 0) return;
    try {
      const hash = await this.hashOf(actorId);
      for (const event of events) this.dataset.writeDataPoint(dataPoint(hash, event));
    } catch {
      // A lost event must not fail the request it describes.
    }
  }

  private async hashOf(actorId: string): Promise<string> {
    const known = this.hashes.get(actorId);
    if (known) return known;
    const encoder = new TextEncoder();
    this.key ??= crypto.subtle.importKey('raw', encoder.encode(this.salt), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const mac = new Uint8Array(await crypto.subtle.sign('HMAC', await this.key, encoder.encode(actorMessage(actorId))));
    const hex = Array.from(mac, (b) => b.toString(16).padStart(2, '0')).join('').slice(0, HASH_HEX_CHARS);
    this.hashes.set(actorId, hex);
    return hex;
  }
}

export function activityLogOf(dataset: ActivityDataset | undefined, salt: string | undefined): ActivityLog {
  const secret = (salt ?? '').trim();
  return dataset && secret ? new DatasetActivityLog(dataset, secret) : NO_ACTIVITY;
}
