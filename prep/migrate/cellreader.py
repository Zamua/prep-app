"""The cell side of the comparison, behind one narrow port.

The verifier never speaks HTTP itself: it holds a `CellReader` and asks
for pages of rows. The fleet adapter is one implementation; a test's
fixture is another, which is what makes the verifier runnable without a
fleet.

`GET /_migrate/dump` is paged by rowid and capped at the same 2,000 rows
the import is, so verifying a 50,000-review user costs the cell one
bounded page at a time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

DUMP_PATH = "/_migrate/dump"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"
PAGE_LIMIT = 2000

# The directory and limiter cells are global: they are addressed by name,
# not by user.
DIRECTORY_CELL = "directory"
LIMITER_CELL = "limiter"


class CellUnreachable(RuntimeError):
    """The fleet answered something the verifier cannot interpret. Never
    swallowed: an unread cell is not a clean cell."""


class CellSealed(CellUnreachable):
    """`POST /_migrate/seal` already ran, so every `/_migrate/*` answers
    410. The verification gate comes before the seal, never after."""


class Page:
    __slots__ = ("rows", "next")

    def __init__(self, rows: list[dict], next_after: int | None) -> None:
        self.rows = rows
        self.next = next_after


class CellReader(Protocol):
    def page(
        self,
        *,
        table: str,
        user: str | None = None,
        cell: str | None = None,
        after: int | None = None,
        limit: int = PAGE_LIMIT,
        columns: Sequence[str] | None = None,
    ) -> Page: ...


def read_all(
    reader: CellReader, *, table: str, user: str | None = None, cell: str | None = None
) -> Iterator[dict]:
    """Every row of one table, paged. A page that returns a cursor it has
    already returned would loop forever; that is reported, not spun on."""
    after: int | None = None
    seen: set[int] = set()
    while True:
        page = reader.page(table=table, user=user, cell=cell, after=after)
        yield from page.rows
        if page.next is None:
            return
        if page.next in seen:
            raise CellUnreachable(f"{table} paged back to cursor {page.next}")
        seen.add(page.next)
        after = page.next


def token_from_file(path: str | Path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise CellUnreachable(f"{path} is empty; the dump needs the fleet's PREP_INTERNAL_TOKEN")
    return token


class HttpCellReader:
    """The fleet adapter. Holds the token; nothing else in the verifier
    sees it."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def page(
        self,
        *,
        table: str,
        user: str | None = None,
        cell: str | None = None,
        after: int | None = None,
        limit: int = PAGE_LIMIT,
        columns: Sequence[str] | None = None,
    ) -> Page:
        query: dict[str, str] = {"table": table, "limit": str(limit)}
        if user is not None:
            query["user"] = user
        if cell is not None:
            query["cell"] = cell
        if after is not None:
            query["after"] = str(after)
        if columns:
            query["columns"] = ",".join(columns)
        url = f"{self.base_url}{DUMP_PATH}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={INTERNAL_TOKEN_HEADER: self._token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 410:
                raise CellSealed(
                    f"{url} answered 410: the fleet is sealed, so it can no longer be verified"
                ) from e
            raise CellUnreachable(f"{url} answered {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise CellUnreachable(f"{url}: {e}") from e
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise CellUnreachable(f"{url} answered without a rows array")
        cursor = body.get("next")
        if cursor is not None and not isinstance(cursor, int):
            raise CellUnreachable(f"{url} answered a non-integer cursor {cursor!r}")
        return Page(rows, cursor)


class FixtureCellReader:
    """A cell side held in memory: `{(cell_or_user, table): [rows]}`. The
    verifier's own tests run against it, and so does anyone reproducing a
    reported divergence without a fleet."""

    def __init__(
        self, tables: dict[tuple[str, str], list[dict]], *, page_size: int = PAGE_LIMIT
    ) -> None:
        self.tables = tables
        self.page_size = page_size
        self.calls: list[tuple[str, str, int | None]] = []

    def page(
        self,
        *,
        table: str,
        user: str | None = None,
        cell: str | None = None,
        after: int | None = None,
        limit: int = PAGE_LIMIT,
        columns: Sequence[str] | None = None,
    ) -> Page:
        owner = cell if cell is not None else (user or "")
        self.calls.append((owner, table, after))
        rows = self.tables.get((owner, table), [])
        start = after or 0
        size = min(limit, self.page_size)
        window = rows[start : start + size]
        nxt = start + size if start + size < len(rows) else None
        if columns:
            window = [{c: r[c] for c in columns if c in r} for r in window]
        return Page([dict(r) for r in window], nxt)
