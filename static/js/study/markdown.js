// markdown.js: the client-side twin of the server's mistune filter
// (escape=True, hard_wrap=False, strikethrough) so offline card
// prompts render the same markup the online templates get. The whole
// input is HTML-escaped BEFORE any transform and every transform only
// inserts fixed tags around already-escaped text, so the returned
// string is safe to assign to innerHTML no matter what the text
// contains. Anything outside the supported subset degrades safely
// (divergence list below). Pathological nesting depth can overflow
// the stack and throw; innerHTML callers guard with a plain-text
// fallback.
// Parity with mistune is pinned by tests/fixtures/markdown/cases.json
// (python: tests/web/test_markdown_parity_fixtures.py, browser:
// tests/e2e/test_markdown_parity.py).
//
// Subset: paragraphs (soft wrap + trailing-two-space hard break),
// #..###### headings, fenced code (``` / ~~~, language-<x> class),
// `code`, **strong**, __strong__, *em*, _em_, ***em strong***,
// ~~del~~, [label](http(s) url), flat tight ul/ol (start attr),
// blockquotes, thematic breaks. Known divergences from mistune, all
// safe but not all literal: tables, indented code, and non-http(s)
// link schemes stay literal text; images render as bang + link; a
// setext underline reads as paragraph + rule; backslash escapes do
// not suppress emphasis; nested lists flatten; list continuation
// lines and lazy blockquote continuations escape into paragraphs;
// loose lists render as separate tight lists.

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// CommonMark code-span trim: one space off each end when both are
// present and the content is not all spaces.
function trimCodeSpan(body) {
  if (body.length > 2 && body.startsWith(" ") && body.endsWith(" ") && body.trim() !== "") {
    return body.slice(1, -1);
  }
  return body;
}

// Emphasis passes over escaped text. Opening markers must not be
// followed by whitespace and closing markers not preceded by it
// (mistune leaves "a * b" alone); _ additionally refuses intraword
// matches so snake_case stays literal.
function emphasis(text) {
  return text
    .replace(/\*\*\*([^\s*](?:[^*]*?[^\s*])?)\*\*\*/g, "<em><strong>$1</strong></em>")
    .replace(/\*\*([^\s*](?:[\s\S]*?[^\s])?)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<!\w)__([^\s_](?:[\s\S]*?[^\s])?)__(?!\w)/g, "<strong>$1</strong>")
    .replace(/\*([^\s*](?:[^*]*?[^\s*])?)\*/g, "<em>$1</em>")
    .replace(/(?<!\w)_([^\s_](?:[^_]*?[^\s_])?)_(?!\w)/g, "<em>$1</em>")
    .replace(/~~([^\s~](?:[\s\S]*?[^\s~])?)~~/g, "<del>$1</del>");
}

// Inline rendering. Code spans and whole anchors are stashed behind
// NUL-framed placeholders (unforgeable: input NULs are replaced up
// front) so markdown inside code stays literal and emphasis passes
// cannot rewrite a URL.
function inlineHTML(text) {
  const stash = [];
  const put = (html) => "\u0000" + (stash.push(html) - 1) + "\u0000";
  let s = text
    .replace(/``([^\n]+?)``/g, (m, body) => put("<code>" + trimCodeSpan(body) + "</code>"))
    .replace(/`([^`\n]+)`/g, (m, body) => put("<code>" + trimCodeSpan(body) + "</code>"))
    // \u0000 is excluded from the URL: a stashed placeholder (a
    // code span inside the parens) must never expand inside an
    // attribute value, so such links stay literal text instead.
    .replace(/\[([^\]\n]*)\]\((https?:\/\/[^\s)\u0000]+)\)/g, (m, label, url) =>
      put('<a href="' + url + '">' + emphasis(label) + "</a>")
    );
  s = emphasis(s).replace(/ {2,}\n/g, "<br />\n");
  for (let guard = 0; s.includes("\u0000") && guard <= stash.length; guard++) {
    s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[Number(i)]);
  }
  return s;
}

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})[ \t]*(.*)$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*)$/;
const HR = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const BULLET = /^ {0,3}[-*+]\s+(.*)$/;
const ORDERED = /^ {0,3}(\d{1,9})\.\s+(.*)$/;
// The source is escaped, so the blockquote marker is &gt; here.
const QUOTE = /^ {0,3}&gt; ?(.*)$/;
const BLANK = /^\s*$/;

function listBlock(lines, i, itemRe, openTag, closeTag) {
  let items = "";
  while (i < lines.length) {
    const m = lines[i].match(itemRe);
    if (!m) break;
    items += "<li>" + inlineHTML(m[m.length - 1].replace(/[ \t]+$/, "")) + "</li>\n";
    i += 1;
  }
  return {html: openTag + "\n" + items + closeTag + "\n", next: i};
}

function renderBlocks(lines) {
  let out = "";
  let para = [];
  const flush = () => {
    if (!para.length) return;
    const body = para
      .map((l) => l.replace(/^[ \t]+/, ""))
      .join("\n")
      .replace(/[ \t]+$/, "");
    out += "<p>" + inlineHTML(body) + "</p>\n";
    para = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE_OPEN);
    if (fence && !(fence[1][0] === "`" && fence[2].includes("`"))) {
      flush();
      const close = new RegExp("^ {0,3}" + fence[1][0] + "{" + fence[1].length + ",}[ \\t]*$");
      const body = [];
      i += 1;
      while (i < lines.length && !close.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or EOF)
      const lang = fence[2].trim().split(/\s+/)[0];
      out +=
        "<pre><code" +
        (lang ? ' class="language-' + lang + '"' : "") +
        ">" +
        (body.length ? body.join("\n") + "\n" : "") +
        "</code></pre>\n";
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      flush();
      const n = heading[1].length;
      out += "<h" + n + ">" + inlineHTML(heading[2].trim()) + "</h" + n + ">\n";
      i += 1;
      continue;
    }
    if (HR.test(line)) {
      flush();
      out += "<hr />\n";
      i += 1;
      continue;
    }
    const quote = line.match(QUOTE);
    if (quote) {
      flush();
      const inner = [];
      while (i < lines.length) {
        const m = lines[i].match(QUOTE);
        if (!m) break;
        inner.push(m[1]);
        i += 1;
      }
      out += "<blockquote>\n" + renderBlocks(inner) + "</blockquote>\n";
      continue;
    }
    if (BULLET.test(line)) {
      flush();
      const block = listBlock(lines, i, BULLET, "<ul>", "</ul>");
      out += block.html;
      i = block.next;
      continue;
    }
    const ord = line.match(ORDERED);
    if (ord) {
      flush();
      const start = Number(ord[1]);
      const openTag = start !== 1 ? '<ol start="' + start + '">' : "<ol>";
      const block = listBlock(lines, i, ORDERED, openTag, "</ol>");
      out += block.html;
      i = block.next;
      continue;
    }
    if (BLANK.test(line)) {
      flush();
      i += 1;
      continue;
    }
    para.push(line);
    i += 1;
  }
  flush();
  return out;
}

// Render markdown text to a safe HTML string for innerHTML. Empty or
// missing text renders to "".
export function markdownHTML(text) {
  if (!text) return "";
  const escaped = escapeHtml(
    String(text).replace(/\r\n?/g, "\n").replace(/\u0000/g, "\uFFFD")
  );
  return renderBlocks(escaped.split("\n"));
}
