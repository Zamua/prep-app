// The nunjucks environment over a precompiled template map, with the shim
// and the icon global registered. `index.ts` binds it to the baked maps;
// tests bind it to maps compiled on the spot.
import nunjucks, { type Environment } from "nunjucks/browser/nunjucks-slim.js";
import type { Clock, Renderer } from "../../../app/ports";
import { makeIconGlobal } from "./icons";
import { registerShims } from "./shims";

export interface EnvironmentOptions {
  clock: Clock;
  root: string;
  templates: Record<string, unknown>;
  icons: Record<string, string>;
}

export function buildEnvironment({ clock, root, templates, icons }: EnvironmentOptions): Environment {
  const env = new nunjucks.Environment(new nunjucks.PrecompiledLoader(templates), { autoescape: true });
  registerShims(env, { clock, root });
  env.addGlobal("icon", makeIconGlobal(icons));
  return env;
}

// A context arrives as data: `deck_display` is the `{slug: display}` map
// the templates call as a function, and `root` is the mount prefix.
export function prepareContext(context: Record<string, unknown>, root: string): Record<string, unknown> {
  const out: Record<string, unknown> = { ...context, root };
  const display = context["deck_display"];
  if (display !== null && typeof display === "object") {
    const table = display as Record<string, string>;
    out["deck_display"] = (slug: unknown): string => {
      if (!slug) return "";
      const key = String(slug);
      return Object.prototype.hasOwnProperty.call(table, key) ? table[key]! : key;
    };
  }
  return out;
}

export function rendererOver(env: Environment, root: string): Renderer {
  return {
    render(template: string, context: Record<string, unknown>): string {
      return env.render(template, prepareContext(context, root));
    },
  };
}
