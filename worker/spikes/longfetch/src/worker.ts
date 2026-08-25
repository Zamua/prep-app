import { DurableObject } from 'cloudflare:workers';

interface Env {
  SLOW: DurableObjectNamespace;
  SLOW_ORIGIN: string;
}

async function timedFetch(origin: string, seconds: number) {
  const t0 = Date.now();
  try {
    const res = await fetch(`${origin}/sleep/${seconds}`);
    const text = await res.text();
    return { ok: true, status: res.status, body: text, elapsedMs: Date.now() - t0 };
  } catch (e) {
    return { ok: false, error: String(e), elapsedMs: Date.now() - t0 };
  }
}

export class SlowCell extends DurableObject<Env> {
  async slow(seconds: number) {
    return timedFetch(this.env.SLOW_ORIGIN, seconds);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const m = url.pathname.match(/^\/(entry|cell)\/(\d+)$/);
    if (!m) return new Response('use /entry/<s> or /cell/<s>', { status: 404 });
    const seconds = Number(m[2]);
    if (m[1] === 'entry') return Response.json({ where: 'entry', ...(await timedFetch(env.SLOW_ORIGIN, seconds)) });
    const stub = env.SLOW.get(env.SLOW.idFromName('slow')) as unknown as { slow(s: number): Promise<unknown> };
    return Response.json({ where: 'cell', ...(await stub.slow(seconds) as object) });
  },
};
