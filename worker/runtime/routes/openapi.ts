// The unauthenticated surface the entry worker answers before any identity
// is resolved: the OpenAPI document, the Swagger and ReDoc shells and the
// OAuth2 redirect, /llms.txt, and the VAPID public key a browser needs to
// subscribe.
//
// The shells load their viewer from a CDN, which test mode strips so a
// screenshot never reaches the network. Stripping keeps the line's
// indentation, so the rest of the markup is unchanged.
import doc from '../openapi.json';
import { llmsTxt } from './legal.js';
import { OAUTH2_REDIRECT_HTML } from './oauth2Redirect.js';

export const OPENAPI_DOCUMENT: Record<string, unknown> = doc as Record<string, unknown>;

const SWAGGER_CSS = '<link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">';
const SWAGGER_JS = '<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>';
const REDOC_FONTS = '<link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">';
const REDOC_JS = '<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"> </script>';
const FAVICON = '<link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png">';

const INDENT = '    ';
const line = (body: string) => INDENT + body;

export function swaggerShell(parity: boolean): string {
  const tag = (t: string) => line(parity ? '' : t);
  return [
    '',
    line('<!DOCTYPE html>'),
    line('<html>'),
    line('<head>'),
    line('<meta name="viewport" content="width=device-width, initial-scale=1.0">'),
    tag(SWAGGER_CSS),
    tag(FAVICON),
    line('<title>prep - Swagger UI</title>'),
    line('</head>'),
    line('<body>'),
    line('<div id="swagger-ui">'),
    line('</div>'),
    tag(SWAGGER_JS),
    line('<!-- `SwaggerUIBundle` is now available on the page -->'),
    line('<script>'),
    line('const ui = SwaggerUIBundle({'),
    line("    url: '/openapi.json',"),
    line('"dom_id": "#swagger-ui",'),
    '"layout": "BaseLayout",',
    '"deepLinking": true,',
    '"showExtensions": true,',
    '"showCommonExtensions": true,',
    "oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect',",
    line('presets: ['),
    line('    SwaggerUIBundle.presets.apis,'),
    line('    SwaggerUIBundle.SwaggerUIStandalonePreset'),
    line('    ],'),
    line('})'),
    line('</script>'),
    line('</body>'),
    line('</html>'),
    INDENT,
  ].join('\n');
}

export function redocShell(parity: boolean): string {
  const tag = (t: string) => line(parity ? '' : t);
  return [
    '',
    line('<!DOCTYPE html>'),
    line('<html>'),
    line('<head>'),
    line('<title>prep - ReDoc</title>'),
    line('<!-- needed for adaptive design -->'),
    line('<meta charset="utf-8"/>'),
    line('<meta name="viewport" content="width=device-width, initial-scale=1">'),
    line(''),
    tag(REDOC_FONTS),
    line(''),
    tag(FAVICON),
    line('<!--'),
    line("ReDoc doesn't change outer page styles"),
    line('-->'),
    line('<style>'),
    line('  body {'),
    line('    margin: 0;'),
    line('    padding: 0;'),
    line('  }'),
    line('</style>'),
    line('</head>'),
    line('<body>'),
    line('<noscript>'),
    line('    ReDoc requires Javascript to function. Please enable it to browse the documentation.'),
    line('</noscript>'),
    line('<redoc spec-url="/openapi.json"></redoc>'),
    tag(REDOC_JS),
    line('</body>'),
    line('</html>'),
    INDENT,
  ].join('\n');
}

export interface PublicRouteEnv {
  parity: boolean;
  vapidPublicKey: string;
}

const HTML = 'text/html; charset=utf-8';

/** The routes that need no identity, or null when the path is not one. */
export function servePublic(request: Request, url: URL, env: PublicRouteEnv): Response | null {
  if (request.method !== 'GET') return null;
  if (url.pathname === '/openapi.json') return Response.json(OPENAPI_DOCUMENT);
  if (url.pathname === '/docs') return new Response(swaggerShell(env.parity), { headers: { 'content-type': HTML } });
  // Named by the shell's `oauth2RedirectUrl`, so it must exist or Swagger's
  // authorize flow dead-ends on a 404.
  if (url.pathname === '/docs/oauth2-redirect') return new Response(OAUTH2_REDIRECT_HTML, { headers: { 'content-type': HTML } });
  if (url.pathname === '/redoc') return new Response(redocShell(env.parity), { headers: { 'content-type': HTML } });
  if (url.pathname === '/llms.txt') return llmsTxt();
  // Unauthenticated by design: the PWA subscribe handshake does not
  // reliably carry the identity headers.
  if (url.pathname === '/notify/vapid-public-key') return Response.json({ key: env.vapidPublicKey });
  return null;
}
