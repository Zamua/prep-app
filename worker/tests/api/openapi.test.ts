// The unauthenticated JSON surface: the recorded OpenAPI document and the
// the two doc shells, whose vendor tags are stripped in test mode so
// the pixel harness never reaches a CDN.
import { describe, expect, it } from 'vitest';
import { OPENAPI_DOCUMENT, redocShell, servePublic, swaggerShell } from '../../runtime/routes/openapi.js';
import worker from '../../runtime/worker.js';
import { loadCorpus, ORIGIN, replayEnv } from './harness.js';

const get = (path: string) => new Request(`${ORIGIN}${path}`);

describe('GET /openapi.json', () => {
  it('serves the recorded document', async () => {
    const recorded = loadCorpus('api').pairs.find((p) => p.name === 'openapi')!.response.json;
    expect(OPENAPI_DOCUMENT).toEqual(recorded);
    const { env } = replayEnv();
    const res = await worker.fetch(get('/openapi.json'), env);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(recorded);
  });

  it('names the whole public surface, so a new route cannot slip out undocumented', () => {
    const paths = Object.keys(OPENAPI_DOCUMENT['paths'] as Record<string, unknown>);
    for (const path of ['/api/v1/decks', '/api/v1/decks/{name}/cards', '/api/v1/decks/{name}/export.csv', '/api/v1/decks/{name}/import-csv', '/mcp']) {
      expect(paths, path).toContain(path);
    }
  });
});

describe('the doc shells', () => {
  it('match the recorded bodies exactly', () => {
    const corpus = loadCorpus('api');
    expect(swaggerShell(true)).toBe(corpus.pairs.find((p) => p.name === 'docs-shell')!.response.text);
    expect(redocShell(true)).toBe(corpus.pairs.find((p) => p.name === 'redoc-shell')!.response.text);
  });

  it('carry the vendor bundles off testMode, and only the indentation on it', () => {
    expect(swaggerShell(false)).toContain('cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js');
    expect(redocShell(false)).toContain('cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js');
    expect(swaggerShell(true)).not.toContain('cdn.jsdelivr.net');
    expect(redocShell(true)).not.toContain('fonts.googleapis.com');
    // The stripped line keeps its place, so the two bodies differ only in
    // the tags themselves.
    expect(swaggerShell(true).split('\n')).toHaveLength(swaggerShell(false).split('\n').length);
    expect(redocShell(true).split('\n')).toHaveLength(redocShell(false).split('\n').length);
  });
});

describe('the routes that need no identity', () => {
  const env = { testMode: true, vapidPublicKey: 'BCT1' };

  it('answers the four of them and nothing else', () => {
    for (const path of ['/openapi.json', '/docs', '/redoc', '/notify/vapid-public-key']) {
      expect(servePublic(get(path), new URL(`${ORIGIN}${path}`), env), path).not.toBeNull();
    }
    expect(servePublic(get('/api/v1/decks'), new URL(`${ORIGIN}/api/v1/decks`), env)).toBeNull();
  });

  it('is read-only: a POST falls through to identification', () => {
    const request = new Request(`${ORIGIN}/openapi.json`, { method: 'POST' });
    expect(servePublic(request, new URL(`${ORIGIN}/openapi.json`), env)).toBeNull();
  });

  it('hands out the VAPID key with no credential, as the subscribe handshake needs', async () => {
    const { env: full } = replayEnv();
    const res = await worker.fetch(get('/notify/vapid-public-key'), full);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { key: string }).key).toMatch(/^BC/);
  });
});
