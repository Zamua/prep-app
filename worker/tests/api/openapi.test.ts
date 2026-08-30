// The unauthenticated JSON surface and the two doc shells, whose vendor tags
// are stripped in test mode so visual tests never reach a CDN.
import { describe, expect, it } from 'vitest';
import { OPENAPI_DOCUMENT, redocShell, servePublic, swaggerShell } from '../../runtime/routes/openapi.js';
import worker from '../../runtime/worker.js';
import { ORIGIN, workerEnv } from './harness.js';

const get = (path: string) => new Request(`${ORIGIN}${path}`);

interface OpenApiOperation {
  parameters?: Array<Record<string, unknown>>;
  requestBody?: Record<string, unknown>;
  responses?: Record<string, unknown>;
}

interface OpenApiDocument {
  openapi: string;
  info: { title: string; version: string; description: string };
  paths: Record<string, Record<string, OpenApiOperation>>;
  components: { schemas: Record<string, Record<string, unknown>> };
}

const document = OPENAPI_DOCUMENT as unknown as OpenApiDocument;
const PUBLIC_OPERATIONS = [
  ['GET', '/api/v1/decks'],
  ['POST', '/api/v1/decks'],
  ['GET', '/api/v1/decks/{name}'],
  ['GET', '/api/v1/decks/{name}/cards'],
  ['GET', '/api/v1/decks/{name}/export.csv'],
  ['POST', '/api/v1/decks/{name}/import-csv'],
  ['POST', '/mcp'],
] as const;

function operation(method: string, path: string): OpenApiOperation {
  return document.paths[path]![method.toLowerCase()]!;
}

describe('GET /openapi.json', () => {
  it('serves the document owned by the route', async () => {
    const { env } = workerEnv();
    const res = await worker.fetch(get('/openapi.json'), env);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(OPENAPI_DOCUMENT);
  });

  it('defines every public method and path, with no extras', () => {
    const methods = new Set(['get', 'post', 'put', 'patch', 'delete', 'options', 'head']);
    const actual = Object.entries(document.paths)
      .filter(([path]) => path.startsWith('/api/v1/') || path === '/mcp')
      .flatMap(([path, pathItem]) => Object.keys(pathItem).filter((method) => methods.has(method)).map((method) => `${method.toUpperCase()} ${path}`))
      .sort();
    expect(actual).toEqual(PUBLIC_OPERATIONS.map(([method, path]) => `${method} ${path}`).sort());
  });

  it('pins the document identity and bearer security contract', () => {
    expect(document.openapi).toBe('3.1.0');
    expect(document.info.title).toBe('prep');
    expect(document.info.version).toBe('1.0.0');
    expect(document.info.description).toContain('Authorization: Bearer prep_pat_');

    for (const [method, path] of PUBLIC_OPERATIONS) {
      const authorization = operation(method, path).parameters?.find((parameter) => parameter['in'] === 'header' && parameter['name'] === 'authorization');
      expect(authorization, `${method} ${path}`).toMatchObject({
        in: 'header',
        name: 'authorization',
        required: false,
        schema: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      });
    }
  });

  it('pins representative request and response schemas', () => {
    const newDeck = document.components.schemas['_NewDeckBody'] as {
      required: string[];
      properties: Record<string, unknown>;
    };
    expect(newDeck.required).toEqual(['name']);
    expect(newDeck.properties).toMatchObject({
      name: { type: 'string', minLength: 2, maxLength: 30 },
      context_prompt: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    });

    const card = document.components.schemas['_CardJson'] as {
      required: string[];
      properties: Record<string, unknown>;
    };
    expect(card.required).toEqual(['type', 'prompt', 'answer']);
    expect(card.properties).toMatchObject({
      type: { type: 'string' },
      prompt: { type: 'string' },
      answer: { type: 'string' },
      choices: { anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'null' }] },
    });

    const csvOutcome = document.components.schemas['_CsvImportOutcome'] as {
      required: string[];
      properties: Record<string, unknown>;
    };
    expect(csvOutcome.required).toEqual(['deck_id', 'deck_name', 'inserted', 'skipped_duplicates', 'errors']);
    expect(csvOutcome.properties).toMatchObject({
      deck_id: { type: 'integer' },
      inserted: { type: 'integer' },
      skipped_duplicates: { type: 'integer' },
      errors: { type: 'array', items: { type: 'string' } },
    });

    expect(operation('POST', '/api/v1/decks').requestBody).toMatchObject({
      required: true,
      content: { 'application/json': { schema: { $ref: '#/components/schemas/_NewDeckBody' } } },
    });
    expect(operation('GET', '/api/v1/decks/{name}/export.csv').responses).toMatchObject({
      '200': { content: { 'text/csv': {} } },
    });
    expect(operation('POST', '/api/v1/decks/{name}/import-csv').responses).toMatchObject({
      '200': { content: { 'application/json': { schema: { $ref: '#/components/schemas/_CsvImportOutcome' } } } },
      '400': { description: 'empty body or malformed CSV' },
    });
  });
});

describe('the doc shells', () => {
  it('links each shell to the current document', () => {
    expect(swaggerShell(true)).toContain("url: '/openapi.json'");
    expect(redocShell(true)).toContain('spec-url="/openapi.json"');
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

  it('answers every public route and nothing else', () => {
    for (const path of ['/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc', '/llms.txt', '/notify/vapid-public-key']) {
      expect(servePublic(get(path), new URL(`${ORIGIN}${path}`), env), path).not.toBeNull();
    }
    expect(servePublic(get('/api/v1/decks'), new URL(`${ORIGIN}/api/v1/decks`), env)).toBeNull();
  });

  it('is read-only: a POST falls through to identification', () => {
    const request = new Request(`${ORIGIN}/openapi.json`, { method: 'POST' });
    expect(servePublic(request, new URL(`${ORIGIN}/openapi.json`), env)).toBeNull();
  });

  it('hands out the VAPID key with no credential, as the subscribe handshake needs', async () => {
    const { env: full } = workerEnv();
    const res = await worker.fetch(get('/notify/vapid-public-key'), full);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { key: string }).key).toMatch(/^BC/);
  });
});
