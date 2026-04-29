"""Inline SVG icon registry.

Phosphor Light icons (https://phosphoricons.com, MIT) are downloaded into
static/icons/ at design time, loaded into memory once at boot, and rendered
inline so they pick up `currentColor` and don't require an extra HTTP round
trip per use.

Usage from Jinja:
    {{ icon('arrow-left') }}
    {{ icon('check', class_='verdict-mark') }}

The svg blob already declares fill="currentColor" so any color/size styling
goes through the wrapper element. We strip the source's xmlns to keep the
serialized output compact (root <svg> in HTML5 doesn't need xmlns).
"""

from __future__ import annotations

from pathlib import Path

from markupsafe import Markup

# static/ lives at the repo root, not under the prep/ package.
_ICONS_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

# Loaded lazily on first call; cached process-wide. Editing an SVG on disk
# requires an app restart (same as templates/CSS in production).
_CACHE: dict[str, str] = {}


def _load(name: str) -> str:
    if name not in _CACHE:
        path = _ICONS_DIR / f"{name}.svg"
        if not path.exists():
            return ""  # missing icon: render nothing rather than break the page
        raw = path.read_text(encoding="utf-8").strip()
        _CACHE[name] = raw
    return _CACHE[name]


def icon(name: str, *, class_: str = "icon", title: str | None = None) -> Markup:
    """Render an inline SVG icon. Pass `class_` to scope styling; pass
    `title` to label decoratively-meaningless icons (most callers omit it
    and rely on aria-hidden via the wrapping element)."""
    svg = _load(name)
    if not svg:
        return Markup("")
    # Inject our class + aria-hidden right after the opening <svg.
    # Phosphor sources start with `<svg xmlns="..." viewBox="0 0 256 256" fill="currentColor">`
    # — keep their attributes intact, just add ours.
    open_end = svg.find(">")
    if open_end < 0:
        return Markup("")
    head = svg[:open_end]
    tail = svg[open_end:]
    extra = f' class="{class_}" aria-hidden="true"'
    if title:
        extra += f' role="img" aria-label="{title}"'
        # When titled the icon is no longer aria-hidden — replace.
        extra = extra.replace(' aria-hidden="true"', "")
    return Markup(head + extra + tail)
