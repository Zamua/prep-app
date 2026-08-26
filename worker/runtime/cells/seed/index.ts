// The parity seed profiles (prep/dev/parity_seed.py), written through the
// repositories so ids and timestamps come out as Python's do.
import type { Hasher, UserRepos } from '../../../app/ports.js';
import { profileEmpty } from './empty.js';
import { profileReader } from './reader.js';
import { profileStudy } from './study.js';

export const PARITY_TZ = 'America/New_York';
export const PARITY_DISPLAY_NAME = 'Parity';
export const DEVICE_LABEL = 'iPhone';

export interface Delta {
  days?: number;
  hours?: number;
  minutes?: number;
}

export interface SeedContext {
  repos: UserRepos;
  user: string;
  hasher: Hasher;
  /** The clock shifted by the delta, in the column format. */
  at(delta?: Delta): string;
}

export type SeedProfile = (ctx: SeedContext) => Promise<Record<string, unknown>>;

export const PROFILES: Record<string, SeedProfile> = {
  empty: profileEmpty,
  reader: profileReader,
  study: profileStudy,
};

/** The user row as `create_user` writes it: upserted with the parity name and timezone. */
export function createUser(repos: UserRepos, user: string): void {
  repos.prefs.upsert(user, { email: user, displayName: PARITY_DISPLAY_NAME });
  const prefs = repos.prefs.getNotificationPrefs();
  prefs.tz = PARITY_TZ;
  repos.prefs.setNotificationPrefs(prefs);
}
