// Declared in phase 1 so the migration tag is settled; behavior arrives later.
import { DurableObject } from 'cloudflare:workers';
import type { Env } from '../env.js';

export class DirectoryCell extends DurableObject<Env> {
  async fetch(_request: Request): Promise<Response> {
    return new Response('not implemented', { status: 501 });
  }
}
