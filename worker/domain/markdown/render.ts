// The HTML renderer over the block and inline token trees. Raw HTML in a
// card is escaped, never passed through.
import { splitWhitespace, strip } from "./chars";
import type { InlineParser } from "./inline";
import type { Env, Token } from "./tokens";
import { escapeHtml, safeEntity, safeUrl, striptags } from "./url";

const INLINE_STRIP = " \r\n\t\f";

export function renderTokens(tokens: Token[], inline: InlineParser, env: Env): string {
  let out = "";
  for (const tok of tokens) out += renderToken(tok, inline, env);
  return out;
}

function children(tok: Token, inline: InlineParser, env: Env): string {
  if (tok.children) return renderTokens(tok.children, inline, env);
  if (tok.text !== undefined) return renderTokens(inline.call(strip(tok.text, INLINE_STRIP), env), inline, env);
  return "";
}

function attr(tok: Token, name: string): unknown {
  return tok.attrs ? tok.attrs[name] : undefined;
}

function renderToken(tok: Token, inline: InlineParser, env: Env): string {
  switch (tok.type) {
    case "text":
      return escapeHtml(tok.raw!);
    case "emphasis":
      return "<em>" + children(tok, inline, env) + "</em>";
    case "strong":
      return "<strong>" + children(tok, inline, env) + "</strong>";
    case "strikethrough":
      return "<del>" + children(tok, inline, env) + "</del>";
    case "link":
      return link(children(tok, inline, env), attr(tok, "url") as string, attr(tok, "title") as string | undefined);
    case "image":
      return image(children(tok, inline, env), attr(tok, "url") as string, attr(tok, "title") as string | undefined);
    case "codespan":
      return "<code>" + escapeHtml(tok.raw!) + "</code>";
    case "linebreak":
      return "<br />\n";
    case "softbreak":
      return "\n";
    case "inline_html":
      return escapeHtml(tok.raw!);
    case "paragraph":
      return "<p>" + children(tok, inline, env) + "</p>\n";
    case "heading": {
      const tag = "h" + String(attr(tok, "level"));
      return "<" + tag + ">" + children(tok, inline, env) + "</" + tag + ">\n";
    }
    case "blank_line":
      return "";
    case "thematic_break":
      return "<hr />\n";
    case "block_text":
      return children(tok, inline, env);
    case "block_code":
      return blockCode(tok.raw!, attr(tok, "info") as string | undefined);
    case "block_quote":
      return "<blockquote>\n" + children(tok, inline, env) + "</blockquote>\n";
    case "block_html":
      return "<p>" + escapeHtml(strip(tok.raw!)) + "</p>\n";
    case "list": {
      const body = children(tok, inline, env);
      if (attr(tok, "ordered")) {
        const start = attr(tok, "start");
        return "<ol" + (start !== undefined ? ' start="' + String(start) + '"' : "") + ">\n" + body + "</ol>\n";
      }
      return "<ul>\n" + body + "</ul>\n";
    }
    case "list_item":
      return "<li>" + children(tok, inline, env) + "</li>\n";
    case "table":
      return "<table>\n" + children(tok, inline, env) + "</table>\n";
    case "table_head":
      return "<thead>\n<tr>\n" + children(tok, inline, env) + "</tr>\n</thead>\n";
    case "table_body":
      return "<tbody>\n" + children(tok, inline, env) + "</tbody>\n";
    case "table_row":
      return "<tr>\n" + children(tok, inline, env) + "</tr>\n";
    case "table_cell": {
      const tag = attr(tok, "head") ? "th" : "td";
      const align = attr(tok, "align") as string | null;
      return "  <" + tag + (align ? ' style="text-align:' + align + '"' : "") + ">" + children(tok, inline, env) + "</" + tag + ">\n";
    }
    default:
      return "";
  }
}

function link(text: string, url: string, title?: string): string {
  let s = '<a href="' + safeUrl(url) + '"';
  if (title) s += ' title="' + safeEntity(title) + '"';
  return s + ">" + text + "</a>";
}

function image(text: string, url: string, title?: string): string {
  let s = '<img src="' + safeUrl(url) + '" alt="' + striptags(text) + '"';
  if (title) s += ' title="' + safeEntity(title) + '"';
  return s + " />";
}

function blockCode(code: string, info?: string): string {
  let html = "<pre><code";
  if (info !== undefined) {
    info = safeEntity(strip(info));
    if (info) html += ' class="language-' + splitWhitespace(info)[0]! + '"';
  }
  return html + ">" + escapeHtml(code) + "</code></pre>\n";
}
