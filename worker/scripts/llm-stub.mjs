// Canned OpenAI-compatible chat-completions server, so a local node can run
// the AI flows without an upstream, and so tests/agents.test.ts can drive one
// with a real socket in front of it.
//
//   node scripts/llm-stub.mjs [--port 8089] [--fixtures <dir>] [--record]
//
// Replay is keyed on `messages` alone, so a model rename never invalidates a
// fixture, and a HIT is byte-for-byte proof that the envelope the caller sent
// is the one the fixture was recorded for. A miss is a 404 plus the request
// body under `<fixtures>/missing/`; with --record it is one forwarded call to
// the real upstream whose answer becomes the fixture. A miss means a prompt
// stopped being deterministic: fix the prompt, never the key.
//
// Control endpoints under `/_control/`, POST unless noted: `hold`, `release`,
// GET `held`, `latency` {ms}, `canned` {content} (the answer served when no
// fixture matches, null to clear), `reset`, GET `requests`.
//
// Recording, once, against the real free tier:
//   PREP_LLM_UPSTREAM_BASE_URL=<base>/v1 PREP_LLM_UPSTREAM_API_KEY=<key> \
//   PREP_LLM_UPSTREAM_MODEL=<model> node scripts/llm-stub.mjs --record
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_FIXTURES = join(HERE, '..', 'tests', 'fixtures', 'llm');

// A held request answers 503 after this, so a forgotten hold cannot hang a run.
const HOLD_TIMEOUT_MS = 120_000;

function parseArgs(argv) {
  const out = { port: 8089, fixtures: DEFAULT_FIXTURES, record: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--port') out.port = Number(argv[++i]);
    else if (argv[i] === '--fixtures') out.fixtures = argv[++i];
    else if (argv[i] === '--record') out.record = true;
  }
  return out;
}

/** Key-sorted, space-free JSON: the same bytes whatever order a caller built
 * the object in, so a fixture keeps resolving. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

const keyOf = (messages) => createHash('sha256').update(canonical(messages), 'utf8').digest('hex');
const fixtureFile = (dir, key) => join(dir, `${key.slice(0, 16)}.json`);

/** The recorded response text, or null. A renamed or hand-edited file must
 * not serve under a key it was not recorded for. */
function loadFixture(dir, key) {
  const path = fixtureFile(dir, key);
  if (!existsSync(path)) return null;
  const data = JSON.parse(readFileSync(path, 'utf8'));
  if (data.key !== key) throw new Error(`${path} holds key ${JSON.stringify(data.key)}, expected ${JSON.stringify(key)}`);
  if (typeof data.body !== 'string') throw new Error(`${path}: body must be the upstream response text`);
  return data.body;
}

function writeFixture(dir, messages, body) {
  mkdirSync(dir, { recursive: true });
  const key = keyOf(messages);
  writeFileSync(fixtureFile(dir, key), `${JSON.stringify({ key, messages, body }, null, 2)}\n`);
}

function noteMissing(dir, key, body) {
  const missing = join(dir, 'missing');
  mkdirSync(missing, { recursive: true });
  writeFileSync(fixtureFile(missing, key), `${JSON.stringify(body, null, 2)}\n`);
}

async function forward(body) {
  const base = (process.env.PREP_LLM_UPSTREAM_BASE_URL ?? '').trim().replace(/\/$/, '');
  const apiKey = (process.env.PREP_LLM_UPSTREAM_API_KEY ?? '').trim();
  const model = (process.env.PREP_LLM_UPSTREAM_MODEL ?? '').trim();
  const missing = [
    ['PREP_LLM_UPSTREAM_BASE_URL', base],
    ['PREP_LLM_UPSTREAM_API_KEY', apiKey],
    ['PREP_LLM_UPSTREAM_MODEL', model],
  ]
    .filter(([, v]) => !v)
    .map(([n]) => n);
  if (missing.length) throw new Error(`recording needs ${missing.join(', ')}`);
  const res = await fetch(`${base}/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ ...body, model }),
  });
  return { status: res.status, text: await res.text() };
}

/** One chat-completions answer carrying `content`, in the shape the
 * OpenAI-compatible clients parse. */
const cannedCompletion = (content, model) => ({
  id: 'stub-canned',
  object: 'chat.completion',
  model: model || 'stub-model',
  choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content } }],
  usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
});

const opts = parseArgs(process.argv.slice(2));
const state = { holding: false, waiters: [], held: [], latencyMs: 0, requests: [], canned: null };

function release() {
  state.holding = false;
  for (const resolve of state.waiters.splice(0)) resolve(true);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

const sendJson = (res, status, payload) => {
  const data = Buffer.from(JSON.stringify(payload), 'utf8');
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': data.length });
  res.end(data);
};

const sendRaw = (res, status, text) => {
  const data = Buffer.from(text, 'utf8');
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': data.length });
  res.end(data);
};

async function completion(res, body) {
  const messages = body && typeof body === 'object' ? body.messages : null;
  if (!Array.isArray(messages)) return sendJson(res, 400, { error: 'messages must be a list' });
  const key = keyOf(messages);
  state.requests.push(key);
  if (state.holding) {
    state.held.push(key);
    const released = await Promise.race([new Promise((r) => state.waiters.push(r)), sleep(HOLD_TIMEOUT_MS).then(() => false)]);
    state.held.splice(state.held.indexOf(key), 1);
    if (!released) return sendJson(res, 503, { error: `held request timed out for ${key}` });
  }
  if (state.latencyMs) await sleep(state.latencyMs);

  let text;
  try {
    text = loadFixture(opts.fixtures, key);
  } catch (e) {
    return sendJson(res, 500, { error: String(e instanceof Error ? e.message : e) });
  }
  if (text === null) {
    // A caller that pins the answer is standing in for the agent itself;
    // nothing is written to the fixture set.
    if (state.canned !== null) return sendJson(res, 200, cannedCompletion(state.canned, body.model));
    if (!opts.record) {
      noteMissing(opts.fixtures, key, body);
      return sendJson(res, 404, { error: `no fixture for ${key}` });
    }
    const { status, text: upstream } = await forward(body);
    // Only a good answer becomes a fixture; the failure passes through.
    if (status !== 200) return sendRaw(res, status, upstream);
    text = upstream;
    writeFixture(opts.fixtures, messages, text);
  }
  sendRaw(res, 200, text);
}

const server = createServer(async (req, res) => {
  const path = (req.url ?? '').split('?')[0];
  if (req.method === 'GET') {
    if (path === '/_control/held') return sendJson(res, 200, { count: state.held.length, keys: [...state.held] });
    if (path === '/_control/requests') return sendJson(res, 200, { count: state.requests.length, keys: [...state.requests] });
    return sendJson(res, 404, { error: `no route for GET ${path}` });
  }
  if (req.method !== 'POST') return sendJson(res, 404, { error: `no route for ${req.method} ${path}` });

  const raw = await readBody(req);
  let body = null;
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      return sendJson(res, 400, { error: 'request body is not JSON' });
    }
  }
  if (path === '/v1/chat/completions') return completion(res, body);
  if (path === '/_control/hold') {
    state.holding = true;
    return sendJson(res, 200, { holding: true });
  }
  if (path === '/_control/release') {
    release();
    return sendJson(res, 200, { holding: false });
  }
  if (path === '/_control/latency') {
    const ms = body && typeof body === 'object' ? body.ms : null;
    if (typeof ms !== 'number' || ms < 0) return sendJson(res, 400, { error: 'latency needs a non-negative ms' });
    state.latencyMs = ms;
    return sendJson(res, 200, { ms });
  }
  if (path === '/_control/canned') {
    const content = body && typeof body === 'object' ? (body.content ?? null) : null;
    if (content !== null && typeof content !== 'string') return sendJson(res, 400, { error: 'canned needs a string content, or null' });
    state.canned = content;
    return sendJson(res, 200, { canned: content !== null });
  }
  if (path === '/_control/reset') {
    release();
    state.latencyMs = 0;
    state.requests.length = 0;
    state.canned = null;
    return sendJson(res, 200, { reset: true });
  }
  return sendJson(res, 404, { error: `no route for POST ${path}` });
});

server.listen(opts.port, '127.0.0.1', () => {
  const { port } = server.address();
  // The banner is the port handshake: a caller starting this on port 0 reads
  // the base URL off the first line.
  console.log(`llm stub: http://127.0.0.1:${port}/v1 (fixtures ${opts.fixtures})`);
});
