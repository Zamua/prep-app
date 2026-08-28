// The seed profiles, written through the repositories so ids and timestamps
// come out of the same code paths a real write does. A cell has no database
// file to open, so a fixture is a profile rather than a dump.
import type { Hasher, UserRepos } from '../../../app/ports.js';
import { profileApiE2e } from './apiE2e.js';
import { profileDeviceWipe } from './deviceWipe.js';
import { profileEmpty } from './empty.js';
import { profileIo } from './io.js';
import { profileMergeAnon } from './mergeAnon.js';
import { profileOfflineE2e } from './offlineE2e.js';
import { profileReader } from './reader.js';
import { profileStudy } from './study.js';
import { profileWorkflows } from './workflows.js';

export const SEED_TZ = 'America/New_York';
export const SEED_DISPLAY_NAME = 'Seed';
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
  workflows: profileWorkflows,
  io: profileIo,
  offline_e2e: profileOfflineE2e,
  device_wipe: profileDeviceWipe,
  merge_anon: profileMergeAnon,
  api_e2e: profileApiE2e,
};

/** Profiles whose cell stands in for a pre-signup visitor. The merge reads
 * `is_anonymous` off the profile row and refuses an account without it, so
 * the flag has to be seeded, not asserted. */
const ANONYMOUS_PROFILES: ReadonlySet<string> = new Set(['merge_anon']);

export const isAnonymousProfile = (profile: string): boolean => ANONYMOUS_PROFILES.has(profile);

/** The user row a seeded run starts from: upserted with the seed name and
 * timezone, or an anonymous row for a visitor profile. */
export function createUser(repos: UserRepos, user: string, profile = ''): void {
  if (isAnonymousProfile(profile)) repos.prefs.createAnonymous(user, 'Guest');
  else repos.prefs.upsert(user, { email: user, displayName: SEED_DISPLAY_NAME });
  const prefs = repos.prefs.getNotificationPrefs();
  prefs.tz = SEED_TZ;
  repos.prefs.setNotificationPrefs(prefs);
}
