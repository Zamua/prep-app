"""Route tests for the M1 offline shell (docs/OFFLINE.md section 7).

Covers the un-auth-gated surfaces:
- /offline: renders without auth, self-contained (no identity
  provider, no external CDN or fonts), echoes only token-shaped
  ?build= values into its asset URLs.
- /sw.js: rendered with the build token + a JSON precache manifest
  whose every URL resolves 200 through the app.
- The reverse completeness check: every JS module statically
  reachable from offline-app.js must appear in the precache list.
- The build-token resolution rules (PREP_BUILD_ID shapes) and the
  version-segment acceptance rules the asset routes rely on.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from prep.web.templates import (
    _resolve_build_token,
    get_build_token,
    is_accepted_version_token,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JS_ROOT = _REPO_ROOT / "static" / "js"


# ---- /offline shell -------------------------------------------------------


def test_offline_renders_200_without_auth(unauthed_client: TestClient):
    """The shell must be reachable with no identity at all: the SW
    precaches it before any session exists, and offline cold launch
    serves it with no server round-trip."""
    r = unauthed_client.get("/offline")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "<html" in r.text.lower()


def test_offline_shell_is_self_contained(unauthed_client: TestClient):
    """Regression-pin the self-containment: the shell must reference
    no identity-provider JS, no web-font CDN, no external origin of
    any kind. Any of these would need network and break the
    cold-launch-with-no-connectivity story."""
    body = unauthed_client.get("/offline").text
    lowered = body.lower()
    assert "clerk" not in lowered
    assert "fonts.googleapis" not in lowered
    assert "fonts.gstatic" not in lowered
    assert "htmx" not in lowered
    # The blanket check: nothing in the shell points off-origin.
    assert "https://" not in body
    assert "http://" not in body


def test_offline_references_versioned_assets_at_current_build(unauthed_client: TestClient):
    """Without a ?build= override the shell links the current build's
    stylesheet and importmap prefix."""
    token = get_build_token()
    body = unauthed_client.get("/offline").text
    assert f"/static/css/v{token}/index.css" in body
    assert f"/static/js/v{token}/" in body


# ---- ?build= echo ---------------------------------------------------------


def test_offline_echoes_valid_hex_build_token(client: TestClient):
    """The SW fetches the shell as /offline?build=<its own token> so
    the rendered asset URLs match the cache keys the same install
    stores. A token-shaped value must be echoed verbatim."""
    echoed = "abcdef1234567"
    body = client.get("/offline", params={"build": echoed}).text
    assert f"/static/css/v{echoed}/index.css" in body
    assert f"/static/js/v{echoed}/" in body


def test_offline_echoes_legacy_digit_build_token(client: TestClient):
    """Pre-offline deploys used all-digit boot stamps; a cached page
    or SW from one of those may still ask for its token. Digits are
    an accepted legacy shape."""
    echoed = "1718000000000"
    body = client.get("/offline", params={"build": echoed}).text
    assert f"/static/css/v{echoed}/index.css" in body
    assert f"/static/js/v{echoed}/" in body


def test_offline_never_reflects_malformed_build_tokens(client: TestClient):
    """Anything not token-shaped falls back to the current build token
    and is never reflected into the page: path traversal, markup,
    wrong charset, wrong length."""
    token = get_build_token()
    bad_values = [
        "../../etc/passwd",
        "<script>alert(1)</script>",
        "ABCDEF1234567",  # uppercase: wrong charset
        "abc123",  # hex but too short (6 < 7)
        "a" * 41,  # too long
        "deadbeef.js",  # dot: file-shaped
        "endor",  # the vendor/ collision shape after the v-strip
        "\u0661\u0662\u0663\u0664\u0665\u0666\u0667",  # Arabic-Indic digits: isdigit-true, not ASCII
        "",
    ]
    for bad in bad_values:
        body = client.get("/offline", params={"build": bad}).text
        # Never used as a version segment.
        assert f"v{bad}/" not in body, f"malformed token echoed: {bad!r}"
        # Never reflected raw (the markup case is the XSS pin). Only
        # asserted for values with non-hex characters, so the check
        # can never collide with a substring of the real hex token.
        if bad and not re.fullmatch(r"[0-9a-f]+", bad):
            assert bad not in body, f"malformed token reflected: {bad!r}"
        # The shell still renders against the real current build.
        assert f"/static/css/v{token}/index.css" in body


# ---- /sw.js ---------------------------------------------------------------


def _fetch_sw(client: TestClient) -> str:
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")
    assert "no-cache" in r.headers.get("cache-control", "")
    return r.text


def _extract_precache(sw_body: str) -> list[str]:
    """Pull the substituted PRECACHE manifest back out of the served
    worker: it must be a valid JSON array."""
    m = re.search(r"const PRECACHE = (\[.*?\]);", sw_body, re.DOTALL)
    assert m, "const PRECACHE = [...] not found in /sw.js"
    urls = json.loads(m.group(1))
    assert isinstance(urls, list)
    assert all(isinstance(u, str) for u in urls)
    return urls


def test_sw_js_carries_build_token_and_substituted_placeholders(client: TestClient):
    body = _fetch_sw(client)
    token = get_build_token()
    assert f'const BUILD = "{token}";' in body
    assert "__BUILD__" not in body
    assert "__PRECACHE__" not in body


def test_sw_precache_manifest_shape(client: TestClient):
    """The manifest must include the token-pinned shell, the entry
    stylesheet at its versioned URL, the offline bootstrap module, and
    the PWA icons."""
    token = get_build_token()
    urls = _extract_precache(_fetch_sw(client))
    assert urls, "precache manifest is empty"
    assert f"/offline?build={token}" in urls
    assert f"/static/css/v{token}/index.css" in urls
    assert f"/static/js/v{token}/offline/offline-app.js" in urls
    assert any(u.endswith("/static/pwa/icon-192.png") for u in urls)


def test_every_precached_url_resolves_200(client: TestClient):
    """The check that catches a renamed CSS file breaking offline
    silently: every URL the install handler will fetch must come back
    200 and un-redirected (the SW rejects the install on either)."""
    urls = _extract_precache(_fetch_sw(client))
    for url in urls:
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 200, f"precache URL does not resolve: {url} -> {r.status_code}"


# ---- reverse completeness -------------------------------------------------

_IMPORT_PATTERNS = (
    # import defaultExport, {named} from "spec"; / import "spec";
    re.compile(r"""import\s+(?:[\w{},*\s$]+?from\s+)?["']([^"']+)["']"""),
    # export {x} from "spec"; / export * from "spec";
    re.compile(r"""export\s+[\w{},*\s$]+?from\s+["']([^"']+)["']"""),
    # dynamic import("spec")
    re.compile(r"""import\(\s*["']([^"']+)["']\s*\)"""),
)


def _import_specifiers(source: str) -> set[str]:
    specs: set[str] = set()
    for pattern in _IMPORT_PATTERNS:
        specs.update(pattern.findall(source))
    return specs


def _resolve_specifier(spec: str, importer: Path) -> Path:
    """Resolve an import specifier the way the offline shell's
    importmap does: "@/" maps to static/js/, relative specifiers
    resolve against the importing module. Anything else cannot
    resolve offline at all, which is itself a bug."""
    if spec.startswith("@/"):
        return (_JS_ROOT / spec[2:]).resolve()
    if spec.startswith("."):
        return (importer.parent / spec).resolve()
    raise AssertionError(
        f"{importer.relative_to(_REPO_ROOT)} imports {spec!r}: not an '@/' or relative "
        "specifier, so it cannot resolve in the offline shell's importmap"
    )


def _reachable_modules(entry: Path) -> set[Path]:
    """Statically walk import specifiers transitively from entry."""
    seen: set[Path] = set()
    stack = [entry.resolve()]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        assert mod.is_file(), f"import graph reaches a missing file: {mod}"
        for spec in _import_specifiers(mod.read_text(encoding="utf-8")):
            stack.append(_resolve_specifier(spec, mod))
    return seen


def test_precache_covers_every_module_reachable_from_offline_app(client: TestClient):
    """The 200-resolution test only validates URLs already listed.
    This one catches the import added to a shared module later that
    would break offline cold launch (the module fetch rejects
    offline) while staying green online: every module transitively
    reachable from offline-app.js must have its versioned URL in the
    precache manifest."""
    token = get_build_token()
    urls = set(_extract_precache(_fetch_sw(client)))
    entry = _JS_ROOT / "offline" / "offline-app.js"
    reachable = _reachable_modules(entry)
    assert entry.resolve() in reachable
    assert len(reachable) >= 2  # offline-app.js plus at least store.js
    for mod in sorted(reachable):
        rel = mod.relative_to(_JS_ROOT).as_posix()
        versioned = f"/static/js/v{token}/{rel}"
        assert versioned in urls, f"reachable module missing from precache: {rel}"


# ---- versioned asset routes ----------------------------------------------


def test_asset_routes_serve_current_bytes_for_any_accepted_token(client: TestClient):
    """The version segment is an opaque token: the current hex shape
    and the legacy all-digit boot stamps both serve the current
    build's bytes with immutable caching, so pages cached from a
    prior deploy keep resolving assets."""
    for tok in (get_build_token(), "abcdef1234567", "1718000000000"):
        r = client.get(f"/static/css/v{tok}/index.css")
        assert r.status_code == 200, f"css not served for token {tok!r}"
        assert "text/css" in r.headers.get("content-type", "")
        assert "immutable" in r.headers.get("cache-control", "")
        r = client.get(f"/static/js/v{tok}/offline/offline-app.js")
        assert r.status_code == 200, f"js not served for token {tok!r}"
        assert "immutable" in r.headers.get("cache-control", "")


def test_asset_route_treats_unaccepted_segment_as_literal_path(client: TestClient):
    """A non-token segment is a literal sub-path (so real directories
    starting with 'v' stay reachable), and a garbage one 404s instead
    of aliasing onto the current build."""
    r = client.get("/static/css/vNOTATOKEN/index.css")
    assert r.status_code == 404


# ---- build-token resolution -----------------------------------------------


def test_build_id_hex_used_verbatim(monkeypatch):
    monkeypatch.setenv("PREP_BUILD_ID", "deadbeefcafe1234")
    assert _resolve_build_token() == "deadbeefcafe1234"


def test_build_id_full_git_sha_used_verbatim(monkeypatch):
    sha = "a" * 40
    monkeypatch.setenv("PREP_BUILD_ID", sha)
    assert _resolve_build_token() == sha


def test_build_id_non_hex_hashed_to_12_hex(monkeypatch):
    """An image-tag-shaped PREP_BUILD_ID (dots, 'v' prefix) is not
    token-shaped: it is normalized via sha1 so the served token always
    matches the accepted charset."""
    monkeypatch.setenv("PREP_BUILD_ID", "v0.44.0")
    expect = hashlib.sha1(b"v0.44.0").hexdigest()[:12]
    got = _resolve_build_token()
    assert got == expect
    assert is_accepted_version_token(got)


def test_build_id_unset_falls_back_to_stable_tree_hash(monkeypatch):
    """Dev fallback: no PREP_BUILD_ID means the token is derived from
    the static tree and must be deterministic across calls (a
    boot-varying token is exactly the bug the build-stable token
    replaced)."""
    monkeypatch.delenv("PREP_BUILD_ID", raising=False)
    first = _resolve_build_token()
    second = _resolve_build_token()
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{12}", first)


# ---- token acceptance shapes ----------------------------------------------


def test_accepted_version_token_shapes():
    """The acceptance rule the asset routes + the ?build= echo lean
    on: lowercase hex 7-40 chars, or the legacy all-digit stamps."""
    accepted = ["abcdef0", "deadbeefcafe", "a" * 40, "1234567", "1718000000000"]
    for tok in accepted:
        assert is_accepted_version_token(tok), f"should accept {tok!r}"

    rejected = [
        "abcdef",  # 6 hex chars: under the minimum
        "a" * 41,  # over the maximum
        "DEADBEEF12",  # uppercase
        "deadbeef.js",  # dot
        "endor",  # /static/js/vendor/ after the v-strip
        "ersion.txt",  # /static/js/version.txt after the v-strip
        "\u0661\u0662\u0663\u0664\u0665\u0666\u0667",  # Unicode digits pass bare isdigit()
        "\u00b2" * 8,  # superscript two, ditto
        "",
    ]
    for tok in rejected:
        assert not is_accepted_version_token(tok), f"should reject {tok!r}"
