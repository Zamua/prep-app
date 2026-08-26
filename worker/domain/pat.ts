// Personal access tokens: the display mask. Hashing is the adapter's.

export const TOKEN_PREFIX = 'prep_pat_';

/** `prep_pat_Aa…x9zT`: two characters after the prefix and the last four. */
export function maskToken(token: string): string {
  if (!token || token.length <= TOKEN_PREFIX.length + 6) return '…';
  const middle = token.slice(TOKEN_PREFIX.length, TOKEN_PREFIX.length + 2);
  return `${TOKEN_PREFIX}${middle}…${token.slice(-4)}`;
}
