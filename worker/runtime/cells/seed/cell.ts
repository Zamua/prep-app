// The seed's side of a user cell: the test-only RPCs `/_test/*` drives. A
// Durable Object answers RPCs only through its own methods, so the cell
// class keeps one forwarding line per method and the work lives here.
import type { Clock, UserRepos } from '../../../app/ports.js';
import { isoUtc } from '../../../domain/time.js';
import { clockFor, SESSION_COUNTER_KEY, type Composition } from '../../compose.js';
import type { ReadCachedStorage } from '../../storage.js';
import { NOW_HEADER } from '../router.js';
import { createUser, isAnonymousProfile, PROFILES, type Delta, type SeedProfile } from './index.js';

export class UnknownProfile extends Error {}

/** Seeds an account with no rows: the anonymous accounts a run minted. */
export const ANONYMOUS_PROFILE = 'anonymous';

/** The test-only RPCs a user cell answers. */
export interface SeedRpc {
  wipe(profile: string): Promise<void>;
  seed(profile: string, user: string, at?: string | null): Promise<Record<string, unknown>>;
  forgetJob(jobId: string, at: string): Promise<void>;
}

/** What the seed reaches inside the cell. */
export interface SeedHost {
  c: Composition;
  storage: ReadCachedStorage;
  repos(clock?: Clock): UserRepos;
  reposAt(at: string): UserRepos;
  ensureAlarm(): Promise<void>;
}

const GONE_STATUS = 'gone';
const MS: Record<keyof Delta, number> = { days: 86_400_000, hours: 3_600_000, minutes: 60_000 };

/** The profile's builder; null for the empty anonymous profile. */
function profileOf(profile: string): SeedProfile | null {
  const build = PROFILES[profile];
  if (!build && profile !== ANONYMOUS_PROFILE) throw new UnknownProfile(`unknown profile ${JSON.stringify(profile)}`);
  return build ?? null;
}

/**
 * Step one of a re-seed, alone in its RPC: `deleteAll` and a profile's rows
 * in one call cannot both be proven durable, and the write is rejected.
 * Validates first, so an unknown profile costs the cell nothing.
 */
export async function wipe(host: SeedHost, profile: string): Promise<void> {
  profileOf(profile);
  await host.storage.deleteAll();
}

/** Step two: the schema and the profile's rows on the cell `wipe` emptied,
 * returned as the delta the caller asserts against. */
export async function seed(host: SeedHost, profile: string, user: string, at: string | null): Promise<Record<string, unknown>> {
  const build = profileOf(profile);
  const { c, storage } = host;
  c.migrateUserCell(storage);
  c.resetIdBlock(storage);
  await storage.put(SESSION_COUNTER_KEY, 0);
  c.resetRandom();
  if (!build) return {};

  const clock = at ? clockFor(c, new Request('https://cell.internal/', { headers: { [NOW_HEADER]: at } })) : c.clock;
  const now = clock.now();
  const repos = host.repos(clock);
  createUser(repos, user, profile);
  repos.prefs.setIdBase(0);
  const atDelta = (delta: Delta = {}): string => {
    let ms = 0;
    for (const [k, v] of Object.entries(delta)) ms += (v ?? 0) * MS[k as keyof Delta];
    return isoUtc(new Date(now.getTime() + ms));
  };
  const ids = await build({ repos, pins: c.seedPins(storage), user, hasher: c.hasher, at: atDelta });
  await c.directory.register(user, isAnonymousProfile(profile), isoUtc(now), { idx: 0 });
  await host.ensureAlarm();
  return { user, profile, now: isoUtc(now), ...ids };
}

/**
 * A job whose record is gone: the badge row closed on the terminal status
 * and the progress row a poll answers from dropped out from under it. The
 * only way a test reaches the partial's `gone` branch.
 */
export function forgetJob(host: SeedHost, jobId: string, at: string): void {
  const repos = host.reposAt(at);
  repos.tx.sync(() => {
    repos.jobs.updateStatus(jobId, GONE_STATUS);
    repos.jobs.setTerminalAt(jobId);
    repos.jobProgress.remove(jobId);
  });
}
