// One user's cell. In phase 1 it serves the recorded Python pages for a seeded
// profile; the profile and the flags the pages set live in storage so an
// eviction mid-flow loses nothing.
import { DurableObject } from 'cloudflare:workers';
import { derive } from '../../app/viewmodels/derive.js';
import { appBase } from '../appBase.js';
import { compose } from '../compose.js';
import type { Env } from '../env.js';
import { errorPage } from '../errors.js';

export interface ParityState {
  profile: string;
  flags: string[];
}

const STATE_KEY = 'parity';

export class UnknownProfile extends Error {}

export class UserCell extends DurableObject<Env> {
  async seed(profile: string): Promise<Record<string, unknown>> {
    const seed = compose(this.env).pages.seed(profile);
    if (!seed) throw new UnknownProfile(`unknown profile ${JSON.stringify(profile)}`);
    await this.ctx.storage.put<ParityState>(STATE_KEY, { profile, flags: [] });
    return seed;
  }

  async fetch(request: Request): Promise<Response> {
    const c = compose(this.env);
    const url = new URL(request.url);
    const state = (await this.ctx.storage.get<ParityState>(STATE_KEY)) ?? null;
    const page = state && c.pages.resolve(state.profile, request.method, url.pathname, state.flags);
    if (!state || !page) return errorPage(c.renderer, c.buildToken, 404, request, 'Not Found');

    const headers: Record<string, string> = {};
    if (page.headers['content-type']) headers['content-type'] = page.headers['content-type'];
    if (page.headers.location) headers.location = page.headers.location;
    const body = page.template
      ? c.renderer.render(page.template, derive(page.template, { ...(page.context ?? {}), app_base: appBase(request) }))
      : (page.body ?? '');
    const response = new Response(body, { status: page.status, headers });

    if (page.sets.length) {
      const flags = [...new Set([...state.flags, ...page.sets])];
      await this.ctx.storage.put<ParityState>(STATE_KEY, { profile: state.profile, flags });
    }
    return response;
  }
}
