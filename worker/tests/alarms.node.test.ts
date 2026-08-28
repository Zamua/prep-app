// The user alarm against a real celld node. The fake bus proves the plan;
// only a node proves that a cell nobody is addressing comes back on its own,
// with the wake the last activation wrote and no request to remind it.
//
// Slow by construction (a build, a deploy, and minutes of real wall time), so
// it asks to be run rather than joining the default suite:
//   PREP_ALARM_NODE=1 PREP_CRASH_PORT=8797 PREP_CRASH_STATE_DIR=/private/tmp/prep-alarm-state \
//     PREP_DEV_S3_BUCKET=prep-alarm npx vitest run tests/alarms.node.test.ts \
//     --testTimeout=600000 --hookTimeout=900000
// with the scratch MinIO up. Its own bucket, port and state directory keep it
// off the crash suite's, so the two can run side by side. Skipped without the
// flag, and without celld.
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { BASE, INTERNAL_TOKEN, killNode, restartNode, sleep, startNode, stopNode } from './crash/node.js';

const CELLD = process.env['CELLD_BIN'] ?? join(homedir(), '.local', 'bin', 'celld');
const suite = process.env['PREP_ALARM_NODE'] === '1' && existsSync(CELLD) ? describe : describe.skip;

/** `CELLD_WAKER_TICK_MS`, the orphan-alarm scan; measured at a minute. */
const WAKER_TICK_MS = Number(process.env['CELLD_WAKER_TICK_MS'] ?? 60_000);
/** The shortest interval a trivia deck accepts. */
const DECK_INTERVAL_MS = 60_000;

/**
 * One login for the whole file: the seed pins the directory's id
 * block 0 to whoever it seeds, so a second name would collide with the first.
 * Each test re-seeds it, which wipes the cell it is about to use.
 */
const LOGIN = 'wake@example.test';

const identity = (login: string): Record<string, string> => ({
  'tailscale-user-login': login,
  'tailscale-user-name': 'Seed',
  'x-internal-token': INTERNAL_TOKEN,
});

/**
 * A cell is briefly out of reach after a restart, and a write it cannot yet
 * prove durable comes back as `DurabilityUnproven`. Both are retryable
 * refusals, so every call here rides them out; each one is idempotent.
 */
async function call(path: string, init: RequestInit, seconds = 30): Promise<Response> {
  const until = Date.now() + seconds * 1000;
  let last = '';
  for (;;) {
    try {
      const res = await fetch(`${BASE}${path}`, { redirect: 'manual', ...init });
      if (res.status < 500) return res;
      last = `${res.status} ${(await res.text()).slice(0, 200)}`;
    } catch (e) {
      last = e instanceof Error ? e.message : String(e);
    }
    if (Date.now() > until) throw new Error(`${path}: ${last}`);
    await sleep(500);
  }
}

interface Seeded {
  decks: { trivia: { id: number; slug: string } };
}

async function seed(login: string): Promise<Seeded> {
  const res = await call('/_test/seed', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-internal-token': INTERNAL_TOKEN },
    body: JSON.stringify({ user: login, profile: 'reader' }),
  });
  if (!res.ok) throw new Error(`seed ${login}: ${res.status} ${await res.text()}`);
  return (await res.json()) as Seeded;
}

/** The notification log page. Each trivia fire leaves one deep link in it, so
 * counting them counts the fires without any endpoint built for the test. */
async function triviaFires(login: string, slug: string): Promise<number> {
  const res = await call('/notify/log', { headers: identity(login) });
  if (!res.ok) throw new Error(`log ${login}: ${res.status} ${await res.text()}`);
  const html = await res.text();
  return html.split(`/trivia/session/${slug}?cards=`).length - 1;
}

/** Resets the deck to the one-minute cadence, which also clears the ignored
 * streak, so the next wake is a minute after the last fire and not two. */
async function setInterval(login: string, deckId: number, minutes: number): Promise<void> {
  const res = await call(`/trivia/decks/${deckId}/interval`, {
    method: 'POST',
    headers: { ...identity(login), 'content-type': 'application/x-www-form-urlencoded' },
    body: `minutes=${minutes}`,
  });
  if (res.status >= 400) throw new Error(`interval ${deckId}: ${res.status} ${await res.text()}`);
}

/**
 * Drops the session the last fire picked. A fire leaves an active queue whose
 * `last_active` is that instant, and a deck with one is left alone for five
 * minutes so a user mid-session is not nagged; clearing it puts the deck's
 * own interval back in charge of the next wake.
 */
async function abandonSession(login: string, slug: string): Promise<void> {
  const res = await call(`/trivia/session/${slug}/abandon`, { method: 'POST', headers: identity(login) });
  if (res.status >= 400) throw new Error(`abandon ${slug}: ${res.status} ${await res.text()}`);
}

/** Polls until the deck has fired at least `wanted` times. */
async function untilFires(login: string, slug: string, wanted: number, seconds: number): Promise<number> {
  const deadline = Date.now() + seconds * 1000;
  for (;;) {
    const seen = await triviaFires(login, slug);
    if (seen >= wanted) return seen;
    if (Date.now() > deadline) throw new Error(`${login}: saw ${seen} trivia fires, wanted ${wanted}`);
    await sleep(2_000);
  }
}

suite('a cell nobody is addressing', () => {
  beforeAll(() => {
    startNode();
  }, 900_000);

  afterAll(() => {
    stopNode();
  });

  it('fires its next trivia notification while dormant, on the interval it stored', async () => {
    const seeded = await seed(LOGIN);
    const deck = seeded.decks.trivia;
    // The seed itself arms the cell: a deck that has never been notified is
    // due, so the first fire needs no request of its own.
    const first = await untilFires(LOGIN, deck.slug, 1, 60);
    await setInterval(LOGIN, deck.id, 1);
    await abandonSession(LOGIN, deck.slug);

    // Nothing addresses the cell from here. celld is free to evict it; the
    // alarm it wrote is what brings it back.
    await sleep(DECK_INTERVAL_MS + 60_000);

    expect(await triviaFires(LOGIN, deck.slug)).toBeGreaterThan(first);
  }, 600_000);

  it('fires it after the node that armed it has died and come back', async () => {
    const seeded = await seed(LOGIN);
    const deck = seeded.decks.trivia;
    await untilFires(LOGIN, deck.slug, 1, 60);
    await setInterval(LOGIN, deck.id, 1);
    await abandonSession(LOGIN, deck.slug);
    const before = await triviaFires(LOGIN, deck.slug);

    killNode();
    restartNode();

    // No request wakes it: the orphan-alarm scan does, and the alarm the dead
    // node had armed is still the one that is due.
    await sleep(WAKER_TICK_MS + DECK_INTERVAL_MS + 60_000);

    expect(await triviaFires(LOGIN, deck.slug)).toBeGreaterThan(before);
  }, 600_000);
});
