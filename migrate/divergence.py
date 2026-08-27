"""What the verifier reports when the two sides disagree.

One divergence names four things: the user, the table, the row and the
field. A count that differs is still reported per row, by naming the ids
one side holds and the other does not, because "reviews: 4999 != 5000"
sends the operator back to SQL to find out which one.

Values are rendered on the way in, so a report line carries the exact
bytes that differed. A REAL also carries its IEEE-754 pattern: two
doubles can print the same and not be the same, and the migration is a
copy, so any difference at all is a defect.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

# The row identity used when a check is about the table rather than a row.
TABLE_SCOPE = "-"


def float_bits(value: float) -> str:
    """The big-endian IEEE-754 pattern, hex. Bit equality is the oracle."""
    return struct.pack(">d", value).hex()


def render(value: object) -> str:
    """A value as the report prints it. Floats carry their bit pattern;
    `repr` alone would collapse two distinct doubles onto one line."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value!r} (bits {float_bits(value)})"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def row_key(key_columns: tuple[str, ...], row: dict) -> str:
    """A row's identity as the report prints it: `question_id=41`, or
    `session_id='s1',question_id=41` for a composite key."""
    return ",".join(
        f"{c}={row.get(c)!r}" if isinstance(row.get(c), str) else f"{c}={row.get(c)}"
        for c in key_columns
    )


@dataclass(frozen=True)
class Divergence:
    """One disagreement, fully addressed."""

    tier: int
    table: str
    row: str
    field: str
    snapshot: str
    cell: str
    user: str | None = None
    note: str = ""

    def line(self) -> str:
        where = f"user={self.user} " if self.user is not None else ""
        head = f"tier{self.tier} {where}table={self.table} row={self.row} field={self.field}"
        body = f"  snapshot: {self.snapshot}\n  cell:     {self.cell}"
        return f"{head}\n{body}" + (f"\n  note:     {self.note}" if self.note else "")

    def as_json(self) -> dict:
        return {
            "tier": self.tier,
            "user": self.user,
            "table": self.table,
            "row": self.row,
            "field": self.field,
            "snapshot": self.snapshot,
            "cell": self.cell,
            "note": self.note,
        }


_BITS = re.compile(r"\(bits ([0-9a-f]{16})\)")


def ulp_gap(snapshot: str, cell: str) -> int | None:
    """How many representable doubles separate two rendered floats, or None
    if either side is not a float. Adjacent doubles differ by 1: celld's
    SQLite bridge shifts that last bit on some values, and only that shift
    is waivable."""
    a, b = _BITS.search(snapshot), _BITS.search(cell)
    if not a or not b:
        return None
    x, y = int(a.group(1), 16), int(b.group(1), 16)

    def ordered(v: int) -> int:
        """Sign-magnitude to a monotone ordering, so adjacency holds across zero."""
        return -(v & 0x7FFFFFFFFFFFFFFF) if v >> 63 else v

    return abs(ordered(x) - ordered(y))


@dataclass
class Report:
    """The verifier's whole answer. `checks` counts what was actually
    compared, so a run that silently examined nothing cannot read as
    clean."""

    divergences: list[Divergence] = field(default_factory=list)
    # Tier-2 float divergences the operator waived: celld's SQLite bridge
    # shifts the last bit of some doubles. Reported, never hidden, but they
    # do not make a run dirty because tier 3 proves no schedule moved.
    waived: list[Divergence] = field(default_factory=list)
    # Abort criteria the runbook gates on separately, reported here because
    # the verifier is already looking. They do not mean the copy is
    # unfaithful, so they do not make a run dirty.
    warnings: list[Divergence] = field(default_factory=list)
    checks: dict[str, int] = field(default_factory=dict)
    users: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, *found: Divergence) -> None:
        self.divergences.extend(found)

    def waive_ulp(self) -> int:
        """Move single-ULP tier-2 float divergences out of the dirty set.
        A gap wider than one representable double is a real corruption and
        stays, which is what keeps the waiver from swallowing a bug."""
        kept: list[Divergence] = []
        for d in self.divergences:
            if d.tier == 2 and ulp_gap(d.snapshot, d.cell) == 1:
                self.waived.append(d)
            else:
                kept.append(d)
        self.divergences = kept
        return len(self.waived)

    def warn(self, *found: Divergence) -> None:
        self.warnings.extend(found)

    def counted(self, what: str, n: int = 1) -> None:
        self.checks[what] = self.checks.get(what, 0) + n

    @property
    def clean(self) -> bool:
        return not self.divergences

    def warning_text(self) -> str:
        return "\n".join(f"WARNING (abort criterion)\n{d.line()}" for d in self.warnings)

    def text(self, limit: int | None = None) -> str:
        if self.clean:
            checks = ", ".join(f"{k}={v}" for k, v in sorted(self.checks.items()))
            head = f"clean: {len(self.users)} users, {checks}"
            if self.waived:
                head += f"\n  ({len(self.waived)} tier2 single-ULP float differences waived; see the JSON report)"
            return head
        shown = self.divergences if limit is None else self.divergences[:limit]
        lines = [d.line() for d in shown]
        if limit is not None and len(self.divergences) > limit:
            lines.append(
                f"... {len(self.divergences) - limit} further divergences, all in the JSON report"
            )
        by_tier: dict[int, int] = {}
        for d in self.divergences:
            by_tier[d.tier] = by_tier.get(d.tier, 0) + 1
        tally = ", ".join(f"tier{t}={n}" for t, n in sorted(by_tier.items()))
        return "\n".join([*lines, "", f"NOT CLEAN: {len(self.divergences)} divergences ({tally})"])

    def as_json(self) -> dict:
        return {
            "clean": self.clean,
            "users": self.users,
            "checks": self.checks,
            "notes": self.notes,
            "warnings": [d.as_json() for d in self.warnings],
            "divergences": [d.as_json() for d in self.divergences],
            "waived": [d.as_json() for d in self.waived],
        }
