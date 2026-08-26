// The HTML routes a user cell serves (docs/PHASE-3.md C). Each entry names
// a use case; this module only parses the request into the shape the app
// layer takes and turns its answer back into the router's `Handled`.
import { dashboard } from '../../../app/dashboard/index.js';
import * as decks from '../../../app/decks/pages.js';
import { setNotificationsEnabled } from '../../../app/decks/service.js';
import { AppError } from '../../../app/errors.js';
import type { PageRequest, PageResult } from '../../../app/pageResult.js';
import type { UserRepos } from '../../../app/ports.js';
import { notificationLog, notifySettings } from '../../../app/notify/routes.js';
import * as account from '../../../app/settings/account.js';
import * as agentSettings from '../../../app/settings/agent.js';
import * as apiSettings from '../../../app/settings/api.js';
import * as editor from '../../../app/settings/editor.js';
import * as openrouter from '../../../app/settings/openrouter.js';
import * as srs from '../../../app/settings/srs.js';
import * as shell from '../../../app/study/shell.js';
import * as trivia from '../../../app/trivia/pages.js';
import { isoUtc } from '../../../domain/py.js';
import { errorContext } from '../../errors.js';
import { HTML, type CellPorts, type CellRequest, type Gate, type Handled, type Route } from '../router.js';

/** A urlencoded body, parsed once. Any other content type posts no fields,
 * which is what FastAPI's `Form(...)` also sees for a body it cannot read. */
async function formOf(request: Request): Promise<URLSearchParams> {
  if (request.method === 'GET' || request.method === 'HEAD') return new URLSearchParams();
  const type = request.headers.get('content-type') ?? '';
  if (type.startsWith('multipart/form-data')) {
    const data = await request.formData();
    const out = new URLSearchParams();
    for (const [k, v] of data.entries()) out.append(k, typeof v === 'string' ? v : '');
    return out;
  }
  if (!type.startsWith('application/x-www-form-urlencoded')) return new URLSearchParams();
  return new URLSearchParams(await request.text());
}

function cookiesOf(request: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (request.headers.get('cookie') ?? '').split(';')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    out[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
  }
  return out;
}

async function pageRequestOf(req: CellRequest): Promise<PageRequest> {
  const hxHeader = req.request.headers.get('hx-request');
  return {
    params: req.params,
    query: req.url.searchParams,
    form: await formOf(req.request),
    htmx: hxHeader === 'true',
    hxHeader,
    userAgent: req.request.headers.get('user-agent'),
    cookies: cookiesOf(req.request),
    now: isoUtc(req.clock.now()),
  };
}

/** The Referer when it is same-origin, else the route's own default: a
 * cross-site Referer is an open-redirect target, not a destination. */
function backTo(req: CellRequest, fallback: string): string {
  const referer = req.request.headers.get('referer') ?? '';
  if (!referer) return fallback;
  try {
    const url = new URL(referer);
    if (url.host !== req.url.host) return fallback;
    return url.pathname + (url.search || '');
  } catch {
    return fallback;
  }
}

function handled(result: PageResult, req: CellRequest): Handled {
  if ('redirect' in result) {
    return { redirect: result.back ? backTo(req, result.redirect) : result.redirect, status: result.status ?? 303, headers: result.headers };
  }
  if ('page' in result) return { page: result.page, context: result.context, status: result.status, headers: result.headers };
  if ('json' in result) return { json: result.json, status: result.status, headers: result.headers };
  if ('text' in result) return { text: result.text, status: result.status, headers: { 'content-type': HTML, ...result.headers } };
  return { empty: true, status: result.status, headers: result.headers };
}

/** Python raises `HTTPException` and the shared handler renders the error
 * page; here the page carries the caller's own context, so the masthead
 * still shows who is signed in. */
function errorPageOf(e: AppError, req: CellRequest): Handled {
  return { page: 'error.html', context: errorContext(e.status, req.url.pathname, e.detail), status: e.status };
}

interface Ctx {
  repos: UserRepos;
  ports: CellPorts;
  subject: string;
}

type Handler = (req: PageRequest, ctx: Ctx) => PageResult | Promise<PageResult>;

function route(method: string, pattern: string, gate: Gate, handler: Handler): Route {
  return {
    method,
    pattern,
    gate,
    async handler(req: CellRequest): Promise<Handled> {
      const parsed = await pageRequestOf(req);
      try {
        return handled(await handler(parsed, { repos: req.repos, ports: req.ports, subject: req.identity.subject }), req);
      } catch (e) {
        if (e instanceof AppError) return errorPageOf(e, req);
        throw e;
      }
    },
  };
}

const deckDeps = (ports: CellPorts) => ({ freeTierConfigured: ports.freeTierConfigured, random: ports.random, runner: ports.runner });
const triviaDeps = (repos: UserRepos, ports: CellPorts) => ({ repos, agent: ports.agent, runner: ports.runner });
const shellDeps = (repos: UserRepos, ports: CellPorts) => ({ repos, signInUrl: ports.authUrls.signIn });
const accountDeps = (ports: CellPorts) => ({ authProvider: ports.authProvider, deleter: null });
const notifyDeps = (repos: UserRepos, ports: CellPorts) => ({ repos, webPush: ports.webPush, vapidPublicKey: ports.vapidPublicKey });
const openRouterDeps = (ports: CellPorts) => ({
  freeTierConfigured: ports.freeTierConfigured,
  cipher: ports.cipher,
  auth: ports.openRouter,
  appBase: ports.appBase,
});

export const pageRoutes: readonly Route[] = [
  // The dashboard. A visitor never reaches a cell, so the landing page
  // stays the entry worker's.
  route('GET', '/', 'signedIn', (_p, { repos }) => ({ page: 'index.html', context: dashboard(repos) })),

  // Deck creation.
  route('GET', '/decks/new', 'signedIn', () => decks.deckNewChooser()),
  route('GET', '/decks/new/srs', 'signedIn', () => decks.deckNewSrsForm()),
  route('GET', '/decks/new/trivia', 'signedIn', () => decks.deckNewTriviaForm()),
  route('POST', '/decks/new/srs', 'signedIn', (p, { repos, ports }) => decks.deckNewSrsCreate(repos, p, deckDeps(ports))),
  route('POST', '/decks/new/trivia', 'signedIn', (p, { repos, ports }) => decks.deckNewTriviaCreate(repos, p, deckDeps(ports))),

  // One deck. Every sub-resource is declared before `/deck/{name}` so a
  // path segment is never mistaken for a deck slug.
  route('POST', '/deck/{name}/delete', 'signedIn', (p, { repos }) => decks.deckDelete(repos, p)),
  route('POST', '/deck/{name}/topic', 'signedIn', (p, { repos }) => decks.deckUpdateTopic(repos, p)),
  route('POST', '/deck/{name}/rename', 'signedIn', (p, { repos }) => decks.deckRename(repos, p)),
  route('GET', '/deck/{name}/edit-with-ai', 'signedIn', (p, { repos }) => decks.deckEditWithAi(repos, p)),
  route('GET', '/deck/{name}/edit-with-claude', 'signedIn', (p) => decks.deckEditWithClaude(p)),
  route('GET', '/deck/{name}/split', 'signedIn', (p, { repos }) => decks.deckSplitForm(repos, p)),
  route('POST', '/deck/{name}/split', 'signedIn', (p, { repos }) => decks.deckSplitSubmit(repos, p)),
  route('POST', '/deck/{name}/pin', 'signedIn', (p, { repos }) => decks.deckPin(repos, p)),
  route('POST', '/deck/{name}/retention', 'signedIn', (p, { repos }) => decks.deckRetention(repos, p)),
  route('POST', '/deck/{name}/notifications', 'signedIn', (p, { repos }) => decks.deckNotifications(repos, p)),
  route('GET', '/deck/{name}/export', 'signedIn', (p, { repos }) => decks.deckExportHub(repos, p)),
  route('GET', '/deck/{name}/question/new', 'signedIn', (p, { repos }) => decks.questionNewForm(repos, p)),
  route('POST', '/deck/{name}/question/new', 'signedIn', (p, { repos }) => decks.questionNewSubmit(repos, p)),
  route('GET', '/deck/{name}', 'signedIn', (p, { repos }) => decks.deckView(repos, p)),

  // Questions.
  route('GET', '/question/{qid}/edit', 'signedIn', (p, { repos }) => decks.questionEditForm(repos, p)),
  route('POST', '/question/{qid}/edit', 'signedIn', (p, { repos }) => decks.questionEditSubmit(repos, p)),
  route('POST', '/question/{qid}/suspend', 'signedIn', (p, { repos }) => decks.questionSuspend(repos, p)),
  route('POST', '/question/{qid}/unsuspend', 'signedIn', (p, { repos }) => decks.questionUnsuspend(repos, p)),
  route('POST', '/question/{qid}/improve', 'signedIn', (p, { repos, ports }) => decks.questionImprove(repos, p, deckDeps(ports))),

  // Settings.
  route('GET', '/settings/agent', 'signedIn', (_p, { repos, ports }) => agentSettings.renderAgentSettings(repos, ports.freeTierConfigured)),
  route('GET', '/settings/agent/openrouter/start', 'signedIn', (_p, { ports }) => openrouter.openrouterStart(openRouterDeps(ports))),
  route('GET', '/settings/agent/openrouter/callback', 'signedIn', (p, { repos, ports }) =>
    openrouter.openrouterCallback(repos, p, openRouterDeps(ports)),
  ),
  route('POST', '/settings/agent/byok/{provider}/connect', 'signedIn', (p, { repos, ports }) =>
    agentSettings.byokConnect(repos, p, { freeTierConfigured: ports.freeTierConfigured, cipher: ports.cipher }),
  ),
  route('POST', '/settings/agent/byok/{provider}/disconnect', 'signedIn', (p, { repos, ports }) =>
    agentSettings.byokDisconnect(repos, p, ports.freeTierConfigured),
  ),
  route('POST', '/settings/agent/byok/{provider}/use', 'signedIn', (p, { repos, ports }) => agentSettings.byokUse(repos, p, ports.freeTierConfigured)),
  route('GET', '/settings/srs', 'signedIn', (_p, { repos }) => srs.srsSettings(repos)),
  route('POST', '/settings/srs', 'signedIn', (p, { repos }) => srs.srsSettingsSave(repos, p)),
  route('GET', '/settings/editor', 'signedIn', (_p, { repos }) => editor.editorSettings(repos)),
  route('POST', '/settings/editor', 'signedIn', (p, { repos }) => editor.editorSettingsSave(repos, p)),
  route('GET', '/settings/api', 'signedIn', (_p, { repos }) => apiSettings.apiSettings(repos)),
  route('POST', '/settings/api/tokens', 'signedIn', (p, { repos, ports, subject }) =>
    apiSettings.apiTokenCreate(repos, p, { subject, random: ports.random, hasher: ports.hasher }),
  ),
  route('POST', '/settings/api/tokens/{token_id}/delete', 'signedIn', (p, { repos }) => apiSettings.apiTokenDelete(repos, p)),
  route('GET', '/settings/account', 'signedIn', (_p, { ports }) => account.accountSettings(accountDeps(ports))),
  route('POST', '/settings/account/delete', 'signedIn', (p, { repos, ports }) => account.accountDelete(repos, p, accountDeps(ports))),

  // Notifications: the two pages; the prefs and subscribe endpoints are the
  // JSON surface.
  route('GET', '/notify', 'signedIn', (_p, { repos, ports }) => notifySettings(notifyDeps(repos, ports))),
  route('GET', '/notify/log', 'signedIn', (_p, { repos, ports }) => notificationLog(notifyDeps(repos, ports))),

  // Trivia. The session and deck sub-paths precede `/trivia/{question_id}`.
  route('GET', '/trivia/session/{deck_name}', 'signedIn', (p, { repos, ports }) => trivia.triviaSession(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/answer', 'signedIn', (p, { repos, ports }) => trivia.triviaSessionAnswer(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/abandon', 'signedIn', (p, { repos, ports }) => trivia.triviaSessionAbandon(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/snooze', 'signedIn', (p, { repos, ports }) => trivia.triviaSessionSnooze(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/override', 'signedIn', (p, { repos, ports }) => trivia.triviaSessionOverride(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/regrade', 'signedIn', (p, { repos, ports }) => trivia.triviaSessionRegrade(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/mute', 'signedIn', (p, { repos, ports }) => trivia.triviaDeckMute(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/unmute', 'signedIn', (p, { repos, ports }) => trivia.triviaDeckUnmute(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/notifications', 'signedIn', (p, { repos, ports }) =>
    trivia.triviaDeckNotifications(p, triviaDeps(repos, ports), (id, enabled) => setNotificationsEnabled(repos, id, enabled)),
  ),
  route('POST', '/trivia/decks/{deck_id}/interval', 'signedIn', (p, { repos, ports }) => trivia.triviaDeckInterval(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/session_size', 'signedIn', (p, { repos, ports }) => trivia.triviaDeckSessionSize(p, triviaDeps(repos, ports))),
  route('GET', '/trivia/{question_id}', 'signedIn', (p, { repos, ports }) => trivia.triviaCard(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/answer', 'signedIn', (p, { repos, ports }) => trivia.triviaAnswer(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/regrade', 'signedIn', (p, { repos, ports }) => trivia.triviaCardRegrade(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/override', 'signedIn', (p, { repos, ports }) => trivia.triviaCardOverride(p, triviaDeps(repos, ports))),

  // The study shell.
  route('POST', '/study/{name}/begin', 'signedIn', (p, { repos, ports }) => shell.studyBegin(p, shellDeps(repos, ports))),
  route('GET', '/study/{name}', 'signedIn', (p, { repos, ports }) => shell.studyView(p, shellDeps(repos, ports))),
  route('GET', '/session/{sid}', 'signedIn', (p, { repos, ports }) => shell.sessionView(p, shellDeps(repos, ports))),
  route('POST', '/session/{sid}/abandon', 'signedIn', (p, { repos, ports }) => shell.sessionAbandon(p, shellDeps(repos, ports))),
  route('POST', '/session/{sid}/snooze', 'signedIn', (p, { repos, ports }) => shell.sessionSnooze(p, shellDeps(repos, ports))),
  route('GET', '/grading/{wid}', 'signedIn', (p, { repos, ports }) => shell.gradingView(p, shellDeps(repos, ports))),
];
