// Reports what the entry worker and a cell each see in env, plus whether
// a process.env global exists at all.
import { DurableObject } from 'cloudflare:workers';

interface Env {
  PROBE: DurableObjectNamespace;
  [k: string]: unknown;
}

function snapshot(env: Env) {
  const g = globalThis as Record<string, unknown>;
  const proc = g.process as { env?: Record<string, string> } | undefined;
  const proc2 = g.process as { versions?: unknown } | undefined;
  return {
    keys: Object.keys(env).sort(),
    values: Object.fromEntries(Object.entries(env).filter(([, v]) => typeof v === 'string')),
    processVersions: proc2?.versions ?? null,
    PUBLIC_VAR: env.PUBLIC_VAR ?? null,
    OVERRIDE_ME: env.OVERRIDE_ME ?? null,
    NODE_SECRET: env.NODE_SECRET ?? null,
    hasProcessGlobal: typeof proc !== 'undefined',
    processEnvNodeSecret: proc?.env?.NODE_SECRET ?? null,
  };
}

export class ProbeCell extends DurableObject<Env> {
  probe() {
    return snapshot(this.env);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') return new Response('ok');
    const stub = env.PROBE.get(env.PROBE.idFromName('probe')) as unknown as { probe(): Promise<unknown> };
    const body = { entry: snapshot(env), cell: await stub.probe() };
    return Response.json(body);
  },
};
