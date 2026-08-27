"""Registry properties: names, covers, selection env, shot numbering."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.parity.harness import registry
from tests.parity.harness.constants import REPO_ROOT, SCHEMES

TEMPLATES = REPO_ROOT / "templates"

# Pages a browser never renders as a document on their own.
_NOT_PAGES = {"base.html"}


def _split_cover(cover: str) -> tuple[str, str | None]:
    name, _, state = cover.partition("#")
    return name, (state or None)


def test_flows_load_with_unique_kebab_names():
    flows = registry.all_flows()
    assert flows
    names = [f.name for f in flows]
    assert len(names) == len(set(names))
    assert all(registry._NAME_RE.match(n) for n in names)


def test_flow_phases_and_schemes_are_known():
    for f in registry.all_flows():
        assert f.phase in (1, 3, 4, 5), f.name
        assert set(f.schemes) <= set(SCHEMES), f.name
        assert f.service_workers in ("block", "allow"), f.name


def test_every_cover_names_a_template_and_a_present_state():
    for f in registry.all_flows():
        assert f.covers, f"{f.name} covers nothing"
        for cover in f.covers:
            name, state = _split_cover(cover)
            path = TEMPLATES / name
            assert path.is_file(), f"{f.name}: {cover} names no template"
            # Status codes reach error.html as a variable, not a literal.
            if state and not state.isdigit():
                assert state in path.read_text(), f"{f.name}: {state!r} not in {name}"


def test_seed_profiles_exist():
    from prep.dev.parity_seed import PROFILES

    for f in registry.all_flows():
        if f.seed is not None:
            assert f.seed in PROFILES, f"{f.name} seeds unknown profile {f.seed!r}"


def test_seed_timezone_matches_the_shared_constant():
    from prep.dev import parity_seed
    from tests.parity.harness.constants import PARITY_TZ

    assert parity_seed.PARITY_TZ == PARITY_TZ


def test_seed_timestamps_follow_the_process_clock():
    from datetime import datetime, timezone

    from prep.dev.parity_seed import at
    from prep.infrastructure import clock

    pinned = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock.set_clock(clock.FixedClock(pinned))
    try:
        assert at(hours=1) == "2030-01-02T04:04:05+00:00"
    finally:
        clock.reset_clock()


def test_registering_a_name_twice_is_an_error():
    name = next(iter(registry.all_flows())).name
    with pytest.raises(ValueError):
        registry.flow(name, phase=1, seed=None, covers=("index.html",))


def test_selection_env(monkeypatch):
    flows = registry.all_flows()
    monkeypatch.delenv(registry.PHASE_ENV, raising=False)
    monkeypatch.delenv(registry.FLOWS_ENV, raising=False)
    monkeypatch.delenv(registry.SCHEME_ENV, raising=False)
    assert registry.selected() == []

    monkeypatch.setenv(registry.PHASE_ENV, "1")
    picked = {f.name for f, _ in registry.selected()}
    assert picked == {f.name for f in flows if f.phase <= 1}

    monkeypatch.setenv(registry.PHASE_ENV, "all")
    assert {f.name for f, _ in registry.selected()} == {f.name for f in flows}

    monkeypatch.setenv(registry.FLOWS_ENV, "dash*,deck")
    assert {f.name for f, _ in registry.selected()} == {"dashboard", "dashboard-empty", "deck"}

    monkeypatch.setenv(registry.SCHEME_ENV, "dark")
    assert {s for _, s in registry.selected()} == {"dark"}


def test_shots_number_themselves_in_call_order():
    seen: list[tuple[str, str]] = []

    def sink(name, label, after_swap):
        seen.append((name, label))
        return Path("/dev/null") / name

    ctx = registry.FlowCtx(
        page=None, base_url="http://x", seed={}, llm=None, scheme="dark", sink=sink
    )
    ctx.shot("Splash!")
    ctx.shot("second step")
    assert [n for n, _ in seen] == ["01-splash@dark.png", "02-second-step@dark.png"]
    assert ctx.url("/deck/a") == "http://x/deck/a"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "three templates from earlier phases carry no flow: "
        "partials/notif_edit.html, partials/pin_form.html, settings_account.html"
    ),
)
def test_every_page_template_and_partial_is_covered():
    covered = {_split_cover(c)[0] for f in registry.all_flows() for c in f.covers}
    expected = {
        str(p.relative_to(TEMPLATES))
        for p in TEMPLATES.rglob("*.html")
        if not p.relative_to(TEMPLATES).parts[0] == "macros"
    } - _NOT_PAGES
    missing = sorted(expected - covered)
    assert not missing, f"templates no flow covers: {missing}"
