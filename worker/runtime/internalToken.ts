// The harness and migration routes open only to a caller holding the
// deploy's internal token.
import { tagsEqual } from '../domain/tagsEqual.js';

export const INTERNAL_TOKEN_HEADER = 'x-internal-token';

/** The refusal for a request without the token, or null when it may pass. A
 * deploy with no token configured opens nothing. */
export function refuseWithoutInternalToken(request: Request, token: string): Response | null {
  if (!token) return Response.json({ detail: 'PREP_INTERNAL_TOKEN not configured' }, { status: 503 });
  if (!tagsEqual(request.headers.get(INTERNAL_TOKEN_HEADER) ?? '', token)) return Response.json({ detail: 'invalid X-Internal-Token' }, { status: 401 });
  return null;
}
