import { describe, expect, it } from "vitest";
import { strip, unicodeRe } from "../../domain/markdown/chars";

describe("the renderer's character helpers", () => {
  it("strips the block-separator whitespace class, and leaves the BOM", () => {
    expect(strip("\x1c a 　")).toBe("a");
    expect(strip("﻿a﻿")).toBe("﻿a﻿");
    expect(strip("xxaxx", "x")).toBe("a");
  });

  it("unicodeRe translates ^ $ . and \\s under multiline", () => {
    const re = unicodeRe("^ {0,3}>(?P<q>.*?)$", "y", true);
    re.lastIndex = 2;
    expect(re.exec("a\n> b c\nd")?.groups?.q).toBe(" b c");
    expect(unicodeRe("\\s+", "").test("\x1f")).toBe(true);
    expect(unicodeRe("[^\\s*]", "").test("\x1f")).toBe(false);
  });
});
