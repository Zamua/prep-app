# Refactor notes — duplication watch (2026-05-03 audit)

A running log of duplication / shared-pattern candidates noticed during
the audit-fix passes. Append-only as we go; pruned + folded into the
real fix in the dedup sweep (audit-fix #5, task #183).

## Format

Each entry: where, what, why-it-matters, suggested-fix. Keep it terse.

---

## Backend

### TriviaQueueRepo.mark_answered + set_last_correctness
- **Where:** `prep/trivia/repo.py` lines ~108 and ~152.
- **What:** Both update `trivia_queue.last_answered_correctly` for a
  given question_id. `mark_answered` also rotates queue_position;
  `set_last_correctness` does not.
- **Suggested fix:** A single `record_verdict(question_id, correct, *,
  rotate=True)` would express the difference as a flag. Both also reset
  the deck's notification_ignored_streak — extract a private helper.

### `_explore_ctx` helper in trivia/routes.py
- **Where:** Called four times (standalone answer, session answer,
  standalone regrade, session regrade) with near-identical args.
- **What:** Builds the chat-handoff prefill URLs + Google search URL
  for the trivia card's "Explore" pill.
- **Suggested fix:** Move to `prep/trivia/service.py` so it's not a
  route-level private. Take a single `Result` value object instead of
  the loose user_answer/correct/idk kwargs.

### `from prep import db as _legacy_db` in 9 files
- **Where:** notify/repo, notify/_legacy_module, study/routes,
  study/repo, auth/identity, auth/repo, trivia/scheduler,
  decks/routes, decks/repo.
- **What:** Cross-context backdoor through the legacy db facade.
  The actual SQL lives in 957-line prep/db.py.
- **Suggested fix:** Audit-fix #3 (split prep/db.py into per-context
  repos). After that, `from prep import db` should be deletable.

### Inline `<script>` blobs across templates
- **Where:** templates/trivia/card.html (regrade auto-disable, submit
  pending state), templates/deck.html (edit-pill toggle, suspend
  toggle, delete-confirm dialog), templates/notify_settings.html
  (subscribe + test buttons).
- **What:** Each template ships its own hand-rolled JS. No bundling,
  no lint, easy to introduce regressions (popover layout bug today).
- **Suggested fix:** Pull repeated patterns into a small
  `static/app.js`. Patterns to extract: button-pending-state,
  details-popover-close-on-outside-click (already global), form-submit
  with disabled-while-loading.

### Repo direct dict returns (DDD smell)
- **Where:** `DeckRepo.list_trivia_decks() -> list[dict]`,
  `DeckRepo.due_breakdown() -> list[tuple[str, int]]`,
  `_legacy_db.list_decks() -> list[dict]`, etc.
- **What:** Repos handing dicts to callers leaks SQL row shape.
  CLAUDE.md DDD invariant says "Repos return entities, not dicts."
- **Suggested fix:** Extend the entity (Deck or DeckSummary) to carry
  the missing fields, OR add a dedicated read-model entity for
  scheduler-shape data.

### Test fixtures use `importlib.reload(_legacy_module)`
- **Where:** tests/test_notification_log.py and adjacent.
- **What:** Reloads modules to swap in per-test sqlite. Order-dependent
  + fragile.
- **Suggested fix:** Driven by audit-fix #2 (split _legacy_module). A
  proper `notify_module_for_db(db_path)` factory would let tests build
  a fresh instance instead of mutating module state.

## Frontend

### Status badge / chip pattern repeats
- **Where:** `.notif-pill`, `.edit-pill`, `.tag`, `.notif-preset`,
  `.deck-mastery-chip-*`, `.trivia-disc summary`, `.trivia-regrade-btn`.
- **What:** Many small variants of "outlined italic-uppercase pill
  with optional icon." Each defines its own padding / font-size /
  border-radius.
- **Suggested fix:** Single `.pill` base class with modifiers
  (`.pill--accent`, `.pill--paused`, `.pill--active`). Today everything
  copy-pastes the chrome.

### Mastery bar duplication
- **Where:** `.deck-mastery` + `.deck-mastery-bar` + `.deck-mastery-fill-*`
  on the deck page; `.deck-mastery-mini-*` on the index page.
- **What:** Same green/red/empty bar concept, two near-identical
  implementations differing in height + label placement.
- **Suggested fix:** One `.mastery-bar` component with `--size`
  variants (`--lg` / `--sm`) and consistent fill semantics.

### Color-mix(in srgb, var(--X) N%, transparent) repeats
- **Where:** ~50 places across style.css.
- **What:** Tinting palette colors at varying alpha. Same idiom
  reused with different N values for hover / background / border.
- **Suggested fix:** Define semantic tokens in `:root` or the
  variable block — `--right-tint-bg`, `--right-tint-border`,
  `--wrong-tint-bg`, etc. Then components reference tokens, not
  raw color-mix expressions.

### Outline pill summary chrome (Explain / Explore / Re-grade)
- **Where:** `.trivia-disc summary` + `.trivia-regrade-btn` (almost
  identical: 0.65rem padding, 0.85rem font, italic uppercase, 999px
  border-radius, var(--rule) border, color-mix paper background).
- **What:** Two selectors, one chrome — diverged because one is a
  `<details><summary>` and the other is a `<button>`.
- **Suggested fix:** Single class (`.disc-pill`) applied to both.
