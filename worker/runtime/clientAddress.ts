// The requesting client's address as the ingress reports it. Only the
// ingress-set header is read, never a client-forgeable one.
import { limiterBucket, SENTINEL_BUCKET } from '../domain/instant/index.js';

const DEFAULT_CLIENT_IP_HEADER = 'x-real-ip';
const XFF_LAST_MODE = 'x-forwarded-for-last';

export interface ClientIpEnv {
  PREP_CLIENT_IP_HEADER?: string;
}

/** The raw value of the configured header, or '' when it is absent. */
export function clientAddress(request: Request, env: ClientIpEnv): string {
  const mode = (env.PREP_CLIENT_IP_HEADER || DEFAULT_CLIENT_IP_HEADER).trim().toLowerCase();
  if (mode === XFF_LAST_MODE) {
    const raw = request.headers.get('x-forwarded-for') ?? '';
    return (raw.split(',').pop() ?? '').trim();
  }
  return (request.headers.get(mode) ?? '').trim();
}

/**
 * The limiter bucket for the requesting client. A missing or non-IP value
 * fails closed to the shared sentinel, never to a client-forgeable address.
 */
export function clientIp(request: Request, env: ClientIpEnv): string {
  const value = clientAddress(request, env);
  return value ? limiterBucket(value) : SENTINEL_BUCKET;
}
