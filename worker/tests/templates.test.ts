import { describe, expect, it } from "vitest";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { BUILD, STATIC, TEMPLATES, WORKER, bakeIcons, precompileTemplates } from "../scripts/build.mjs";
import { buildEnvironment, prepareContext, rendererOver } from "../runtime/adapters/nunjucks/environment";
import { derive } from "../app/viewmodels/derive";
import { EXPORT_TOO_LARGE } from "../app/decks/importLimits";

const NOW = new Date("2026-03-14T15:00:00Z");
const clock = { now: () => NOW };
const CONTEXTS = join(WORKER, "tests", "fixtures", "template-contexts");

// The same bake the build does, into a scratch corner of build/, so the
// test exercises the precompiled map and the slim runtime the worker ships.
const scratch = join(BUILD, "vitest-templates");
mkdirSync(scratch, { recursive: true });
const compiledCount = precompileTemplates(TEMPLATES, scratch);
bakeIcons(join(STATIC, "icons"), scratch);
const templates = (await import(pathToFileURL(join(scratch, "templates.js")).href)).default as Record<string, unknown>;
const icons = (await import(pathToFileURL(join(scratch, "icons.js")).href)).default as Record<string, string>;

function makeRenderer(root = "") {
  return rendererOver(buildEnvironment({ clock, root, templates, icons }), root);
}

function walk(dir: string, suffix: string): string[] {
  const out: string[] = [];
  (function visit(d: string) {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      if (statSync(p).isDirectory()) visit(p);
      else if (name.endsWith(suffix)) out.push(relative(dir, p).split(sep).join("/"));
    }
  })(dir);
  return out.sort();
}

// Python-only syntax and the Python request object; a template that still
// reads either renders `undefined` silently under nunjucks.
const SMELLS = ["request.", "request.scope", " in (", ".items()", ".update(", ".get(", "namespace(", "selectattr", "rejectattr", "[:"];

describe("the ported templates", () => {
  it("all 49 precompile for nunjucks-slim", () => {
    const sources = walk(TEMPLATES, ".html");
    expect(sources).toHaveLength(49);
    expect(compiledCount).toBe(49);
    expect(Object.keys(templates).sort()).toEqual(sources);
  });

  it("carry no Python-only syntax", () => {
    for (const rel of walk(TEMPLATES, ".html")) {
      const src = readFileSync(join(TEMPLATES, rel), "utf8").replace(/\{#[\s\S]*?#\}/g, "");
      for (const smell of SMELLS) {
        expect(src.includes(smell), `${rel} still contains ${smell}`).toBe(false);
      }
      expect(/\b(True|False|None)\b/.test(src), `${rel} still contains a Python literal`).toBe(false);
    }
  });
});

describe("the renderer", () => {
  const renderer = makeRenderer();

  it("renders every golden context without throwing", () => {
    if (!existsSync(CONTEXTS)) return;
    const files = walk(CONTEXTS, ".json");
    expect(files.length).toBeGreaterThan(100);
    for (const rel of files) {
      const entry = JSON.parse(readFileSync(join(CONTEXTS, rel), "utf8")) as { template: string; context: Record<string, unknown> };
      const context = derive(entry.template, entry.context);
      const html = renderer.render(entry.template, context);
      expect(html.length, rel).toBeGreaterThan(0);
    }
  });

  it("hands the created-token dialog the absolute app root", () => {
    const html = makeRenderer("/prep").render(
      "settings_api.html",
      baseContext({ tokens: [], created_plaintext: "prep_pat_seed", flash: null, app_base: "https://prep.example.test" }),
    );
    expect(html).toContain('data-app-base="https://prep.example.test/prep"');
    expect(html).not.toContain("undefined");
  });

  it("merges root into the context and prefixes every app URL with it", () => {
    const html = makeRenderer("/prep").render("privacy.html", baseContext({ user: null }));
    expect(html).toContain('href="/prep/static/css/vce11d0000000/index.css"');
    expect(html).toContain('"@/": "/prep/static/js/vce11d0000000/"');
    expect(makeRenderer().render("privacy.html", baseContext({ user: null }))).toContain('href="/static/css/vce11d0000000/index.css"');
  });

  it("turns the deck_display table into the lookup the templates call", () => {
    const fn = prepareContext({ deck_display: { capitals: "World Capitals" } }, "")["deck_display"] as (s: unknown) => string;
    expect(fn("capitals")).toBe("World Capitals");
    expect(fn("unknown-slug")).toBe("unknown-slug");
    expect(fn("")).toBe("");
    expect(fn(null)).toBe("");
    const html = renderer.render("partials/plan_progress.html", {
      ...baseContext({}),
      wid: "w1",
      deck_name: "capitals",
      progress: { status: "planning", plan: null, total: null, round: 1 },
    });
    expect(html).toContain("World Capitals · plan");
  });

  it("escapes a deck name that tries to close the script", () => {
    const xss = "</script><script>alert(1)</script>";
    const html = renderer.render("sign_out_interstitial.html", { ...baseContext({ user: null }), redirect_url: xss });
    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain('const home = "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e";');
  });

  it("renders icons inline with their class", () => {
    const html = renderer.render("error.html", { ...baseContext({ user: null }), status_code: 404, headline: "Not found.", blurb: "", path: "/x" });
    expect(html).toContain("<svg");
    expect(html).toMatch(/class="icon icon-inline" aria-hidden="true"/);
  });

  // The one block the reference template does not have, so no golden
  // compares it: the export hub's 413.
  it("shows the export refusal on the hub, with the other formats still offered", () => {
    const context = { ...baseContext({}), deck_name: "capitals", deck_type: "srs", error: EXPORT_TOO_LARGE };
    const html = renderer.render("deck_export.html", context);
    expect(html).toContain(`<p class="form-error">${EXPORT_TOO_LARGE}</p>`);
    expect(html).toContain('data-export-url="/deck/capitals/export.csv"');
    expect(renderer.render("deck_export.html", { ...context, error: null })).not.toContain("form-error");
  });

  it("reads the derived groupings in the reorganize plan", () => {
    const context = derive("partials/transform_progress.html", {
      ...baseContext({}),
      wid: "t1",
      scope: "reorganize",
      target_id: 2,
      deck_name: "",
      progress: {
        status: "awaiting_apply",
        plan: {
          notes: "",
          modifications: [{ question_id: 41 }],
          additions: [{ type: "short", prompt: "Q?", dest_deck: "distsys" }],
          deletions: [101],
          new_decks: [],
          deck_renames: [],
          card_moves: [{ question_id: 201, dest_deck: "consensus" }],
          deck_deletions: [3, 99],
        },
      },
      desc: {},
      modification_diffs: [{ question_id: 41, deck_name: "capitals", old: { prompt: "a" }, new: { prompt: "b" } }],
      deletion_decks: { "101": "capitals" },
      move_source_decks: { "201": "distsys" },
      deck_id_to_name: { "3": "history-trivia" },
    });
    const html = renderer.render("partials/transform_progress.html", context);
    expect(html).toContain('<span class="transform-bucket-deck">capitals</span>');
    expect(html).toContain('<span class="transform-bucket-deck">distsys</span>');
    expect(html).toContain('<span class="transform-bucket-deck">consensus</span>');
    expect(html).toContain("history-trivia");
    expect(html).toContain("deck № 99");
  });
});

function baseContext(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    user: { tailscale_login: "seed@example.com", display_name: "Seed", is_anonymous: 0, editor_input_mode: "vim" },
    agent_available: true,
    static_css_mtime: "ce11d0000000",
    auth_provider: "tailscale",
    sign_in_url: "",
    sign_up_url: "",
    sign_out_url: "",
    clerk_publishable_key: null,
    clerk_frontend_api_host: null,
    notif_unseen_count: 0,
    deck_display: { capitals: "World Capitals" },
    app_base: "https://prep.example.test",
    ...overrides,
  };
}
