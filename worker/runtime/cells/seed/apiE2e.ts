import { assembleToken, maskToken } from '../../../domain/pat.js';
import type { SeedContext } from './index.js';

/** The secret half of the e2e bearer token. Fixed so the suite can name the
 * token without a mint round-trip; it authenticates only against a seeded
 * cell, which no deploy serving real users has. */
const E2E_SECRET = new Uint8Array(32).fill(0x2e);

/** The token the e2e API suites present, for the owner they present it as.
 * The subject rides in the token, so it can only be built once the user is
 * known. */
export function apiE2eToken(user: string): string {
  return assembleToken(user, E2E_SECRET);
}

/** An owner whose only fixture is a usable PAT: the API suites create their
 * own decks through the public API and need nothing else. Kept apart from
 * `reader`, whose legacy-format token pins a masked prefix in a pixel
 * golden and cannot authenticate. */
export async function profileApiE2e(ctx: SeedContext): Promise<Record<string, unknown>> {
  const { repos, user, hasher, at } = ctx;
  const plaintext = apiE2eToken(user);
  const token = repos.tokens.insert(await hasher.sha256Hex(plaintext), maskToken(plaintext), 'e2e');
  repos.pins.tokenCreatedAt(token.id, at({ days: -1 }));
  return { api_tokens: [token.id] };
}
