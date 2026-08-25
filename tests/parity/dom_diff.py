"""DOM-equivalence differ for rendered HTML.

Two documents are equal when their element trees match: same tags in
the same order, same attribute sets with decoded values, same text
after entity decoding and whitespace collapsing. Serialization noise
(attribute order, `&#34;` versus `&quot;`, a trailing newline,
comments, the doctype) never counts. Order still matters where the
browser cares: the sequence of `<script>` and `<link>` resources is
compared as a list.

`dom_diff(a, b)` returns every difference with a CSS-like path so a
failing comparison names the node.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# Text inside these keeps its whitespace verbatim.
PRE_ELEMENTS = frozenset({"pre", "textarea", "script", "style"})

# Elements the parser must not auto-close while inside (raw text).
_WS = re.compile(r"\s+")


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Diff:
    path: str
    kind: str
    a: object
    b: object

    def __str__(self) -> str:
        return f"{self.path}: {self.kind}: {self.a!r} != {self.b!r}"


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self._cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: ("" if v is None else v) for k, v in attrs}, parent=self._cur)
        self._cur.children.append(node)
        if tag not in VOID_ELEMENTS:
            self._cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: ("" if v is None else v) for k, v in attrs}, parent=self._cur)
        self._cur.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        node = self._cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is None or node.parent is None:
            return
        self._cur = node.parent

    def handle_data(self, data: str) -> None:
        self._cur.children.append(data)

    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass


def parse(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    _normalize(builder.root, preformatted=False)
    return builder.root


def _normalize(node: Node, preformatted: bool) -> None:
    """Merge adjacent text, collapse whitespace outside preformatted
    elements, and drop text that collapsed to nothing."""
    pre = preformatted or node.tag in PRE_ELEMENTS
    merged: list[Node | str] = []
    for child in node.children:
        if isinstance(child, str) and merged and isinstance(merged[-1], str):
            merged[-1] = merged[-1] + child
        else:
            merged.append(child)
    out: list[Node | str] = []
    for child in merged:
        if isinstance(child, str):
            text = child if pre else _WS.sub(" ", child).strip()
            if pre and node.tag in PRE_ELEMENTS:
                text = text.strip("\n") if node.tag != "textarea" else text
            if text:
                out.append(text)
        else:
            _normalize(child, pre)
            out.append(child)
    node.children = out


def _path(node: Node, index_among_siblings: int | None = None) -> str:
    parts: list[str] = []
    cur: Node | None = node
    while cur is not None and cur.tag != "#document":
        parent = cur.parent
        label = cur.tag
        if parent is not None:
            same = [c for c in parent.children if isinstance(c, Node) and c.tag == cur.tag]
            if len(same) > 1:
                label = f"{cur.tag}:nth-of-type({same.index(cur) + 1})"
        if cur.attrs.get("id"):
            label = f"{cur.tag}#{cur.attrs['id']}"
        parts.append(label)
        cur = parent
    return " > ".join(reversed(parts)) or "#document"


def _is_json_script(node: Node) -> bool:
    return node.tag == "script" and node.attrs.get("type", "").strip() == "application/json"


def _resource_list(root: Node) -> list[tuple[str, str]]:
    """The ordered `(tag, src|href)` list of every script and link."""
    out: list[tuple[str, str]] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "script" and child.attrs.get("src"):
                out.append(("script", child.attrs["src"]))
            elif child.tag == "link" and child.attrs.get("href"):
                out.append(("link", child.attrs["href"]))
            walk(child)

    walk(root)
    return out


def _diff_nodes(a: Node, b: Node, out: list[Diff]) -> None:
    path = _path(a)
    if a.tag != b.tag:
        out.append(Diff(path, "tag", a.tag, b.tag))
        return
    if a.attrs != b.attrs:
        for key in sorted(set(a.attrs) | set(b.attrs)):
            va, vb = a.attrs.get(key), b.attrs.get(key)
            if va != vb:
                out.append(Diff(f"{path}[{key}]", "attribute", va, vb))
    if _is_json_script(a) and _is_json_script(b):
        ta = "".join(c for c in a.children if isinstance(c, str))
        tb = "".join(c for c in b.children if isinstance(c, str))
        try:
            ja, jb = json.loads(ta), json.loads(tb)
        except ValueError:
            if ta != tb:
                out.append(Diff(path, "json-text", ta, tb))
            return
        if ja != jb:
            out.append(Diff(path, "json", ja, jb))
        return
    if len(a.children) != len(b.children):
        out.append(
            Diff(
                path,
                "children",
                [_summary(c) for c in a.children],
                [_summary(c) for c in b.children],
            )
        )
        return
    for ca, cb in zip(a.children, b.children, strict=True):
        if isinstance(ca, str) or isinstance(cb, str):
            if ca != cb:
                out.append(Diff(path, "text", ca, cb))
            continue
        _diff_nodes(ca, cb, out)


def _summary(child: Node | str) -> str:
    if isinstance(child, str):
        return f"#text({child[:40]!r})"
    return f"<{child.tag}>"


def dom_diff(a: str, b: str) -> list[Diff]:
    """Every difference between two HTML documents; empty when the
    documents are DOM-equivalent."""
    ra, rb = parse(a), parse(b)
    out: list[Diff] = []
    _diff_nodes(ra, rb, out)
    resources_a, resources_b = _resource_list(ra), _resource_list(rb)
    if resources_a != resources_b:
        out.append(Diff("#document", "resources", resources_a, resources_b))
    return out


def dom_equal(a: str, b: str) -> bool:
    return not dom_diff(a, b)
