// The token shapes the parsers hand the renderer.

export interface Token {
  type: string;
  raw?: string;
  text?: string;
  children?: Token[];
  attrs?: Record<string, unknown>;
  style?: string;
  marker?: string;
  tight?: boolean;
  bullet?: string;
  ref?: string;
  label?: string;
  /** false on text the emphasis pass must leave alone (escapes). */
  emphasis?: boolean;
}

export interface RefLink {
  url: string;
  label: string;
  title?: string;
}

export interface Env {
  refLinks: Map<string, RefLink>;
  blankLineStarts?: { src: string; starts: number[] };
}

export function newEnv(): Env {
  return { refLinks: new Map() };
}
