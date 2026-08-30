// The MCP endpoint: the catalog an external client negotiates against,
// the JSON-RPC envelopes, and tool-level refusals. Driven through the entry
// worker so the bearer gate is real.
import { zipSync } from 'fflate';
import { beforeAll, describe, expect, it } from 'vitest';
import { dispatch, MCP_PROTOCOL_VERSION } from '../../app/api/mcp.js';
import { TOOLS } from '../../app/api/tools.js';
import type { V1Repos } from '../../app/api/v1.js';
import { ARCHIVE_TOO_LARGE, EXPORT_TOO_LARGE, MAX_APKG_UPLOAD_BYTES, MAX_EXPORT_QUESTIONS, uploadTooLarge } from '../../app/decks/importLimits.js';
import { SqlJsApkg } from '../../runtime/adapters/apkg.js';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { cell } from '../repos/setup.js';
import { mintToken, ORIGIN, SEED_USER, workerEnv, seed } from './harness.js';

let env: Env;
let bearer: string;

const EXPECTED_TOOL_NAMES = [
  'prep_list_decks',
  'prep_get_deck',
  'prep_list_cards',
  'prep_export_deck_csv',
  'prep_create_deck',
  'prep_import_csv',
  'prep_rename_deck',
  'prep_delete_deck',
  'prep_set_deck_pinned',
  'prep_set_topic_prompt',
  'prep_get_card',
  'prep_add_card',
  'prep_update_card',
  'prep_delete_card',
  'prep_suspend_card',
  'prep_export_deck_apkg',
  'prep_import_apkg',
] as const;

type InputSchema = {
  type?: unknown;
  required?: unknown;
  properties?: Record<string, Record<string, unknown>>;
};

function inputSchema(name: string): InputSchema {
  return TOOLS.find((candidate) => candidate.name === name)!.inputSchema as InputSchema;
}

async function rpc(body: unknown): Promise<{ status: number; json: unknown; text: string }> {
  const res = await worker.fetch(
    new Request(`${ORIGIN}/mcp`, {
      method: 'POST',
      headers: { authorization: `Bearer ${bearer}`, 'content-type': 'application/json', accept: 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    }),
    env,
  );
  const text = await res.text();
  return { status: res.status, json: text ? JSON.parse(text) : null, text };
}

const call = (name: string, args: Record<string, unknown> = {}) => rpc({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name, arguments: args } });

/** The text a tool answered with, and whether it reported an error. */
async function tool(name: string, args: Record<string, unknown> = {}): Promise<{ text: string; isError: boolean }> {
  const { json } = await call(name, args);
  const result = (json as { result: { content: { text: string }[]; isError: boolean } }).result;
  return { text: result.content[0]!.text, isError: result.isError };
}

beforeAll(async () => {
  const state = workerEnv();
  env = state.env;
  await seed(env, 'reader', SEED_USER);
  bearer = await mintToken(state.userStorage(SEED_USER), SEED_USER, 'mcp');
}, 60_000);

describe('the tool catalog', () => {
  it('defines the exact unique tool set', () => {
    const names = TOOLS.map((tool) => tool.name);
    expect(names).toEqual(EXPECTED_TOOL_NAMES);
    expect(new Set(names).size).toBe(EXPECTED_TOOL_NAMES.length);
  });

  it('pins contract-critical input schemas', () => {
    const create = inputSchema('prep_create_deck');
    expect(create.type).toBe('object');
    expect(create.required).toEqual(['name']);
    expect(create.properties).toMatchObject({ name: { type: 'string' }, context_prompt: { type: 'string' } });

    const add = inputSchema('prep_add_card');
    expect(add.type).toBe('object');
    expect(add.required).toEqual(['deck', 'type', 'prompt', 'answer']);
    expect(add.properties).toMatchObject({
      deck: { type: 'string' },
      type: { type: 'string', enum: ['short', 'mcq', 'multi', 'code'] },
      choices: { type: 'array', items: { type: 'string' } },
    });

    const suspend = inputSchema('prep_suspend_card');
    expect(suspend.required).toEqual(['card_id', 'suspended']);
    expect(suspend.properties).toMatchObject({ card_id: { type: 'integer' }, suspended: { type: 'boolean' } });

    const importApkg = inputSchema('prep_import_apkg');
    expect(importApkg.required).toEqual(['name', 'apkg_base64']);
    expect(importApkg.properties).toMatchObject({ name: { type: 'string' }, apkg_base64: { type: 'string' } });
  });

  it('publishes the catalog through tools/list', async () => {
    const { json } = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    expect((json as { result: { tools: unknown[] } }).result.tools).toEqual(TOOLS);
  });

  it('names every tool the dispatcher can run', async () => {
    for (const t of TOOLS) {
      const { json } = await call(t.name, {});
      expect(json, `${t.name} is not dispatched`).not.toHaveProperty('error');
    }
  });
});

describe('the JSON-RPC envelope', () => {
  it('answers a parse error 400 with code -32700', async () => {
    const { status, json } = await rpc('{');
    expect(status).toBe(400);
    expect(json).toEqual({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
  });

  it('answers a non-object request 400 with code -32600', async () => {
    const { status, json } = await rpc([1]);
    expect(status).toBe(400);
    expect(json).toEqual({ jsonrpc: '2.0', id: null, error: { code: -32600, message: 'Invalid Request' } });
  });

  it('answers an unknown method 200 with code -32601', async () => {
    const { status, json } = await rpc({ jsonrpc: '2.0', id: 3, method: 'resources/list' });
    expect(status).toBe(200);
    expect(json).toEqual({ jsonrpc: '2.0', id: 3, error: { code: -32601, message: 'unknown method: "resources/list"' } });
  });

  it('answers an unknown tool 200 with code -32602', async () => {
    const { json } = await call('prep_nope');
    expect(json).toEqual({ jsonrpc: '2.0', id: 7, error: { code: -32602, message: 'unknown tool: "prep_nope"' } });
  });

  it('handshakes on initialize', async () => {
    const { json } = await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: MCP_PROTOCOL_VERSION } });
    expect(json).toEqual({
      jsonrpc: '2.0',
      id: 1,
      result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: { tools: {} }, serverInfo: { name: 'prep', version: '1.0.0' } },
    });
  });

  it('answers the initialized notification with an empty 204', async () => {
    const { status, text } = await rpc({ jsonrpc: '2.0', method: 'notifications/initialized' });
    expect(status).toBe(204);
    expect(text).toBe('');
  });

  it('refuses without a bearer token', async () => {
    const res = await worker.fetch(
      new Request(`${ORIGIN}/mcp`, { method: 'POST', headers: { accept: 'application/json' }, body: '{}' }),
      env,
    );
    expect(res.status).toBe(401);
  });
});

describe('the tool refusals', () => {
  it.each([
    ['prep_get_deck', {}, 'missing required arg: name'],
    ['prep_get_deck', { name: 'nope' }, "deck not found: 'nope'"],
    ['prep_list_cards', { name: 'nope' }, "deck not found: 'nope'"],
    ['prep_export_deck_csv', { name: 'nope' }, "deck not found: 'nope'"],
    ['prep_create_deck', { name: 'world-capitals' }, "deck 'world-capitals' already exists"],
    ['prep_import_csv', { name: 'world-capitals' }, 'missing required arg: csv (full CSV body)'],
    ['prep_rename_deck', { name: 'world-capitals' }, 'missing required args: name, new_name'],
    ['prep_rename_deck', { name: 'nope', new_name: 'x' }, "deck not found: 'nope'"],
    ['prep_rename_deck', { name: 'scratch', new_name: 'world-capitals' }, "deck 'world-capitals' already exists"],
    ['prep_delete_deck', { name: 'nope' }, "deck not found: 'nope'"],
    ['prep_set_deck_pinned', { name: 'scratch' }, 'pinned must be a boolean'],
    ['prep_set_deck_pinned', { name: 'nope', pinned: true }, "deck not found: 'nope'"],
    ['prep_set_topic_prompt', { name: 'nope', context_prompt: 'x' }, "deck not found: 'nope'"],
    ['prep_get_card', { card_id: 'one' }, 'card_id must be an integer'],
    ['prep_get_card', { card_id: 999999 }, 'card not found: 999999'],
    ['prep_add_card', {}, 'missing required arg: deck'],
    ['prep_add_card', { deck: 'nope', type: 'short', prompt: 'p', answer: 'a' }, "deck not found: 'nope'"],
    ['prep_add_card', { deck: 'scratch', type: 'essay', prompt: 'p', answer: 'a' }, "unknown type 'essay'; expected short|mcq|multi|code"],
    ['prep_add_card', { deck: 'scratch', type: 'short', answer: 'a' }, 'missing required arg: prompt'],
    ['prep_add_card', { deck: 'scratch', type: 'short', prompt: 'p' }, 'missing required arg: answer'],
    ['prep_add_card', { deck: 'scratch', type: 'mcq', prompt: 'p', answer: 'a', choices: 'x' }, 'choices must be an array of strings'],
    ['prep_update_card', { card_id: 999999, type: 'short', prompt: 'p', answer: 'a' }, 'card not found: 999999'],
    ['prep_delete_card', { card_id: 999999 }, 'card not found: 999999'],
    ['prep_suspend_card', { card_id: 1 }, 'suspended must be a boolean'],
    ['prep_suspend_card', { card_id: 999999, suspended: true }, 'card not found: 999999'],
    ['prep_import_apkg', { name: 'x' }, 'missing required arg: apkg_base64'],
    ['prep_import_apkg', { name: 'x', apkg_base64: 'not base64!' }, "apkg_base64 didn't decode: Only base64 data is allowed"],
  ])('%s %j', async (name, args, message) => {
    const result = await tool(name as string, args as Record<string, unknown>);
    expect(result.isError).toBe(true);
    expect(result.text).toBe(message);
  });

  it('round-trips a deck through the two apkg tools', async () => {
    const exported = JSON.parse((await tool('prep_export_deck_apkg', { name: 'world-capitals' })).text) as {
      filename: string;
      apkg_base64: string;
      byte_count: number;
    };
    expect(exported.filename).toBe('world-capitals.apkg');
    expect(exported.byte_count).toBeGreaterThan(0);

    const imported = JSON.parse((await tool('prep_import_apkg', { name: 'from-apkg', apkg_base64: exported.apkg_base64 })).text) as {
      deck_name: string;
      inserted: number;
      cloze_skipped: number;
      errors: string[];
    };
    expect(imported.deck_name).toBe('from-apkg');
    expect(imported.inserted).toBeGreaterThan(0);
    expect(imported.cloze_skipped).toBe(0);
    expect(imported.errors).toEqual([]);
  });

  it('refuses a blob that is not an apkg, past its own validation', async () => {
    const result = await tool('prep_import_apkg', { name: 'restored', apkg_base64: 'AAAA' });
    expect(result.isError).toBe(true);
    expect(result.text).toContain('not a valid .apkg');
  });
});

// `/mcp` is the second door to both codecs and shares the isolate the page
// caps exist for, so it carries the same ceilings. Dispatched directly: the
// bearer gate is real above, and these need thousands of rows.
describe('the apkg tools under the same ceilings the pages have', () => {
  const deps = { apkg: new SqlJsApkg(), subject: 'someone', now: '2026-03-14T15:00:00+00:00' };

  async function direct(repos: V1Repos, name: string, args: Record<string, unknown>): Promise<{ text: string; isError: boolean }> {
    const out = (await dispatch(repos, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }, deps)) as {
      json: { result: { content: { text: string }[]; isError: boolean } };
    };
    return { text: out.json.result.content[0]!.text, isError: out.json.result.isError };
  }

  it('refuses a payload past the body ceiling from its encoded length alone', async () => {
    const c = cell();
    const oversized = 'A'.repeat(Math.ceil((MAX_APKG_UPLOAD_BYTES + 1024) / 3) * 4);
    const result = await direct(c.repos, 'prep_import_apkg', { name: 'restored', apkg_base64: oversized });
    expect(result).toEqual({ isError: true, text: uploadTooLarge(MAX_APKG_UPLOAD_BYTES) });
  });

  it('refuses an archive past the inflated ceiling in the words the page uses', async () => {
    const c = cell();
    const bomb = zipSync({ 'collection.anki21': new Uint8Array(48 * 1024 * 1024) }, { level: 9 });
    let binary = '';
    for (const b of bomb) binary += String.fromCharCode(b);
    const result = await direct(c.repos, 'prep_import_apkg', { name: 'restored', apkg_base64: btoa(binary) });
    expect(result).toEqual({ isError: true, text: ARCHIVE_TOO_LARGE });
  });

  it('refuses to export a deck the export hub refuses', async () => {
    const c = cell();
    const deckId = c.repos.decks.create('too-big');
    for (let i = 0; i <= MAX_EXPORT_QUESTIONS; i++) c.repos.questions.add(deckId, { type: 'short', prompt: `p${i}`, answer: 'a' });
    const result = await direct(c.repos, 'prep_export_deck_apkg', { name: 'too-big' });
    expect(result).toEqual({ isError: true, text: EXPORT_TOO_LARGE });
  });

  it('exports a deck at the cap', async () => {
    const c = cell();
    const deckId = c.repos.decks.create('exactly');
    for (let i = 0; i < MAX_EXPORT_QUESTIONS; i++) c.repos.questions.add(deckId, { type: 'short', prompt: `p${i}`, answer: 'a' });
    expect((await direct(c.repos, 'prep_export_deck_apkg', { name: 'exactly' })).isError).toBe(false);
  }, 30_000);
});

describe('the tool writes', () => {
  it('creates, renames, pins, edits and deletes through the catalog', async () => {
    const created = JSON.parse((await tool('prep_create_deck', { name: 'tool-deck', context_prompt: 'made here' })).text) as { name: string; id: number };
    expect(created.name).toBe('tool-deck');
    expect(JSON.parse((await tool('prep_get_deck', { name: 'tool-deck' })).text)).toMatchObject({ context_prompt: 'made here', card_count: 0 });

    const card = JSON.parse((await tool('prep_add_card', { deck: 'tool-deck', type: 'short', prompt: 'Q?', answer: 'A', answer_regex: 'a' })).text) as { id: number };
    expect(JSON.parse((await tool('prep_get_card', { card_id: card.id })).text)).toMatchObject({ type: 'short', prompt: 'Q?', answer: 'A', suspended: false });

    await tool('prep_suspend_card', { card_id: card.id, suspended: true });
    expect(JSON.parse((await tool('prep_get_card', { card_id: card.id })).text)).toMatchObject({ suspended: true });

    await tool('prep_update_card', { card_id: card.id, type: 'short', prompt: 'Q2?', answer: 'B' });
    expect(JSON.parse((await tool('prep_get_card', { card_id: card.id })).text)).toMatchObject({ prompt: 'Q2?', answer: 'B' });

    expect(JSON.parse((await tool('prep_rename_deck', { name: 'tool-deck', new_name: 'tool-renamed' })).text)).toEqual({ name: 'tool-renamed' });
    expect(JSON.parse((await tool('prep_set_deck_pinned', { name: 'tool-renamed', pinned: true })).text)).toEqual({ name: 'tool-renamed', pinned: true });
    expect(JSON.parse((await tool('prep_set_topic_prompt', { name: 'tool-renamed', context_prompt: '' })).text)).toEqual({
      name: 'tool-renamed',
      context_prompt: '',
    });

    expect(JSON.parse((await tool('prep_delete_card', { card_id: card.id })).text)).toEqual({ ok: true, deleted_id: card.id });
    expect(JSON.parse((await tool('prep_delete_deck', { name: 'tool-renamed' })).text)).toEqual({ ok: true, deleted: 'tool-renamed' });
    expect((await tool('prep_get_deck', { name: 'tool-renamed' })).isError).toBe(true);
  });

  it('imports and exports the same CSV shape', async () => {
    const csv = 'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\nshort,,Round trip?,yes,,,,,,\n';
    const outcome = JSON.parse((await tool('prep_import_csv', { name: 'csv-deck', csv })).text) as { inserted: number; errors: string[] };
    expect(outcome).toMatchObject({ deck_name: 'csv-deck', inserted: 1, skipped_duplicates: 0, errors: [] });
    expect((await tool('prep_export_deck_csv', { name: 'csv-deck' })).text).toBe(
      'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\r\nshort,,Round trip?,yes,,,,,,\r\n',
    );
  });
});
