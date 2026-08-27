"""The export directory into the fleet, one chunk at a time.

`POST /_migrate/import` takes one user's one table, under the same 4 MiB
and 2,000 rows the cell was measured against. Nothing here is keyed by a
run id: every write keys on data the export already carries, so a second
run of the same export inserts nothing, and a run killed halfway resumes
from `GET /_migrate/status`, which is the fleet's own count of what it
holds rather than a local file this tool could lose.

    .venv/bin/python -m prep.migrate.importer \
        --export <dir> --base-url <url> --token-file <path> [--users <file>]

`--no-resume` sends the whole export again against a fleet that already
has it. That is the rehearsal's second pass: every insert count zero is
what proves the run is re-runnable.

No local progress file: a status call is one request per user, and a
progress file that disagrees with the fleet is worse than no progress
file at all.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from prep.migrate import layout
from prep.migrate.export import DROPPED_BYOK_PROVIDER

IMPORT_PATH = "/_migrate/import"
STATUS_PATH = "/_migrate/status"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

# The endpoint's own caps (worker/domain/migrate.ts). Kept here so a chunk
# that would be refused is never built, and never sent twice.
MAX_CHUNK_ROWS = 2000
MAX_CHUNK_BYTES = 4 * 1024 * 1024

# Bodies are encoded with compact separators, so an array of n rows costs
# the sum of the rows plus n - 1 commas plus two brackets. The slack covers
# those and the rounding in nothing else.
_SLACK = 64
_COMPACT = (",", ":")

# The global cells, and the file each one's table comes from. The
# directory's `users` rows are absent on purpose: they are written by the
# per-user register, which is also what hands out the id block.
GLOBALS: tuple[tuple[str, str], ...] = (
    ("directory", "account_merges"),
    ("limiter", "instant_generations"),
)


class ImportRefused(RuntimeError):
    """The fleet declined a chunk, or could not be reached. Never
    swallowed: a chunk that did not land is a user that did not migrate."""


class FleetSealed(ImportRefused):
    """`POST /_migrate/seal` already ran, so every `/_migrate/*` answers
    410. The seal is the last step of the cutover; nothing imports after."""


def token_from_file(path: str | Path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise ImportRefused(f"{path} is empty; the import needs the fleet's PREP_INTERNAL_TOKEN")
    return token


class CellWriter(Protocol):
    def status(self, *, user: str | None = None, cell: str | None = None) -> dict: ...

    def send(self, chunk: dict) -> dict: ...


class HttpCellWriter:
    """The fleet adapter. Holds the token; nothing else in the importer
    sees it."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def status(self, *, user: str | None = None, cell: str | None = None) -> dict:
        query = {"user": user} if user is not None else {"cell": cell or ""}
        return self._json(f"{self.base_url}{STATUS_PATH}?{urllib.parse.urlencode(query)}", None)

    def send(self, chunk: dict) -> dict:
        body = json.dumps(chunk, ensure_ascii=True, separators=_COMPACT).encode("ascii")
        return self._json(f"{self.base_url}{IMPORT_PATH}", body)

    def _json(self, url: str, body: bytes | None) -> dict:
        headers = {INTERNAL_TOKEN_HEADER: self._token}
        if body is not None:
            headers["Content-Type"] = "application/json"
            # Explicit, so the endpoint's cap is decided before it reads a
            # byte rather than while it streams.
            headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 410:
                raise FleetSealed(f"{url} answered 410: the fleet is sealed") from e
            raise ImportRefused(f"{url} answered {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise ImportRefused(f"{url}: {e}") from e


# ---- chunking -------------------------------------------------------------


def chunk_rows(
    rows: Iterable[dict],
    *,
    max_rows: int = MAX_CHUNK_ROWS,
    max_bytes: int = MAX_CHUNK_BYTES,
    envelope: int = 0,
) -> Iterator[list[dict]]:
    """Rows in batches the endpoint will accept. Streams: the heaviest
    user's 50,000 reviews never land in memory whole, and neither the tool
    nor the cell holds more than one batch."""
    budget = max_bytes - envelope - _SLACK
    batch: list[dict] = []
    size = 0
    for row in rows:
        cost = len(json.dumps(row, ensure_ascii=True, separators=_COMPACT).encode("ascii")) + 1
        if cost > budget:
            raise ImportRefused(f"one row is {cost} bytes, past the {max_bytes} byte chunk cap")
        if batch and (len(batch) >= max_rows or size + cost > budget):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += cost
    if batch:
        yield batch


def envelope_bytes(chunk: dict) -> int:
    """What a chunk costs before its rows: the ids, the table name, and the
    profile when it carries one."""
    return len(
        json.dumps({**chunk, "rows": []}, ensure_ascii=True, separators=_COMPACT).encode("ascii")
    )


# ---- the resume point -----------------------------------------------------


def first_short_table(
    expected: dict[str, int],
    present: dict[str, int],
    order: Sequence[str] = layout.DATA_TABLES,
) -> str | None:
    """Where a killed run restarts: the first table in insert order the
    fleet holds fewer rows of than the export has. Re-sending a whole table
    is always safe, because the insert ignores a key the cell already has,
    so this is a floor on the work and never a correctness argument."""
    for table in order:
        if present.get(table, 0) < expected.get(table, 0):
            return table
    return None


def expected_counts(out: Path, user: str, counts: dict) -> dict[str, int]:
    """The manifest's counts as the cells will hold them: the subscription
    credential is exported faithfully and dropped at import, so counting it
    as present would leave every re-run restarting at the same table."""
    expected = {t: int(counts.get(t, 0)) for t in layout.DATA_TABLES}
    if expected.get("byok_credentials"):
        rows = layout.iter_rows(layout.table_path(out, user, "byok_credentials"))
        expected["byok_credentials"] = sum(
            1 for r in rows if r.get("provider") != DROPPED_BYOK_PROVIDER
        )
    return expected


# ---- the run --------------------------------------------------------------


@dataclasses.dataclass
class Report:
    users: int = 0
    complete: int = 0
    chunks: int = 0
    dropped: int = 0
    inserted: dict[str, int] = dataclasses.field(default_factory=dict)

    def record(self, body: dict) -> None:
        self.chunks += 1
        self.dropped += int(body.get("dropped", 0))
        for table, n in (body.get("inserted") or {}).items():
            self.inserted[table] = self.inserted.get(table, 0) + int(n)

    @property
    def rows(self) -> int:
        return sum(self.inserted.values())


def import_user(
    writer: CellWriter,
    out: Path,
    entry: dict,
    report: Report,
    *,
    resume: bool = True,
    max_rows: int = MAX_CHUNK_ROWS,
) -> None:
    """One user, in the order the cell needs: the profile chunk registers
    the user and seeds its id block, then the tables parents first."""
    user, idx = entry["id"], int(entry["idx"])
    expected = expected_counts(out, user, entry.get("counts") or {})
    status = writer.status(user=user) if resume else None
    report.users += 1

    if status is None or not status.get("profile"):
        chunk = {
            "user": user,
            "idx": idx,
            "table": None,
            "rows": [],
            "profile": layout.read_profile(out, user),
        }
        report.record(writer.send(chunk))

    if status is None:
        start: str | None = layout.DATA_TABLES[0]
    else:
        start = first_short_table(
            expected, {k: int(v) for k, v in (status.get("tables") or {}).items()}
        )
        if start is None:
            report.complete += 1
            return

    for table in layout.DATA_TABLES[layout.DATA_TABLES.index(start) :]:
        head = {"user": user, "idx": idx, "table": table}
        rows = layout.iter_rows(layout.table_path(out, user, table))
        for batch in chunk_rows(rows, max_rows=max_rows, envelope=envelope_bytes(head)):
            report.record(writer.send({**head, "rows": batch}))


def import_globals(
    writer: CellWriter,
    out: Path,
    manifest: dict,
    report: Report,
    *,
    resume: bool = True,
    max_rows: int = MAX_CHUNK_ROWS,
) -> None:
    """`account_merges` into the directory and the limiter's window, ids
    preserved. Both are the source of a decision nothing else can rebuild:
    `previous_ids` for an offline device's old owner, and the burst
    allowance at the moment of highest exposure."""
    counts = manifest.get("globals") or {}
    for cell, table in GLOBALS:
        expected = int(counts.get(table, 0))
        if (
            resume
            and int(((writer.status(cell=cell).get("tables")) or {}).get(table, 0)) >= expected
        ):
            continue
        path = (
            layout.directory_path(out, table) if cell == "directory" else layout.limiter_path(out)
        )
        head = {"cell": cell, "table": table}
        for batch in chunk_rows(
            layout.iter_rows(path), max_rows=max_rows, envelope=envelope_bytes(head)
        ):
            report.record(writer.send({**head, "rows": batch}))


def run(
    out: Path,
    writer: CellWriter,
    *,
    users: Sequence[str] | None = None,
    resume: bool = True,
    max_rows: int = MAX_CHUNK_ROWS,
) -> Report:
    out = Path(out)
    manifest = layout.read_manifest(out)
    wanted = set(users) if users is not None else None
    report = Report()
    for entry in manifest["users"]:
        if wanted is None or entry["id"] in wanted:
            import_user(writer, out, entry, report, resume=resume, max_rows=max_rows)
    # After the users, so a run killed in the middle leaves the audit for
    # the resume rather than a directory that names accounts with no cells.
    if wanted is None:
        import_globals(writer, out, manifest, report, resume=resume, max_rows=max_rows)
    return report


# ---- cli ------------------------------------------------------------------


def _users_file(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    return [
        line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m prep.migrate.importer",
        description="Replay a celld migration export into a fleet.",
    )
    parser.add_argument("--export", required=True, type=Path, help="an export directory")
    parser.add_argument("--base-url", required=True, help="the fleet's entry worker")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument(
        "--users", type=Path, help="one user id per line; the default is the whole manifest"
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="send every chunk again instead of starting from the fleet's counts",
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=MAX_CHUNK_ROWS,
        help=f"rows per chunk, at most {MAX_CHUNK_ROWS}; smaller trades requests for a smaller peak",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        writer = HttpCellWriter(
            args.base_url, token_from_file(args.token_file), timeout=args.timeout
        )
        if not 1 <= args.chunk_rows <= MAX_CHUNK_ROWS:
            raise ImportRefused(f"--chunk-rows must be between 1 and {MAX_CHUNK_ROWS}")
        report = run(
            args.export,
            writer,
            users=_users_file(args.users),
            resume=args.resume,
            max_rows=args.chunk_rows,
        )
    except (ImportRefused, OSError, ValueError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(f"users      {report.users} ({report.complete} already complete)")
    print(f"chunks     {report.chunks}")
    print(f"rows       {report.rows} inserted, {report.dropped} dropped by a disposition")
    for table in sorted(report.inserted):
        print(f"  {table:<26} {report.inserted[table]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
