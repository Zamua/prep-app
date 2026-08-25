// The Renderer port over nunjucks-slim and the maps `npm run build` bakes.
import type { Clock, Renderer } from "../../../app/ports";
import icons from "../../../build/icons.js";
import templates from "../../../build/templates.js";
import { buildEnvironment, rendererOver } from "./environment";

export interface RendererOptions {
  clock: Clock;
  root?: string;
}

// One environment per renderer; compose.ts memoizes the renderer per
// isolate, so every cell shares the compiled templates.
export function createRenderer({ clock, root = "" }: RendererOptions): Renderer {
  return rendererOver(buildEnvironment({ clock, root, templates, icons }), root);
}
