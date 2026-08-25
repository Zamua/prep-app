"""Comparator properties on synthetic images."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from tests.parity.harness import compare as cmp

W, H = 600, 900
AREA = W * H


def _base(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), 240, dtype=np.uint8)
    # Some structure so the image is not flat.
    for y in range(0, H, 40):
        img[y : y + 20, :, :] = rng.integers(60, 200, size=3, dtype=np.uint8)
    return img


def _save(tmp_path, name: str, arr: np.ndarray):
    p = tmp_path / name
    Image.fromarray(arr, "RGB").save(p)
    return p


def _scatter_points(n: int, spacing: int = 16) -> list[tuple[int, int]]:
    """`n` pixels with at least `spacing` between any two, so no 8x8
    block ever holds more than one."""
    pts = []
    for y in range(3, H, spacing):
        for x in range(3, W, spacing):
            pts.append((y, x))
            if len(pts) == n:
                return pts
    raise AssertionError("not enough room")


def _run(tmp_path, golden: np.ndarray, candidate: np.ndarray) -> cmp.Report:
    g = _save(tmp_path, "golden.png", golden)
    c = _save(tmp_path, "candidate.png", candidate)
    return cmp.compare(g, c, tmp_path / "out" / "diff.png")


def test_identical_passes(tmp_path):
    base = _base()
    r = _run(tmp_path, base, base.copy())
    assert r.passed and r.failing == 0 and r.diff_path is None
    assert not (tmp_path / "out" / "diff.png").exists()


def test_pass_removes_a_stale_diff(tmp_path):
    stale = tmp_path / "out" / "diff.png"
    stale.parent.mkdir()
    stale.write_bytes(b"old verdict")
    base = _base()
    assert _run(tmp_path, base, base.copy()).passed
    assert not stale.exists()


def test_within_channel_tolerance_everywhere_passes(tmp_path):
    base = _base()
    cand = np.clip(base.astype(np.int16) + cmp.CHANNEL_TOL, 0, 255).astype(np.uint8)
    assert _run(tmp_path, base, cand).passed


def test_over_channel_tolerance_everywhere_fails(tmp_path):
    base = _base()
    cand = np.clip(base.astype(np.int16) + cmp.CHANNEL_TOL + 1, 0, 255).astype(np.uint8)
    r = _run(tmp_path, base, cand)
    assert not r.passed and r.reason == "ratio+blocks"


def test_scattered_aa_noise_passes(tmp_path):
    base = _base()
    cand = base.copy()
    n = int(AREA * 0.0001)  # 0.01%
    rng = np.random.default_rng(7)
    for y, x in _scatter_points(n):
        cand[y, x] = np.clip(cand[y, x].astype(np.int16) + rng.integers(3, 60), 0, 255)
    r = _run(tmp_path, base, cand)
    assert r.passed, r.summary()
    assert r.failing == n


def test_one_shifted_glyph_fails_block_rule_only(tmp_path):
    base = _base()
    # A 3x9 device-pixel stem (one CSS pixel wide at DPR 3) on the flat
    # background, moved right by one CSS pixel.
    stem = np.array([20, 20, 20], dtype=np.uint8)
    golden = base.copy()
    golden[300:309, 100:103] = stem
    cand = base.copy()
    cand[300:309, 103:106] = stem
    r = _run(tmp_path, golden, cand)
    assert not r.passed
    assert r.reason == "blocks", r.summary()
    assert r.failing <= cmp.MAX_FAIL_RATIO * AREA
    assert r.failing_blocks >= 1
    assert r.diff_path is not None and r.diff_path.exists()


def test_scattered_over_ratio_fails_ratio_only(tmp_path):
    base = _base()
    cand = base.copy()
    n = int(AREA * 0.0003)  # 0.03%
    for y, x in _scatter_points(n):
        cand[y, x] = 0
    r = _run(tmp_path, base, cand)
    assert not r.passed
    assert r.reason == "ratio", r.summary()
    assert r.failing_blocks == 0


def test_size_mismatch_fails(tmp_path):
    base = _base()
    r = _run(tmp_path, base, base[:-1, :, :].copy())
    assert not r.passed and r.reason == "size"
    assert "size mismatch" in r.summary()


def test_diff_mask_marks_pixels_and_outlines_blocks(tmp_path):
    base = _base()
    golden = base.copy()
    cand = base.copy()
    cand[400:408, 200:208] = 0  # one whole block
    r = _run(tmp_path, golden, cand)
    assert not r.passed
    diff = cmp.load_rgb(r.diff_path)
    assert tuple(diff[404, 204]) == cmp._RED
    assert tuple(diff[400, 200]) == cmp._YELLOW
    # Untouched pixels are dimmed, not copied.
    assert (diff[10, 10] < base[10, 10]).all()


def test_block_counts_cover_partial_edge_blocks():
    mask = np.zeros((13, 11), dtype=bool)
    mask[12, 10] = True
    counts = cmp.block_counts(mask)
    assert counts.shape == (2, 2)
    assert counts[1, 1] == 1


@pytest.mark.parametrize("alpha", [True, False])
def test_alpha_channel_is_ignored(tmp_path, alpha):
    base = _base()
    p = tmp_path / "rgba.png"
    im = Image.fromarray(base, "RGB")
    if alpha:
        im = im.convert("RGBA")
    im.save(p)
    assert cmp.load_rgb(p).shape == (H, W, 3)
