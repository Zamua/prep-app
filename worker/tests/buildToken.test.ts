import { describe, expect, it } from "vitest";
import { BUILD, bakeBuildInfo } from "../scripts/build.mjs";
import { isAcceptedVersionToken, resolveToken, sha1Hex } from "../runtime/tokenRules";

const BAKED = "ce11d0000000";

describe("sha1Hex", () => {
  it("matches the reference vectors, including multi-block input", () => {
    expect(sha1Hex("")).toBe("da39a3ee5e6b4b0d3255bfef95601890afd80709");
    expect(sha1Hex("abc")).toBe("a9993e364706816aba3e25717850c26c9cd0d89d");
    expect(sha1Hex("The quick brown fox jumps over the lazy dog")).toBe(
      "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12",
    );
    // 56 and 64 bytes: the length field spills into a second block.
    expect(sha1Hex("a".repeat(56))).toBe("c2db330f6083854c99d4b5bfb6e8f29f201be699");
    expect(sha1Hex("a".repeat(64))).toBe("0098ba824b5c16427bd7a1122a5a442a25ec644d");
    expect(sha1Hex("prep:v0.44.0")).toBe("32963d484208242566ddbc002eb7e564f99dabe3");
  });
});

describe("resolveBuildToken", () => {
  it("uses a hex token verbatim", () => {
    expect(resolveToken("deadbeefcafe1234", BAKED)).toBe("deadbeefcafe1234");
    expect(resolveToken("a".repeat(40), BAKED)).toBe("a".repeat(40));
    expect(resolveToken("  deadbeef  ", BAKED)).toBe("deadbeef");
  });

  it("hashes a non-hex value to 12 hex chars the routes accept", () => {
    // hashlib.sha1(b"v0.44.0").hexdigest()[:12]
    expect(resolveToken("v0.44.0", BAKED)).toBe("d8392eb81ff8");
    expect(isAcceptedVersionToken(resolveToken("v0.44.0", BAKED))).toBe(true);
  });

  it("falls back to the baked token when unset or blank", () => {
    expect(resolveToken(undefined, BAKED)).toBe(BAKED);
    expect(resolveToken(null, BAKED)).toBe(BAKED);
    expect(resolveToken("", BAKED)).toBe(BAKED);
    expect(resolveToken("   ", BAKED)).toBe(BAKED);
  });

  it("hashes a too-short or uppercase hex value rather than trusting its shape", () => {
    expect(resolveToken("abc123", BAKED)).toBe(sha1Hex("abc123").slice(0, 12));
    expect(resolveToken("DEADBEEF12", BAKED)).toBe(sha1Hex("DEADBEEF12").slice(0, 12));
  });
});

describe("isAcceptedVersionToken", () => {
  it("accepts lowercase hex 7-40 and the legacy digit stamps", () => {
    for (const tok of ["abcdef0", "deadbeefcafe", "a".repeat(40), "1234567", "1718000000000", "0"]) {
      expect(isAcceptedVersionToken(tok), tok).toBe(true);
    }
  });

  it("rejects every other shape", () => {
    const rejected = [
      "abcdef",
      "a".repeat(41),
      "DEADBEEF12",
      "deadbeef.js",
      "endor",
      "ersion.txt",
      "١٢٣٤٥٦٧",
      "²".repeat(8),
      "1234567\n",
      "",
    ];
    for (const tok of rejected) {
      expect(isAcceptedVersionToken(tok), JSON.stringify(tok)).toBe(false);
    }
  });
});

describe("resolveBuildToken", () => {
  it("defaults to the token the build baked", async () => {
    bakeBuildInfo("ce11d0000000", BUILD);
    const { resolveBuildToken } = await import("../runtime/buildToken");
    expect(resolveBuildToken(undefined)).toBe("ce11d0000000");
    expect(resolveBuildToken("")).toBe("ce11d0000000");
    expect(resolveBuildToken("deadbeef")).toBe("deadbeef");
    expect(resolveBuildToken("v0.44.0")).toBe("d8392eb81ff8");
  });
});
