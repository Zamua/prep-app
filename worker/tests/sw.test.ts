import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it } from "vitest";
import { BUILD, STATIC, bakeBuildInfo, bakeServiceWorker } from "../scripts/build.mjs";
import { pythonJson } from "./pyoracle";

const TOKEN = "ce11d0000000";
const FIXTURE = new URL("./fixtures/precache-ce11d0000000.json", import.meta.url).pathname;

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

describe("precacheUrls", () => {
  it("matches the checked-in copy of the Python manifest for this tree", () => {
    const expected = JSON.parse(readFileSync(FIXTURE, "utf8")) as string[];
    expect(sw.precacheUrls(tree, TOKEN)).toEqual(expected);
  });

  it("matches prep.web.pwa._precache_urls computed live for the same tree", () => {
    const expected = pythonJson<string[]>(
      "import json; from prep.web.pwa import _precache_urls; print(json.dumps(_precache_urls('ce11d0000000', '')))",
    );
    expect(sw.precacheUrls(tree, TOKEN)).toEqual(expected);
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
  it("is the Python manifest with root ''", () => {
    const expected = pythonJson<Record<string, unknown>>(
      "import json; from prep.web.pwa import manifest; print(json.dumps(json.loads(manifest().body)))",
    );
    expect(sw.manifestDocument("")).toEqual(expected);
  });

  it("labels a staging root", () => {
    const doc = sw.manifestDocument("/prep-staging");
    expect(doc.name).toBe("prep · a commonplace book (staging)");
    expect(doc.short_name).toBe("prep (staging)");
    expect(doc.scope).toBe("/prep-staging/");
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
