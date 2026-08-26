// The MCP endpoint: the catalog an external client negotiates against,
// the JSON-RPC envelopes, and the tool-level refusals the corpus does not
// reach. Driven through the entry worker so the bearer gate is real.
import { beforeAll, describe, expect, it } from 'vitest';
import { APKG_PENDING, MCP_PROTOCOL_VERSION } from '../../app/api/mcp.js';
import { TOOLS } from '../../app/api/tools.js';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { loadCorpus, mintToken, ORIGIN, PARITY_USER, replayEnv, seed } from './harness.js';

let env: Env;
let bearer: string;

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
  const replay = replayEnv();
  env = replay.env;
  await seed(env, 'reader', PARITY_USER);
  bearer = await mintToken(replay.userStorage(PARITY_USER), PARITY_USER, 'mcp');
}, 60_000);

describe('the tool catalog', () => {
  it('is the seventeen objects the corpus recorded', async () => {
    const corpus = loadCorpus('contracts');
    const recorded = corpus.pairs.find((p) => p.name === 'mcp-tools-list')!.response.json as { result: { tools: unknown[] } };
    expect(TOOLS).toHaveLength(17);
    const { json } = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    expect((json as { result: { tools: unknown[] } }).result.tools).toEqual(recorded.result.tools);
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
    expect(json).toEqual({ jsonrpc: '2.0', id: 3, error: { code: -32601, message: "unknown method: 'resources/list'" } });
  });

  it('answers an unknown tool 200 with code -32602', async () => {
    const { json } = await call('prep_nope');
    expect(json).toEqual({ jsonrpc: '2.0', id: 7, error: { code: -32602, message: "unknown tool: 'prep_nope'" } });
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

  it('refuses the apkg codecs until phase 5, past their own validation', async () => {
    expect(await tool('prep_export_deck_apkg', { name: 'world-capitals' })).toEqual({ isError: true, text: APKG_PENDING });
    expect(await tool('prep_import_apkg', { name: 'restored', apkg_base64: 'AAAA' })).toEqual({ isError: true, text: APKG_PENDING });
  });
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
