"""The committed corpora are what the Python implementation emits
right now, and the Python implementation passes them.

`test_oracles[<name>]` re-runs an extractor in memory and compares
with `tests/fixtures/parity/<name>/`; a drift means a corpus must be
regenerated on purpose (`python -m tests.parity.oracles.<name>`).
The replay tests consume a corpus the way another implementation
would. The coverage tests keep the golden HTML set honest about the
templates and status branches it claims to cover.

Red proof knobs (docs/PARITY-GATE.md section E):
`PARITY_PERTURB_FSRS=1` fails `test_oracles[fsrs]` only;
`PARITY_PERTURB_DOM=1` fails `test_oracles[html]` only.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

from tests.parity.dom_diff import dom_diff
from tests.parity.oracles import REPO_ROOT, read_corpus
from tests.parity.oracles import contracts as contracts_mod
from tests.parity.oracles import fsrs as fsrs_mod
from tests.parity.oracles import grading as grading_mod
from tests.parity.oracles import markdown as markdown_mod
from tests.parity.oracles import merge as merge_mod
from tests.parity.oracles import offline as offline_mod
from tests.parity.oracles import render_templates as html_mod
from tests.parity.oracles.contexts import DIFF_CARD_STATES, DIFF_FIELDS, all_contexts

EXTRACTORS = {
    "fsrs": fsrs_mod,
    "grading": grading_mod,
    "markdown": markdown_mod,
    "merge": merge_mod,
    "offline": offline_mod,
    "contracts": contracts_mod,
    "html": html_mod,
}

TEMPLATES = REPO_ROOT / "templates"
FLOAT_TOL = 1e-9
VOLATILE_MARK = "<VOLATILE>"
ENV_PERTURB_DOM = "PARITY_PERTURB_DOM"


@lru_cache(maxsize=None)
def candidate(name: str) -> dict[str, str]:
    return EXTRACTORS[name].extract()


def committed(name: str) -> dict[str, str]:
    corpus = read_corpus(name)
    assert corpus, (
        f"no corpus at tests/fixtures/parity/{name}; run python -m tests.parity.oracles.{name}"
    )
    return corpus


# ---- comparison helpers -------------------------------------------------


def _close(a, b, path: str, out: list[str]) -> None:
    """Structural equality with a float tolerance."""
    if (
        isinstance(a, float)
        and isinstance(b, (int, float))
        or isinstance(b, float)
        and isinstance(a, (int, float))
    ):
        if not math.isclose(a, b, rel_tol=0, abs_tol=FLOAT_TOL):
            out.append(f"{path}: {a!r} != {b!r}")
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: only in {'corpus' if key in a else 'candidate'}")
            else:
                _close(a[key], b[key], f"{path}.{key}", out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _close(x, y, f"{path}[{i}]", out)
        return
    if a != b:
        out.append(f"{path}: {a!r} != {b!r}")


def _resolve(node, parts: list[str]):
    """Every (container, key) a dotted pointer with `*` reaches."""
    if not parts:
        return []
    head, rest = parts[0], parts[1:]
    if isinstance(node, list):
        indexes = range(len(node)) if head == "*" else [int(head)]
        hits = []
        for i in indexes:
            hits += [(node, i)] if not rest else _resolve(node[i], rest)
        return hits
    if isinstance(node, dict):
        keys = list(node) if head == "*" else ([head] if head in node else [])
        hits = []
        for key in keys:
            hits += [(node, key)] if not rest else _resolve(node[key], rest)
        return hits
    return []


def scrub_volatile(pairs: list[dict]) -> list[dict]:
    pairs = copy.deepcopy(pairs)
    for pair in pairs:
        for glob, pointer, regex in contracts_mod.VOLATILE:
            if not fnmatch.fnmatchcase(pair["name"], glob):
                continue
            for container, key in _resolve(pair, pointer.split(".")):
                value = container[key]
                if isinstance(value, str):
                    container[key] = re.sub(regex, VOLATILE_MARK, value)
    return pairs


def _is_html(response: dict) -> bool:
    return "text/html" in (response.get("content_type") or "")


def compare_pairs(corpus_text: str, candidate_text: str) -> list[str]:
    corpus, cand = json.loads(corpus_text), json.loads(candidate_text)
    out: list[str] = []
    _close(corpus["header"], cand["header"], "header", out)
    a_pairs, b_pairs = scrub_volatile(corpus["pairs"]), scrub_volatile(cand["pairs"])
    if [p["name"] for p in a_pairs] != [p["name"] for p in b_pairs]:
        out.append("pair names differ")
        return out
    for a, b in zip(a_pairs, b_pairs, strict=True):
        name = a["name"]
        if _is_html(a["response"]) and _is_html(b["response"]):
            text_a, text_b = a["response"].pop("text"), b["response"].pop("text")
            for d in dom_diff(text_a, text_b):
                out.append(f"{name}: html: {d}")
        _close(a, b, name, out)
    return out


def compare_fsrs(corpus_text: str, candidate_text: str) -> list[str]:
    out: list[str] = []
    _close(json.loads(corpus_text), json.loads(candidate_text), "corpus", out)
    return out


def compare_html(rel: str, corpus_text: str, candidate_text: str) -> list[str]:
    if rel == "index.json":
        return [] if corpus_text == candidate_text else ["index.json differs"]
    if os.environ.get(ENV_PERTURB_DOM) == "1" and rel == "deck@srs-populated.html":
        candidate_text = candidate_text.replace('data-qid="41"', 'data-qid="9941"', 1)
    return [f"{rel}: {d}" for d in dom_diff(corpus_text, candidate_text)]


def compare_exact(rel: str, corpus_text: str, candidate_text: str) -> list[str]:
    if corpus_text == candidate_text:
        return []
    try:
        out: list[str] = []
        _close(json.loads(corpus_text), json.loads(candidate_text), rel, out)
        return out or [f"{rel}: text differs"]
    except ValueError:
        return [f"{rel}: text differs"]


def compare(name: str, rel: str, corpus_text: str, candidate_text: str) -> list[str]:
    if name == "fsrs":
        return compare_fsrs(corpus_text, candidate_text)
    if name == "contracts":
        return compare_pairs(corpus_text, candidate_text)
    if name == "html":
        return compare_html(rel, corpus_text, candidate_text)
    return compare_exact(rel, corpus_text, candidate_text)


# ---- the corpus matches a fresh extraction ------------------------------


@pytest.mark.parametrize("name", list(EXTRACTORS))
def test_oracles(name: str):
    corpus = committed(name)
    fresh = candidate(name)
    assert sorted(corpus) == sorted(fresh), "corpus file set drifted"
    problems: list[str] = []
    for rel in sorted(corpus):
        problems += compare(name, rel, corpus[rel], fresh[rel])
    assert not problems, "\n".join(problems[:40]) + (
        f"\n... {len(problems) - 40} more" if len(problems) > 40 else ""
    )


# ---- Python replays its own corpora --------------------------------------


def test_python_replays_fsrs_corpus():
    from datetime import datetime

    from fsrs import Scheduler

    from prep.domain.srs import CardSRSState, Verdict, schedule_review

    corpus = json.loads(committed("fsrs")["corpus.json"])
    assert corpus["header"]["transitions"] >= fsrs_mod.MIN_TRANSITIONS
    previous = fsrs_mod._install_fuzz_free_schedulers(tuple(Scheduler().parameters))
    problems: list[str] = []
    try:
        for case in corpus["cases"]:
            for i, row in enumerate(case["reviews"]):
                s = row["input"]
                state = CardSRSState(
                    stability=s["stability"],
                    difficulty=s["difficulty"],
                    fsrs_state=s["fsrs_state"],
                    last_review=datetime.fromisoformat(s["last_review"])
                    if s["last_review"]
                    else None,
                )
                now = datetime.fromisoformat(row["now"])
                try:
                    result = schedule_review(
                        state, Verdict(row["verdict"]), now=now, desired_retention=case["retention"]
                    )
                except AssertionError:
                    if "error" not in row:
                        problems.append(f"{case['id']}[{i}]: raised, corpus has output")
                    continue
                if "error" in row:
                    problems.append(f"{case['id']}[{i}]: corpus expects a refusal")
                    continue
                got = {
                    **fsrs_mod._state_dict(result.state),
                    "next_due": result.next_due.isoformat(),
                    "interval_seconds": result.interval_seconds,
                    "step_bucket": result.step_bucket,
                }
                _close(row["output"], got, f"{case['id']}[{i}]", problems)
    finally:
        fsrs_mod._restore_schedulers(previous)
    assert not problems, "\n".join(problems[:20])


def test_python_replays_grading_corpus():
    from prep.domain.grading import grade, match_regex, validate_regex_update

    corpus = json.loads(committed("grading")["corpus.json"])
    for row in corpus["grade"]:
        got = grading_mod._call(grade, row["question"], row["user_answer"], idk=row["idk"])
        assert got == {k: row[k] for k in ("result", "error") if k in row}, row["id"]
    for row in corpus["match_regex"]:
        assert match_regex(row["pattern"], row["given"]) == row["result"], row["id"]
    for row in corpus["validate_regex_update"]:
        got = validate_regex_update(
            row["pattern"], expected_literal=row["expected_literal"], prior_given=row["prior_given"]
        )
        assert got == row["result"], row["id"]


def test_markdown_corpus_matches_cases_json():
    corpus = json.loads(committed("markdown")["corpus.json"])["cases"]
    cases = {c["id"]: c for c in markdown_mod.load_cases()}
    assert [c["id"] for c in corpus] == list(cases)
    for row in corpus:
        assert row["input"] == cases[row["id"]]["input"], row["id"]
        assert row["expected"] == cases[row["id"]]["expected"], row["id"]
        assert markdown_mod.render(row["input"]) == row["expected"], row["id"]


def test_merge_corpus_covers_every_user_scoped_table():
    before = json.loads(committed("merge")["before.json"])
    after = json.loads(committed("merge")["after.json"])
    anon = before["header"]["anon"]
    for table, columns in before["header"]["user_scoped_tables"].items():
        assert any(before["tables"][table][c][anon] for c in columns), f"{table} unseeded"
        for c in columns:
            assert after["tables"][table][c][anon] == [], f"{table}.{c} still owned by anon"
    assert after["users"][anon] is None
    assert after["result"]["merged"] is True
    assert after["previous_ids"] == [anon]
    assert {"capitals", "capitals-2"} <= set(after["target_deck_slugs"])
    assert len(after["target_deck_slugs"]) == 104


def test_contracts_corpus_lists_every_route():
    corpus = json.loads(committed("contracts")["pairs.json"])
    listed = {(r["method"], r["path"]) for r in corpus["header"]["routes"]}
    for prefix in contracts_mod.PREFIXES:
        assert any(path.startswith(prefix) for _, path in listed), prefix
    assert ("GET", "/openapi.json") in listed
    assert ("POST", "/forget-device") in listed
    names = [p["name"] for p in corpus["pairs"]]
    assert len(names) == len(set(names))
    mcp_tools = {
        p["request"]["json"]["params"]["name"]
        for p in corpus["pairs"]
        if p["request"]["path"] == "/mcp"
        and isinstance(p["request"]["json"], dict)
        and p["request"]["json"].get("method") == "tools/call"
    }
    tools_list = next(p for p in corpus["pairs"] if p["name"] == "mcp-tools-list")
    advertised = {t["name"] for t in tools_list["response"]["json"]["result"]["tools"]}
    assert advertised <= mcp_tools, advertised - mcp_tools
    assert len(advertised) == 17


def test_contracts_cookie_lifecycle_recorded():
    corpus = json.loads(committed("contracts")["pairs.json"])
    by_name = {p["name"]: p for p in corpus["pairs"]}
    mint = by_name["instant-visitor-mints"]["response"]["set_cookie"]
    assert any(c.startswith("prep_anon=v1.") and "Max-Age=15552000" in c for c in mint)
    assert by_name["cookie-fresh-no-refresh"]["response"]["set_cookie"] == []
    refresh = by_name["cookie-refreshed-after-window"]["response"]["set_cookie"]
    assert any(c.startswith("prep_anon=v1.") for c in refresh)
    assert refresh != mint
    for name in ("forget-device", "cookie-bad-signature-cleared", "cookie-garbage-cleared"):
        cleared = by_name[name]["response"]["set_cookie"]
        assert any('prep_anon=""' in c and "Max-Age=0" in c for c in cleared), name
    assert by_name["forget-device"]["response"]["status"] == 303


# ---- coverage of the golden HTML set -------------------------------------


def _page_and_partial_templates() -> set[str]:
    out = set()
    for path in TEMPLATES.rglob("*.html"):
        rel = path.relative_to(TEMPLATES).as_posix()
        if rel.startswith("macros/"):
            continue
        out.add(rel)
    return out


def test_every_template_and_partial_has_a_context():
    covered = {c.template for c in all_contexts()}
    missing = sorted(_page_and_partial_templates() - covered)
    assert not missing, missing
    unknown = sorted(covered - _page_and_partial_templates())
    assert not unknown, unknown


_STATUS_LITERALS = (
    re.compile(r"(?:_status|progress\.status) == '([a-z_]+)'"),
    re.compile(r"(?:_status|progress\.status) in \(([^)]*)\)"),
    re.compile(r"^\s*'([a-z_]+)':\s*\(", re.MULTILINE),
)

PROGRESS_PARTIALS = {
    "partials/plan_progress.html": "starting",
    "partials/transform_progress.html": "",
    "partials/trivia_generating_progress.html": "starting",
}


def status_literals(partial: str) -> set[str]:
    source = (TEMPLATES / partial).read_text(encoding="utf-8")
    found: set[str] = set()
    for pattern in _STATUS_LITERALS:
        for m in pattern.finditer(source):
            found |= {s.strip().strip("'") for s in m.group(1).split(",")}
    return {s for s in found if s}


@pytest.mark.parametrize("partial", list(PROGRESS_PARTIALS))
def test_every_status_literal_has_a_context(partial: str):
    empty_label = PROGRESS_PARTIALS[partial]
    literals = status_literals(partial)
    assert len(literals) >= 4, literals
    seen: set[str] = set()
    for c in all_contexts():
        if c.template != partial:
            continue
        status = c.context["progress"].get("status")
        seen.add(status if status else empty_label)
    assert literals <= seen, sorted(literals - seen)


def test_diff_card_covers_every_field_and_the_empty_state():
    assert {f"changed-{f}" for f in DIFF_FIELDS} <= set(DIFF_CARD_STATES)
    assert "unchanged" in DIFF_CARD_STATES
    rendered = {c.name for c in all_contexts() if c.template == "partials/transform_diff_card.html"}
    assert set(DIFF_CARD_STATES) == rendered


def test_xss_deck_name_is_escaped_in_goldens():
    corpus = committed("html")
    for rel in ("index@xss-deck-name.html", "deck@xss-deck-name.html"):
        assert "<script>alert(1)</script>" not in corpus[rel], rel
        assert "alert(1)" in corpus[rel], rel


def test_golden_index_lists_every_file():
    corpus = committed("html")
    index = json.loads(corpus["index.json"])
    assert {e["file"] for e in index} == set(corpus) - {"index.json"}
    assert all(Path(e["file"]).suffix == ".html" for e in index)
