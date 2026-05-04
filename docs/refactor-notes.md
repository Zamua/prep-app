# Refactor notes — duplication watch (2026-05-03 audit)

A running log of duplication / shared-pattern candidates noticed during
the audit-fix passes. Append-only as we go; pruned + folded into the
real fix in the dedup sweep (audit-fix #5, task #183).

## Format

Each entry: where, what, why-it-matters, suggested-fix. Keep it terse.

---

## Backend

### ✅ TriviaQueueRepo.mark_answered + set_last_correctness — DONE (#183)
- **Where:** `prep/trivia/repo.py` lines ~108 and ~152.
- **Fix shipped:** Extracted `_deck_id_for_question` and
  `_reset_deck_streak` static helpers shared by both methods. Both
  public methods are now ~10 lines each (down from ~25).

### ✅ `_explore_ctx` helper in trivia/routes.py — DONE (#182)
- **Where:** Was called four times (standalone answer, session answer,
  standalone regrade, session regrade) with near-identical args.
- **Fix shipped:** Moved to `prep/trivia/service.py` as
  `build_explore_ctx(*, deck_name, q, user_answer, correct, expected,
  idk=False)`. Routes now `**build_explore_ctx(...)` at all 4 sites.

### ✅ `from prep import db as _legacy_db` in 9 files — DONE (#181)
- **Where:** Was in notify/repo, notify/_legacy_module, study/routes,
  study/repo, auth/identity, auth/repo, trivia/scheduler,
  decks/routes, decks/repo.
- **Fix shipped:** Audit-fix #3 split prep/db.py into per-context
  repos. `prep/db.py` is gone; all 9 files now import from their
  respective context repos. `_legacy_db` is dead.

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

### ✅ Repo direct dict returns (DDD smell) — partially DONE (#183)
- **Where:** `DeckRepo.list_trivia_decks()` was `list[dict]`.
  `DeckRepo.due_breakdown()` is `list[tuple[str, int]]`.
- **Fix shipped:** `list_trivia_decks` now returns `list[Deck]`.
  `Deck` entity gained `notification_ignored_streak` (the only
  scheduler-required field that wasn't already on the entity).
  Scheduler tick + 9 test callsites migrated to attribute access.
- **Still TODO (low priority):** `due_breakdown` returns
  `list[tuple[str, int]]`, which is typed but not entity-shaped. A
  `DueDigestEntry(name, count)` value object would make intent
  clearer; deferred since callers are tiny.

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

### ✅ Outline pill summary chrome (Explain / Explore / Re-grade) — DONE (#183)
- **Where:** `.trivia-disc summary` + `.trivia-regrade-btn` (almost
  identical: 0.65rem padding, 0.85rem font, italic uppercase, 999px
  border-radius, var(--rule) border, color-mix paper background).
- **Fix shipped:** Single multi-selector rule
  (`.trivia-disc > summary, .trivia-regrade-btn`) holds the shared
  chrome + active state. Per-element-specific bits (`width: 100%`,
  `:disabled`, icon size on the button; `list-style: none` and
  marker-hide on the summary) live in tiny follow-up rules.
  Net: -25 lines, single source of truth for the active-state tint.
