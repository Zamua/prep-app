import { existsSync, readFileSync } from "node:fs";
import { dirname, join, normalize } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { BUILD, STATIC, bakeBuildInfo, bakeServiceWorker } from "../scripts/build.mjs";

const TOKEN = "ce11d0000000";

type Sw = typeof import("../runtime/sw");
type Tree = { css: string[]; js: string[] };

// runtime/sw.ts imports the baked modules, so they are baked from the real
// static tree first and the module loaded after.
let sw: Sw;
let tree: Tree;
beforeAll(async () => {
  tree = bakeServiceWorker(STATIC, BUILD);
  bakeBuildInfo(TOKEN, BUILD);
  sw = await import("../runtime/sw");
});

/** Every module `entry` reaches through a relative or `@/` import, static or
 * dynamic, as paths under static/js. */
function moduleClosure(entry: string): Set<string> {
  const js = join(STATIC, "js");
  const seen = new Set<string>();
  const queue = [entry];
  const specifier = /(?:\bfrom\s*|\bimport\s*\(?\s*)["']((?:\.{1,2}\/|@\/)[^"']+)["']/g;
  while (queue.length) {
    const rel = queue.shift()!;
    if (seen.has(rel)) continue;
    seen.add(rel);
    for (const [, spec] of readFileSync(join(js, rel), "utf8").matchAll(specifier)) {
      queue.push(spec!.startsWith("@/") ? spec!.slice(2) : normalize(join(dirname(rel), spec!)));
    }
  }
  return seen;
}

describe("precacheUrls", () => {
  it("opens with the offline shell and holds its stylesheet, its module and both icons", () => {
    const urls = sw.precacheUrls(tree, TOKEN);
    expect(urls[0]).toBe(`/offline?build=${TOKEN}`);
    expect(urls).toEqual(
      expect.arrayContaining([
        `/static/css/v${TOKEN}/index.css`,
        `/static/js/v${TOKEN}/offline/offline-app.js`,
        ...sw.MANIFEST_ICONS.map((icon) => `/static/pwa/${icon}`),
      ]),
    );
  });

  it("names only files the static tree holds", () => {
    for (const url of sw.precacheUrls(tree, TOKEN).slice(1)) {
      const rel = url.replace(/^\/static\//, "").replace(`/v${TOKEN}/`, "/");
      expect(existsSync(join(STATIC, rel)), url).toBe(true);
    }
  });

  // A module the offline app imports but the worker never precached fails
  // the cold offline launch while every online path stays green.
  it("holds every module the offline app reaches", () => {
    const listed = new Set(sw.precacheUrls(tree, TOKEN));
    const reached = moduleClosure("offline/offline-app.js");
    expect(reached.size).toBeGreaterThan(1);
    for (const rel of reached) expect(listed.has(`/static/js/v${TOKEN}/${rel}`), rel).toBe(true);
  });

  it("prefixes every URL with the root", () => {
    const urls = sw.precacheUrls({ css: ["index.css"], js: ["offline/offline-app.js"] }, TOKEN, "/prep");
    expect(urls).toEqual([
      "/prep/offline?build=ce11d0000000",
      "/prep/static/css/vce11d0000000/index.css",
      "/prep/static/js/vce11d0000000/offline/offline-app.js",
      "/prep/static/pwa/icon-192.png",
      "/prep/static/pwa/icon-512.png",
    ]);
  });
});

describe("serviceWorkerScript", () => {
  it("substitutes both placeholders everywhere and keeps $ sequences literal", () => {
    const out = sw.serviceWorkerScript('a __BUILD__ b __BUILD__ c __PRECACHE__', "abc1234", ["/x?$&"]);
    expect(out).toBe('a abc1234 b abc1234 c ["/x?$&"]');
  });

  it("renders the real source with the token and a parseable manifest", () => {
    const urls = sw.precacheUrls(tree, TOKEN);
    const body = sw.serviceWorkerScript(readFileSync(`${STATIC}/sw.js`, "utf8"), TOKEN, urls);
    expect(body).toContain(`const BUILD = "${TOKEN}";`);
    expect(body).not.toContain("__BUILD__");
    expect(body).not.toContain("__PRECACHE__");
    const m = /const PRECACHE = (\[.*?\]);/s.exec(body);
    expect(JSON.parse(m![1]!)).toEqual(urls);
  });
});

describe("manifestDocument", () => {
  // The whole document, because the browser keys the installed app on the
  // scope and paints its splash from the two colours.
  it("is the app's name, scope, colours and both icons at the root", () => {
    expect(sw.manifestDocument("")).toEqual({
      name: "prep \u00b7 a commonplace book",
      short_name: "prep",
      description: "Spaced-repetition flashcards. Learn anything.",
      display: "standalone",
      scope: "/",
      start_url: "/",
      background_color: "#f4ecdc",
      theme_color: "#f5efe6",
      icons: [
        { src: "/static/pwa/icon-192.png", sizes: "192x192", type: "image/png" },
        { src: "/static/pwa/icon-512.png", sizes: "512x512", type: "image/png" },
      ],
    });
  });

  it("labels a staging root and carries it into every URL", () => {
    const doc = sw.manifestDocument("/prep-staging") as Record<string, unknown> & { icons: { src: string }[] };
    expect(doc.name).toBe("prep · a commonplace book (staging)");
    expect(doc.short_name).toBe("prep (staging)");
    expect(doc.scope).toBe("/prep-staging/");
    expect(doc.start_url).toBe("/prep-staging/");
    expect(doc.icons.map((i) => i.src)).toEqual([
      "/prep-staging/static/pwa/icon-192.png",
      "/prep-staging/static/pwa/icon-512.png",
    ]);
  });
});

describe("offlineBuild", () => {
  const url = (build?: string) => new URL(`https://prep.test/offline${build === undefined ? "" : `?build=${encodeURIComponent(build)}`}`);

  it("echoes an accepted token", () => {
    expect(sw.offlineBuild(url("abcdef1234567"), TOKEN)).toBe("abcdef1234567");
    expect(sw.offlineBuild(url("1718000000000"), TOKEN)).toBe("1718000000000");
  });

  it("never reflects anything else", () => {
    for (const bad of ["../../etc/passwd", "<script>alert(1)</script>", "ABCDEF1234567", "abc123", "a".repeat(41), "deadbeef.js", "endor", "١٢٣٤٥٦٧", ""]) {
      expect(sw.offlineBuild(url(bad), TOKEN), JSON.stringify(bad)).toBe(TOKEN);
    }
    expect(sw.offlineBuild(url(), TOKEN)).toBe(TOKEN);
  });
});

describe("servePwa", () => {
  const at = (path: string) => new URL(`https://prep.test${path}`);

  it("serves /sw.js as no-cache javascript carrying the token", async () => {
    const res = sw.servePwa(at("/sw.js"), TOKEN)!;
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/javascript");
    expect(res.headers.get("cache-control")).toBe("no-cache");
    const body = await res.text();
    expect(body).toContain(`const BUILD = "${TOKEN}";`);
    expect(body).toContain(`/static/js/v${TOKEN}/offline/offline-app.js`);
    expect(body).not.toContain("__PRECACHE__");
  });

  it("serves the same bytes for repeated fetches and distinct bytes per token", async () => {
    const a = await sw.servePwa(at("/sw.js"), TOKEN)!.text();
    const b = await sw.servePwa(at("/sw.js"), TOKEN)!.text();
    const c = await sw.servePwa(at("/sw.js"), "abcdef1234567")!.text();
    expect(a).toBe(b);
    expect(c).not.toBe(a);
    expect(c).toContain('const BUILD = "abcdef1234567";');
  });

  it("serves /manifest.json", async () => {
    const res = sw.servePwa(at("/manifest.json"), TOKEN)!;
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(await res.json()).toEqual(sw.manifestDocument(""));
  });

  it("answers null elsewhere", () => {
    expect(sw.servePwa(at("/"), TOKEN)).toBeNull();
    expect(sw.servePwa(at("/offline"), TOKEN)).toBeNull();
    expect(sw.servePwa(at("/sw.js/x"), TOKEN)).toBeNull();
  });
});
