import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { markdownHTML } from "../../domain/markdown";

// The rendered HTML is inert: only the tags and attributes mistune emits
// for this subset appear, every other `<` is escaped, and no attribute
// carries script.
const REPO = new URL("../../..", import.meta.url).pathname;
const corpus = JSON.parse(readFileSync(`${REPO}tests/fixtures/parity/markdown/corpus.json`, "utf8")) as {
  cases: { id: string; input: string }[];
};

const TAGS = new Set([
  "p", "em", "strong", "del", "a", "img", "code", "pre", "br", "hr", "blockquote",
  "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
  "table", "thead", "tbody", "tr", "th", "td",
]);
const ATTRS = new Set(["href", "src", "alt", "class", "start", "style", "title"]);

const HOSTILE = [
  "<script>alert(1)</script>",
  '<img src=x onerror="alert(1)">',
  "<svg/onload=alert(1)>",
  '<iframe src="javascript:alert(1)"></iframe>',
  '"><script>alert(1)</script>',
  "[click](javascript:alert(1))",
  "[click](JaVaScRiPt:alert(1))",
  "[click](  javascript:alert(1))",
  "[click](java\tscript:alert(1))",
  "[click](&#106;avascript:alert(1))",
  "[click](&#x6A;&#x61;vascript:alert(1))",
  "[click](javascript&#58;alert(1))",
  "[click](%6Aavascript:alert(1))",
  "[click](%256Aavascript:alert(1))",
  "[click](vbscript:msgbox(1))",
  "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
  "![x](javascript:alert(1))",
  "![x](data:text/html,<script>alert(1)</script>)",
  "![x](data:image/svg+xml;base64,PHN2Zy8+)",
  '[x](https://a.b/" onclick="alert(1))',
  '[x](https://a.b/ "t" onclick="alert(1)")',
  '[x](https://a.b/ "t\\" onclick=\\"alert(1)")',
  '![x" onerror="alert(1)](https://a.b/i.png)',
  '![x](https://a.b/i.png "t" onerror="alert(1)")',
  "[x](https://a.b/?q=<script>)",
  "[x](<https://a.b/ onclick=alert(1)>)",
  "<a href=\"javascript:alert(1)\">x</a>",
  "<a href='javascript:alert(1)'>*x*</a>",
  "text &lt;script&gt;alert(1)&lt;/script&gt;",
  "&#60;script&#62;alert(1)&#60;/script&#62;",
  "&lt;img src=x onerror=alert(1)&gt;",
  "```<script>\nalert(1)\n```",
  '``` x" onclick="alert(1)\nz\n```',
  "``` x&quot;y\nz\n```",
  "`<script>alert(1)</script>`",
  "a\u0000b",
  "[x](https://a.b/\u0000)",
  "```\nunterminated <script>alert(1)",
  "~~~python\nunterminated\n",
  "<!-- <script>alert(1)</script> -->",
  "<?php echo 1 ?>",
  "<![CDATA[<script>]]>",
  "<!DOCTYPE html><script>alert(1)</script>",
  "| a | b |\n|---|---|\n| <script> | onclick=x |",
  "| <img src=x onerror=alert(1)> |\n|---|\n| x |",
  "> ".repeat(10000) + "x",
  "> ".repeat(20000) + "x",
  "[".repeat(5000) + "x" + "]".repeat(5000),
  "*".repeat(5000),
  "~~".repeat(3000) + "x",
  "<a>".repeat(2000),
  "- ".repeat(2000) + "x",
];

function assertInert(input: string, html: string): void {
  expect(html).not.toMatch(/<script/i);
  let rest = html;
  for (const tag of html.matchAll(/<(\/?)([a-zA-Z][a-zA-Z0-9]*)([^<>]*)>/g)) {
    const [whole, , name, attrs] = tag;
    expect(TAGS.has(name!), `${JSON.stringify(input)} emitted <${name}>`).toBe(true);
    const body = attrs!.replace(/\s*\/$/, "");
    for (const a of body.matchAll(/\s*([^\s=]+)(?:="([^"]*)")?/g)) {
      const [, attr, value] = a;
      expect(ATTRS.has(attr!), `${JSON.stringify(input)} emitted attribute ${attr}`).toBe(true);
      expect(a[0].includes("="), `${JSON.stringify(input)} emitted bare attribute ${attr}`).toBe(true);
      if (attr === "href" || attr === "src") {
        const scheme = (value ?? "").replace(/&[^;]+;/g, "").trim().toLowerCase();
        expect(scheme, `${JSON.stringify(input)} href ${value}`).not.toMatch(/^(javascript|vbscript|data:text)/);
      }
    }
    const skeleton = whole.replace(/="[^"]*"/g, '=""');
    expect(skeleton).not.toMatch(/ on[a-z]+=/i);
    expect(skeleton).not.toMatch(/javascript:/i);
    rest = rest.replace(whole, "");
  }
  expect(rest, `${JSON.stringify(input)} leaked a bracket`).not.toMatch(/[<>]/);
}

describe("markdown output is inert", () => {
  for (const c of corpus.cases) {
    it(`corpus ${c.id}`, () => assertInert(c.input, markdownHTML(c.input)));
  }
  for (const input of HOSTILE) {
    it(`hostile ${JSON.stringify(input.slice(0, 40))}`, () => assertInert(input, markdownHTML(input)));
  }

  it("shows a script payload as text through the paragraph path", () => {
    const html = markdownHTML('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">');
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).toContain("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
    expect(html).not.toContain("<img");
  });

  it("neutralises harmful schemes to the sink", () => {
    expect(markdownHTML("[c](javascript:alert(1))")).toBe('<p><a href="#harmful-link">c</a></p>\n');
    expect(markdownHTML("[c](&#106;avascript:alert(1))")).toBe('<p><a href="#harmful-link">c</a></p>\n');
    expect(markdownHTML("![i](data:text/html,x)")).toBe('<p><img src="#harmful-link" alt="i" /></p>\n');
  });

  it("caps container nesting and never throws", () => {
    const deep = markdownHTML("> ".repeat(20000) + "x");
    expect((deep.match(/<blockquote>/g) ?? []).length).toBe(20);
    expect(deep).toContain("&gt; &gt;");
    expect(typeof markdownHTML("- ".repeat(5000) + "x")).toBe("string");
  });
});
