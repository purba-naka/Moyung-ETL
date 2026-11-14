"""Check that a run kept the source's numbers: an independent look, not one the agent wrote.

Checks in the spec test what the agent thought of. This looks at every number column of every
source and asks where it went. A column with non-zero numbers is `used` when an output has it
(same name, its last header level, its name as a value after an unpivot, or the same sum after a
rename), `excluded` when the spec says why it was dropped, else `lost`. A run with a lost column
is `unexplained`, and `plan_load` refuses it.

For columns an output keeps under the same name, a sample of output values must be found in the
source column. A miss is only reported: a unit conversion is a fair reason for one.

ponytail: matching is by name and sum, not by following each step; a column renamed AND
filtered reads as lost (blocks; never lets a loss through). Upgrade: each node reports its
column map in `infer_schema`.
"""

from __future__ import annotations

from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from excel_etl.nodes.base import quote
from excel_etl.pipeline import PipelineSpec

_SAMPLE = 20
_SHOW = 30  # lost/excluded columns listed in the text; the rest are counted


class VerifyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "explained", "unexplained"]
    checked: int  # number columns of the sources that hold non-zero values
    lost: tuple[str, ...] = ()  # "source.column (N non-zero)"
    excluded: tuple[str, ...] = ()  # "source.column: reason"
    values: tuple[str, ...] = ()  # sampled output values not found in the source column

    def text(self) -> str:
        lines = [f"verify: {self.status}, {self.checked} number columns of the sources checked"]
        for title, items in (("LOST (no output has it)", self.lost), ("excluded", self.excluded)):
            if items:
                lines.append(f"  {title}: {len(items)}")
                lines += [f"    {x}" for x in items[:_SHOW]]
                if len(items) > _SHOW:
                    lines.append(f"    ... {len(items) - _SHOW} more")
        lines += [f"  values: {v}" for v in self.values]
        if self.lost:
            lines.append(
                "  keep the lost columns in an output, or list them in the spec's `excluded` "
                "(source -> {column: reason}) for a person to accept"
            )
        return "\n".join(lines)


def _numeric(typ: str) -> bool:
    return typ in ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE", "FLOAT") or (
        typ.startswith("DECIMAL")
    )


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in con.execute(f"DESCRIBE {quote(table)}").fetchall()]


def _sums(con: duckdb.DuckDBPyConnection, table: str, cols: list[str]) -> dict[str, tuple]:
    """column -> (sum, non-zero count), one query for the whole table."""
    if not cols:
        return {}
    parts = ", ".join(
        f"sum(CAST({quote(c)} AS DOUBLE)), count(*) FILTER ({quote(c)} <> 0)" for c in cols
    )
    row = con.execute(f"SELECT {parts} FROM {quote(table)}").fetchall()[0]
    return {c: (row[2 * i] or 0.0, row[2 * i + 1]) for i, c in enumerate(cols)}


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def verify(spec: PipelineSpec, con: duckdb.DuckDBPyConnection) -> VerifyReport:
    """Compare the source tables and output tables still held in `con` after a run."""
    outs = {o.input for o in spec.outputs.values()}
    out_cols = {t: _columns(con, t) for t in outs}
    names = {c.casefold() for cols in out_cols.values() for c, _ in cols}
    out_sums = [
        s
        for t, cols in out_cols.items()
        for s, _ in _sums(con, t, [c for c, typ in cols if _numeric(typ)]).values()
    ]
    text_cols = [(t, c) for t, cols in out_cols.items() for c, typ in cols if typ == "VARCHAR"]

    checked, lost, excluded, values = 0, [], [], []
    for src in spec.sources:
        cols = [c for c, typ in _columns(con, src) if _numeric(typ)]
        reasons = {k.casefold(): v for k, v in spec.excluded.get(src, {}).items()}
        missing: dict[str, int] = {}
        for c, (total, nonzero) in _sums(con, src, cols).items():
            if not nonzero:
                continue  # an empty column carries no data to lose
            checked += 1
            leaf = c.rsplit("|", 1)[-1].strip().casefold()
            if c.casefold() in reasons:
                excluded.append(f"{src}.{c}: {reasons[c.casefold()]}")
            elif c.casefold() not in names and leaf not in names:
                if any(_close(total, s) for s in out_sums):
                    continue  # renamed: the same numbers are there under another name
                missing[c] = nonzero
            elif c.casefold() in names:
                values += _sample(con, src, c, out_cols)
        if missing:
            # after an unpivot the header names are values of a text column
            wanted = {c.casefold(): c for c in missing} | {
                c.rsplit("|", 1)[-1].strip().casefold(): c for c in missing
            }
            for t, col in text_cols:
                rows = con.execute(
                    f"SELECT DISTINCT lower({quote(col)}) FROM {quote(t)} "
                    f"WHERE lower({quote(col)}) IN (SELECT unnest(?::VARCHAR[]))",
                    [list(wanted)],
                ).fetchall()
                for (v,) in rows:
                    missing.pop(wanted[v], None)
            lost += [f"{src}.{c} ({n} non-zero)" for c, n in missing.items()]
    status = "unexplained" if lost else ("explained" if excluded else "ok")
    return VerifyReport(
        status=status,
        checked=checked,
        lost=tuple(lost),
        excluded=tuple(excluded),
        values=tuple(values),
    )


def _sample(
    con: duckdb.DuckDBPyConnection, src: str, col: str, out_cols: dict[str, list[tuple[str, str]]]
) -> list[str]:
    """Output values of `col` that the source column does not hold, as report lines."""
    found = []
    for t, cols in out_cols.items():
        hit = next((c for c, typ in cols if c.casefold() == col.casefold() and _numeric(typ)), None)
        if hit is None:
            continue
        # ORDER BY keeps the sample the same from run to run
        miss = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT CAST({quote(hit)} AS DOUBLE) v "
            f"FROM {quote(t)} WHERE {quote(hit)} IS NOT NULL ORDER BY v LIMIT {_SAMPLE}) "
            f"WHERE v NOT IN (SELECT CAST({quote(col)} AS DOUBLE) FROM {quote(src)} "
            f"WHERE {quote(col)} IS NOT NULL)"
        ).fetchall()[0][0]
        if miss:
            found.append(
                f"{t}.{hit}: {miss} of up to {_SAMPLE} sampled values are not in {src}.{col} "
                "(fine after a unit conversion, otherwise check the steps)"
            )
    return found
