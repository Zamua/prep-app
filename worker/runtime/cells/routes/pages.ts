// The HTML routes a user cell serves (docs/PHASE-3.md C). Each entry names
// a use case; this module only parses the request into the shape the app
// layer takes and turns its answer back into the router's `Handled`.
import { dashboard } from '../../../app/dashboard/index.js';
import * as decks from '../../../app/decks/pages.js';
import { setNotificationsEnabled } from '../../../app/decks/service.js';
import { notificationLog, notifySettings } from '../../../app/notify/routes.js';
import * as account from '../../../app/settings/account.js';
import * as agentSettings from '../../../app/settings/agent.js';
import * as apiSettings from '../../../app/settings/api.js';
import * as editor from '../../../app/settings/editor.js';
import * as openrouter from '../../../app/settings/openrouter.js';
import * as srs from '../../../app/settings/srs.js';
import * as shell from '../../../app/study/shell.js';
import * as trivia from '../../../app/trivia/pages.js';
import type { UserRepos } from '../../../app/ports.js';
import { route } from './adapt.js';
import type { CellPorts, Route } from '../router.js';

const deckDeps = (ports: CellPorts) => ({ freeTierConfigured: ports.freeTierConfigured, random: ports.random, runner: ports.runner });
const triviaDeps = (repos: UserRepos, ports: CellPorts) => ({ repos, agent: ports.agent, runner: ports.runner, freeTierConfigured: ports.freeTierConfigured });
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
  route('GET', '/', 'user', (_p, { repos }) => ({ page: 'index.html', context: dashboard(repos) })),

  // Deck creation.
  route('GET', '/decks/new', 'user', () => decks.deckNewChooser()),
  route('GET', '/decks/new/srs', 'user', () => decks.deckNewSrsForm()),
  route('GET', '/decks/new/trivia', 'user', () => decks.deckNewTriviaForm()),
  route('POST', '/decks/new/srs', 'user', (p, { repos, ports }) => decks.deckNewSrsCreate(repos, p, deckDeps(ports))),
  route('POST', '/decks/new/trivia', 'user', (p, { repos, ports }) => decks.deckNewTriviaCreate(repos, p, deckDeps(ports))),

  // One deck. Every sub-resource is declared before `/deck/{name}` so a
  // path segment is never mistaken for a deck slug.
  route('POST', '/deck/{name}/delete', 'user', (p, { repos }) => decks.deckDelete(repos, p)),
  route('POST', '/deck/{name}/topic', 'user', (p, { repos }) => decks.deckUpdateTopic(repos, p)),
  route('POST', '/deck/{name}/rename', 'user', (p, { repos }) => decks.deckRename(repos, p)),
  route('GET', '/deck/{name}/edit-with-ai', 'user', (p, { repos }) => decks.deckEditWithAi(repos, p)),
  route('GET', '/deck/{name}/edit-with-claude', 'user', (p) => decks.deckEditWithClaude(p)),
  route('GET', '/deck/{name}/split', 'user', (p, { repos }) => decks.deckSplitForm(repos, p)),
  route('POST', '/deck/{name}/split', 'user', (p, { repos }) => decks.deckSplitSubmit(repos, p)),
  route('POST', '/deck/{name}/pin', 'user', (p, { repos }) => decks.deckPin(repos, p)),
  route('POST', '/deck/{name}/retention', 'user', (p, { repos }) => decks.deckRetention(repos, p)),
  route('POST', '/deck/{name}/notifications', 'user', (p, { repos }) => decks.deckNotifications(repos, p)),
  route('GET', '/deck/{name}/export', 'user', (p, { repos }) => decks.deckExportHub(repos, p)),
  route('GET', '/deck/{name}/export.csv', 'user', (p, { repos }) => decks.deckExportCsv(repos, p)),
  route('GET', '/deck/{name}/question/new', 'user', (p, { repos }) => decks.questionNewForm(repos, p)),
  route('POST', '/deck/{name}/question/new', 'user', (p, { repos }) => decks.questionNewSubmit(repos, p)),
  route('GET', '/deck/{name}', 'user', (p, { repos }) => decks.deckView(repos, p)),

  // Questions.
  route('GET', '/question/{qid}/edit', 'user', (p, { repos }) => decks.questionEditForm(repos, p)),
  route('POST', '/question/{qid}/edit', 'user', (p, { repos }) => decks.questionEditSubmit(repos, p)),
  route('POST', '/question/{qid}/suspend', 'user', (p, { repos }) => decks.questionSuspend(repos, p)),
  route('POST', '/question/{qid}/unsuspend', 'user', (p, { repos }) => decks.questionUnsuspend(repos, p)),
  route('POST', '/question/{qid}/improve', 'user', (p, { repos, ports }) => decks.questionImprove(repos, p, deckDeps(ports))),

  // Settings.
  route('GET', '/settings/agent', 'signedIn', (_p, { repos, ports }) => agentSettings.renderAgentSettings(repos, ports.freeTierConfigured)),
  route('POST', '/settings/agent/connect', 'signedIn', (_p, { repos, ports }) => agentSettings.subscriptionRefusal(repos, ports.freeTierConfigured)),
  route('POST', '/settings/agent/disconnect', 'signedIn', (_p, { repos, ports }) => agentSettings.subscriptionRefusal(repos, ports.freeTierConfigured)),
  route('GET', '/settings/agent/openrouter/start', 'signedIn', (_p, { ports, subject }) => openrouter.openrouterStart(subject, openRouterDeps(ports))),
  route('GET', '/settings/agent/openrouter/callback', 'signedIn', (p, { repos, ports, subject }) =>
    openrouter.openrouterCallback(subject, repos, p, openRouterDeps(ports)),
  ),
  route('POST', '/settings/agent/byok/{provider}/connect', 'signedIn', (p, { repos, ports }) =>
    agentSettings.byokConnect(repos, p, { freeTierConfigured: ports.freeTierConfigured, cipher: ports.cipher }),
  ),
  route('POST', '/settings/agent/byok/{provider}/disconnect', 'signedIn', (p, { repos, ports }) =>
    agentSettings.byokDisconnect(repos, p, ports.freeTierConfigured),
  ),
  route('POST', '/settings/agent/byok/{provider}/use', 'signedIn', (p, { repos, ports }) => agentSettings.byokUse(repos, p, ports.freeTierConfigured)),
  route('GET', '/settings/srs', 'user', (_p, { repos }) => srs.srsSettings(repos)),
  route('POST', '/settings/srs', 'user', (p, { repos }) => srs.srsSettingsSave(repos, p)),
  route('GET', '/settings/editor', 'user', (_p, { repos }) => editor.editorSettings(repos)),
  route('POST', '/settings/editor', 'user', (p, { repos }) => editor.editorSettingsSave(repos, p)),
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
  route('GET', '/trivia/session/{deck_name}', 'user', (p, { repos, ports }) => trivia.triviaSession(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/answer', 'user', (p, { repos, ports }) => trivia.triviaSessionAnswer(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/abandon', 'user', (p, { repos, ports }) => trivia.triviaSessionAbandon(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/snooze', 'user', (p, { repos, ports }) => trivia.triviaSessionSnooze(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/override', 'user', (p, { repos, ports }) => trivia.triviaSessionOverride(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/session/{deck_name}/regrade', 'user', (p, { repos, ports }) => trivia.triviaSessionRegrade(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/mute', 'user', (p, { repos, ports }) => trivia.triviaDeckMute(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/unmute', 'user', (p, { repos, ports }) => trivia.triviaDeckUnmute(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/notifications', 'user', (p, { repos, ports }) =>
    trivia.triviaDeckNotifications(p, triviaDeps(repos, ports), (id, enabled) => setNotificationsEnabled(repos, id, enabled)),
  ),
  route('POST', '/trivia/decks/{deck_id}/interval', 'user', (p, { repos, ports }) => trivia.triviaDeckInterval(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/decks/{deck_id}/session_size', 'user', (p, { repos, ports }) => trivia.triviaDeckSessionSize(p, triviaDeps(repos, ports))),
  route('GET', '/trivia/{question_id}', 'user', (p, { repos, ports }) => trivia.triviaCard(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/answer', 'user', (p, { repos, ports }) => trivia.triviaAnswer(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/regrade', 'user', (p, { repos, ports }) => trivia.triviaCardRegrade(p, triviaDeps(repos, ports))),
  route('POST', '/trivia/{question_id}/override', 'user', (p, { repos, ports }) => trivia.triviaCardOverride(p, triviaDeps(repos, ports))),

  // The study shell.
  route('POST', '/study/{name}/begin', 'user', (p, { repos, ports }) => shell.studyBegin(p, shellDeps(repos, ports))),
  route('GET', '/study/{name}', 'user', (p, { repos, ports }) => shell.studyView(p, shellDeps(repos, ports))),
  route('GET', '/session/{sid}', 'user', (p, { repos, ports }) => shell.sessionView(p, shellDeps(repos, ports))),
  route('POST', '/session/{sid}/abandon', 'user', (p, { repos, ports }) => shell.sessionAbandon(p, shellDeps(repos, ports))),
  route('POST', '/session/{sid}/snooze', 'user', (p, { repos, ports }) => shell.sessionSnooze(p, shellDeps(repos, ports))),
  route('GET', '/grading/{wid}', 'user', (p, { repos, ports }) => shell.gradingView(p, shellDeps(repos, ports))),
];
