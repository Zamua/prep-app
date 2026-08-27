import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  STATIC,
  bakeBuildInfo,
  bakeIcons,
  copyStatic,
  precacheTree,
  precompileTemplates,
  walk,
} from "../scripts/build.mjs";

const scratch: string[] = [];
function tmp(): string {
  const d = mkdtempSync(join(tmpdir(), "prep-build-"));
  scratch.push(d);
  return d;
}
afterEach(() => {
  for (const d of scratch.splice(0)) rmSync(d, { recursive: true, force: true });
});

function seed(root: string, files: Record<string, string>) {
  for (const [rel, body] of Object.entries(files)) {
    mkdirSync(join(root, rel, ".."), { recursive: true });
    writeFileSync(join(root, rel), body);
  }
}

describe("walk", () => {
  it("orders paths component-wise, as pathlib sorts", () => {
    const root = tmp();
    seed(root, { "a/b.css": "", "a-b.css": "", "a.css": "", "b/c/d.css": "", "b.css": "" });
    expect(walk(root)).toEqual(["a/b.css", "a-b.css", "a.css", "b/c/d.css", "b.css"]);
  });
});

describe("precompileTemplates", () => {
  it("writes one entry per template, keyed by its relative name", () => {
    const templates = tmp();
    const out = tmp();
    seed(templates, { "base.html": "<p>{{ x }}</p>", "partials/row.html": "{% extends 'base.html' %}", "notes.txt": "skip" });
    expect(precompileTemplates(templates, out)).toBe(2);
    const js = readFileSync(join(out, "templates.js"), "utf8");
    expect(js).toContain('"base.html":');
    expect(js).toContain('"partials/row.html":');
    expect(js).not.toContain("notes.txt");
    expect(existsSync(join(out, "templates.d.ts"))).toBe(true);
  });

  it("fails naming the template that does not parse", () => {
    const templates = tmp();
    seed(templates, { "ok.html": "fine", "bad/slice.html": "{{ items[:3] }}" });
    expect(() => precompileTemplates(templates, tmp())).toThrow(/template bad\/slice\.html does not precompile/);
  });
});

describe("bakeIcons", () => {
  it("maps every static/icons svg by stem to its trimmed source", () => {
    const out = tmp();
    const n = bakeIcons(join(STATIC, "icons"), out);
    expect(n).toBeGreaterThanOrEqual(38);
    const js = readFileSync(join(out, "icons.js"), "utf8");
    const icons = JSON.parse(js.replace(/^.*?export default /s, "").replace(/;\s*$/, "")) as Record<string, string>;
    expect(Object.keys(icons)).toHaveLength(n);
    expect(icons["arrow-left"]).toBe(readFileSync(join(STATIC, "icons", "arrow-left.svg"), "utf8").trim());
    for (const svg of Object.values(icons)) expect(svg.startsWith("<svg")).toBe(true);
  });
});

describe("precacheTree", () => {
  it("lists the css tree and only the four js subtrees, in order", () => {
    const root = tmp();
    seed(root, {
      "sw.js": "",
      "css/index.css": "",
      "css/components/a.css": "",
      "css/base.css": "",
      "js/app.js": "",
      "js/modules/m.js": "",
      "js/offline/o.js": "",
      "js/study/s.js": "",
      "js/dashboard/d.js": "",
      "js/vendor/v.js": "",
    });
    expect(precacheTree(root)).toEqual({
      css: ["base.css", "components/a.css", "index.css"],
      js: ["offline/o.js", "study/s.js", "dashboard/d.js", "modules/m.js"],
    });
  });
});

describe("bakeBuildInfo", () => {
  it("bakes the resolved token", () => {
    const out = tmp();
    expect(bakeBuildInfo("ce11d0000000", out)).toBe("ce11d0000000");
    expect(readFileSync(join(out, "buildinfo.js"), "utf8")).toContain('export const BUILD_TOKEN = "ce11d0000000";');
    expect(bakeBuildInfo("v0.44.0", out)).toBe("d8392eb81ff8");
    expect(bakeBuildInfo(undefined, out)).toMatch(/^[0-9a-f]{40}$/);
  });
});

describe("copyStatic", () => {
  it("copies static/ minus the build inputs and the rendered sw.js", () => {
    const dist = tmp();
    copyStatic(STATIC, dist);
    const files = walk(join(dist, "static"));
    expect(files).toContain("css/index.css");
    expect(files).toContain("js/vendor/htmx-2.0.4.min.js");
    expect(files).toContain("fonts/fraunces-latin.woff2");
    expect(files).toContain("pwa/icon-192.png");
    expect(files).toContain("cm-bundle.js");
    expect(files).not.toContain("sw.js");
    expect(files.some((f) => f.startsWith("mockups/"))).toBe(false);
    expect(files.some((f) => f.startsWith("cm/"))).toBe(false);
  });

  it("drops files removed from the tree on the next copy", () => {
    const dist = tmp();
    copyStatic(STATIC, dist);
    writeFileSync(join(dist, "static", "stale.css"), "");
    copyStatic(STATIC, dist);
    expect(existsSync(join(dist, "static", "stale.css"))).toBe(false);
  });
});
