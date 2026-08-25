// The PWA install surface: /sw.js and /manifest.json, plus the offline
// shell's `?build=` rule for the router. Reachable without identity: the
// install handshake happens before any session exists.
import { ASSET_TREE, SW_SOURCE } from '../build/sw.js';
import { isAcceptedVersionToken } from './tokenRules.js';

export interface AssetTree {
  css: string[];
  js: string[];
}

// Precached at their plain URLs: the push handler and the manifest
// reference them unversioned.
export const MANIFEST_ICONS = ['icon-192.png', 'icon-512.png'];

/** Every URL the offline shell needs to cold-launch with no network: the
 * shell pinned to this token, the whole css tree and the four js subtrees
 * at their versioned URLs, then the icons. The order is the manifest's
 * contract with the Python route. */
export function precacheUrls(tree: AssetTree, token: string, root = ''): string[] {
  return [
    `${root}/offline?build=${token}`,
    ...tree.css.map((rel) => `${root}/static/css/v${token}/${rel}`),
    ...tree.js.map((rel) => `${root}/static/js/v${token}/${rel}`),
    ...MANIFEST_ICONS.map((icon) => `${root}/static/pwa/${icon}`),
  ];
}

/** Substitutes the two placeholders at every occurrence. A function
 * replacement keeps `$` sequences in the JSON literal. */
export function serviceWorkerScript(source: string, token: string, urls: string[]): string {
  const json = JSON.stringify(urls);
  return source.replaceAll('__BUILD__', () => token).replaceAll('__PRECACHE__', () => json);
}

export function manifestDocument(root = ''): Record<string, unknown> {
  const staging = root.includes('staging');
  return {
    name: `prep · a commonplace book${staging ? ' (staging)' : ''}`,
    short_name: `prep${staging ? ' (staging)' : ''}`,
    description: 'Spaced-repetition flashcards. Learn anything.',
    display: 'standalone',
    scope: `${root}/`,
    start_url: `${root}/`,
    background_color: '#f4ecdc',
    theme_color: '#f5efe6',
    icons: [
      { src: `${root}/static/pwa/icon-192.png`, sizes: '192x192', type: 'image/png' },
      { src: `${root}/static/pwa/icon-512.png`, sizes: '512x512', type: 'image/png' },
    ],
  };
}

/** The shell's `?build=` echo: the service worker fetches the shell at its
 * own token so the asset URLs rendered into it match the cache keys the
 * same install stores. Anything not token-shaped is never reflected. */
export function offlineBuild(url: URL, token: string): string {
  const requested = url.searchParams.get('build');
  return requested && isAcceptedVersionToken(requested) ? requested : token;
}

const scripts = new Map<string, string>();

function serviceWorkerFor(token: string): string {
  let script = scripts.get(token);
  if (script === undefined) {
    script = serviceWorkerScript(SW_SOURCE, token, precacheUrls(ASSET_TREE, token, ''));
    scripts.set(token, script);
  }
  return script;
}

/** `null` off the two PWA paths; the router carries on. */
export function servePwa(url: URL, token: string): Response | null {
  switch (url.pathname) {
    case '/sw.js':
      return new Response(serviceWorkerFor(token), {
        headers: { 'content-type': 'application/javascript', 'cache-control': 'no-cache' },
      });
    case '/manifest.json':
      return Response.json(manifestDocument(''));
    default:
      return null;
  }
}
