// Personal access tokens: minting and the SHA-256 the owner's cell compares
// against `api_tokens.token_hash`. The format itself is `domain/pat`.
import type { Hasher, Random } from '../../app/ports.js';
import { SECRET_BYTES, assembleToken, maskToken, parseToken } from '../../domain/pat.js';

export interface IssuedToken {
  token: string;
  hash: string;
  mask: string;
}

export class PatIssuer {
  constructor(
    private readonly random: Random,
    private readonly hasher: Hasher,
  ) {}

  async issue(subject: string): Promise<IssuedToken> {
    const token = assembleToken(subject, this.random.bytes(SECRET_BYTES));
    return { token, hash: await this.hasher.sha256Hex(token), mask: maskToken(token) };
  }
}

/** The owner and the hash the router forwards, or null for any other value. */
export async function tokenRouting(hasher: Hasher, token: string): Promise<{ subject: string; hash: string } | null> {
  const parsed = parseToken(token);
  if (!parsed) return null;
  return { subject: parsed.subject, hash: await hasher.sha256Hex(token.trim()) };
}
