// /settings/api: personal access tokens. The plaintext is rendered on
// the response that mints it and nowhere else - never persisted to a
// session, never in a query string, never returned by a GET.
import { assembleToken, maskToken, SECRET_BYTES } from '../../domain/pat.js';
import { pyStrip } from '../../domain/py.js';
import { HTML, page, type PageRequest, type PageResult } from '../pageResult.js';
import type { Hasher, Random, UserRepos } from '../ports.js';

function render(repos: UserRepos, extra: { created_plaintext?: string | null; flash?: string | null } = {}): PageResult {
  return page('settings_api.html', {
    tokens: repos.tokens.list(),
    created_plaintext: extra.created_plaintext ?? null,
    flash: extra.flash ?? null,
  });
}

export function apiSettings(repos: UserRepos): PageResult {
  return render(repos);
}

export async function apiTokenCreate(
  repos: UserRepos,
  req: PageRequest,
  deps: { subject: string; random: Random; hasher: Hasher },
): Promise<PageResult> {
  const label = pyStrip(req.form.get('label') ?? '') || null;
  const token = assembleToken(deps.subject, deps.random.bytes(SECRET_BYTES));
  repos.tokens.insert(await deps.hasher.sha256Hex(token), maskToken(token), label);
  return render(repos, { created_plaintext: token });
}

export function apiTokenDelete(repos: UserRepos, req: PageRequest): PageResult {
  repos.tokens.delete(Number(req.params['token_id']));
  // The htmx form swaps `closest tr` with the body, so an empty 200
  // removes the row in place instead of scrolling to the top.
  if (req.hxHeader) return { text: '', status: 200, headers: { 'content-type': HTML } };
  return render(repos, { flash: 'Token revoked.' });
}
