// nunjucks-slim + the precompiled template map (build/templates.js, from
// precompile.mjs). Custom filters and the icon global are stubs: the
// spike measures bundle size and render cost, not fidelity.
import { DurableObject } from 'cloudflare:workers';
import nunjucks from 'nunjucks/browser/nunjucks-slim.js';
import templates from '../build/templates.js';

interface Env {
  PAGE: DurableObjectNamespace;
}

function makeEnv() {
  const env = new nunjucks.Environment(new nunjucks.PrecompiledLoader(templates), { autoescape: true });
  env.addFilter('markdown', (s: string) => new nunjucks.runtime.SafeString(`<p>${s}</p>`));
  env.addFilter('relative_time', (s: unknown) => `${s}`);
  env.addFilter('wakes_in', (s: unknown) => `${s}`);
  env.addFilter('tojson', (v: unknown) => JSON.stringify(v));
  env.addGlobal('icon', (name: string, kw?: { class_?: string }) =>
    new nunjucks.runtime.SafeString(`<svg class="${kw?.class_ ?? 'icon'}" data-icon="${name}"></svg>`),
  );
  return env;
}

// The context a FastAPI handler would pass for a signed-in Clerk user.
function context(page: string) {
  return {
    request: { scope: { get: (_k: string, d: string) => d } },
    static_css_mtime: 1756000000,
    auth_provider: 'clerk',
    clerk_publishable_key: 'pk_test_spike',
    clerk_frontend_api_host: 'clerk.example.test',
    user: { display_name: 'Spike', tailscale_login: 'spike@example.test', is_anonymous: false, editor_input_mode: null },
    notif_unseen_count: 2,
    sign_out_url: '/sign-out',
    status_code: 404,
    headline: 'Not found',
    blurb: `Rendered ${page} inside a cell.`,
    path: `/spike/${page}`,
  };
}

interface RenderOut {
  page: string;
  bytes: number;
  firstRenderMs: number;
  steadyRenderUs: number;
  envMs: number;
  activationRenders: number;
}

let envSingleton: nunjucks.Environment | null = null;
let rendersSinceActivation = 0;

function renderPage(page: string): RenderOut {
  const t0 = Date.now();
  if (!envSingleton) envSingleton = makeEnv();
  const envMs = Date.now() - t0;
  const t1 = Date.now();
  const html = envSingleton.render(page, context(page));
  const firstRenderMs = Date.now() - t1;
  // Steady state: 50 renders, averaged in microseconds.
  const t2 = Date.now();
  for (let i = 0; i < 50; i++) envSingleton.render(page, context(page));
  const steadyRenderUs = Math.round(((Date.now() - t2) * 1000) / 50);
  rendersSinceActivation++;
  return { page, bytes: html.length, firstRenderMs, steadyRenderUs, envMs, activationRenders: rendersSinceActivation };
}

export class PageCell extends DurableObject<Env> {
  render(page: string) {
    return renderPage(page);
  }
  html(page: string) {
    if (!envSingleton) envSingleton = makeEnv();
    return envSingleton.render(page, context(page));
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const page = url.searchParams.get('page') ?? 'error.html';
    const cell = url.searchParams.get('cell') ?? 'p1';
    const stub = env.PAGE.get(env.PAGE.idFromName(cell)) as unknown as {
      render(p: string): Promise<RenderOut>;
      html(p: string): Promise<string>;
    };
    if (url.pathname === '/html') {
      return new Response(await stub.html(page), { headers: { 'content-type': 'text/html; charset=utf-8' } });
    }
    const t0 = Date.now();
    const out = await stub.render(page);
    return Response.json({ where: 'cell', cell, requestMs: Date.now() - t0, ...out });
  },
};
