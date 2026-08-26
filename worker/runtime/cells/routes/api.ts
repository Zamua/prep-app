// The JSON routes a user cell serves (docs/PHASE-3.md D): the translation
// layer between one request and one use case. Every handler here parses,
// calls, and hands back a result shape; no API logic lives in this file.
import { dispatch, INVALID_REQUEST, PARSE_ERROR } from '../../../app/api/mcp.js';
import * as v1 from '../../../app/api/v1.js';
import { workflowBadge } from '../../../app/badge/badge.js';
import { deckMenus, overview } from '../../../app/dashboard/overview.js';
import type { ApiResult } from '../../../app/http.js';
import * as notify from '../../../app/notify/routes.js';
import { snapshot } from '../../../app/offline/snapshot.js';
import { syncBatch } from '../../../app/offline/sync.js';
import { agentAvailable } from '../../../app/pageContext.js';
import * as study from '../../../app/study/api.js';
import { gradingPoll } from '../../../app/study/grading.js';
import { jsonInvalid, pythonJsonError, RequestValidationError } from '../../../app/validation.js';
import type { CellRequest, Handled, Route } from '../router.js';

/** A body pydantic would have parsed: a decode failure is the model's 422. */
async function jsonBody(req: CellRequest): Promise<unknown> {
  const text = await req.request.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    const { message, position } = pythonJsonError(text);
    throw new RequestValidationError([jsonInvalid(message, position)]);
  }
}

/** The 422 FastAPI answers when a request model fails to validate. */
function handle(fn: (req: CellRequest) => Promise<ApiResult> | ApiResult): (req: CellRequest) => Promise<Handled> {
  return async (req) => {
    try {
      return (await fn(req)) as Handled;
    } catch (e) {
      if (e instanceof RequestValidationError) return { json: { detail: e.errors }, status: 422 };
      throw e;
    }
  };
}

function studyDeps(req: CellRequest): study.StudyDeps {
  return {
    repos: req.repos,
    clock: req.clock,
    userAgent: req.request.headers.get('user-agent'),
    agentAvailable: agentAvailable(req.repos, req.ports.freeTierConfigured),
    runner: req.ports.runner,
    freeTierConfigured: req.ports.freeTierConfigured,
  };
}

const notifyDeps = (req: CellRequest): notify.NotifyDeps => ({
  repos: req.repos,
  webPush: req.ports.webPush,
  vapidPublicKey: req.ports.vapidPublicKey,
});

const v1Repos = (req: CellRequest): v1.V1Repos => ({ decks: req.repos.decks, questions: req.repos.questions, trivia: req.repos.trivia });

export const apiRoutes: readonly Route[] = [
  // ---- dashboard and the masthead badge ------------------------------------
  { method: 'GET', pattern: '/api/dashboard/overview', gate: 'user', handler: handle((req) => overview(req.repos, req.repos.prefs.get())) },
  { method: 'GET', pattern: '/api/dashboard/deck-menus', gate: 'user', handler: handle((req) => deckMenus(req.repos)) },
  { method: 'GET', pattern: '/api/active-workflows-badge', gate: 'user', handler: handle((req) => workflowBadge(req.repos)) },

  // ---- offline -------------------------------------------------------------
  {
    method: 'GET',
    pattern: '/api/offline/snapshot',
    gate: 'user',
    handler: handle(async (req) =>
      snapshot(req, {
        id: req.identity.subject,
        displayName: req.repos.prefs.get()?.display_name ?? null,
        previousIds: await req.ports.previousIds(),
      }),
    ),
  },
  { method: 'POST', pattern: '/api/offline/sync', gate: 'user', handler: handle(async (req) => ({ json: syncBatch(req, await jsonBody(req)) })) },

  // ---- study ---------------------------------------------------------------
  {
    method: 'POST',
    pattern: '/api/study/decks/{name}/session',
    gate: 'user',
    handler: handle(async (req) => study.begin(studyDeps(req), req.params['name']!, await jsonBody(req))),
  },
  { method: 'GET', pattern: '/api/study/decks/{name}/next', gate: 'user', handler: handle((req) => study.deckNext(studyDeps(req), req.params['name']!)) },
  {
    method: 'POST',
    pattern: '/api/study/decks/{name}/submit',
    gate: 'user',
    handler: handle(async (req) => study.deckSubmit(studyDeps(req), req.params['name']!, await jsonBody(req))),
  },
  { method: 'GET', pattern: '/api/study/sessions/{sid}/next', gate: 'user', handler: handle((req) => study.sessionNext(studyDeps(req), req.params['sid']!)) },
  {
    method: 'POST',
    pattern: '/api/study/sessions/{sid}/advance',
    gate: 'user',
    handler: handle(async (req) => study.sessionAdvance(studyDeps(req), req.params['sid']!, await jsonBody(req))),
  },
  {
    method: 'POST',
    pattern: '/api/study/sessions/{sid}/submit',
    gate: 'user',
    handler: handle(async (req) => study.sessionSubmit(studyDeps(req), req.params['sid']!, await jsonBody(req))),
  },
  {
    method: 'POST',
    pattern: '/api/study/sessions/{sid}/draft',
    gate: 'user',
    handler: handle(async (req) => study.sessionDraft(studyDeps(req), req.params['sid']!, await jsonBody(req))),
  },
  { method: 'POST', pattern: '/api/study/sessions/{sid}/abandon', gate: 'user', handler: handle((req) => study.sessionAbandon(studyDeps(req), req.params['sid']!)) },
  {
    method: 'POST',
    pattern: '/api/study/sessions/{sid}/snooze',
    gate: 'user',
    handler: handle(async (req) => study.sessionSnooze(studyDeps(req), req.params['sid']!, await jsonBody(req))),
  },
  { method: 'POST', pattern: '/api/study/cards', gate: 'user', handler: handle(async (req) => study.authorCard(studyDeps(req), await jsonBody(req))) },
  {
    method: 'GET',
    pattern: '/api/study/grading/{wid}',
    gate: 'user',
    handler: handle((req) => gradingPoll(studyDeps(req), req.params['wid']!, req.url.searchParams.get('sid') ?? '')),
  },

  // ---- notify --------------------------------------------------------------
  { method: 'GET', pattern: '/notify', gate: 'signedIn', handler: handle((req) => notify.notifySettings(notifyDeps(req))) },
  { method: 'GET', pattern: '/notify/log', gate: 'signedIn', handler: handle((req) => notify.notificationLog(notifyDeps(req))) },
  { method: 'POST', pattern: '/notify/prefs', gate: 'signedIn', handler: handle(async (req) => notify.savePrefs(notifyDeps(req), await jsonBody(req))) },
  { method: 'POST', pattern: '/notify/subscribe', gate: 'signedIn', handler: handle(async (req) => notify.subscribe(notifyDeps(req), await jsonBody(req))) },
  { method: 'POST', pattern: '/notify/unsubscribe', gate: 'signedIn', handler: handle(async (req) => notify.unsubscribe(notifyDeps(req), await jsonBody(req))) },
  { method: 'POST', pattern: '/notify/test', gate: 'signedIn', handler: handle((req) => notify.sendTest(notifyDeps(req))) },

  // ---- the public REST surface ---------------------------------------------
  { method: 'GET', pattern: '/api/v1/decks', gate: 'pat', handler: handle((req) => v1.listDecks(v1Repos(req))) },
  { method: 'POST', pattern: '/api/v1/decks', gate: 'pat', handler: handle(async (req) => v1.createDeck(v1Repos(req), await jsonBody(req))) },
  { method: 'GET', pattern: '/api/v1/decks/{name}', gate: 'pat', handler: handle((req) => v1.deckMeta(v1Repos(req), req.params['name']!)) },
  { method: 'GET', pattern: '/api/v1/decks/{name}/cards', gate: 'pat', handler: handle((req) => v1.listCards(v1Repos(req), req.params['name']!)) },
  { method: 'GET', pattern: '/api/v1/decks/{name}/export.csv', gate: 'pat', handler: handle((req) => v1.exportCsv(v1Repos(req), req.params['name']!)) },
  {
    method: 'POST',
    pattern: '/api/v1/decks/{name}/import-csv',
    gate: 'pat',
    handler: handle(async (req) => v1.importCsv(v1Repos(req), req.params['name']!, await req.request.text())),
  },

  // ---- MCP -----------------------------------------------------------------
  {
    method: 'POST',
    pattern: '/mcp',
    gate: 'pat',
    handler: async (req) => {
      let body: unknown;
      try {
        body = JSON.parse(await req.request.text());
      } catch {
        return PARSE_ERROR() as Handled;
      }
      if (typeof body !== 'object' || body === null || Array.isArray(body)) return INVALID_REQUEST() as Handled;
      const result = dispatch(v1Repos(req), body);
      // A JSON-RPC notification has no response body; the 204 must carry none.
      if ('json' in result && result.json === null && result.status === 204) {
        return { empty: true, status: 204, headers: { 'content-type': 'application/json' } };
      }
      return result as Handled;
    },
  },
];
