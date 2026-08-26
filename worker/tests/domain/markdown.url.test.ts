import { describe, expect, it } from "vitest";
import { pythonJson } from "../pyoracle";
import { escapeUrl, safeEntity, safeUrl, striptags, unescape, unquote } from "../../domain/markdown/url";
import { pyStrip, pyre } from "../../domain/markdown/chars";

// mistune.util and HTMLRenderer.safe_url against the venv's own answers.
const URLS = [
  "https://a.com/`x`",
  "https://ex.com/a?b=1&c=2",
  "https://ex.com/?q=&amp;b",
  "&#106;avascript:alert(1)",
  "javascript:alert(1)",
  "  JAVAscript:x",
  "%6a%61vascript:x",
  "%256a%61vascript:x",
  "%25256a%61vascript:x",
  "data:image/png;base64,AAA",
  "data:text/html,x",
  "/rel",
  "#frag",
  "?q",
  "rel/path:x",
  "a:b/c",
  "mailto:a@b.c",
  "tel:123",
  "ftp://x",
  "vbscript:x",
  "é/ü",
  'a b"c',
  "&ampfoo;",
  "&xyz;",
  "&#0;",
  "&#x110000;",
  "&#128;",
  "&#xD800;",
  "&NotEqualTilde;",
  "&amp;lt;",
  "&lt",
  "%C3é%A9",
  "%zz",
  " x",
  "https://x.com/%20a",
  "\u{1F600} x",
  " javascript:x",
];

const STRIP = ['<img src="x" alt="A &amp; b">rest', "<em>a</em> <!-- c --> b", "<img src='x' alt='q'/>"];

interface Row {
  escape_url: string;
  unescape: string;
  safe_url: string;
  safe_entity: string;
  unquote: string;
}

const expected = pythonJson<{ urls: Record<string, Row>; strip: Record<string, string> }>(`
import json, sys
from urllib.parse import unquote
from mistune.util import escape_url, unescape, striptags, safe_entity
from mistune.renderers.html import HTMLRenderer
r = HTMLRenderer(escape=True)
urls = json.loads(${JSON.stringify(JSON.stringify(URLS))})
strip = json.loads(${JSON.stringify(JSON.stringify(STRIP))})
print(json.dumps({
  "urls": {u: {"escape_url": escape_url(u), "unescape": unescape(u), "safe_url": r.safe_url(u),
               "safe_entity": safe_entity(u), "unquote": unquote(u)} for u in urls},
  "strip": {s: striptags(s) for s in strip},
}))
`);

describe("url helpers match mistune", () => {
  for (const u of URLS) {
    it(`handles ${JSON.stringify(u)}`, () => {
      const row = expected.urls[u]!;
      expect(unescape(u)).toBe(row.unescape);
      expect(escapeUrl(u)).toBe(row.escape_url);
      expect(safeUrl(u)).toBe(row.safe_url);
      expect(safeEntity(u)).toBe(row.safe_entity);
      expect(unquote(u)).toBe(row.unquote);
    });
  }
  it("striptags", () => {
    for (const s of STRIP) expect(striptags(s)).toBe(expected.strip[s]);
  });
});

describe("python string helpers", () => {
  it("strips the isspace set, not the JS one", () => {
    expect(pyStrip("\x1c a 　")).toBe("a");
    expect(pyStrip("﻿a﻿")).toBe("﻿a﻿");
    expect(pyStrip("xxaxx", "x")).toBe("a");
  });
  it("pyre translates ^ $ . and \\s under re.M", () => {
    const re = pyre("^ {0,3}>(?P<q>.*?)$", "y", true);
    re.lastIndex = 2;
    expect(re.exec("a\n> b c\nd")?.groups?.q).toBe(" b c");
    expect(pyre("\\s+", "").test("\x1f")).toBe(true);
    expect(pyre("[^\\s*]", "").test("\x1f")).toBe(false);
  });
});
