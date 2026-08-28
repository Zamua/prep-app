// The URL policy one helper at a time. The rendered-HTML corpus covers what
// they compose to; these are the boundaries a payload aims at, where a change
// shows up as an escape that silently stops escaping rather than as a wrong
// page.
import { describe, expect, it } from 'vitest';
import { escapeHtml, escapeUrl, safeEntity, safeUrl, stripTags, unescape, unquote } from '../../domain/markdown/url';

const table = (fn: (s: string) => string, rows: readonly [string, string][]) => {
  for (const [given, want] of rows) expect(fn(given), JSON.stringify(given)).toBe(want);
};

describe('escapeHtml', () => {
  it('escapes the four that can close a tag or an attribute, and nothing else', () => {
    table((s) => escapeHtml(s), [
      ['<&">', '&lt;&amp;&quot;&gt;'],
      ["'", "'"],
      ['plain', 'plain'],
    ]);
    expect(escapeHtml('"', false)).toBe('"');
  });
});

describe('unescape', () => {
  it('resolves a reference only when it ends in a semicolon', () => {
    table(unescape, [
      ['&amp;', '&'],
      ['&amp', '&amp'],
      ['a &lt; b', 'a < b'],
      ['&#x6a;', 'j'],
      ['&#13;', '\r'],
      ['no ampersand', 'no ampersand'],
    ]);
  });

  it('takes the longest known prefix of an unterminated name', () => {
    table(unescape, [
      ['&notit;', '¬it;'],
      ['&nots;', '¬s;'],
    ]);
  });

  it('replaces a code point no document can hold', () => {
    table(unescape, [
      // Past the Unicode range, and a lone surrogate half.
      ['&#x110000;', '�'],
      ['&#xD800;', '�'],
      ['&#0;', '�'],
      // A control character resolves to nothing rather than into the output.
      ['&#8;', ''],
    ]);
  });
});

describe('safeEntity', () => {
  it('unescapes once and re-escapes, so an encoded tag cannot survive as markup', () => {
    table(safeEntity, [
      ['<b>', '&lt;b&gt;'],
      ['&#x3c;script&#x3e;', '&lt;script&gt;'],
      ['&amp;lt;', '&amp;lt;'],
    ]);
  });
});

describe('escapeUrl', () => {
  it('percent-encodes everything a URL may not carry, over UTF-8 bytes', () => {
    table(escapeUrl, [
      ['a b', 'a%20b'],
      ['ä', '%C3%A4'],
      ['a"b', 'a%22b'],
      ["a'b", 'a%27b'],
      ['a<b>c', 'a%3Cb%3Ec'],
      // Already-encoded input is left alone: `%` is a URL character.
      ['a%20b', 'a%20b'],
      // The entity is resolved first, so the link is encoded once, not twice.
      ['&amp;x', '&x'],
    ]);
  });
});

describe('unquote', () => {
  it('decodes complete pairs and leaves the rest as text', () => {
    table(unquote, [
      ['%6a%61vascript:', 'javascript:'],
      ['%2F', '/'],
      ['%c3%a4', 'ä'],
      ['%zz', '%zz'],
      ['a%2', 'a%2'],
      ['100%', '100%'],
      ['nothing to decode', 'nothing to decode'],
    ]);
  });

  it('replaces a byte sequence that is not text rather than failing the link', () => {
    expect(unquote('%ff')).toBe('�');
  });

  it('decodes one layer, so a double-encoded payload still reads as encoded', () => {
    expect(unquote('%25366a%61vascript:')).toBe('%366aavascript:');
  });
});

describe('safeUrl', () => {
  it('passes the protocols a link may use, in any case', () => {
    table(safeUrl, [
      ['http://x/', 'http://x/'],
      ['HTTPS://X/', 'HTTPS://X/'],
      ['mailto:a@b.c', 'mailto:a@b.c'],
      ['tel:+1', 'tel:+1'],
      ['data:image/png;base64,AAA', 'data:image/png;base64,AAA'],
    ]);
  });

  it('passes a link with no protocol at all', () => {
    table(safeUrl, [
      ['/rel', '/rel'],
      ['#frag', '#frag'],
      ['?q=1', '?q=1'],
      ['rel/path', 'rel/path'],
      // A colon after the first slash is part of a path, not a scheme.
      ['a/b:c', 'a/b:c'],
    ]);
  });

  it('sinks a scripting scheme however it is spelled, through three decodes', () => {
    for (const url of [
      'javascript:alert(1)',
      ' \t javascript:alert(1)',
      '%6a%61vascript:alert(1)',
      '%256a%61vascript:alert(1)',
      '%25256a%61vascript:alert(1)',
      'vbscript:x',
      'data:text/html,x',
      'weird:thing',
    ]) {
      expect(safeUrl(url), url).toBe('#harmful-link');
    }
  });

  it('escapes the href it does pass, so a quote cannot open an attribute', () => {
    expect(safeUrl('"onmouseover=x')).toBe('&quot;onmouseover=x');
  });
});

describe('stripTags', () => {
  it('keeps an image’s alt text and drops every other tag and comment', () => {
    table(stripTags, [
      ['<b>x</b>', 'x'],
      ['<img src="a" alt="pic">', 'pic'],
      ["<img src='a' alt='pic'>", 'pic'],
      ['<!-- c -->x', 'x'],
      ['a<br>b', 'ab'],
      ['plain', 'plain'],
    ]);
  });
});
