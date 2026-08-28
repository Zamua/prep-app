import { describe, expect, it } from "vitest";
import { pyStrip, pyre } from "../../domain/markdown/chars";

describe("the renderer's character helpers", () => {
  it("strips the block-separator whitespace class, and leaves the BOM", () => {
    expect(pyStrip("\x1c a 　")).toBe("a");
    expect(pyStrip("﻿a﻿")).toBe("﻿a﻿");
    expect(pyStrip("xxaxx", "x")).toBe("a");
  });

  it("pyre translates ^ $ . and \\s under multiline", () => {
    const re = pyre("^ {0,3}>(?P<q>.*?)$", "y", true);
    re.lastIndex = 2;
    expect(re.exec("a\n> b c\nd")?.groups?.q).toBe(" b c");
    expect(pyre("\\s+", "").test("\x1f")).toBe(true);
    expect(pyre("[^\\s*]", "").test("\x1f")).toBe(false);
  });
});
