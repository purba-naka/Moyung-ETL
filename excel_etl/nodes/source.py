"""read_region: turn a spec `Source` into a duckdb table, finding the table by its header text.

Sheet and header text are matched ignoring letter case and spacing; more than one candidate
is an error that lists them. Judging shifted/drift/missing per source is `rebind`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal

import duckdb

from excel_etl.inspect.encode import column_names, few, text_key
from excel_etl.inspect.regions import Region, SheetGrid, detect_regions
from excel_etl.nodes.base import NodeError, Relation, quote
from excel_etl.workbook import Workbook

if TYPE_CHECKING:
    from excel_etl.pipeline import ColumnExpect, Source

type How = Literal["name", "name_like", "fallback"]
_BIGINT = (-(2**63), 2**63 - 1)
_HEADER_SEARCH = 12  # rows from a table's top in which its header may start


def _find_sheet(wb: Workbook, source: Source) -> tuple[str, How]:
    """The sheet and how it was found: "name", "name_like" or "fallback"."""
    ref = source.sheet
    names = [s.name for s in wb.sheets]
    for how, want in (("name", ref.name), ("name_like", ref.name_like)):
        if want is None:
            continue
        hits = [n for n in names if n == want] or [
            n for n in names if text_key(n) == text_key(want)
        ]
        if len(hits) > 1:
            raise NodeError(f"sheet {want!r} matches several sheets: {few(hits, 5)}")
        if hits:
            return hits[0], how
    if ref.fallback_contains:
        keys = [text_key(t) for t in ref.fallback_contains]
        hits = [s.name for s in wb.sheets if _has_all(wb.values(s.name), keys)]
        if len(hits) > 1:
            raise NodeError(
                f"no sheet is named {ref.name or ref.name_like!r}, and {len(hits)} sheets hold "
                f"all of {ref.fallback_contains}: {few(hits, 5)}"
            )
        if hits:
            return hits[0], "fallback"
    wanted = ref.name or ref.name_like or ref.fallback_contains
    raise NodeError(f"no sheet matches {wanted!r}; sheets: {few(names, 8)}")


def _has_all(values, keys: list[str]) -> bool:
    texts = {text_key(v) for row in values for v in row if isinstance(v, str)}
    return all(any(k in t for t in texts) for k in keys)


def _header(wb: Workbook, g: SheetGrid, sheet: str, source: Source) -> tuple[int, Region]:
    """First table whose header rows hold every anchor text, as (header top row, table).

    A text may repeat elsewhere on the sheet (side tables), so each table is judged only by
    the cells inside its own columns.
    """
    rows = source.region.header_rows
    anchors = [text_key(a) for a in source.region.header_anchor]
    found = False
    for reg in detect_regions(wb, sheet, g):
        top, left, bottom, right = reg.box
        for r in range(top, min(bottom, top + _HEADER_SEARCH) + 1):
            texts = {
                text_key(lbl)
                for rr in range(r, r + rows)
                for c in range(left, right + 1)
                if (lbl := g.label(rr, c)) is not None
            }
            if all(a in texts for a in anchors):
                return r, reg
    for r in range(len(g.values)):  # only to tell "not found" from "found outside a table"
        texts = {text_key(lbl) for c in range(len(g.values[r])) if (lbl := g.label(r, c))}
        found = found or all(a in texts for a in anchors)
    where = "but not inside a table" if found else "not found"
    raise NodeError(
        f"header {source.region.header_anchor} {where} on sheet {sheet!r}; "
        "check the texts with find_text and the table with sheet_regions"
    )


def _unique(names: list[str]) -> list[str]:
    # compared without letter case: duckdb treats `Ton` and `TON` as one name and would
    # rename the second to `TON_1` when the table is created
    seen: set[str] = set()
    out = []
    for n in names:
        new, k = n, 1
        while new.casefold() in seen:
            k += 1
            new = f"{n} ({k})"
        seen.add(new.casefold())
        out.append(new)
    return out


def _type(values: list[object]) -> str:
    """duckdb type for a column. Mixed kinds become text, so nothing is lost or guessed."""
    kinds = {type(v) for v in values}
    if not kinds or kinds == {str}:
        return "VARCHAR"
    if kinds == {int} and all(_BIGINT[0] <= v <= _BIGINT[1] for v in values):  # type: ignore[operator]
        return "BIGINT"
    if kinds <= {int, float} and float in kinds:
        return "DOUBLE"
    one = {
        bool: "BOOLEAN",
        time: "TIME",
        timedelta: "INTERVAL",
        date: "DATE",
        datetime: "TIMESTAMP",
    }
    if len(kinds) == 1 and (only := next(iter(kinds))) in one:
        return one[only]
    if kinds == {date, datetime}:
        return "TIMESTAMP"
    return "VARCHAR"


@dataclass(frozen=True)
class Location:
    """Where a source table sits in one workbook. Rows and columns are 0-based."""

    sheet: str
    how: How  # how the sheet was found
    header: int
    left: int
    right: int
    names: tuple[str, ...]  # column names, header levels joined as PARENT|LEAF
    grid: SheetGrid
    last: int  # last row of the table (0-based), by the `end` rule of the source
    below: int  # filled rows after `last`: data the `end` rule cut off
    errors: int  # cells of the table holding an Excel error; they are read as NULL


def locate(wb: Workbook, source: Source) -> Location:
    """Find the sheet and the table of a spec source; no cell values are loaded into duckdb."""
    sheet, how = _find_sheet(wb, source)
    g = SheetGrid(wb, sheet)
    top, reg = _header(wb, g, sheet, source)
    _, left, _, right = reg.box
    hdr = tuple(range(top, top + source.region.header_rows))
    shape = Region(reg.box, hdr, hdr[-1] + 1, (), (), "rows", (), ())
    names = tuple(_unique(column_names(g, shape, sep_safe=False)))
    last, below = _table_end(g, hdr[-1], left, right, source.region.blank_gap)
    bad = wb.error_cells(sheet) or frozenset()
    errors = sum(1 for r, c in bad if hdr[-1] < r <= last and left <= c <= right)
    return Location(sheet, how, top, left, right, names, g, last, below, errors)


def _table_end(g: SheetGrid, header_last: int, left: int, right: int, gap: int) -> tuple[int, int]:
    """(last row of the table, filled rows after it). The table stops at `gap` empty rows."""
    last, blank = header_last, 0
    stop = len(g.values)
    for r in range(header_last + 1, len(g.values)):
        if any((r, c) in g.filled for c in range(left, right + 1)):
            last, blank = r, 0
        else:
            blank += 1
            if blank >= gap:
                stop = r + 1
                break
    below = sum(
        1
        for r in range(stop, len(g.values))
        if any((r, c) in g.filled for c in range(left, right + 1))
    )
    return last, below


def expect_hits(want: ColumnExpect, names: Sequence[str]) -> list[str]:
    """The column names an `expect_columns` entry matches (`match` ignores letter case)."""
    if want.match is not None:
        return [n for n in names if n.casefold() == want.match.casefold()]
    rx = re.compile(want.match_regex or "")
    return [n for n in names if rx.search(n)]


_FITS: dict[str, tuple[str, ...]] = {
    "text": ("VARCHAR",),
    "int": ("BIGINT",),
    "float": ("DOUBLE", "BIGINT"),
    "date": ("DATE",),
    "datetime": ("TIMESTAMP",),
    "bool": ("BOOLEAN",),
}


def type_fits(want: str, actual: str) -> bool:
    """Whether a column read as duckdb type `actual` satisfies an `expect_columns` type."""
    return actual in _FITS[want]


def missing_required(source: Source, names: Sequence[str]) -> list[ColumnExpect]:
    """The `required` expect_columns entries that match none of the table's columns."""
    return [w for w in source.expect_columns if w.required and not expect_hits(w, names)]


def expect_label(want: ColumnExpect) -> str:
    return repr(want.match) if want.match is not None else f"/{want.match_regex}/"


def read_region(
    wb: Workbook, source: Source, con: duckdb.DuckDBPyConnection, name: str
) -> Relation:
    """`locate` the source, then `read_location`."""
    return read_location(locate(wb, source), source, con, name)


def read_location(
    loc: Location, source: Source, con: duckdb.DuckDBPyConnection, name: str
) -> Relation:
    """Load the table a spec source points at as table `name` in `con`, header joined as
    `PARENT|LEAF`. Cell values keep their type when a column has one kind; a column with
    mixed kinds (a `JAN` label among dates) is text. Formulas without a cached value are NULL.
    """
    g, left, right, names = loc.grid, loc.left, loc.right, list(loc.names)
    hdr = tuple(range(loc.header, loc.header + source.region.header_rows))

    last = loc.last
    rows = [g.row(r, left, right) for r in range(hdr[-1] + 1, last + 1)]

    cols = [[row[i] for row in rows] for i in range(len(names))]
    types = [_type([v for v in col if v is not None]) for col in cols]
    # a text column holding other kinds (a `JAN` label among dates) keeps each as its text form
    cols = [
        [v if v is None or isinstance(v, str) else str(v) for v in col] if t == "VARCHAR" else col
        for col, t in zip(cols, types, strict=True)
    ]
    con.execute(f"DROP TABLE IF EXISTS {quote(name)}")
    con.execute(
        f"CREATE TABLE {quote(name)} ("
        + ", ".join(f"{quote(n)} {t}" for n, t in zip(names, types, strict=True))
        + ")"
    )
    if rows:
        # one list parameter per column, unnested: ~100x faster than executemany per row
        con.execute(
            f"INSERT INTO {quote(name)} SELECT " + ", ".join(f"unnest(?::{t}[])" for t in types),
            cols,
        )
    return con.table(name)
