// The abuse log: one JSON line per abuse event on the console, carrying the
// client only as its subnet. Normal requests, paths, headers, bodies and user
// ids never reach it. Called after the handler, so it cannot change a
// response, and it never throws.
import { abuseEventOf, abuseLine, routeKindOf } from '../domain/abuse.js';
import { clientAddress, type ClientIpEnv } from './clientAddress.js';

interface EdgeProps {
  country?: unknown;
}

export function noteResponse(request: Request, url: URL, env: ClientIpEnv, status: number): void {
  try {
    const route = routeKindOf(url.pathname);
    const event = abuseEventOf(route, status);
    if (!event) return;
    // `cf` exists only when the Workers runtime built the request, which is
    // also the only place the edge-set header is trustworthy.
    const edge = (request as { cf?: EdgeProps }).cf;
    const ip = clientAddress(request, env) || (edge ? request.headers.get('cf-connecting-ip') : null);
    console.log(JSON.stringify(abuseLine(event, { route, status, ip, country: edge?.country })));
  } catch {
    // A log failure must not fail the request it describes.
  }
}
