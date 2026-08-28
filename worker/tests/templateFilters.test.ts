import { describe, expect, it } from "vitest";
import full from "nunjucks";
import slim from "nunjucks/browser/nunjucks-slim.js";
import {
  getDefault,
  htmlsafeJson,
  items,
  join,
  parseIso,
  floatText,
  printf,
  toInt,
  stableJson,
  roundHalfEven,
  sliceOf,
  asText,
  registerFilters,
  relativeTime,
  replaceAll,
  wakesIn,
} from "../runtime/adapters/nunjucks/filters";
import { makeIconGlobal, renderIcon } from "../runtime/adapters/nunjucks/icons";

// The parity instant (docs/PARITY-GATE.md section 0).
const NOW = new Date("2026-03-14T15:00:00Z");
const clock = { now: () => NOW };

// One template through the slim runtime, exactly as the worker renders:
// precompiled by the full library, executed by nunjucks-slim with the
// shim registered.
function render(src: string, context: Record<string, unknown> = {}, root = ""): string {
  const js = full.precompileString(src, {
    name: "t.html",
    env: new full.Environment(null, { autoescape: true }),
    wrapper: (templates: Array<{ template: string }>) => `(function() {${templates[0]!.template}})()`,
  });
  const compiled = { "t.html": new Function(`return ${js}`)() as unknown };
  const env = new slim.Environment(new slim.PrecompiledLoader(compiled), { autoescape: true });
  registerFilters(env, { clock, root });
  env.addGlobal("icon", makeIconGlobal({ check: '<svg viewBox="0 0 8 8" fill="currentColor"><path d="M0 0"/></svg>' }));
  return env.render("t.html", context);
}

describe("round: Python round(), ties to even on the exact value", () => {
  it.each([
    [0.5, 0],
    [1.5, 2],
    [2.5, 2],
    [-2.5, -2],
    [2.4, 2],
    [2.6, 3],
    [-0.4, 0],
    [28, 28],
  ])("round(%s) = %s", (x, want) => {
    expect(roundHalfEven(x)).toBe(want);
  });
  it.each([
    [2.675, 2, 2.67],
    [0.125, 2, 0.12],
    [-0.125, 2, -0.12],
    [33.333333, 2, 33.33],
    [1.005, 2, 1],
    [50, 2, 50],
  ])("round(%s, %s) = %s", (x, p, want) => {
    expect(roundHalfEven(x, p)).toBe(want);
  });
  it("is the |round filter, chaining into |int like the deck page", () => {
    expect(render("{{ (100 * 1 / 3)|round|int }}|{{ 2.5|round }}|{{ 0.5|round|int }}")).toBe("33|2|0");
  });
});

describe("floattext: a float as text", () => {
  it.each([
    [50, "50.0"],
    [0.8, "0.8"],
    [1e16, "1e+16"],
    [1e22, "1e+22"],
    [0.0001, "0.0001"],
    [0.00001, "1e-05"],
    [0.1 + 0.2, "0.30000000000000004"],
    [-0, "-0.0"],
    [1.5e16, "1.5e+16"],
    [123456789012345, "123456789012345.0"],
    [12345.678, "12345.678"],
    [NaN, "nan"],
    [Infinity, "inf"],
  ])("str(%s) = %s", (x, want) => {
    expect(floatText(x)).toBe(want);
  });
  it("keeps 50.0 as 50.0 through the mastery bar's round(2)", () => {
    expect(render("{{ (100 * 1 / 2)|round(2)|floattext }}|{{ (100 * 1 / 3)|round(2)|floattext }}")).toBe("50.0|33.33");
  });
});

describe("int: Python int() truncation with Jinja's fallbacks", () => {
  it.each([
    [3.7, 3],
    [-3.7, -3],
    ["42", 42],
    ["1.5", 1],
    [true, 1],
    [null, 0],
    ["junk", 0],
    [undefined, 0],
  ])("int(%s) = %s", (x, want) => {
    expect(toInt(x)).toBe(want);
  });
  it("is the |int filter", () => {
    expect(render("{{ (m / 60)|int }}h", { m: 90 })).toBe("1h");
  });
});

describe("string: Python str() of template values", () => {
  it("prints None, True and False as Python does and Undefined as nothing", () => {
    expect(asText(null)).toBe("None");
    expect(asText(true)).toBe("True");
    expect(asText(false)).toBe("False");
    expect(asText(undefined)).toBe("");
    expect(asText(3)).toBe("3");
  });
  it("compares equal strings the way the diff card needs", () => {
    expect(render("{% if a|string != b|string %}changed{% else %}same{% endif %}", { a: "", b: "" })).toBe("same");
    expect(render("{% if a|string != b|string %}changed{% else %}same{% endif %}", { a: "x", b: "" })).toBe("changed");
  });
});

describe("format: Python % formatting", () => {
  it.each([
    ["%.0f", 2.5, "2"],
    ["%.0f", 0.5, "0"],
    ["%.0f", 94.99999999999999, "95"],
    ["%.0f%%", 0.9 * 100, "90%"],
    ["%02d:00", 9, "09:00"],
    ["%02d:00", 22, "22:00"],
    ["%d", 3.7, "3"],
    ["%.2f", 2.675, "2.67"],
    ["%s", null, "None"],
  ])("%s %% %s = %s", (fmt, arg, want) => {
    expect(printf(fmt, arg)).toBe(want);
  });
  it("takes a tuple with width, alignment and sign flags", () => {
    expect(printf("%5.1f|%-4d|%+d|%s", [3.14159, 7, 3, null])).toBe("  3.1|7   |+3|None");
  });
  it("refuses an argument count mismatch like Python", () => {
    expect(() => printf("%d %d", 1)).toThrow(/not enough arguments/);
    expect(() => printf("%d", [1, 2])).toThrow(/not all arguments/);
  });
  it("is the |format filter at the retention and time sites", () => {
    expect(render("{{ '%.0f%%'|format(r * 100) }} {{ '%02d:00'|format(h) }}", { r: 0.95, h: 8 })).toBe("95% 08:00");
  });
});

describe("slice: Python slice semantics", () => {
  it.each([
    ["hello", 0, 2, "he"],
    ["hello", -3, undefined, "llo"],
    ["hello", 1, -1, "ell"],
    ["hello", 10, undefined, ""],
    ["héllo𝄞x", 5, 6, "𝄞"],
  ])("%s[%s:%s] = %s", (s, a, b, want) => {
    expect(sliceOf(s, a, b)).toBe(want);
  });
  it("slices arrays and passes null through", () => {
    expect(sliceOf([1, 2, 3, 4], 0, 2)).toEqual([1, 2]);
    expect(sliceOf([1, 2, 3, 4], -2)).toEqual([3, 4]);
    expect(sliceOf(null, 0, 3)).toBeNull();
  });
  it("replaces the [:n] sites, including inside a for", () => {
    expect(render("{{ t|slice(0, 3) }}{% for x in xs|slice(0, 2) %}{{ x }}{% endfor %}", { t: "abcdef", xs: [1, 2, 3] })).toBe("abc12");
    expect(render("{{ d|slice(5, 10) }}", { d: "2026-03-14T15:00:00" })).toBe("03-14");
  });
});

describe("tojson: markupsafe htmlsafe_json_dumps", () => {
  it("sorts keys, uses Python separators and escapes to ASCII", () => {
    expect(stableJson({ b: 1, a: [1, 2.5, "x"], c: { z: null, y: true } })).toBe('{"a": [1, 2.5, "x"], "b": 1, "c": {"y": true, "z": null}}');
    expect(stableJson('é — 𝄞 "q" \\ \n \x01 \x7f')).toBe('"\\u00e9 \\u2014 \\ud834\\udd1e \\"q\\" \\\\ \\n \\u0001 \\u007f"');
  });
  it("escapes <, >, & and ' as \\u003c, \\u003e, \\u0026, \\u0027", () => {
    expect(htmlsafeJson("</script><script>alert(1)</script> & 'x'")).toBe(
      '"\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e \\u0026 \\u0027x\\u0027"',
    );
    expect(htmlsafeJson({ "k<": "v>" })).toBe('{"k\\u003c": "v\\u003e"}');
  });
  it("is safe under autoescape, so a deck name cannot close the script", () => {
    const out = render("<script>const n = {{ name|tojson }};</script>", { name: "</script><script>alert(1)</script>" });
    expect(out).toBe('<script>const n = "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e";</script>');
  });
});

describe("get, items, replace, join", () => {
  it("get returns the default only when the key is absent", () => {
    expect(getDefault({ a: null }, "a", "d")).toBeNull();
    expect(getDefault({ a: 1 }, "b", "d")).toBe("d");
    expect(getDefault({ "101": "capitals" }, 101, "x")).toBe("capitals");
    expect(getDefault(null, "a", "d")).toBe("d");
    expect(getDefault(new Map([[1, "one"]]), 1, "d")).toBe("one");
  });
  it("get is a global taking a default, as the 8 sites use it", () => {
    expect(render("{{ get(m, k, 'dflt') }}", { m: { done: "Done." }, k: "gone" })).toBe("dflt");
    expect(render("{{ get(m, k, 'dflt') }}", { m: { done: "Done." }, k: "done" })).toBe("Done.");
  });
  it("items keeps insertion order", () => {
    expect(items({ b: 1, a: 2 })).toEqual([
      ["b", 1],
      ["a", 2],
    ]);
    expect(items(new Map([[2, "x"], [1, "y"]]))).toEqual([
      ["2", "x"],
      ["1", "y"],
    ]);
    expect(render("{% for k, v in items(m) %}{{ k }}={{ v }};{% endfor %}", { m: { b: 1, a: 2 } })).toBe("b=1;a=2;");
  });
  it("replace replaces every occurrence", () => {
    expect(replaceAll("a_b_c", "_", " ")).toBe("a b c");
    expect(render("{{ s|replace('_', ' ') }}", { s: "trivia_gen_x" })).toBe("trivia gen x");
    expect(render("{{ u|replace('https://', '') }}", { u: "https://x.test/keys" })).toBe("x.test/keys");
  });
  it("join joins with the separator", () => {
    expect(join(["a", "b"], ", ")).toBe("a, b");
    expect(render("{{ xs|join(' ~ ') }}", { xs: [1, 2] })).toBe("1 ~ 2");
  });
});

describe("root global", () => {
  it('is "" by default and the mount prefix otherwise', () => {
    expect(render('<a href="{{ root }}/deck/x">')).toBe('<a href="/deck/x">');
    expect(render('<a href="{{ root }}/deck/x">', {}, "/prep")).toBe('<a href="/prep/deck/x">');
  });
});

describe("none test", () => {
  it("matches null alone, as Python's None", () => {
    expect(render("{% if v is none %}N{% else %}-{% endif %}", { v: null })).toBe("N");
    expect(render("{% if v is not none %}S{% else %}-{% endif %}", { v: 0 })).toBe("S");
    expect(render("{% if v is none %}N{% else %}-{% endif %}", {})).toBe("-");
  });
});

describe("icon global", () => {
  const icons = { check: '<svg xmlns="x" viewBox="0 0 8 8" fill="currentColor"><path d="M0 0"/></svg>' };
  it("injects class and aria-hidden after the opening tag", () => {
    expect(renderIcon(icons, "check")).toBe('<svg xmlns="x" viewBox="0 0 8 8" fill="currentColor" class="icon" aria-hidden="true"><path d="M0 0"/></svg>');
    expect(renderIcon(icons, "check", "icon icon-inline")).toContain(' class="icon icon-inline" aria-hidden="true">');
  });
  it("swaps aria-hidden for role and aria-label when titled", () => {
    expect(renderIcon(icons, "check", "icon", "Done")).toBe('<svg xmlns="x" viewBox="0 0 8 8" fill="currentColor" class="icon" role="img" aria-label="Done"><path d="M0 0"/></svg>');
  });
  it("renders nothing for an unknown name", () => {
    expect(renderIcon(icons, "nope")).toBe("");
  });
  it("takes keyword arguments from a template and is not escaped", () => {
    expect(render("{{ icon('check', class_='icon icon-inline') }}")).toBe('<svg viewBox="0 0 8 8" fill="currentColor" class="icon icon-inline" aria-hidden="true"><path d="M0 0"/></svg>');
    expect(render("{{ icon('check', title='Done') }}")).toContain('role="img" aria-label="Done"');
    expect(render("[{{ icon('missing') }}]")).toBe("[]");
  });
});

describe("markdown filter", () => {
  it("renders the shared markdown module as safe HTML and nothing for empty", () => {
    expect(render("{{ t|markdown }}", { t: "Capital of **France**?" })).toBe("<p>Capital of <strong>France</strong>?</p>\n");
    expect(render("[{{ t|markdown }}]", { t: "" })).toBe("[]");
    expect(render("[{{ t|markdown }}]", { t: null })).toBe("[]");
  });
  it("escapes raw HTML inside the asText", () => {
    expect(render("{{ t|markdown }}", { t: "<b>x</b>" })).toBe("<p>&lt;b&gt;x&lt;/b&gt;</p>\n");
  });
});

describe("parseIso", () => {
  it("reads the app's timestamp shapes, naive as UTC", () => {
    expect(parseIso("2026-03-14T15:00:00+00:00")).toBe(NOW.getTime());
    expect(parseIso("2026-03-14T15:00:00Z")).toBe(NOW.getTime());
    expect(parseIso("2026-03-14T15:00:00")).toBe(NOW.getTime());
    expect(parseIso("2026-03-14 15:00:00")).toBe(NOW.getTime());
    expect(parseIso("2026-03-14T14:00:00-01:00")).toBe(NOW.getTime());
    expect(parseIso("2026-03-14")).toBe(Date.UTC(2026, 2, 14));
    expect(parseIso("2026-03-14T15:00:00.250000Z")).toBe(NOW.getTime() + 250);
  });
  it("rejects what fromisoformat rejects", () => {
    expect(parseIso("garbage")).toBeNull();
    expect(parseIso("2026-13-01")).toBeNull();
    expect(parseIso(42)).toBeNull();
    expect(parseIso(null)).toBeNull();
  });
});

describe("wakes_in: every branch of prep.app._wakes_in at the parity instant", () => {
  it.each([
    ["", ""],
    [null, ""],
    ["garbage", ""],
    ["2026-03-14T15:00:00+00:00", ""],
    ["2026-03-14T14:59:59Z", ""],
    ["2026-03-14T15:00:00.500000+00:00", ""],
    ["2026-03-14T15:00:30+00:00", "in <1 min"],
    ["2026-03-14T15:00:59Z", "in <1 min"],
    ["2026-03-14T15:01:29Z", "in 1 min"],
    ["2026-03-14T15:01:31Z", "in 2 min"],
    ["2026-03-14T15:59:40Z", "in 1 hr"],
    ["2026-03-14T16:00:00Z", "in 1 hr"],
    ["2026-03-14T16:29:59Z", "in 2 hrs"],
    ["2026-03-14T16:45:00+00:00", "in 2 hrs"],
    ["2026-03-15T02:00:00Z", "in 11 hrs"],
    ["2026-03-15T03:00:00Z", "in 12 hrs"],
    ["2026-03-15 03:00:00", "in 12 hrs"],
    ["2026-03-15T15:00:00+00:00", "tomorrow"],
    ["2026-03-16T02:59:00Z", "in 2 days"],
    ["2026-03-21T15:00:00+00:00", "in 7 days"],
    ["2026-04-13T15:00:00Z", "next month"],
    ["2026-05-14T15:00:00Z", "in 2 months"],
    ["2027-03-14T15:00:00Z", "next year"],
    ["2028-03-14T15:00:00Z", "in 2 years"],
    ["2031-03-14T15:00:00Z", "forever"],
    ["2099-01-01T00:00:00+00:00", "forever"],
  ])("wakes_in(%s) = %s", (iso, want) => {
    expect(wakesIn(iso, NOW)).toBe(want);
  });
  it("is the |wakes_in filter against the clock port", () => {
    expect(render("{{ t|wakes_in }}", { t: "2026-03-15T15:00:00+00:00" })).toBe("tomorrow");
  });
});

describe("relative_time: every branch of prep.app._relative_time at the parity instant", () => {
  it.each([
    ["", ""],
    [null, ""],
    ["garbage", "garbage"],
    ["2026-03-14T15:00:01Z", "in the future"],
    ["2026-03-14T15:00:00+00:00", "just now"],
    ["2026-03-14T14:59:30+00:00", "just now"],
    ["2026-03-14T14:59:15Z", "0 min ago"],
    ["2026-03-14T14:30:00Z", "30 min ago"],
    ["2026-03-14T14:00:01Z", "59 min ago"],
    ["2026-03-14T14:00:00.250000Z", "59 min ago"],
    ["2026-03-14T14:00:00Z", "1 hr ago"],
    ["2026-03-14 14:00:00", "1 hr ago"],
    ["2026-03-14T14:00:00-01:00", "just now"],
    ["2026-03-14T12:00:00+00:00", "3 hrs ago"],
    ["2026-03-13T15:00:01Z", "23 hrs ago"],
    ["2026-03-13T15:00:00Z", "1 day ago"],
    ["2026-03-13", "1 day ago"],
    ["2026-03-12T15:00:00Z", "2 days ago"],
    ["2026-02-13T15:00:00Z", "29 days ago"],
    ["2026-02-12T15:00:00+00:00", "1 mo ago"],
    ["2025-04-14T15:00:00Z", "11 mo ago"],
    ["2025-03-15T15:00:00Z", "0 yrs ago"],
    ["2025-03-14T15:00:00Z", "1 yr ago"],
    ["2024-12-01T09:00:00+00:00", "1 yr ago"],
    ["2024-03-14T15:00:00Z", "2 yrs ago"],
  ])("relative_time(%s) = %s", (iso, want) => {
    expect(relativeTime(iso, NOW)).toBe(want);
  });
  it("is the |relative_time filter against the clock port", () => {
    expect(render("{{ t|relative_time }}", { t: "2026-03-14T12:00:00+00:00" })).toBe("3 hrs ago");
  });
});

describe("Jinja constructs the ported templates rely on", () => {
  it("tuple membership rewritten to arrays evaluates, and `not in` too", () => {
    expect(render("{% if s in ['done', 'failed'] %}T{% endif %}{% if s not in ['done'] %}N{% endif %}", { s: "failed" })).toBe("TN");
  });
  it("a set inside a for reaches the outer variable, which the diff card counts on", () => {
    expect(render("{% set any = false %}{% for k in [1, 2] %}{% if k == 2 %}{% set any = true %}{% endif %}{% endfor %}{{ any }}")).toBe("true");
  });
  it("unpacks entry arrays in a for, which the grouped sections read", () => {
    expect(render("{% for label, xs in groups %}{{ label }}:{{ xs|length }};{% endfor %}", { groups: [["a", [1, 2]], ["b", [3]]] })).toBe("a:2;b:1;");
  });
});
