// One user's day as the requests their devices send: the study loop, card
// authoring over MCP and in the app, and the page reads. Every action goes
// through the entry worker, so the whole request path is what gets measured.
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { INTERNAL_TOKEN, ORIGIN, SEED_USER } from '../api/harness.js';
import { DECKS, type Account } from './account.js';

const SIGNED_IN = {
  'tailscale-user-login': SEED_USER,
  'tailscale-user-name': 'Seed',
  'x-internal-token': INTERNAL_TOKEN,
};

type Session = { id: string; version: number; status: string; state: string };
type View = { card?: { question_id: number }; session: Session };

export class Day {
  private view: View | null = null;
  private seq = 0;

  constructor(
    private readonly env: Env,
    private readonly account: Account,
  ) {}

  private async send(path: string, init: RequestInit & { headers?: Record<string, string> }, auth: 'session' | 'token' = 'session'): Promise<Response> {
    const headers: Record<string, string> = { accept: 'application/json', ...(init.headers ?? {}) };
    if (auth === 'session') Object.assign(headers, SIGNED_IN);
    else headers['authorization'] = `Bearer ${this.account.token}`;
    const res = await worker.fetch(new Request(`${ORIGIN}${path}`, { ...init, headers }), this.env);
    if (res.status >= 500) throw new Error(`${init.method ?? 'GET'} ${path}: ${res.status} ${await res.text()}`);
    return res;
  }

  private async postJson(path: string, body: unknown, auth: 'session' | 'token' = 'session'): Promise<Response> {
    return this.send(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, auth);
  }

  private async tool(name: string, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const res = await this.postJson('/mcp', { jsonrpc: '2.0', id: ++this.seq, method: 'tools/call', params: { name, arguments: args } }, 'token');
    const body = (await res.json()) as { result: { content: { text: string }[]; isError?: boolean } };
    if (body.result.isError) throw new Error(`${name}: ${body.result.content[0]?.text}`);
    return JSON.parse(body.result.content[0]?.text ?? '{}') as Record<string, unknown>;
  }

  // ---- the study loop --------------------------------------------------------

  async begin(deck = DECKS[0]!): Promise<void> {
    const res = await this.postJson(`/api/study/decks/${deck}/session`, {});
    this.view = (await res.json()) as View;
  }

  /** True when there was a card to answer. */
  async submit(right = true): Promise<boolean> {
    const view = this.view;
    if (!view?.card) return false;
    const qid = view.card.question_id;
    const known = this.account.answers.get(qid);
    const body: Record<string, unknown> = { question_id: qid, version: view.session.version, idk: false };
    if (!known || known.type === 'self') {
      body['answer'] = 'my attempt';
      body['verdict'] = right ? 'right' : 'wrong';
    } else {
      body['answer'] = right ? known.answer : 'nope';
    }
    const res = await this.postJson(`/api/study/sessions/${view.session.id}/submit`, body);
    const out = (await res.json()) as { session: Session };
    this.view = { session: out.session };
    return true;
  }

  async advance(): Promise<void> {
    const view = this.view;
    if (!view) return;
    const res = await this.postJson(`/api/study/sessions/${view.session.id}/advance`, { version: view.session.version });
    this.view = (await res.json()) as View;
  }

  // ---- authoring ------------------------------------------------------------

  async mcpAddCard(deck: string, n: number): Promise<number> {
    const out = await this.tool('prep_add_card', { deck, type: 'short', prompt: `mcp card ${n} for ${deck}`, answer: `mcp answer ${n}` });
    return Number(out['id']);
  }

  async mcpEditCard(id: number, n: number): Promise<void> {
    await this.tool('prep_update_card', { card_id: id, type: 'short', prompt: `edited card ${n}`, answer: `edited answer ${n}` });
  }

  async appAddCard(deck: string, n: number): Promise<void> {
    const res = await this.postJson('/api/study/cards', { deck_id: this.account.deckIds.get(deck), prompt: `in-app card ${n}`, answer: `in-app answer ${n}` });
    if (res.status !== 201) throw new Error(`in-app add: ${res.status} ${await res.text()}`);
  }

  // ---- reads ----------------------------------------------------------------

  async dashboard(): Promise<void> {
    await this.send('/', { headers: { accept: 'text/html' } });
  }

  async deckPage(deck = DECKS[0]!): Promise<void> {
    await this.send(`/deck/${deck}`, { headers: { accept: 'text/html' } });
  }
}
