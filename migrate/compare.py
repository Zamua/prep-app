"""The comparison itself: pure functions over two lists of rows.

No I/O, no HTTP, no SQLite. Both sides arrive as plain dicts, so the same
functions compare a snapshot table against a cell dump, a fake against a
fixture, and one export against another.

Two rules the whole file exists to enforce:

* **No tolerance on a copy.** A migrated column is compared byte for byte
  (TEXT), value for value (INTEGER) or bit for bit (REAL). The 1e-9 the
  FSRS port is allowed belongs to a *computation*; using it here would
  hide the drift being hunted.
* **No bare counts.** A row present on one side and absent on the other is
  reported by its key, not folded into a total.

One transport caveat, and it is the only coercion in the file: JSON cannot
tell `30` from `30.0`, so `JSON.stringify` writes an integral double as an
integer. A REAL column's cell value is therefore widened to float before
its bits are taken. Every other type mismatch is reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from migrate.divergence import TABLE_SCOPE, Divergence, float_bits, render, row_key

# `PRAGMA table_info` type names that hold an IEEE-754 double.
REAL_TYPES = frozenset({"REAL", "DOUBLE", "FLOAT", "NUMERIC"})


def is_real_column(declared_type: str) -> bool:
    return declared_type.strip().upper() in REAL_TYPES


def normalise(value: object, *, real: bool) -> object:
    """The value as the comparison sees it. Bools collapse to int (SQLite
    has no boolean); a REAL column widens an integral JSON number."""
    if isinstance(value, bool):
        return int(value)
    if real and isinstance(value, int):
        return float(value)
    return value


def same(left: object, right: object, *, real: bool) -> bool:
    a, b = normalise(left, real=real), normalise(right, real=real)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        # Bit equality, so -0.0 and 0.0 differ and two NaNs of the same
        # payload agree. `==` would get both backwards.
        if not (isinstance(a, float) and isinstance(b, float)):
            return False
        return float_bits(a) == float_bits(b)
    return type(a) is type(b) and a == b


def close(left: object, right: object, tolerance: float) -> bool:
    """Tier 3's only tolerance: the FSRS port's own, on a value both sides
    computed rather than copied."""
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance


def index_by_key(rows: Iterable[dict], key_columns: Sequence[str]) -> dict[tuple, dict]:
    return {tuple(row.get(c) for c in key_columns): dict(row) for row in rows}


def compare_rows(
    *,
    tier: int,
    table: str,
    key_columns: Sequence[str],
    snapshot_rows: Iterable[dict],
    cell_rows: Iterable[dict],
    real_columns: frozenset[str] = frozenset(),
    fields: Sequence[str] | None = None,
    user: str | None = None,
) -> list[Divergence]:
    """Field by field over the rows the two sides share, plus one
    divergence per row only one side holds.

    `fields` restricts the comparison; the default is every column the
    snapshot row carries. A snapshot column the cell row lacks is itself a
    divergence: the cell schema may add columns, never drop one.
    """
    keys = tuple(key_columns)
    left = index_by_key(snapshot_rows, keys)
    right = index_by_key(cell_rows, keys)
    found: list[Divergence] = []

    for key in sorted(left.keys() - right.keys(), key=repr):
        found.append(
            Divergence(
                tier=tier,
                user=user,
                table=table,
                row=row_key(keys, left[key]),
                field="<row>",
                snapshot="present",
                cell="absent",
                note="the import did not land this row",
            )
        )
    for key in sorted(right.keys() - left.keys(), key=repr):
        found.append(
            Divergence(
                tier=tier,
                user=user,
                table=table,
                row=row_key(keys, right[key]),
                field="<row>",
                snapshot="absent",
                cell="present",
                note="the cell holds a row the snapshot does not",
            )
        )

    for key in sorted(left.keys() & right.keys(), key=repr):
        found.extend(
            _compare_pair(
                tier=tier,
                user=user,
                table=table,
                keys=keys,
                srow=left[key],
                crow=right[key],
                real_columns=real_columns,
                fields=fields,
            )
        )
    return found


def compare_streams(
    *,
    tier: int,
    table: str,
    key_columns: Sequence[str],
    snapshot_rows: Iterable[dict],
    cell_rows: Iterable[dict],
    real_columns: frozenset[str] = frozenset(),
    fields: Sequence[str] | None = None,
    user: str | None = None,
) -> Iterator[Divergence]:
    """`compare_rows` over two iterators rather than two lists.

    Both sides arrive in rowid order, which for a faithful import is the
    same order, so a matched pair is compared and dropped as it goes and
    the working set stays empty. Only rows the two sides disagree about
    are held, so the cost is the size of the divergence rather than the
    size of the table: a 50,000-review account is compared field by field
    without either side ever being materialised.
    """
    keys = tuple(key_columns)
    left_pending: dict[tuple, dict] = {}
    right_pending: dict[tuple, dict] = {}

    def pair(srow: dict, crow: dict) -> list[Divergence]:
        return _compare_pair(
            tier=tier,
            user=user,
            table=table,
            keys=keys,
            srow=srow,
            crow=crow,
            real_columns=real_columns,
            fields=fields,
        )

    left, right = iter(snapshot_rows), iter(cell_rows)
    lrow, rrow = next(left, None), next(right, None)
    while lrow is not None or rrow is not None:
        if lrow is not None and rrow is not None and _key_of(keys, lrow) == _key_of(keys, rrow):
            yield from pair(lrow, rrow)
            lrow, rrow = next(left, None), next(right, None)
            continue
        if lrow is not None:
            key = _key_of(keys, lrow)
            held = right_pending.pop(key, None)
            if held is None:
                left_pending[key] = lrow
            else:
                yield from pair(lrow, held)
            lrow = next(left, None)
        if rrow is not None:
            key = _key_of(keys, rrow)
            held = left_pending.pop(key, None)
            if held is None:
                right_pending[key] = rrow
            else:
                yield from pair(held, rrow)
            rrow = next(right, None)

    for key in sorted(left_pending, key=repr):
        yield Divergence(
            tier=tier,
            user=user,
            table=table,
            row=row_key(keys, left_pending[key]),
            field="<row>",
            snapshot="present",
            cell="absent",
            note="the import did not land this row",
        )
    for key in sorted(right_pending, key=repr):
        yield Divergence(
            tier=tier,
            user=user,
            table=table,
            row=row_key(keys, right_pending[key]),
            field="<row>",
            snapshot="absent",
            cell="present",
            note="the cell holds a row the snapshot does not",
        )


def _key_of(keys: tuple[str, ...], row: dict) -> tuple:
    return tuple(row.get(c) for c in keys)


def _compare_pair(
    *,
    tier: int,
    user: str | None,
    table: str,
    keys: tuple[str, ...],
    srow: dict,
    crow: dict,
    real_columns: frozenset[str],
    fields: Sequence[str] | None,
) -> list[Divergence]:
    """Two rows the two sides agree on the identity of, field by field. A
    snapshot column the cell row lacks is itself a divergence: the cell
    schema may add columns, never drop one."""
    columns = list(fields) if fields is not None else list(srow.keys())
    identity = row_key(keys, srow)
    found: list[Divergence] = []
    for column in columns:
        if column not in crow:
            found.append(
                Divergence(
                    tier=tier,
                    user=user,
                    table=table,
                    row=identity,
                    field=column,
                    snapshot=render(srow.get(column)),
                    cell="<column absent>",
                    note="the cell dump has no such column",
                )
            )
            continue
        real = column in real_columns
        if not same(srow.get(column), crow.get(column), real=real):
            found.append(
                Divergence(
                    tier=tier,
                    user=user,
                    table=table,
                    row=identity,
                    field=column,
                    snapshot=render(normalise(srow.get(column), real=real)),
                    cell=render(normalise(crow.get(column), real=real)),
                )
            )
    return found


def compare_count(
    *,
    tier: int,
    table: str,
    expected: int,
    actual: int,
    user: str | None = None,
    note: str = "",
) -> list[Divergence]:
    """A count check for a table whose rows are not being enumerated. The
    row scope says so rather than pretending to name one."""
    if expected == actual:
        return []
    return [
        Divergence(
            tier=tier,
            user=user,
            table=table,
            row=TABLE_SCOPE,
            field="<count>",
            snapshot=str(expected),
            cell=str(actual),
            note=note,
        )
    ]


def compare_scalar(
    *,
    tier: int,
    table: str,
    row: str,
    field: str,
    snapshot_value: object,
    cell_value: object,
    real: bool = False,
    user: str | None = None,
    note: str = "",
) -> list[Divergence]:
    if same(snapshot_value, cell_value, real=real):
        return []
    return [
        Divergence(
            tier=tier,
            user=user,
            table=table,
            row=row,
            field=field,
            snapshot=render(normalise(snapshot_value, real=real)),
            cell=render(normalise(cell_value, real=real)),
            note=note,
        )
    ]


def suffix_by(
    rows: Sequence[dict], kept_keys: set, key_column: str, order_column: str
) -> tuple[object | None, object | None]:
    """`(oldest kept, newest dropped)` by `order_column`, for a table the
    export filters to a trailing window. A clean filter is a suffix: every
    dropped row is older than every kept one."""
    kept = [r for r in rows if r.get(key_column) in kept_keys]
    dropped = [r for r in rows if r.get(key_column) not in kept_keys]
    oldest_kept = min((r[order_column] for r in kept), default=None)
    newest_dropped = max((r[order_column] for r in dropped), default=None)
    return oldest_kept, newest_dropped
