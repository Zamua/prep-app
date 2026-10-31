import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
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

/** The default export of a module the build wrote. */
async function generated<T>(path: string): Promise<T> {
  return ((await import(/* @vite-ignore */ pathToFileURL(path).href)) as { default: T }).default;
}

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
  it("writes one entry per template, keyed by its relative name", async () => {
    const templates = tmp();
    const out = tmp();
    seed(templates, { "base.html": "<p>{{ x }}</p>", "partials/row.html": "{% extends 'base.html' %}", "notes.txt": "skip" });
    expect(precompileTemplates(templates, out)).toBe(2);
    expect(Object.keys(await generated<Record<string, unknown>>(join(out, "templates.js"))).sort()).toEqual(["base.html", "partials/row.html"]);
    expect(existsSync(join(out, "templates.d.ts"))).toBe(true);
  });

  it("fails naming the template that does not parse", () => {
    const templates = tmp();
    seed(templates, { "ok.html": "fine", "bad/slice.html": "{{ items[:3] }}" });
    expect(() => precompileTemplates(templates, tmp())).toThrow(/template bad\/slice\.html does not precompile/);
  });
});

describe("bakeIcons", () => {
  it("maps every static/icons svg by stem to its trimmed source", async () => {
    const out = tmp();
    const svgs = readdirSync(join(STATIC, "icons")).filter((f) => f.endsWith(".svg"));
    expect(bakeIcons(join(STATIC, "icons"), out)).toBe(svgs.length);
    const icons = await generated<Record<string, string>>(join(out, "icons.js"));
    expect(Object.keys(icons).sort()).toEqual(svgs.map((f) => f.slice(0, -".svg".length)).sort());
    expect(icons["arrow-left"]).toBe(readFileSync(join(STATIC, "icons", "arrow-left.svg"), "utf8").trim());
    for (const svg of Object.values(icons)) expect(svg.startsWith("<svg")).toBe(true);
  });
});

describe("precacheTree", () => {
  it("lists the whole css tree and the js the offline app loads, never vendor", () => {
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
    const tree = precacheTree(root);
    expect([...tree.css].sort()).toEqual(["base.css", "components/a.css", "index.css"]);
    expect([...tree.js].sort()).toEqual(["dashboard/d.js", "modules/m.js", "offline/o.js", "study/s.js"]);
  });
});

describe("bakeBuildInfo", () => {
  it("bakes the resolved token", async () => {
    const out = tmp();
    expect(bakeBuildInfo("ce11d0000000", out)).toBe("ce11d0000000");
    const baked = (await import(/* @vite-ignore */ pathToFileURL(join(out, "buildinfo.js")).href)) as { BUILD_TOKEN: string };
    expect(baked.BUILD_TOKEN).toBe("ce11d0000000");
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
