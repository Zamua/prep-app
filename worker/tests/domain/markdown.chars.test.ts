import { describe, expect, it } from "vitest";
import { trim, unicodeRe } from "../../domain/markdown/chars";

describe("the renderer's character helpers", () => {
  it("strips the block-separator whitespace class, and leaves the BOM", () => {
    expect(trim("\x1c a 　")).toBe("a");
    expect(trim("﻿a﻿")).toBe("﻿a﻿");
    expect(trim("xxaxx", "x")).toBe("a");
  });

  it("unicodeRe translates ^ $ . and \\s under multiline", () => {
    const re = unicodeRe("^ {0,3}>(?<q>.*?)$", "y", true);
    re.lastIndex = 2;
    expect(re.exec("a\n> b c\nd")?.groups?.q).toBe(" b c");
    expect(unicodeRe("\\s+", "").test("\x1f")).toBe(true);
    expect(unicodeRe("[^\\s*]", "").test("\x1f")).toBe(false);
  });
});
