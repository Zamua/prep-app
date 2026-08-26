// POST /api/instant/generate, in the entry worker: a visitor has no cell,
// so the limiter, the directory and the mint all run here. The body is
// parsed by hand so every failure keeps the `kind` wire shape.
import { generateInstantDeck, type InstantDeps } from '../../app/instant/generate.js';
import { limiterBucket, SENTINEL_BUCKET } from '../../domain/instant/index.js';
import { ANON_COOKIE_HEADER } from '../compose.js';

const DEFAULT_CLIENT_IP_HEADER = 'x-real-ip';
const XFF_LAST_MODE = 'x-forwarded-for-last';

/** Far above any legitimate topic payload, and enforced before the parse:
 * the endpoint is anonymous and the limiter only runs after it. */
export const MAX_BODY_BYTES = 16 * 1024;

export interface ClientIpEnv {
  PREP_CLIENT_IP_HEADER?: string;
}

/**
 * The limiter bucket for the requesting client. Only the ingress-set header
 * is read; a missing or non-IP value fails closed to the shared sentinel,
 * never to a client-forgeable address.
 */
export function clientIp(request: Request, env: ClientIpEnv): string {
  const mode = (env.PREP_CLIENT_IP_HEADER || DEFAULT_CLIENT_IP_HEADER).trim().toLowerCase();
  let value: string;
  if (mode === XFF_LAST_MODE) {
    const raw = request.headers.get('x-forwarded-for') ?? '';
    value = (raw.split(',').pop() ?? '').trim();
  } else {
    value = (request.headers.get(mode) ?? '').trim();
  }
  if (!value) return SENTINEL_BUCKET;
  return limiterBucket(value);
}

/** Body bytes, or null when the request must be refused: absent, non-integer
 * or over-cap Content-Length, or a stream that runs past the cap. */
export async function readBody(request: Request): Promise<Uint8Array | null> {
  const declared = request.headers.get('content-length');
  if (declared === null || !/^\d+$/.test(declared.trim()) || Number(declared) > MAX_BODY_BYTES) return null;
  const raw = new Uint8Array(await request.arrayBuffer());
  return raw.length > MAX_BODY_BYTES ? null : raw;
}

export interface InstantIdentity {
  userId: string | null;
  /** null when there is no account row yet, which takes the anonymous budget. */
  userIsAnonymous: boolean | null;
}

export async function serveInstant(
  request: Request,
  deps: InstantDeps & ClientIpEnv,
  who: InstantIdentity,
): Promise<Response> {
  const ip = clientIp(request, deps);
  let body: unknown = null;
  try {
    const raw = await readBody(request);
    body = raw === null ? null : JSON.parse(new TextDecoder().decode(raw));
  } catch {
    // Malformed, oversized and aborted bodies refuse alike.
    body = null;
  }
  const outcome = await generateInstantDeck(deps, { ip, body, userId: who.userId, userIsAnonymous: who.userIsAnonymous });
  if (!outcome.ok) {
    const headers: Record<string, string> = {};
    if (outcome.retryAfterS !== null) headers['retry-after'] = String(outcome.retryAfterS);
    return Response.json(outcome.payload, { status: outcome.status, headers });
  }
  const res = Response.json({ kind: 'ok', redirect: `/deck/${outcome.slug}` });
  // The cookie itself is the composition root's to write; the handler only
  // names the account it just minted.
  if (outcome.mintedId !== null) res.headers.set(ANON_COOKIE_HEADER, `mint=${outcome.mintedId}`);
  return res;
}
