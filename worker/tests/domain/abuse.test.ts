// Which responses are abuse signals, and the one line they produce. The line
// has a fixed key set so a caller cannot put a path, header or id into it.
import { describe, expect, it } from 'vitest';
import { abuseEventOf, abuseLine, routeKindOf } from '../../domain/abuse';
import { isPatSurface } from '../../domain/pat';
import { MIGRATE_PREFIX } from '../../runtime/routes/migrate';

describe('routeKindOf', () => {
  it('names a route from a fixed vocabulary, never by a path that can carry an id', () => {
    expect(routeKindOf('/api/v1/decks')).toBe('pat');
    expect(routeKindOf('/api/v1')).toBe('pat');
    expect(routeKindOf('/mcp')).toBe('pat');
    expect(routeKindOf('/webhooks/clerk')).toBe('webhook');
    expect(routeKindOf('/api/instant/generate')).toBe('instant');
    expect(routeKindOf('/api/active-workflows-badge')).toBe('badge');
    expect(routeKindOf(`${MIGRATE_PREFIX}/dump`)).toBe('migrate');
    expect(routeKindOf('/api/decks/12/cards/34')).toBe('api');
    expect(routeKindOf('/decks/12')).toBe('page');
    expect(routeKindOf('/')).toBe('page');
  });

  it('agrees with the entry worker on which paths are bearer-only', () => {
    for (const path of ['/api/v1/x', '/api/v1', '/mcp', '/api/v10', '/mcpx', '/api/v1x', '/api/decks']) {
      expect(routeKindOf(path) === 'pat', path).toBe(isPatSurface(path));
    }
  });
});

describe('abuseEventOf', () => {
  it('maps refused credentials, rate limits and rejected webhooks, and nothing else', () => {
    expect(abuseEventOf('badge', 401)).toBe('auth-failure');
    expect(abuseEventOf('pat', 401)).toBe('auth-failure');
    expect(abuseEventOf('page', 403)).toBe('auth-failure');
    expect(abuseEventOf('instant', 429)).toBe('rate-limited');
    expect(abuseEventOf('webhook', 400)).toBe('webhook-rejected');
    expect(abuseEventOf('webhook', 401)).toBe('webhook-rejected');
  });

  it('is null for everything that is not a signal', () => {
    for (const route of ['pat', 'webhook', 'instant', 'badge', 'migrate', 'api', 'page'] as const) {
      for (const status of [101, 200, 201, 204, 303, 304, 404, 405, 410, 413, 500, 503]) {
        expect(abuseEventOf(route, status), `${route} ${status}`).toBeNull();
      }
    }
    expect(abuseEventOf('webhook', 422)).toBeNull();
    expect(abuseEventOf('webhook', 503)).toBeNull();
  });
});

describe('abuseLine', () => {
  const KEYS = ['event', 'route', 'status', 'subnet', 'country'];

  it('carries only the named fields, however many a caller passes', () => {
    const extra = {
      route: 'pat',
      status: 401,
      ip: '203.0.113.77',
      headers: { cookie: 'secret-cookie' },
      url: 'https://x.example/secret-path',
      user: 'user_secret',
    } as unknown as Parameters<typeof abuseLine>[1];
    const line = abuseLine('auth-failure', extra);
    expect(Object.keys(line)).toEqual(KEYS);
    expect(JSON.stringify(line)).not.toMatch(/secret|203\.0\.113\.77/);
    expect(line.subnet).toBe('203.0.113.0/24');
  });

  it('keeps a country only when it is two letters or digits', () => {
    const read = (country: unknown) => abuseLine('auth-failure', { route: 'page', status: 401, ip: '192.0.2.1', country });
    expect(read('us').country).toBe('US');
    for (const country of ['<script>', 'USA', '', 7, null, undefined]) {
      expect(read(country).country, String(country)).toBeNull();
    }
  });

  it('logs an unusable address as unknown', () => {
    expect(abuseLine('rate-limited', { route: 'instant', status: 429, ip: 'garbage' }).subnet).toBe('unknown');
    expect(abuseLine('rate-limited', { route: 'instant', status: 429, ip: null }).subnet).toBe('unknown');
  });
});
