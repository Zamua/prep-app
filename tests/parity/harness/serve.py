"""Local parity target launcher: `python -m tests.parity.harness.serve`.

Requires `PREP_FAKE_NOW` (set by `server.py`), which pins every
server-side clock read, and `PREP_PARITY_MODE=1`, under which
`prep.app` mounts the parity routes.
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)

    if not os.environ.get("PREP_FAKE_NOW"):
        raise SystemExit("PREP_FAKE_NOW must be set for a parity target")
    if os.environ.get("PREP_PARITY_MODE") != "1":
        raise SystemExit("PREP_PARITY_MODE=1 must be set for a parity target")

    import uvicorn

    from prep.app import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
