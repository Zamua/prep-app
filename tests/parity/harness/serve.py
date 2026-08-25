"""Local parity target launcher: `python -m tests.parity.harness.serve`.

Pins the clock, mounts the parity routes, runs uvicorn. With
`prep.infrastructure.clock` present, `PREP_FAKE_NOW` (set by
`server.py`) pins every server-side read; a tree without the seam
falls back to freezegun (ticking from `PARITY_FREEZE_NOW`) so the
harness still runs there.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

FREEZE_ENV = "PARITY_FREEZE_NOW"


def _pin_clock() -> str:
    if importlib.util.find_spec("prep.infrastructure.clock") is not None:
        if not os.environ.get("PREP_FAKE_NOW"):
            raise SystemExit("PREP_FAKE_NOW must be set for a parity target")
        return "clock"
    at = os.environ.get(FREEZE_ENV)
    if not at:
        raise SystemExit(f"{FREEZE_ENV} must be set on a tree without the clock seam")
    from freezegun import freeze_time

    freeze_time(at, tick=True).start()
    return "freezegun"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)

    how = _pin_clock()
    print(f"parity target: clock via {how}", file=sys.stderr)

    import uvicorn

    from prep.app import app
    from prep.dev import parity_seed

    parity_seed.register(app)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
