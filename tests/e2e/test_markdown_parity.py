"""Browser side of the card-prompt markdown parity pin.

tests/web/test_markdown_parity_fixtures.py pins the server's mistune
filter to tests/fixtures/markdown/cases.json; this module pins the JS
twin (static/js/study/markdown.js) to the same file by importing it
into a real Chromium page and rendering every case. Comparison is
whitespace-normalized (inter-tag whitespace collapsed; whitespace
inside text and code content still counts). Cases where the twin
deliberately diverges carry js_expected, which only this suite reads.

Also pins the safety property the escape-first design guarantees: a
card whose prompt is an XSS payload, rendered through the REAL
studyCardView component (innerHTML path), shows the payload as text
with no script/img node and no dialog.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "markdown" / "cases.json"

with open(FIXTURE_PATH, encoding="utf-8") as _f:
    CASES = json.load(_f)["cases"]

_RUN_CASES_JS = """
async ({prefix, cases}) => {
  const m = await import(prefix + "study/markdown.js");
  const out = [];
  for (const c of cases) {
    try {
      out.push({id: c.id, ok: true, value: m.markdownHTML(c.input)});
    } catch (e) {
      out.push({id: c.id, ok: false, error: String(e)});
    }
  }
  return out;
}
"""

# Mounts the real answer-entry view with a hostile prompt. alert is
# stubbed to a counter first: an <img onerror> that survived escaping
# would fire it asynchronously, and a counter read beats waiting on a
# dialog that must never open.
_XSS_MOUNT_JS = """
async ({prefix, prompt}) => {
  const components = await import(prefix + "study/components.js");
  window.__xssFired = 0;
  const nativeAlert = window.alert;
  window.alert = () => { window.__xssFired += 1; };
  const host = document.createElement("div");
  document.body.appendChild(host);
  const card = {question_id: 999, type: "short", prompt, answer: "n/a"};
  host.appendChild(
    components.studyCardView(card, {onAnswer: () => {}, onIdk: () => {}, onPause: () => {}})
  );
  await new Promise((r) => setTimeout(r, 300));
  const promptEl = host.querySelector(".study-prompt");
  const result = {
    text: promptEl ? promptEl.innerText : null,
    scriptNodes: promptEl ? promptEl.querySelectorAll("script").length : -1,
    imgNodes: promptEl ? promptEl.querySelectorAll("img").length : -1,
    fired: window.__xssFired,
  };
  window.alert = nativeAlert;
  host.remove();
  return result;
}
"""


# Deep-nesting fallback probe: render the pathological prompt raw
# (expecting a stack-overflow throw), then mount it through the real
# card view, which must fall back to plain text instead of dying.
_DEEP_NESTING_JS = """
async ({prefix, prompt}) => {
  const m = await import(prefix + "study/markdown.js");
  let threw = false;
  try {
    m.markdownHTML(prompt);
  } catch (e) {
    threw = true;
  }
  const components = await import(prefix + "study/components.js");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const card = {question_id: 998, type: "short", prompt, answer: "n/a"};
  host.appendChild(
    components.studyCardView(card, {onAnswer: () => {}, onIdk: () => {}, onPause: () => {}})
  );
  const promptEl = host.querySelector(".study-prompt");
  const result = {
    threw,
    hasPrompt: Boolean(promptEl),
    blockquotes: promptEl ? promptEl.querySelectorAll("blockquote").length : -1,
    textStart: promptEl ? promptEl.textContent.trim().slice(0, 4) : null,
  };
  host.remove();
  return result;
}
"""


def _module_prefix(page) -> str:
    """The versioned "@/" URL prefix from the offline shell's
    importmap, so imports resolve without an import map applying
    inside evaluate()'d scripts."""
    prefix = page.evaluate(
        "() => JSON.parse(document.querySelector('script[type=importmap]').textContent)"
        ".imports['@/']"
    )
    assert prefix, "offline shell importmap missing the '@/' entry"
    return prefix


def _normalize(html: str) -> str:
    """Whitespace-insensitive comparison shape: collapse whitespace
    that sits strictly between tags. Whitespace inside code blocks
    borders non-tag characters, so it survives."""
    return re.sub(r">\s+<", "><", html).strip()


def test_markdown_cases_in_browser(offline_server, offline_page):
    """Every fixture case through the real markdownHTML() in Chromium.
    The count floor keeps the pin falsifiable, matching the Python
    suite's floor."""
    assert len(CASES) >= 25
    offline_page.goto(offline_server.base_url + "/offline")
    prefix = _module_prefix(offline_page)
    results = offline_page.evaluate(_RUN_CASES_JS, {"prefix": prefix, "cases": CASES})
    assert len(results) == len(CASES)

    problems = []
    for case, result in zip(CASES, results, strict=True):
        want = case.get("js_expected", case["expected"])
        if not result.get("ok"):
            problems.append(f"{case['id']}: threw {result.get('error')}")
        elif _normalize(result["value"]) != _normalize(want):
            problems.append(f"{case['id']}: got {result['value']!r}, expected {want!r}")
    assert not problems, "markdown.js diverged from fixtures:\n" + "\n".join(problems)


def test_prompt_xss_renders_inert(offline_server, offline_page):
    """A card prompt carrying script/img payloads, rendered through
    the real study card view: the payload is visible text, the prompt
    holds no script or img node, and nothing fires."""
    page = offline_page
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(offline_server.base_url + "/offline")
    result = page.evaluate(
        _XSS_MOUNT_JS,
        {
            "prefix": _module_prefix(page),
            "prompt": '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">',
        },
    )
    assert "<script>alert(1)</script>" in result["text"]
    assert '<img src=x onerror="alert(1)">' in result["text"]
    assert result["scriptNodes"] == 0
    assert result["imgNodes"] == 0
    assert result["fired"] == 0
    assert dialogs == []


def test_pathological_nesting_still_mounts(offline_server, offline_page):
    """A prompt nested 20,000 blockquotes deep mounts without throwing
    and renders exactly what the server renders: mistune nests 20
    levels and keeps the rest as text, and the shared renderer agrees."""
    page = offline_page
    page.goto(offline_server.base_url + "/offline")
    result = page.evaluate(
        _DEEP_NESTING_JS,
        {"prefix": _module_prefix(page), "prompt": "> " * 20000 + "x"},
    )
    assert result["threw"] is False
    assert result["hasPrompt"] is True
    assert result["blockquotes"] == 20
    assert result["textStart"] == "> > "
