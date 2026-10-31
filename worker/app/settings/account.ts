// /settings/account: the delete-account flow. Only a provider with
// first-class accounts has a deleter; a proxy-supplied identity would just
// come back on the next request, so without one the route 404s.
import { notFound } from '../errors.js';
import { page, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos } from '../ports.js';

export const ACCOUNT_DELETE_PATH = '/settings/account/delete';
export const NO_FLOW = 'this deploy has no in-app account-delete flow';
export const MISMATCH = "That doesn't match your account ID. Type it exactly as shown to confirm.";
export const DELETE_FAILED = "Your account couldn't be deleted right now, and nothing was removed. Try again in a minute.";
/** The app's sign-out page: it signs the browser out of the provider and
 * drops the anonymous cookie, which the provider's own URL does not. */
const SIGN_OUT_PATH = '/sign-out';

/** The upstream delete. The local rows go when the provider's webhook
 * arrives, so the page never wipes the cell itself. */
export interface AccountDeleter {
  /** Resolves once the provider has deleted the account. */
  deleteUpstream(subject: string): Promise<void>;
}

export interface AccountDeps {
  deleter: AccountDeleter | null;
}

function deleterFor(deps: AccountDeps): AccountDeleter {
  if (!deps.deleter) throw notFound(NO_FLOW);
  return deps.deleter;
}

export function accountSettings(deps: AccountDeps): PageResult {
  deleterFor(deps);
  return page('settings_account.html', { error: null });
}

export async function accountDelete(repos: UserRepos, req: PageRequest, deps: AccountDeps): Promise<PageResult> {
  const deleter = deleterFor(deps);
  const expected = (repos.prefs.get()?.login ?? '').trim();
  if (!expected || (req.form.get('confirm') ?? '').trim() !== expected) return page('settings_account.html', { error: MISMATCH }, 400);
  try {
    await deleter.deleteUpstream(expected);
  } catch {
    return page('settings_account.html', { error: DELETE_FAILED }, 502);
  }
  return { redirect: SIGN_OUT_PATH, status: 303 };
}
