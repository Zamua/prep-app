// The error pages, ported from prep/web/errors.py: the copy verbatim, the
// detail folded into the blurb when it adds something.
import type { Renderer } from '../app/ports.js';
import { appBase } from './appBase.js';

export const ERROR_COPY: Record<number, readonly [string, string]> = {
  400: ['Bad request.', "Something in that URL didn't quite parse."],
  401: [
    'Not signed in.',
    "prep needs to know who you are. On the public deploy that's a sign-in flow; " +
      "on the tailnet shape it's the Tailscale-User-Login header from the proxy. " +
      'For local development, set PREP_DEFAULT_USER (the make dev shim does this automatically).',
  ],
  403: ['Forbidden.', "That's not yours to look at."],
  404: ['Not found.', "We couldn't find what you were looking for. Maybe a typo, or the link is stale."],
  409: ['Out of date.', 'Something changed since this page loaded. Reload and try again.'],
  422: ['Bad input.', "The form didn't validate. Go back and try again."],
  429: ['Busy right now.', 'More requests than the service can take at the moment.'],
  500: ['Something broke.', "Sorry — that's on our end. The error has been logged."],
};

const FALLBACK_COPY: readonly [string, string] = [
  'Something went sideways.',
  'An unexpected error happened. The team has been notified.',
];

/** The nine context-processor names for a page rendered outside a cell,
 * plus the request origin every Python context carried as `request`. */
export function anonymousContext(buildToken: string, appBase: string): Record<string, unknown> {
  return {
    app_base: appBase,
    user: null,
    agent_available: false,
    auth_provider: 'tailscale',
    sign_in_url: '',
    sign_up_url: '',
    sign_out_url: '',
    clerk_publishable_key: null,
    clerk_frontend_api_host: null,
    notif_unseen_count: 0,
    deck_display: {},
    static_css_mtime: buildToken,
  };
}

export function errorContext(status: number, path: string, detail?: string): Record<string, unknown> {
  const [headline, copy] = ERROR_COPY[status] ?? FALLBACK_COPY;
  const blurb = detail && detail !== headline ? `${copy} (${detail})` : copy;
  return { status_code: status, headline, blurb, path };
}

export const HTML = 'text/html; charset=utf-8';

export function htmlResponse(html: string, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(html, { status, headers: { 'content-type': HTML, ...headers } });
}

export function errorPage(
  renderer: Renderer,
  buildToken: string,
  status: number,
  request: Request,
  detail?: string,
): Response {
  const path = new URL(request.url).pathname;
  const context = { ...anonymousContext(buildToken, appBase(request)), ...errorContext(status, path, detail) };
  return htmlResponse(renderer.render('error.html', context), status);
}
