// /settings/account: the delete-account flow. Only a provider with
// first-class accounts has one; a proxy-supplied identity would just come
// back on the next request, so the route 404s there.
import { pyStrip } from '../../domain/py.js';
import { notFound } from '../errors.js';
import { page, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos } from '../ports.js';

export const NO_FLOW = 'this deploy has no in-app account-delete flow';
export const MISMATCH = "That doesn't match your account ID. Type it exactly as shown to confirm.";

/** The upstream delete. The local rows go when the provider's webhook
 * arrives, so the page never wipes the cell itself. */
export interface AccountDeleter {
  /** Resolves on success; rejects with a message the page renders. */
  deleteUpstream(subject: string): Promise<void>;
  /** Where the browser lands once the account is gone. */
  signOutUrl(): string;
}

export interface AccountDeps {
  authProvider: string;
  deleter: AccountDeleter | null;
}

function deleterFor(deps: AccountDeps): AccountDeleter {
  if (deps.authProvider !== 'clerk' || !deps.deleter) throw notFound(NO_FLOW);
  return deps.deleter;
}

export function accountSettings(deps: AccountDeps): PageResult {
  deleterFor(deps);
  return page('settings_account.html', { error: null });
}

export async function accountDelete(repos: UserRepos, req: PageRequest, deps: AccountDeps): Promise<PageResult> {
  const deleter = deleterFor(deps);
  const expected = pyStrip(repos.prefs.get()?.tailscale_login ?? '');
  if (pyStrip(req.form.get('confirm') ?? '') !== expected) return page('settings_account.html', { error: MISMATCH }, 400);
  try {
    await deleter.deleteUpstream(expected);
  } catch (e) {
    return page('settings_account.html', { error: e instanceof Error ? e.message : String(e) }, 502);
  }
  return { redirect: deleter.signOutUrl() || '/', status: 303 };
}
