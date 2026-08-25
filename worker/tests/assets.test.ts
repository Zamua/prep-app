import { describe, expect, it } from "vitest";
import { serveStatic } from "../runtime/assets";

const TOKEN = "ce11d0000000";

// A stand-in for the assets binding: a path -> body map, answering 404
// elsewhere and echoing the request headers it was handed.
function fakeAssets(files: Record<string, string>) {
  const seen: Request[] = [];
  const fetcher = {
    async fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
      const req = input instanceof Request ? input : new Request(input, init);
      seen.push(req);
      const path = new URL(req.url).pathname;
      const body = files[path];
      if (body === undefined) return new Response("not found", { status: 404 });
      const type = path.endsWith(".css") ? "text/css" : path.endsWith(".js") ? "text/javascript" : "font/woff2";
      return new Response(req.method === "HEAD" ? null : body, {
        status: 200,
        headers: { "content-type": type, etag: `"${path.length}"` },
      });
    },
  } as unknown as Fetcher;
  return { env: { ASSETS: fetcher }, seen };
}

const TREE = {
  "/static/css/index.css": "body{}",
  "/static/css/components/buttons.css": ".btn{}",
  "/static/js/app.js": "app",
  "/static/js/offline/offline-app.js": "offline",
  "/static/js/vendor/htmx-2.0.4.min.js": "htmx",
  "/static/fonts/fraunces-latin.woff2": "woff",
};

function get(path: string, init?: RequestInit) {
  return new Request(`https://prep.test${path}`, init);
}

describe("serveStatic", () => {
  it("answers null outside /static/", async () => {
    const { env, seen } = fakeAssets(TREE);
    expect(await serveStatic(get("/"), env, TOKEN)).toBeNull();
    expect(await serveStatic(get("/sw.js"), env, TOKEN)).toBeNull();
    expect(await serveStatic(get("/statics/x"), env, TOKEN)).toBeNull();
    expect(seen).toHaveLength(0);
  });

  it("serves an unversioned asset with no-cache", async () => {
    const { env, seen } = fakeAssets(TREE);
    const res = await serveStatic(get("/static/css/index.css"), env, TOKEN);
    expect(res?.status).toBe(200);
    expect(await res?.text()).toBe("body{}");
    expect(res?.headers.get("cache-control")).toBe("no-cache");
    expect(res?.headers.get("content-type")).toBe("text/css");
    expect(new URL(seen[0]!.url).pathname).toBe("/static/css/index.css");
  });

  it("aliases any accepted token onto the current tree with immutable caching", async () => {
    const { env, seen } = fakeAssets(TREE);
    const bodies: string[] = [];
    for (const tok of [TOKEN, "abcdef1234567", "1718000000000", "a".repeat(40)]) {
      const css = await serveStatic(get(`/static/css/v${tok}/components/buttons.css`), env, TOKEN);
      expect(css?.status, tok).toBe(200);
      expect(css?.headers.get("cache-control")).toBe("public, max-age=31536000, immutable");
      bodies.push(await css!.text());
      const js = await serveStatic(get(`/static/js/v${tok}/offline/offline-app.js`), env, TOKEN);
      expect(js?.status, tok).toBe(200);
      expect(js?.headers.get("cache-control")).toBe("public, max-age=31536000, immutable");
      expect(await js!.text()).toBe("offline");
    }
    // The same bytes under every token.
    expect(new Set(bodies)).toEqual(new Set([".btn{}"]));
    // The binding only ever saw the stripped path.
    for (const req of seen) expect(new URL(req.url).pathname).not.toMatch(/\/v[0-9a-f]{7,}\//);
  });

  it("treats a non-token segment as a literal sub-path", async () => {
    const { env } = fakeAssets(TREE);
    const vendor = await serveStatic(get("/static/js/vendor/htmx-2.0.4.min.js"), env, TOKEN);
    expect(vendor?.status).toBe(200);
    expect(await vendor?.text()).toBe("htmx");
    expect(vendor?.headers.get("cache-control")).toBe("no-cache");
    expect(await serveStatic(get("/static/css/vNOTATOKEN/index.css"), env, TOKEN)).toBeNull();
  });

  it("answers null for a missing asset so the router's 404 page renders", async () => {
    const { env } = fakeAssets(TREE);
    expect(await serveStatic(get("/static/css/nope.css"), env, TOKEN)).toBeNull();
    expect(await serveStatic(get(`/static/js/v${TOKEN}/nope.js`), env, TOKEN)).toBeNull();
  });

  it("never lets the alias rule escape the asset kind", async () => {
    const { env, seen } = fakeAssets(TREE);
    // A token under js resolves under js, never under another prefix.
    await serveStatic(get(`/static/js/v${TOKEN}/../css/index.css`), env, TOKEN);
    for (const req of seen) expect(new URL(req.url).pathname.startsWith("/static/")).toBe(true);
  });

  it("serves fonts from /static/fonts", async () => {
    const { env } = fakeAssets(TREE);
    const res = await serveStatic(get("/static/fonts/fraunces-latin.woff2"), env, TOKEN);
    expect(res?.status).toBe(200);
    expect(res?.headers.get("content-type")).toBe("font/woff2");
  });

  it("forwards conditional headers and the method so revalidation works", async () => {
    const { env, seen } = fakeAssets(TREE);
    const res = await serveStatic(
      get("/static/js/app.js", { method: "HEAD", headers: { "if-none-match": '"x"' } }),
      env,
      TOKEN,
    );
    expect(res?.status).toBe(200);
    expect(seen[0]!.method).toBe("HEAD");
    expect(seen[0]!.headers.get("if-none-match")).toBe('"x"');
    expect(await serveStatic(get("/static/js/app.js", { method: "POST" }), env, TOKEN)).toBeNull();
  });
});
