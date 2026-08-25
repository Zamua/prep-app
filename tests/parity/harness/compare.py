"""Pixel comparator (docs/PARITY-GATE.md C4).

A pixel fails when any RGB channel differs by more than `CHANNEL_TOL`.
The image passes when the failing fraction is at most `MAX_FAIL_RATIO`
AND no aligned `BLOCK`x`BLOCK` block holds more than `BLOCK_MAX_FAIL`
failing pixels. Scattered anti-aliasing noise clears both; a glyph
shifted by one CSS pixel at DPR 3 concentrates its failures in a few
blocks and trips the block rule alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

CHANNEL_TOL = 2
MAX_FAIL_RATIO = 0.0002
BLOCK = 8
BLOCK_MAX_FAIL = 2

_DIM = 0.35
_RED = (255, 0, 0)
_YELLOW = (255, 220, 0)


@dataclass(frozen=True)
class Report:
    passed: bool
    reason: str
    golden_size: tuple[int, int]
    candidate_size: tuple[int, int]
    area: int
    failing: int
    failing_blocks: int
    diff_path: Path | None

    @property
    def ratio(self) -> float:
        return self.failing / self.area if self.area else 0.0

    def summary(self) -> str:
        if self.reason == "size":
            return f"size mismatch: golden {self.golden_size} vs candidate {self.candidate_size}"
        parts = [f"{self.failing}/{self.area} px ({self.ratio:.5%}) over tol"]
        if self.failing_blocks:
            parts.append(f"{self.failing_blocks} block(s) over {BLOCK_MAX_FAIL}")
        if self.diff_path:
            parts.append(f"diff {self.diff_path}")
        return "; ".join(parts)


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def fail_mask(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(H, W) bool: any channel differs by more than `CHANNEL_TOL`."""
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return (delta > CHANNEL_TOL).any(axis=2)


def block_counts(mask: np.ndarray) -> np.ndarray:
    """Failing pixels per aligned block; a partial edge block counts
    what it holds."""
    h, w = mask.shape
    ph = (-h) % BLOCK
    pw = (-w) % BLOCK
    padded = np.pad(mask, ((0, ph), (0, pw)))
    bh, bw = padded.shape[0] // BLOCK, padded.shape[1] // BLOCK
    return padded.reshape(bh, BLOCK, bw, BLOCK).sum(axis=(1, 3))


def write_diff_mask(
    candidate: np.ndarray, mask: np.ndarray, blocks_over: np.ndarray, out: Path
) -> None:
    """The candidate dimmed, failing pixels red, failing blocks outlined
    yellow."""
    img = (candidate.astype(np.float32) * _DIM).astype(np.uint8)
    img[mask] = _RED
    h, w = mask.shape
    for by, bx in zip(*np.nonzero(blocks_over), strict=True):
        y0, x0 = by * BLOCK, bx * BLOCK
        y1, x1 = min(y0 + BLOCK, h) - 1, min(x0 + BLOCK, w) - 1
        img[y0, x0 : x1 + 1] = _YELLOW
        img[y1, x0 : x1 + 1] = _YELLOW
        img[y0 : y1 + 1, x0] = _YELLOW
        img[y0 : y1 + 1, x1] = _YELLOW
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img, "RGB").save(out)


def compare(golden: Path, candidate: Path, diff_out: Path) -> Report:
    a = load_rgb(golden)
    b = load_rgb(candidate)
    gsize = (int(a.shape[1]), int(a.shape[0]))
    csize = (int(b.shape[1]), int(b.shape[0]))
    if gsize != csize:
        return Report(False, "size", gsize, csize, 0, 0, 0, None)
    mask = fail_mask(a, b)
    area = int(mask.size)
    failing = int(mask.sum())
    blocks_over = block_counts(mask) > BLOCK_MAX_FAIL
    n_blocks = int(blocks_over.sum())
    reasons = []
    if failing > MAX_FAIL_RATIO * area:
        reasons.append("ratio")
    if n_blocks:
        reasons.append("blocks")
    if not reasons:
        return Report(True, "", gsize, csize, area, failing, 0, None)
    write_diff_mask(b, mask, blocks_over, diff_out)
    return Report(False, "+".join(reasons), gsize, csize, area, failing, n_blocks, diff_out)
