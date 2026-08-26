// The durable-work HTML routes a user cell serves (docs/PHASE-4.md C): the
// two polling pages, their JSON status and htmx fragments, the five gate
// signals, and the four starts. Ownership is parsed from the workflow id,
// so these are declared before nothing and after nothing in particular -
// none of their prefixes collide with a deck slug.
import * as plan from '../../../app/decks/plan.js';
import * as transform from '../../../app/decks/transform.js';
import * as trivia from '../../../app/trivia/generate.js';
import { route } from './adapt.js';
import type { Route } from '../router.js';

export const jobRoutes: readonly Route[] = [
  // Plan-first generation.
  route('GET', '/plan/{wid}/status', 'user', (p, { repos, ports }) => plan.planStatus(repos, p, ports)),
  route('GET', '/plan/{wid}/fragment', 'user', (p, { repos, ports }) => plan.planFragment(repos, p, ports)),
  route('POST', '/plan/{wid}/feedback', 'user', (p, { repos, ports }) => plan.planFeedback(repos, p, ports)),
  route('POST', '/plan/{wid}/accept', 'user', (p, { repos, ports }) => plan.planAccept(repos, p, ports)),
  route('POST', '/plan/{wid}/reject', 'user', (p, { repos, ports }) => plan.planReject(repos, p, ports)),
  route('GET', '/plan/{wid}', 'user', (p, { repos, ports }) => plan.planView(repos, p, ports)),

  // Transform: the deck, reorganize and card starts, then one polling surface.
  route('GET', '/reorganize', 'user', (_p, { repos }) => transform.reorganizeForm(repos)),
  route('POST', '/reorganize', 'user', (p, { repos, ports }) => transform.reorganizeSubmit(repos, p, ports)),
  route('POST', '/deck/{name}/transform', 'user', (p, { repos, ports }) => transform.deckTransform(repos, p, ports)),
  route('GET', '/transform/{wid}/status', 'user', (p, { repos, ports }) => transform.transformStatus(repos, p, ports)),
  route('GET', '/transform/{wid}/fragment', 'user', (p, { repos, ports }) => transform.transformFragment(repos, p, ports)),
  route('POST', '/transform/{wid}/apply', 'user', (p, { repos, ports }) => transform.transformApply(repos, p, ports)),
  route('POST', '/transform/{wid}/reject', 'user', (p, { repos, ports }) => transform.transformReject(repos, p, ports)),
  route('GET', '/transform/{wid}', 'user', (p, { repos, ports }) => transform.transformView(repos, p, ports)),

  // Trivia batch generation.
  route('POST', '/trivia/decks/{deck_id}/generate', 'user', (p, { repos, ports }) => trivia.triviaGenerate(repos, p, ports)),
  route('GET', '/trivia/gen/{wid}/status', 'user', (p, { repos, ports }) => trivia.triviaGenStatus(repos, p, ports)),
  route('GET', '/trivia/gen/{wid}/fragment', 'user', (p, { repos, ports }) => trivia.triviaGenFragment(repos, p, ports)),
  route('GET', '/trivia/gen/{wid}', 'user', (p, { repos, ports }) => trivia.triviaGenView(repos, p, ports)),
];
