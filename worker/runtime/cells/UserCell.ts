// One user's cell: their SQLite behind the repositories, the route table,
// the parity seed and the three-step deletion. Until lanes C and D land
// their routes, an unmatched request replays the recorded Python page.
import { DurableObject } from 'cloudflare:workers';
import type { CellSnapshot, InstantCard, InstantDeckResult, Profile, ProfileClaims, TombstoneReason } from '../../app/entities.js';
import type { CarriedPreferences, Clock, Precheck, UserCellRpc, UserRepos } from '../../app/ports.js';
import { derive } from '../../app/viewmodels/derive.js';
import { RowCapReached } from '../../domain/limits.js';
import { BAD_TOKEN, NO_USER } from '../../domain/pat.js';
import { carryPreferences, type Row } from '../../domain/merge.js';
import { appBase } from '../appBase.js';
import { ANON_COOKIE_HEADER, clockFor, compose, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import { errorPage } from '../errors.js';
import type { CellStorage } from '../storage.js';
import { isoUtc } from '../../domain/py.js';
import { pageContext } from '../../app/pageContext.js';
import {
  applyGate,
  capRefusal,
  gateRefusal,
  identityFrom,
  matchRoute,
  PAT_HASH_HEADER,
  SignInRequired,
  TokenRequired,
  toResponse,
  wantsJson,
  type CellIdentity,
  type CellPorts,
  type Handled,
  type Route,
} from './router.js';
import { apiRoutes } from './routes/api.js';
import { pageRoutes } from './routes/pages.js';
import { createUser, PROFILES, type Delta } from './seed/index.js';

export interface ParityState {
  profile: string;
  flags: string[];
}

const STATE_KEY = 'parity';
const SESSION_COUNTER_KEY = 'parity_session_counter';
/** A profile with no rows: the seed wipes and returns `{}`. */
const ANONYMOUS_PROFILE = 'anonymous';

export class UnknownProfile extends Error {}

export const TOMBSTONED_HEADER = 'x-prep-tombstoned';
/** FastAPI's detail for an unauthenticated request. */
export const NOT_AUTHENTICATED = 'not authenticated';

const MS: Record<keyof Delta, number> = { days: 86_400_000, hours: 3_600_000, minutes: 60_000 };

export class UserCell extends DurableObject<Env> implements UserCellRpc {
  private readonly c: Composition;
  private readonly storage: CellStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.c = compose(env);
    this.storage = ctx.storage as unknown as CellStorage;
    void ctx.blockConcurrencyWhile(async () => {
      this.c.migrateUserCell(this.storage);
    });
  }

  private repos(clock: Clock = this.c.clock): UserRepos {
    return this.c.userRepos(this.storage, clock);
  }

  private routes(): readonly Route[] {
    return [...pageRoutes, ...apiRoutes];
  }

  /** Everything a handler needs beyond its repositories, from the one root. */
  private ports(request: Request, subject: string): CellPorts {
    const c = this.c;
    return {
      random: c.randoms.tokens,
      hasher: c.hasher,
      agent: c.agent,
      runner: c.runner,
      cipher: c.cipher,
      openRouter: c.openRouter,
      webPush: c.webPush,
      authProvider: c.authProvider,
      authUrls: c.authUrls,
      freeTierConfigured: c.freeTierConfigured,
      vapidPublicKey: c.vapidPublicKey,
      appBase: appBase(request),
      previousIds: () => c.directory.previousIds(subject),
    };
  }

  // ---- requests -------------------------------------------------------------

  async fetch(request: Request): Promise<Response> {
    const c = this.c;
    const clock = clockFor(c, request);
    const repos = this.repos(clock);
    const tomb = repos.tombstone.get();
    if (tomb) return Response.json({ tombstoned: tomb.reason }, { status: 410, headers: { [TOMBSTONED_HEADER]: tomb.reason } });

    const url = new URL(request.url);
    const identity = identityFrom(request);
    if (!identity) return errorPage(c.renderer, c.buildToken, 404, request, 'Not Found');
    const credential = this.checkCredential(identity, request, repos);
    if (credential) return credential;

    const match = matchRoute(this.routes(), request.method, url.pathname);
    if (!match) return this.replayFixture(request, url);
    try {
      applyGate(match.route.gate, identity);
    } catch (e) {
      if (e instanceof SignInRequired || e instanceof TokenRequired) return gateRefusal(e, request);
      throw e;
    }
    await this.touch(identity, repos, clock);

    let handled: Handled;
    try {
      handled = await match.route.handler({ request, url, params: match.params, identity, repos, clock, ports: this.ports(request, identity.subject) });
    } catch (e) {
      if (e instanceof RowCapReached) return capRefusal(e, request, (status, detail) => errorPage(c.renderer, c.buildToken, status, request, detail));
      if (e instanceof SignInRequired || e instanceof TokenRequired) return gateRefusal(e, request);
      throw e;
    }
    const base = pageContext(repos, {
      buildToken: c.buildToken,
      appBase: appBase(request),
      authProvider: c.authProvider,
      freeTierConfigured: c.freeTierConfigured,
      urls: c.authUrls,
    });
    return toResponse(handled, (template, context) => c.renderer.render(template, derive(template, { ...base, ...context })));
  }

  /**
   * What the entry worker could only assert. A token proves nothing until
   * its hash matches a row here, and a cookie naming an id whose row is gone
   * (reaped, or no longer flagged anonymous) is a dead credential: honouring
   * the cookie alone would turn a cleared flag into an unrestricted session
   * for whoever still holds it.
   */
  private checkCredential(identity: CellIdentity, request: Request, repos: UserRepos): Response | null {
    if (identity.kind === 'pat') {
      const hash = request.headers.get(PAT_HASH_HEADER);
      if (!hash || !repos.tokens.lookup(hash)) return Response.json({ detail: BAD_TOKEN }, { status: 401 });
      if (repos.prefs.get() === null) return Response.json({ detail: NO_USER }, { status: 401 });
      return null;
    }
    if (identity.kind !== 'anon') return null;
    const profile = repos.prefs.get();
    if (profile !== null && profile.is_anonymous) return null;
    return this.notAuthenticated(request);
  }

  /** 401 plus the ask to forget the cookie that named this cell. */
  private notAuthenticated(request: Request): Response {
    const res = wantsJson(request)
      ? Response.json({ detail: NOT_AUTHENTICATED }, { status: 401 })
      : errorPage(this.c.renderer, this.c.buildToken, 401, request, NOT_AUTHENTICATED);
    res.headers.set(ANON_COOKIE_HEADER, 'clear');
    return res;
  }

  /** `last_seen_at`: an upsert for a provider identity, a touch otherwise. */
  private async touch(identity: CellIdentity, repos: UserRepos, clock: Clock): Promise<void> {
    if (identity.kind === 'anon' || identity.kind === 'pat') {
      repos.prefs.touch();
      return;
    }
    const claims = { email: identity.email, displayName: identity.displayName, profilePicUrl: identity.profilePicUrl };
    if (repos.prefs.get() !== null) {
      repos.prefs.upsert(identity.subject, claims);
      return;
    }
    // First contact: the directory hands out the id block before any row is written.
    const { idx } = await this.c.directory.register(identity.subject, false, isoUtc(clock.now()));
    this.c.seedIdBlock(this.storage, idx);
    repos.prefs.upsert(identity.subject, claims);
    repos.prefs.setIdBase(idx);
  }

  /** Phase 1's recorded pages, by profile and flags, until every route is ported. */
  private async replayFixture(request: Request, url: URL): Promise<Response> {
    const c = this.c;
    const state = (await this.storage.get<ParityState>(STATE_KEY)) ?? null;
    const page = state && c.pages.resolve(state.profile, request.method, url.pathname, state.flags);
    if (!state || !page) return errorPage(c.renderer, c.buildToken, 404, request, 'Not Found');
    const headers: Record<string, string> = {};
    if (page.headers['content-type']) headers['content-type'] = page.headers['content-type'];
    if (page.headers.location) headers.location = page.headers.location;
    const body = page.template
      ? c.renderer.render(page.template, derive(page.template, { ...(page.context ?? {}), app_base: appBase(request) }))
      : (page.body ?? '');
    if (page.sets.length) {
      const flags = [...new Set([...state.flags, ...page.sets])];
      await this.storage.put<ParityState>(STATE_KEY, { profile: state.profile, flags });
    }
    return new Response(body, { status: page.status, headers });
  }

  // ---- the parity seed --------------------------------------------------------

  /**
   * Step one of a re-seed, alone in its RPC: `deleteAll` and a profile's rows
   * in one call cannot both be proven durable, and the write is rejected.
   * Validates first, so an unknown profile costs the cell nothing.
   */
  async wipe(profile: string): Promise<void> {
    if (!PROFILES[profile] && profile !== ANONYMOUS_PROFILE) throw new UnknownProfile(`unknown profile ${JSON.stringify(profile)}`);
    await this.storage.deleteAll();
  }

  /** Step two: the schema and the profile's rows on the cell `wipe` emptied;
   * Python's seed JSON. */
  async seed(profile: string, user: string, at: string | null = null): Promise<Record<string, unknown>> {
    const build = PROFILES[profile];
    if (!build && profile !== ANONYMOUS_PROFILE) throw new UnknownProfile(`unknown profile ${JSON.stringify(profile)}`);
    this.c.migrateUserCell(this.storage);
    this.c.resetIdBlock(this.storage);
    await this.storage.put(SESSION_COUNTER_KEY, 0);
    this.c.resetRandom();
    if (!build) return {};

    const clock = at ? clockFor(this.c, new Request('https://cell.internal/', { headers: { 'x-prep-now': at } })) : this.c.clock;
    const now = clock.now();
    const repos = this.repos(clock);
    createUser(repos, user);
    repos.prefs.setIdBase(0);
    const atDelta = (delta: Delta = {}): string => {
      let ms = 0;
      for (const [k, v] of Object.entries(delta)) ms += (v ?? 0) * MS[k as keyof Delta];
      return isoUtc(new Date(now.getTime() + ms));
    };
    const ids = await build({ repos, user, hasher: this.c.hasher, at: atDelta });
    await this.c.directory.register(user, false, isoUtc(now), { idx: 0 });
    await this.storage.put<ParityState>(STATE_KEY, { profile, flags: [] });
    return { user, profile, now: isoUtc(now), ...ids };
  }

  // ---- RPC -------------------------------------------------------------------

  async precheck(): Promise<Precheck> {
    const repos = this.repos();
    const tomb = repos.tombstone.get();
    if (tomb) return { exists: false, isAnonymous: false, tombstoned: tomb.reason };
    const profile = repos.prefs.get();
    return { exists: profile !== null, isAnonymous: Boolean(profile?.is_anonymous), tombstoned: null };
  }

  async upsert(id: string, claims: ProfileClaims, at: string, idx?: number): Promise<Profile> {
    const repos = this.repos(fixedClock(at));
    if (idx !== undefined && repos.prefs.get() === null) this.c.seedIdBlock(this.storage, idx);
    const profile = repos.prefs.upsert(id, claims);
    if (idx !== undefined && repos.prefs.getIdBase() === 0 && idx !== 0) repos.prefs.setIdBase(idx);
    return profile;
  }

  async lastSeenAt(): Promise<string | null> {
    return this.repos().prefs.get()?.last_seen_at ?? null;
  }

  async dump(): Promise<CellSnapshot> {
    return this.repos().export.dump();
  }

  async importRows(snapshot: CellSnapshot): Promise<Record<string, number>> {
    return this.repos().export.importRows(snapshot, { idempotentBy: 'id' });
  }

  /** The COPY-IF-NULL carry, decided by the domain against this cell's own
   * row: a column the target already chose is never overwritten, so a repeat
   * moves nothing and counts nothing. */
  async carryPreferences(carried: CarriedPreferences): Promise<Record<string, number>> {
    const repos = this.repos();
    const target = repos.prefs.get();
    if (!target) return {};
    const { row, counts } = carryPreferences(carried as unknown as Row, target as unknown as Row);
    if (counts['users.desired_retention']) repos.prefs.setDesiredRetention(row['desired_retention'] as number | null);
    if (counts['users.editor_input_mode']) repos.prefs.setEditorInputMode(String(row['editor_input_mode']));
    return counts;
  }

  async createInstantDeck(input: {
    displayName: string;
    cards: readonly InstantCard[];
    mint: { id: string; displayName: string; idx: number } | null;
    at: string;
  }): Promise<InstantDeckResult> {
    const repos = this.repos(fixedClock(input.at));
    if (input.mint) this.c.seedIdBlock(this.storage, input.mint.idx);
    const result = repos.instant.createInstantDeck(input.displayName, input.cards, input.mint ? { id: input.mint.id, displayName: input.mint.displayName } : null);
    if (input.mint) repos.prefs.setIdBase(input.mint.idx);
    return result;
  }

  /** Step one of deletion: the size, `deleteAll`, then the tombstone alone. Retry-safe. */
  async destroy(reason: TombstoneReason, at: string): Promise<void> {
    const repos = this.repos();
    if (repos.tombstone.get()) return;
    const formerBytes = repos.tombstone.databaseSize();
    await this.storage.deleteAll();
    repos.tombstone.write(reason, at, formerBytes);
  }

  /** Step two, its own RPC: zero-fill to the former size so the next snapshot holds no row bodies. */
  async scrub(at: string): Promise<void> {
    this.repos().tombstone.scrub(at);
  }
}

function fixedClock(at: string): Clock {
  const d = new Date(at);
  return { now: () => new Date(d.getTime()) };
}
