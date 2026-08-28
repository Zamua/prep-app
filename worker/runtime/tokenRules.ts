// The build-token rules, pure so the build script can bake the token
// before build/buildinfo.js exists and the worker can resolve an override
// at runtime.
// Lowercase hex, 7 to 40 chars: a git SHA or SHA prefix. After the "v"
// prefix strip on the versioned asset routes, a token can never collide
// with a real path segment under static/js or static/css.
const TOKEN_RE = /^[0-9a-f]{7,40}$/;

// Legacy all-digit boot stamps, still referenced by pages cached from
// pre-offline deploys. ASCII digits only; a wider digit class would widen
// the echo/alias charset past the service worker's regex.
const LEGACY_STAMP_RE = /^[0-9]+$/;

/** A token-shaped value passes verbatim; any other non-empty value (an
 * image tag like "v0.44.0") is normalized to the first twelve hex digits of
 * its SHA-1, so the served token always matches the accepted charset; empty
 * falls back to `baked`. */
export function resolveToken(raw: string | undefined | null, baked: string): string {
  const value = (raw ?? '').trim();
  if (TOKEN_RE.test(value)) return value;
  if (value) return sha1Hex(value).slice(0, 12);
  return baked;
}

/** Whether a versioned-asset URL segment (after the "v" prefix) may alias
 * onto the current tree and be echoed into the offline shell. */
export function isAcceptedVersionToken(segment: string): boolean {
  return TOKEN_RE.test(segment) || LEGACY_STAMP_RE.test(segment);
}

// Synchronous SHA-1 over UTF-8, so the build script (node) and the worker
// (no sync WebCrypto) share one resolver.
export function sha1Hex(input: string): string {
  const bytes = new TextEncoder().encode(input);
  const total = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(total);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bits = bytes.length * 8;
  view.setUint32(total - 8, Math.floor(bits / 0x100000000));
  view.setUint32(total - 4, bits >>> 0);

  let h0 = 0x67452301;
  let h1 = 0xefcdab89;
  let h2 = 0x98badcfe;
  let h3 = 0x10325476;
  let h4 = 0xc3d2e1f0;
  const w = new Uint32Array(80);
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 80; i++) {
      const x = w[i - 3]! ^ w[i - 8]! ^ w[i - 14]! ^ w[i - 16]!;
      w[i] = (x << 1) | (x >>> 31);
    }
    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    for (let i = 0; i < 80; i++) {
      let f: number;
      let k: number;
      if (i < 20) {
        f = (b & c) | (~b & d);
        k = 0x5a827999;
      } else if (i < 40) {
        f = b ^ c ^ d;
        k = 0x6ed9eba1;
      } else if (i < 60) {
        f = (b & c) | (b & d) | (c & d);
        k = 0x8f1bbcdc;
      } else {
        f = b ^ c ^ d;
        k = 0xca62c1d6;
      }
      const t = (((a << 5) | (a >>> 27)) + f + e + k + w[i]!) >>> 0;
      e = d;
      d = c;
      c = (b << 30) | (b >>> 2);
      b = a;
      a = t;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
  }
  return [h0, h1, h2, h3, h4].map((x) => x.toString(16).padStart(8, '0')).join('');
}
