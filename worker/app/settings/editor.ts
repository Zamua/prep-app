// /settings/editor: the answer box's key bindings.
import { EDITOR_INPUT_MODES } from '../entities.js';
import { badRequest } from '../errors.js';
import { page, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos } from '../ports.js';

export function editorSettings(repos: UserRepos): PageResult {
  return page('settings_editor.html', {
    current_mode: repos.prefs.getEditorInputMode(),
    modes: EDITOR_INPUT_MODES,
    saved: false,
  });
}

export function editorSettingsSave(repos: UserRepos, req: PageRequest): PageResult {
  const mode = req.form.get('mode') ?? '';
  if (!(EDITOR_INPUT_MODES as readonly string[]).includes(mode)) throw badRequest(`Unknown input mode "${mode}".`);
  repos.prefs.setEditorInputMode(mode);
  // The saved mode must reach base.html's data-editor-mode on this very
  // render, so the profile row is re-read after the write.
  return page('settings_editor.html', { current_mode: mode, modes: EDITOR_INPUT_MODES, saved: true });
}
