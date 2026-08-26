// The card-prompt markdown renderer: a port of mistune 3.3.4 as the app
// configures it (escape=True, hard_wrap=False, strikethrough and table
// plugins, HTMLRenderer). One implementation serves the worker's
// template filter and, bundled, the browser.
import { BlockParser, BlockState } from "./block";
import { InlineParser } from "./inline";
import { renderTokens } from "./render";
import { escapeHtml } from "./url";

const block = new BlockParser();
const inline = new InlineParser();

function render(text: string): string {
  let s = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!s.endsWith("\n")) s += "\n";
  const state = new BlockState();
  state.process(s);
  block.parse(state);
  return renderTokens(state.tokens, inline, state.env);
}

/** Markdown to HTML; "" for empty input. Never throws: an input the
 * parser cannot finish degrades to its escaped text. */
export function markdownHTML(text: string | null | undefined): string {
  if (!text) return "";
  try {
    return render(String(text));
  } catch {
    return "<p>" + escapeHtml(String(text)) + "</p>\n";
  }
}
