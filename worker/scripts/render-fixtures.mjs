// The DOM gate's renderer: every golden context under
// tests/fixtures/parity/html/contexts/ through derive and the worker's
// renderer under the parity clock, written to artifacts/parity/ts-html/.
// Bundled for node by scripts/build.mjs as build/render-fixtures.cjs.
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { derive } from "../app/viewmodels/derive.ts";
import { createRenderer } from "../runtime/adapters/nunjucks/index.ts";

const PARITY_NOW = "2026-03-14T15:00:00Z";

// What the Python golden renderer injects as the request (tests/parity/
// oracles/contexts.py `fake_request`); only settings_api reads it.
const FAKE_REQUEST = { url: { scheme: "https", netloc: "parity.example.test", path: "/" } };

function walk(dir) {
  const out = [];
  (function visit(d) {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      if (statSync(p).isDirectory()) visit(p);
      else if (name.endsWith(".json")) out.push(relative(dir, p).split(sep).join("/"));
    }
  })(dir);
  return out.sort();
}

export function renderAll(contextsDir, outDir) {
  const at = new Date(process.env.PREP_FAKE_NOW || PARITY_NOW);
  const renderer = createRenderer({ clock: { now: () => at }, root: "" });
  let n = 0;
  for (const rel of walk(contextsDir)) {
    const entry = JSON.parse(readFileSync(join(contextsDir, rel), "utf8"));
    const context = derive(entry.template, { ...entry.context, request: FAKE_REQUEST });
    const html = renderer.render(entry.template, context);
    const target = join(outDir, rel.replace(/\.json$/, ".html"));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, html);
    n++;
  }
  return n;
}

const repo = join(process.cwd());
const contextsDir = process.argv[2] || join(repo, "tests", "fixtures", "parity", "html", "contexts");
const outDir = process.argv[3] || join(repo, "artifacts", "parity", "ts-html");
console.log(`rendered ${renderAll(contextsDir, outDir)} templates -> ${outDir}`);
