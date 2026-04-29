"""Headless screenshot pass for the prep-app UI sweep.

Drives Chromium at iPhone 15 Pro Max viewport (430x932 logical, DPR 3) over
the dev preview routes, plus the live index/deck pages, and saves a PNG per
screen to ./ui-screenshots/<run-tag>/ relative to the repo root.

Usage:
    .venv/bin/python capture.py                  # tag = before-<timestamp>
    .venv/bin/python capture.py --tag after-foo  # tag = after-foo
    .venv/bin/python capture.py --only result    # only screens whose name contains "result"
    .venv/bin/python capture.py --out /path/to/dir  # override output base

Outputs one PNG per screen, deterministically named "<screen>.png" so the
same screen across runs has the same filename — easy to diff before/after.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000/prep"

# iPhone 15 Pro Max — 430x932 logical, DPR 3.
IPHONE_15_PRO_MAX = {
    "viewport": {"width": 430, "height": 932},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
    ),
}

# (screen_id, url-relative-to-BASE_URL). screen_id is the filename stem.
SCREENS: list[tuple[str, str]] = [
    # Live (real data) — for sanity vs the previews. Replace these
    # paths with whatever decks actually exist in the running instance.
    ("live-index", "/"),
    # Index variants
    ("index-empty", "/dev/preview/index/empty"),
    ("index-populated", "/dev/preview/index/populated"),
    # Deck variants
    ("deck-empty", "/dev/preview/deck/empty"),
    ("deck-populated", "/dev/preview/deck/populated"),
    ("deck-with-suspended", "/dev/preview/deck/with_suspended"),
    # Study variants — one per type
    ("study-mcq", "/dev/preview/study/mcq"),
    ("study-multi", "/dev/preview/study/multi"),
    ("study-code", "/dev/preview/study/code"),
    ("study-short", "/dev/preview/study/short"),
    # Study empty
    ("study-empty", "/dev/preview/study_empty/default"),
    # Result — 4 types × 2 verdicts + idk
    ("result-mcq-right", "/dev/preview/result/mcq-right"),
    ("result-mcq-wrong", "/dev/preview/result/mcq-wrong"),
    ("result-multi-right", "/dev/preview/result/multi-right"),
    ("result-multi-wrong", "/dev/preview/result/multi-wrong"),
    ("result-code-right", "/dev/preview/result/code-right"),
    ("result-code-wrong", "/dev/preview/result/code-wrong"),
    ("result-short-right", "/dev/preview/result/short-right"),
    ("result-short-wrong", "/dev/preview/result/short-wrong"),
    ("result-code-idk", "/dev/preview/result/code-idk"),
    # Workflow status pages
    ("generation-in-progress", "/dev/preview/generation/in-progress"),
    ("generation-complete", "/dev/preview/generation/complete"),
    ("grading-in-progress", "/dev/preview/grading/in-progress"),
]

# Default output dir is repo-relative (ui-tools/ sits one level below
# the repo root); override with --out at invocation time.
OUT_BASE = Path(__file__).resolve().parent.parent / "ui-screenshots"


def capture_all(out_dir: Path, screens: list[tuple[str, str]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(**IPHONE_15_PRO_MAX, color_scheme="light")
            # Suppress page_load animations a bit by waiting after
            # networkidle. The CSS has a 600ms entry rise — let it settle.
            for screen_id, path in screens:
                page = context.new_page()
                url = f"{BASE_URL}{path}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=15_000)
                    # Let animations finish.
                    page.wait_for_timeout(800)
                    target = out_dir / f"{screen_id}.png"
                    page.screenshot(path=str(target), full_page=True)
                    print(f"  ✓ {screen_id:32s} -> {target.name}")
                except Exception as e:
                    print(f"  ✗ {screen_id:32s} FAILED: {e}", file=sys.stderr)
                finally:
                    page.close()
            context.close()
        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=None, help="Output dir suffix. Default: before-<timestamp>.")
    ap.add_argument(
        "--only", default=None, help="Substring filter on screen_id; only those will be captured."
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output base dir. Default: <repo>/ui-screenshots/.",
    )
    args = ap.parse_args()

    tag = args.tag or f"before-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_base = Path(args.out).expanduser().resolve() if args.out else OUT_BASE
    out_dir = out_base / tag

    screens = SCREENS
    if args.only:
        screens = [(sid, url) for sid, url in SCREENS if args.only in sid]
        if not screens:
            print(f"No screens match --only '{args.only}'", file=sys.stderr)
            sys.exit(1)

    print(f"Capturing {len(screens)} screens to {out_dir}")
    t0 = time.time()
    capture_all(out_dir, screens)
    print(f"Done in {time.time() - t0:.1f}s. Output: {out_dir}")


if __name__ == "__main__":
    main()
