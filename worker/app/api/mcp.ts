// The MCP-over-HTTP server, transcribed from prep/api/mcp.py: one
// JSON-RPC 2.0 message per request, the tool catalog of tools.ts, and
// the same user-scoped repositories the REST surface uses.
import { parseIso } from '../../domain/py.js';
import { ankiNotesToDeck } from '../decks/anki.js';
import { buildApkg } from '../decks/ankiExport.js';
import {
  ARCHIVE_TOO_LARGE,
  EXPORT_TOO_LARGE,
  MAX_APKG_UPLOAD_BYTES,
  MAX_EXPORT_QUESTIONS,
  MAX_IMPORT_ROWS,
  MAX_ZIP_ENTRY_BYTES,
  MAX_ZIP_TOTAL_BYTES,
  uploadTooLarge,
} from '../decks/importLimits.js';
import type { NewQuestion, Question, QuestionType } from '../entities.js';
import { json, type ApiResult } from '../http.js';
import { NotAnApkg, ZipEntryTooLarge, type ApkgReader, type ApkgWriter } from '../ports.js';
import { csvToDeck, deckToCsv, questionsForExport } from './deckIo.js';
import { TOOLS } from './tools.js';
import { cardJson, type V1Repos } from './v1.js';

export const MCP_PROTOCOL_VERSION = '2025-06-18';
export const SERVER_NAME = 'prep';
export const SERVER_VERSION = '1.0.0';

const QUESTION_TYPES: readonly string[] = ['short', 'mcq', 'multi', 'code'];

export interface ToolResult {
  content: { type: 'text'; text: string }[];
  isError: boolean;
}

const toolError = (message: string): ToolResult => ({ content: [{ type: 'text', text: message }], isError: true });
const toolText = (text: string): ToolResult => ({ content: [{ type: 'text', text }], isError: false });

/** `json.dumps(value, ensure_ascii=False, indent=2)`. */
const dumps = (value: unknown): string => JSON.stringify(value, null, 2);

const arg = (args: Record<string, unknown>, name: string): string => {
  const raw = args[name];
  return typeof raw === 'string' ? raw.trim() : raw ? String(raw).trim() : '';
};

// ---- tools ----------------------------------------------------------------

type Handler = (repos: V1Repos, args: Record<string, unknown>) => ToolResult;

function cardToDict(q: Question): Record<string, unknown> {
  return {
    id: q.id,
    deck_id: q.deck_id,
    type: q.type,
    topic: q.topic,
    prompt: q.prompt,
    answer: q.answer,
    choices: q.choices,
    rubric: q.rubric,
    skeleton: q.skeleton,
    language: q.language,
    answer_regex: q.answer_regex,
    explanation: q.explanation,
    suspended: q.suspended,
  };
}

/** Shared by add and update: a NewQuestion, or the tool error to answer with. */
function buildNewQuestion(args: Record<string, unknown>): NewQuestion | ToolResult {
  const typeRaw = arg(args, 'type').toLowerCase();
  if (!QUESTION_TYPES.includes(typeRaw)) return toolError(`unknown type '${typeRaw}'; expected short|mcq|multi|code`);
  const prompt = arg(args, 'prompt');
  const answer = arg(args, 'answer');
  if (!prompt) return toolError('missing required arg: prompt');
  if (!answer) return toolError('missing required arg: answer');
  const choices = args['choices'];
  if (choices !== undefined && choices !== null && !Array.isArray(choices)) return toolError('choices must be an array of strings');
  return {
    type: typeRaw as QuestionType,
    topic: arg(args, 'topic') || null,
    prompt,
    answer,
    choices: Array.isArray(choices) && choices.length ? choices.map((c) => String(c)) : null,
    rubric: arg(args, 'rubric') || null,
    skeleton: arg(args, 'skeleton') || null,
    language: arg(args, 'language') || null,
    answer_regex: arg(args, 'answer_regex') || null,
    explanation: arg(args, 'explanation') || null,
  };
}

const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v);

const HANDLERS: Record<string, Handler> = {
  prep_list_decks: (repos) =>
    toolText(
      dumps(
        repos.decks.listSummaries().map((s) => ({
          name: s.name,
          type: s.deck_type || 'srs',
          card_count: s.total,
          due: s.due,
          pinned: s.pinned,
        })),
      ),
    ),

  prep_get_deck: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const deckId = repos.decks.findId(name);
    if (deckId === null) return toolError(`deck not found: '${name}'`);
    const meta = repos.decks.getMeta(deckId);
    return toolText(
      dumps({
        name,
        type: repos.decks.getType(deckId) || 'srs',
        context_prompt: meta.context_prompt,
        card_count: repos.questions.listInDeck(deckId).length,
      }),
    );
  },

  prep_list_cards: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const deckId = repos.decks.findId(name);
    if (deckId === null) return toolError(`deck not found: '${name}'`);
    return toolText(dumps({ deck: name, cards: questionsForExport(repos, deckId).map(cardJson) }));
  },

  prep_export_deck_csv: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const deckId = repos.decks.findId(name);
    if (deckId === null) return toolError(`deck not found: '${name}'`);
    return toolText(deckToCsv(repos, deckId));
  },

  prep_create_deck: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const context = arg(args, 'context_prompt') || null;
    if (repos.decks.findId(name) !== null) return toolError(`deck '${name}' already exists`);
    return toolText(dumps({ name, id: repos.decks.create(name, { contextPrompt: context }) }));
  },

  prep_import_csv: (repos, args) => {
    const name = arg(args, 'name');
    const csvText = typeof args['csv'] === 'string' ? (args['csv'] as string) : '';
    if (!name) return toolError('missing required arg: name');
    if (!csvText.trim()) return toolError('missing required arg: csv (full CSV body)');
    return toolText(dumps(csvToDeck(repos, name, csvText)));
  },

  prep_rename_deck: (repos, args) => {
    const name = arg(args, 'name');
    const newName = arg(args, 'new_name');
    if (!name || !newName) return toolError('missing required args: name, new_name');
    if (repos.decks.findId(name) === null) return toolError(`deck not found: '${name}'`);
    if (repos.decks.findId(newName) !== null) return toolError(`deck '${newName}' already exists`);
    if (!repos.decks.rename(name, newName)) return toolError('rename failed');
    return toolText(dumps({ name: newName }));
  },

  prep_delete_deck: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    if (repos.decks.findId(name) === null) return toolError(`deck not found: '${name}'`);
    repos.decks.delete(name);
    return toolText(dumps({ ok: true, deleted: name }));
  },

  prep_set_deck_pinned: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const pinned = args['pinned'];
    if (typeof pinned !== 'boolean') return toolError('pinned must be a boolean');
    const deckId = repos.decks.findId(name);
    if (deckId === null) return toolError(`deck not found: '${name}'`);
    repos.decks.setPinned(deckId, pinned);
    return toolText(dumps({ name, pinned }));
  },

  prep_set_topic_prompt: (repos, args) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const context = typeof args['context_prompt'] === 'string' ? (args['context_prompt'] as string) : '';
    if (repos.decks.findId(name) === null) return toolError(`deck not found: '${name}'`);
    repos.decks.updateContextPrompt(name, context);
    return toolText(dumps({ name, context_prompt: context }));
  },

  prep_get_card: (repos, args) => {
    const cardId = args['card_id'];
    if (!isInt(cardId)) return toolError('card_id must be an integer');
    const q = repos.questions.get(cardId);
    if (q === null) return toolError(`card not found: ${cardId}`);
    return toolText(dumps(cardToDict(q)));
  },

  prep_add_card: (repos, args) => {
    const deckName = arg(args, 'deck');
    if (!deckName) return toolError('missing required arg: deck');
    const deckId = repos.decks.findId(deckName);
    if (deckId === null) return toolError(`deck not found: '${deckName}'`);
    const built = buildNewQuestion(args);
    if ('isError' in built) return built;
    return toolText(dumps({ id: repos.questions.add(deckId, built) }));
  },

  prep_update_card: (repos, args) => {
    const cardId = args['card_id'];
    if (!isInt(cardId)) return toolError('card_id must be an integer');
    if (repos.questions.get(cardId) === null) return toolError(`card not found: ${cardId}`);
    const built = buildNewQuestion(args);
    if ('isError' in built) return built;
    repos.questions.update(cardId, built);
    return toolText(dumps({ id: cardId }));
  },

  prep_delete_card: (repos, args) => {
    const cardId = args['card_id'];
    if (!isInt(cardId)) return toolError('card_id must be an integer');
    if (!repos.questions.delete(cardId)) return toolError(`card not found: ${cardId}`);
    return toolText(dumps({ ok: true, deleted_id: cardId }));
  },

  prep_suspend_card: (repos, args) => {
    const cardId = args['card_id'];
    const suspended = args['suspended'];
    if (!isInt(cardId)) return toolError('card_id must be an integer');
    if (typeof suspended !== 'boolean') return toolError('suspended must be a boolean');
    if (repos.questions.get(cardId) === null) return toolError(`card not found: ${cardId}`);
    repos.questions.setSuspended(cardId, suspended);
    return toolText(dumps({ id: cardId, suspended }));
  },

};

/** The two tools whose codec is WASM-backed, and so answers a promise. */
const ASYNC_HANDLERS: Record<string, (repos: V1Repos, args: Record<string, unknown>, deps: McpDeps) => Promise<ToolResult>> = {
  prep_export_deck_apkg: async (repos, args, deps) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const deckId = repos.decks.findId(name);
    if (deckId === null) return toolError(`deck not found: '${name}'`);
    const questions = questionsForExport(repos, deckId);
    // `/mcp` is the second door to both codecs and shares the isolate the
    // page caps exist for, so it answers the export hub's refusal too.
    if (questions.length > MAX_EXPORT_QUESTIONS) return toolError(EXPORT_TOO_LARGE);
    const nowMs = parseIso(deps.now).getTime();
    const { col, notes, cards } = buildApkg(name, questions, deps.subject, nowMs, deps.now.slice(0, 10));
    const blob = await deps.apkg.build(col, notes, cards);
    return toolText(dumps({ filename: `${name}.apkg`, apkg_base64: base64(blob), byte_count: blob.length }));
  },

  prep_import_apkg: async (repos, args, deps) => {
    const name = arg(args, 'name');
    if (!name) return toolError('missing required arg: name');
    const b64 = typeof args['apkg_base64'] === 'string' ? (args['apkg_base64'] as string) : '';
    if (!b64) return toolError('missing required arg: apkg_base64');
    // The encoded length bounds the decode, so an oversized payload is
    // refused without ever being turned into bytes.
    if (decodedLength(b64) > MAX_APKG_UPLOAD_BYTES) return toolError(uploadTooLarge(MAX_APKG_UPLOAD_BYTES));
    if (!isStrictBase64(b64)) return toolError("apkg_base64 didn't decode: Only base64 data is allowed");
    let notes;
    try {
      notes = await deps.apkg.notes(unbase64(b64), { maxEntryBytes: MAX_ZIP_ENTRY_BYTES, maxTotalBytes: MAX_ZIP_TOTAL_BYTES });
    } catch (e) {
      if (e instanceof ZipEntryTooLarge) return toolError(ARCHIVE_TOO_LARGE);
      if (e instanceof NotAnApkg) return toolError(e.message);
      throw e;
    }
    return toolText(dumps(ankiNotesToDeck(repos, name, notes, { noteCap: MAX_IMPORT_ROWS })));
  },
};

/** Bytes `atob` would produce, from the encoded length alone. */
const decodedLength = (b64: string): number => {
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
  return Math.floor((b64.length * 3) / 4) - padding;
};

const B64_CHUNK = 8192;

const base64 = (bytes: Uint8Array): string => {
  let binary = '';
  // Chunked: one `+=` per byte builds a rope the length of the archive.
  for (let at = 0; at < bytes.length; at += B64_CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(at, at + B64_CHUNK));
  }
  return btoa(binary);
};

const unbase64 = (text: string): Uint8Array => {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
};

/** `base64.b64decode(s, validate=True)`: alphabet-only, length a multiple of four. */
function isStrictBase64(s: string): boolean {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s)) return false;
  return s.length % 4 === 0;
}

// ---- JSON-RPC -------------------------------------------------------------

const rpcResult = (id: unknown, result: unknown) => ({ jsonrpc: '2.0', id: id ?? null, result });

function rpcError(id: unknown, code: number, message: string, data?: unknown) {
  const err: Record<string, unknown> = { code, message };
  if (data !== undefined && data !== null) err['data'] = data;
  return { jsonrpc: '2.0', id: id ?? null, error: err };
}

export const PARSE_ERROR = () => json(rpcError(null, -32700, 'Parse error'), 400);
export const INVALID_REQUEST = () => json(rpcError(null, -32600, 'Invalid Request'), 400);

/** What the two `.apkg` tools need beyond the repositories. */
export interface McpDeps {
  apkg: ApkgReader & ApkgWriter;
  /** The owner, whose first eight characters seed each exported note's guid. */
  subject: string;
  now: string;
}

/** One JSON-RPC message. `body` is the parsed request; parse failure is the caller's. */
export async function dispatch(repos: V1Repos, body: unknown, deps: McpDeps): Promise<ApiResult> {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return INVALID_REQUEST();
  const message = body as Record<string, unknown>;
  const id = message['id'] ?? null;
  const method = message['method'];
  const params = (message['params'] ?? {}) as Record<string, unknown>;

  if (method === 'initialize') {
    return json(
      rpcResult(id, {
        protocolVersion: MCP_PROTOCOL_VERSION,
        capabilities: { tools: {} },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
      }),
    );
  }
  if (method === 'notifications/initialized') return { json: null, status: 204 };
  if (method === 'tools/list') return json(rpcResult(id, { tools: TOOLS }));
  if (method === 'tools/call') {
    const name = params['name'];
    const args = (params['arguments'] ?? {}) as Record<string, unknown>;
    const handler = typeof name === 'string' ? HANDLERS[name] : undefined;
    const asyncHandler = typeof name === 'string' ? ASYNC_HANDLERS[name] : undefined;
    if (!handler && !asyncHandler) return json(rpcError(id, -32602, `unknown tool: ${pyRepr(name)}`));
    try {
      return json(rpcResult(id, handler ? handler(repos, args) : await asyncHandler!(repos, args, deps)));
    } catch (e) {
      return json(rpcResult(id, toolError(`tool error: ${e instanceof Error ? e.message : String(e)}`)));
    }
  }
  return json(rpcError(id, -32601, `unknown method: ${pyRepr(method)}`));
}

/** `repr()` of the value a client sent, for the error message. */
function pyRepr(value: unknown): string {
  if (typeof value === 'string') return `'${value.split('\\').join('\\\\').split("'").join("\\'")}'`;
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  return String(value);
}
