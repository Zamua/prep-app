// The card-prompt markdown renderer: CommonMark with strikethrough and
// tables, raw HTML escaped rather than passed through, and no hard wrap.
// One implementation serves the worker's template filter and, bundled, the
// browser, so a card cannot render differently online and offline.
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
