// The URL policy: HTML escaping, the entity unescape, percent
// quote/unquote and the safe-protocol check.
import { HTML5_ENTITIES, INVALID_CHARREFS, INVALID_CODEPOINTS } from "./entities";
import { trimStart } from "./chars";

export function escapeHtml(s: string, quote = true): string {
  s = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return quote ? s.replace(/"/g, "&quot;") : s;
}

const CHARREF = /&(#[0-9]{1,7};|#[xX][0-9a-fA-F]+;|[^\t\n\f <&#;]{1,32};)/gu;
export const CHARREF_PREFIX = /^(#[0-9]{1,7};|#[xX][0-9a-fA-F]+;|[^\t\n\f <&#;]{1,32};)/u;

function replaceCharref(s: string): string {
  if (s[0] === "#") {
    const hex = s[1] === "x" || s[1] === "X";
    const num = parseInt(s.slice(hex ? 2 : 1, -1), hex ? 16 : 10);
    const invalid = INVALID_CHARREFS[String(num)];
    if (invalid !== undefined) return invalid;
    if ((num >= 0xd800 && num <= 0xdfff) || num > 0x10ffff) return "�";
    if (INVALID_CODEPOINTS.has(num)) return "";
    return String.fromCodePoint(num);
  }
  const exact = HTML5_ENTITIES[s];
  if (exact !== undefined) return exact;
  for (let x = s.length - 1; x > 1; x--) {
    const hit = HTML5_ENTITIES[s.slice(0, x)];
    if (hit !== undefined) return hit + s.slice(x);
  }
  return "&" + s;
}

/** Character references, resolved only when they end in a semicolon. */
export function unescape(s: string): string {
  if (!s.includes("&")) return s;
  return s.replace(CHARREF, (_m, ref: string) => replaceCharref(ref));
}

export function safeEntity(s: string): string {
  return escapeHtml(unescape(s));
}

const QUOTE_SAFE = new Set(
  [..."ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~", ...":/?#@!$&()*+,;=%"].map((c) =>
    c.charCodeAt(0),
  ),
);
const HEX = "0123456789ABCDEF";
const encoder = new TextEncoder();

/** Percent-encoding over the unescaped link, leaving the characters a URL
 * is allowed to carry. */
export function escapeUrl(link: string): string {
  let out = "";
  for (const b of encoder.encode(unescape(link))) {
    out += QUOTE_SAFE.has(b) ? String.fromCharCode(b) : "%" + HEX[b >> 4] + HEX[b & 15];
  }
  return out;
}

const decoder = new TextDecoder();
const ASCII_RUN = /[\x00-\x7f]+/gu;
const HEX_PAIR = /^[0-9a-fA-F]{2}$/;

/** Percent-decoding per ASCII run; an undecodable byte becomes U+FFFD
 * rather than failing the whole link. */
export function unquote(s: string): string {
  if (!s.includes("%")) return s;
  return s.replace(ASCII_RUN, (run) => {
    const bytes: number[] = [];
    let i = 0;
    while (i < run.length) {
      if (run[i] === "%" && HEX_PAIR.test(run.slice(i + 1, i + 3))) {
        bytes.push(parseInt(run.slice(i + 1, i + 3), 16));
        i += 3;
      } else {
        bytes.push(run.charCodeAt(i));
        i += 1;
      }
    }
    return decoder.decode(Uint8Array.from(bytes));
  });
}

const SAFE_PROTOCOLS = ["http:", "https:", "mailto:", "tel:", "ftp:", "ftps:", "irc:", "ircs:"];
const GOOD_DATA_PROTOCOLS = ["data:image/gif;", "data:image/png;", "data:image/jpeg;", "data:image/webp;"];

function unquoteUrl(url: string): string {
  for (let i = 0; i < 3; i++) {
    const decoded = unquote(url);
    if (decoded === url) break;
    url = decoded;
  }
  return url;
}

/** The escaped href, or the harmful-link sink. Three decode passes, so a
 * scheme hidden under layers of percent-encoding is still seen. */
export function safeUrl(url: string): string {
  const probe = trimStart(unquoteUrl(url).toLowerCase());
  const head = probe.split("/", 1)[0]!;
  const safe =
    SAFE_PROTOCOLS.some((p) => probe.startsWith(p)) ||
    GOOD_DATA_PROTOCOLS.some((p) => probe.startsWith(p)) ||
    probe.startsWith("/") ||
    probe.startsWith("#") ||
    probe.startsWith("?") ||
    !head.includes(":");
  return safe ? escapeHtml(url) : "#harmful-link";
}

const STRIP_IMAGE = /<img\b[^>]*\balt=("([^"]*)"|'([^']*)')[^>]*>/gu;
const STRIP_TAGS = /(<!--[^\n]*?-->|<[^>]*>)/gu;

/** Image alt text out of rendered children. */
export function stripTags(s: string): string {
  s = s.replace(STRIP_IMAGE, (_m, _q, d?: string, sq?: string) => d || sq || "");
  return s.replace(STRIP_TAGS, "");
}
