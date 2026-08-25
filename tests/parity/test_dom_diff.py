"""What the DOM differ ignores, and what it must catch."""

from __future__ import annotations

import pytest

from tests.parity.dom_diff import dom_diff, dom_equal

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="/static/css/vce11d0000000/index.css">
  <script type="importmap">{"imports": {"@/": "/static/js/v1/"}}</script>
  <script type="module" src="/static/js/app.js" defer></script>
  <script src="/static/js/vendor/htmx.min.js" defer></script>
  <script type="application/json" id="dashboard-overview">{"decks": [], "due": 0, "user": {"is_anonymous": false}}</script>
</head>
<body class="page-index" data-editor-mode="vim">
  <!-- a comment -->
  <main>
    <ol class="qcards">
      <li class="qcard" data-qid="41" tabindex="0" role="button">Capital of France?</li>
      <li class="qcard" data-qid="42" tabindex="0">Capital of Japan?</li>
    </ol>
    <input type="text" name="confirm" disabled>
    <pre>  keep   this
spacing </pre>
    <textarea name="prompt">  raw  text  </textarea>
    <p title="say &quot;hi&quot;">Two   words</p>
  </main>
</body>
</html>
"""


def test_identical_is_equal():
    assert dom_diff(PAGE, PAGE) == []


def test_trailing_newline_is_ignored():
    assert dom_equal(PAGE, PAGE.rstrip("\n"))
    assert dom_equal(PAGE, PAGE + "\n\n")


def test_attribute_order_is_ignored():
    swapped = PAGE.replace(
        '<li class="qcard" data-qid="41" tabindex="0" role="button">',
        '<li role="button" tabindex="0" data-qid="41" class="qcard">',
    )
    assert swapped != PAGE
    assert dom_equal(PAGE, swapped)


def test_numeric_and_named_entities_are_equal():
    numeric = PAGE.replace("&quot;", "&#34;")
    assert numeric != PAGE
    assert dom_equal(PAGE, numeric)


def test_boolean_attribute_forms_are_equal():
    explicit = PAGE.replace("disabled>", 'disabled="">')
    assert dom_equal(PAGE, explicit)


def test_whitespace_collapses_outside_preformatted():
    loose = PAGE.replace("<p title", "<p\n   title").replace("Two   words", "Two words")
    assert dom_equal(PAGE, loose)


def test_whitespace_inside_pre_counts():
    changed = PAGE.replace("keep   this", "keep this")
    diffs = dom_diff(PAGE, changed)
    assert diffs and diffs[0].path.endswith("pre")


def test_tojson_separators_are_equal():
    compact = PAGE.replace(
        '{"decks": [], "due": 0, "user": {"is_anonymous": false}}',
        '{"decks":[],"due":0,"user":{"is_anonymous":false}}',
    )
    assert compact != PAGE
    assert dom_equal(PAGE, compact)


def test_json_key_order_is_equal():
    reordered = PAGE.replace(
        '{"decks": [], "due": 0, "user": {"is_anonymous": false}}',
        '{"user": {"is_anonymous": false}, "due": 0, "decks": []}',
    )
    assert dom_equal(PAGE, reordered)


def test_json_value_change_is_reported_at_the_script():
    changed = PAGE.replace('"due": 0', '"due": 1')
    diffs = dom_diff(PAGE, changed)
    assert len(diffs) == 1
    assert diffs[0].kind == "json"
    assert diffs[0].path.endswith("script#dashboard-overview")


def test_comments_are_ignored():
    assert dom_equal(PAGE, PAGE.replace("<!-- a comment -->", ""))


def test_doctype_is_ignored():
    assert dom_equal(PAGE, PAGE.replace("<!doctype html>", "<!DOCTYPE HTML>"))
    assert dom_equal(PAGE, PAGE.replace("<!doctype html>\n", ""))


def test_missing_data_attribute_is_reported_at_its_path():
    missing = PAGE.replace('data-qid="42" ', "")
    diffs = dom_diff(PAGE, missing)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "attribute"
    assert d.path.endswith("li:nth-of-type(2)[data-qid]")
    assert d.a == "42" and d.b is None


def test_changed_data_attribute_is_reported_at_its_path():
    changed = PAGE.replace('data-qid="41"', 'data-qid="99"')
    diffs = dom_diff(PAGE, changed)
    assert [(d.path, d.a, d.b) for d in diffs] == [
        ("html > body > main > ol > li:nth-of-type(1)[data-qid]", "41", "99")
    ]


def test_text_change_is_reported():
    changed = PAGE.replace("Capital of Japan?", "Capital of Peru?")
    diffs = dom_diff(PAGE, changed)
    assert len(diffs) == 1 and diffs[0].kind == "text"


def test_script_order_swapped_is_not_equal():
    swapped = PAGE.replace(
        '<script type="module" src="/static/js/app.js" defer></script>\n'
        '  <script src="/static/js/vendor/htmx.min.js" defer></script>',
        '<script src="/static/js/vendor/htmx.min.js" defer></script>\n'
        '  <script type="module" src="/static/js/app.js" defer></script>',
    )
    assert swapped != PAGE
    diffs = dom_diff(PAGE, swapped)
    assert diffs
    assert any(d.kind == "resources" for d in diffs)


def test_reordered_link_is_not_equal():
    extra = PAGE.replace(
        '<link rel="stylesheet" href="/static/css/vce11d0000000/index.css">',
        '<link rel="stylesheet" href="/static/css/vce11d0000000/index.css">\n'
        '  <link rel="manifest" href="/manifest.json">',
    )
    reordered = PAGE.replace(
        '<link rel="stylesheet" href="/static/css/vce11d0000000/index.css">',
        '<link rel="manifest" href="/manifest.json">\n'
        '  <link rel="stylesheet" href="/static/css/vce11d0000000/index.css">',
    )
    assert not dom_equal(extra, reordered)
    assert any(d.kind == "resources" for d in dom_diff(extra, reordered))


def test_textarea_keeps_its_whitespace():
    changed = PAGE.replace("  raw  text  ", "raw text")
    assert not dom_equal(PAGE, changed)


def test_void_elements_do_not_swallow_siblings():
    a = '<p><input name="a"><span>x</span></p>'
    b = '<p><input name="a"/><span>x</span></p>'
    assert dom_equal(a, b)
    assert not dom_equal(a, '<p><input name="a"><span>y</span></p>')


@pytest.mark.parametrize(
    "a,b",
    [
        ("<p>a</p>", "<p>b</p>"),
        ("<p>a</p>", "<div>a</div>"),
        ("<p>a</p>", "<p>a</p><p>b</p>"),
        ('<a href="/x">a</a>', '<a href="/y">a</a>'),
    ],
)
def test_real_differences_are_caught(a, b):
    assert not dom_equal(a, b)


def test_diff_str_names_the_path():
    diffs = dom_diff("<p>a</p>", "<p>b</p>")
    assert str(diffs[0]).startswith("p: text: 'a' != 'b'")
