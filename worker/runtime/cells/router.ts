// The route table a user cell serves: declaration-order matching, the
// identity gates, and the handler result shapes turned into responses.
// Handlers live in cells/routes; the identity arrives in the headers the
// entry worker sets after stripping any inbound copy.
import type { AgentPort, ApkgReader, ApkgWriter, Cipher, Clock, Hasher, IdentityKind, Random, UserRepos, WebPush, WorkflowRunner, ZipCodec } from '../../app/ports.js';
import type { AuthUrls } from '../../app/pageContext.js';
import type { OpenRouterAuth } from '../../app/settings/openrouter.js';
import { RowCapReached } from '../../domain/limits.js';

export const SUBJECT_HEADER = 'x-prep-subject';
export const DISPLAY_NAME_HEADER = 'x-prep-display-name';
export const KIND_HEADER = 'x-prep-kind';
export const EMAIL_HEADER = 'x-prep-email';
export const PICTURE_HEADER = 'x-prep-picture';
/** SHA-256 of the presented token; the owner's cell matches it against `api_tokens`. */
export const PAT_HASH_HEADER = 'x-prep-pat-hash';
/** The request clock under testMode, ISO; absent outside it. */
export const NOW_HEADER = 'x-prep-now';
export const IDENTITY_HEADERS = [
  SUBJECT_HEADER,
  DISPLAY_NAME_HEADER,
  KIND_HEADER,
  EMAIL_HEADER,
  PICTURE_HEADER,
  PAT_HASH_HEADER,
  NOW_HEADER,
] as const;

export type { IdentityKind };

export interface CellIdentity {
  subject: string;
  kind: IdentityKind;
  displayName: string | null;
  email: string | null;
  profilePicUrl: string | null;
}

/** The identity the entry worker asserted, or null when it asserted none. */
export function identityFrom(request: Request): CellIdentity | null {
  const subject = request.headers.get(SUBJECT_HEADER);
  if (!subject) return null;
  const decode = (name: string) => {
    const v = request.headers.get(name);
    return v ? decodeURIComponent(v) : null;
  };
  const kind = (request.headers.get(KIND_HEADER) || 'fake') as IdentityKind;
  return { subject, kind, displayName: decode(DISPLAY_NAME_HEADER), email: decode(EMAIL_HEADER), profilePicUrl: decode(PICTURE_HEADER) };
}

export type Gate = 'user' | 'signedIn' | 'pat';

export interface CellRequest {
  request: Request;
  url: URL;
  params: Record<string, string>;
  identity: CellIdentity;
  repos: UserRepos;
  clock: Clock;
  ports: CellPorts;
}

/** What a handler needs beyond its repositories, resolved once at the
 * composition root and handed down so no handler names an adapter. */
export interface CellPorts {
  random: Random;
  hasher: Hasher;
  agent: AgentPort;
  runner: WorkflowRunner;
  /** Null without a master key: the BYOK surfaces answer 503. */
  cipher: Cipher | null;
  openRouter: OpenRouterAuth;
  webPush: WebPush;
  zip: ZipCodec;
  apkg: ApkgReader & ApkgWriter;
  authProvider: string;
  authUrls: AuthUrls;
  /** The deploy serves a shared tier, so AI works without a stored key. */
  freeTierConfigured: boolean;
  vapidPublicKey: string;
  appBase: string;
  /** The accounts merged into this one, for the offline snapshot's `previous_ids`. */
  previousIds(): Promise<string[]>;
}

export type Handled =
  | { page: string; context: Record<string, unknown>; status?: number; headers?: Record<string, string> }
  | { json: unknown; status?: number; headers?: Record<string, string> }
  | { redirect: string; status?: number; headers?: Record<string, string> }
  | { text: string; status?: number; headers?: Record<string, string> }
  | { bytes: Uint8Array; status?: number; headers?: Record<string, string> }
  | { empty: true; status?: number; headers?: Record<string, string> };

export interface Route {
  method: string;
  /** `/deck/{name}/pin`: a `{param}` segment matches one path segment. */
  pattern: string;
  gate: Gate;
  handler(req: CellRequest): Promise<Handled> | Handled;
}

/** An anonymous identity on a signed-in route. */
export class SignInRequired extends Error {}
/** A non-token identity on a bearer-only route. */
export class TokenRequired extends Error {}

function compile(pattern: string): { names: string[]; re: RegExp } {
  const names: string[] = [];
  const src = pattern
    .split('/')
    .map((seg) => {
      const m = /^\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(seg);
      if (!m) return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      names.push(m[1]!);
      return '([^/]+)';
    })
    .join('/');
  return { names, re: new RegExp(`^${src}$`) };
}

const compiled = new WeakMap<Route, { names: string[]; re: RegExp }>();

export function matchRoute(routes: readonly Route[], method: string, pathname: string): { route: Route; params: Record<string, string> } | null {
  for (const route of routes) {
    if (route.method !== method) continue;
    let c = compiled.get(route);
    if (!c) {
      c = compile(route.pattern);
      compiled.set(route, c);
    }
    const m = c.re.exec(pathname);
    if (!m) continue;
    const params: Record<string, string> = {};
    c.names.forEach((name, i) => {
      params[name] = decodeURIComponent(m[i + 1]!);
    });
    return { route, params };
  }
  return null;
}

export function applyGate(gate: Gate, identity: CellIdentity): void {
  if (gate === 'user') return;
  if (gate === 'signedIn') {
    if (identity.kind === 'anon') throw new SignInRequired();
    if (identity.kind === 'pat') throw new TokenRequired();
    return;
  }
  if (identity.kind !== 'pat') throw new TokenRequired();
}

export const HTML = 'text/html; charset=utf-8';

/** The `/notify/*` endpoints whose clients parse JSON on the error path too.
 * Suffixes, not equality, so a deck named `test` under `/trivia/session/` is
 * not one of them. */
const JSON_ENDPOINT_SUFFIXES = ['/notify/subscribe', '/notify/unsubscribe', '/notify/test', '/notify/prefs', '/notify/vapid-public-key'];

/** An Accept naming JSON and not HTML, or one of the JSON endpoints
 * whatever the browser asked for. */
export function wantsJson(request: Request): boolean {
  const accept = request.headers.get('accept') ?? '';
  if (accept.includes('application/json') && !accept.includes('text/html')) return true;
  const path = new URL(request.url).pathname;
  return JSON_ENDPOINT_SUFFIXES.some((s) => path.endsWith(s));
}

export function wantsHtml(request: Request): boolean {
  return !wantsJson(request);
}

export function toResponse(handled: Handled, render: (template: string, context: Record<string, unknown>) => string): Response {
  const headers = new Headers(handled.headers ?? {});
  if ('page' in handled) {
    headers.set('content-type', HTML);
    return new Response(render(handled.page, handled.context), { status: handled.status ?? 200, headers });
  }
  if ('json' in handled) {
    headers.set('content-type', 'application/json');
    return new Response(JSON.stringify(handled.json), { status: handled.status ?? 200, headers });
  }
  if ('redirect' in handled) {
    headers.set('location', handled.redirect);
    return new Response(null, { status: handled.status ?? 303, headers });
  }
  if ('text' in handled) {
    if (!headers.has('content-type')) headers.set('content-type', 'text/plain; charset=utf-8');
    return new Response(handled.text, { status: handled.status ?? 200, headers });
  }
  if ('bytes' in handled) {
    if (!headers.has('content-type')) headers.set('content-type', 'application/octet-stream');
    return new Response(handled.bytes, { status: handled.status ?? 200, headers });
  }
  return new Response(null, { status: handled.status ?? 204, headers });
}

/** The 429 a row cap answers with: the error page for a browser, the error
 * envelope for anything else. */
export function capRefusal(e: RowCapReached, request: Request, errorPage: (status: number, detail: string) => Response): Response {
  if (wantsHtml(request)) return errorPage(429, e.message);
  return Response.json({ error: { code: 'deck_limit', message: e.message } }, { status: 429 });
}

export function gateRefusal(e: SignInRequired | TokenRequired, request: Request): Response {
  if (e instanceof SignInRequired) {
    if (wantsHtml(request)) return new Response(null, { status: 303, headers: { location: '/sign-in' } });
    return Response.json({ detail: 'sign in required' }, { status: 403 });
  }
  return Response.json({ detail: 'not authenticated' }, { status: 401 });
}
