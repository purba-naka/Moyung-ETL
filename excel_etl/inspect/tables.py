"""Text views that read a table into duckdb the way a pipeline does: full profile, sample rows,
and which columns of two tables share values. They live apart from `encode` because they need
duckdb, `profile`, `sample` and `relate`, which `encode` must not import.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from excel_etl.inspect.encode import (
    _box,
    _check_budget,
    _clip,
    _fit,
    _fmt,
    _name,
    _paging,
    _region,
    _rows,
    few,
)
from excel_etl.nodes.base import NodeError, connect, quote
from excel_etl.pipeline import Source
from excel_etl.profile import column_line, profile_table
from excel_etl.relate import relate
from excel_etl.sample import sample_table
from excel_etl.workbook import Workbook, WorkbookError


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_region(wb: Workbook, file_id: str, ref: str, con: duckdb.DuckDBPyConnection) -> str:
    """Read one detected table into `con` as it is when a pipeline reads it (same column names
    and types), and return the table name. Raises WorkbookError for a note or KPI block."""
    from excel_etl.nodes.source import locate_all, read_locations
    from excel_etl.synth import source_from_region

    label, *_ = _region(wb, file_id, ref)
    source = Source.model_validate(source_from_region(wb, file_id, ref, "profile"))
    try:
        read_locations(locate_all(wb, source), source, con, "profiled")
    except (NodeError, duckdb.Error) as e:
        raise WorkbookError(f"{label}: {str(e).splitlines()[0]}") from None
    return "profiled"


def profile_full(wb: Workbook, file_id: str, ref: str, offset: int, budget: int) -> dict:
    label, _, region, _ = _region(wb, file_id, ref)
    con = connect()
    try:
        table = load_region(wb, file_id, ref, con)
        prof = profile_table(con, table)
    finally:
        con.close()
    when = _stamp()
    head = (
        f"{label} {_box(region.box)}: {prof['rows']} data rows, {len(prof['columns'])} columns, "
        f"duplicate rows {prof['duplicate_rows']}, basis all rows, sha {wb.sha256[:12]}, at {when}"
        + (
            f"; total rows {_rows(sorted(region.total_rows), 6)} included in these numbers "
            "(a pipeline reads them as data too)"
            if region.total_rows
            else ""
        )
    )
    keys = prof["key_candidates"]
    if prof["rows"]:
        head += "; key candidates: " + (
            ", ".join("+".join(_name(n) for n in k) for k in keys[:5]) if keys else "none"
        )
    body = [_clip(f"{c['name']}: {column_line(c, prof['rows'])}") for c in prof["columns"]]
    if not prof["rows"]:
        body = [f"{c['name']}: no data" for c in prof["columns"]]
    return _fit(head, body, offset, budget)


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return _name(v)
    return _fmt(v)


def sample_data(
    wb: Workbook,
    file_id: str,
    ref: str,
    strategy: str = "head",
    n: int = 10,
    seed: int = 0,
    column: str = "",
    where: str = "",
    offset: int = 0,
    budget: int = 24_000,
) -> dict:
    """Show real rows of one table (region id from sheet_regions), picked by a strategy, to
    check a guess before writing a step. Use after profile_region.

    strategy: head, tail, head_tail (half each), random (same rows for the same `seed`),
    stratified (up to n rows per value of `column`; the first line lists group sizes),
    where (rows where the condition `where` is true, such as `"amount" < 0`; same rules as the
    filter step), nulls (rows where `column` is empty), duplicates (rows that occur more than
    once). `n` is rows (1-1000; per group for stratified). A strategy the table cannot serve
    is an error. Rows are read as a pipeline reads them; lines are `a|b|c` under the column
    names. The first line says what was picked, how many rows matched, the file hash and time.
    `budget` caps the text in chars; `offset` continues a page of rows.
    """
    _paging(offset)
    _check_budget(budget)
    label, _, region, _ = _region(wb, file_id, ref)
    con = connect()
    try:
        table = load_region(wb, file_id, ref, con)
        got = sample_table(con, table, strategy, n, seed, column or None, where or None)
    except NodeError as e:
        raise WorkbookError(str(e)) from None
    finally:
        con.close()
    when = _stamp()
    head = f"{label} {_box(region.box)}: {got['basis']}, sha {wb.sha256[:12]}, at {when}"
    if "groups" in got:
        sizes = ",".join(f"{_name(k)}:{v}" for k, v in got["groups"].items())
        head += f"; group sizes {few(sizes.split(','), 12)}"
    body = ["|".join(_name(c) for c in got["columns"])]
    body += [_clip("|".join(_cell(v) for v in row)) for row in got["rows"]]
    return _fit(head, body, offset, budget)


def relate_tables(
    wb_a: Workbook,
    file_a: str,
    ref_a: str,
    wb_b: Workbook,
    file_b: str,
    ref_b: str,
    offset: int = 0,
    budget: int = 24_000,
) -> dict:
    """Which columns of two tables share values: candidates for a join. Read both the way a
    pipeline reads them; columns are paired only within a type family (numbers, text, dates).

    One line per pair, best first: `cust_id -> id: N:1, shared 2 values; 1 of 3 cust_id values
    not in id, 2 of 4 id values not in cust_id`. Cardinality N:1 = many rows of the first
    table per row of the second (a lookup); N:M means both sides repeat and a join multiplies
    rows. `+n after trim/case` = more values match when spaces and letter case are ignored.
    `X lacking: 'a', 'b'` = a few values of column X the other column does not hold.
    Evidence only: confirm a join with the person before writing it.
    """
    _paging(offset)
    _check_budget(budget)
    label_a, _, _, _ = _region(wb_a, file_a, ref_a)
    label_b, _, _, _ = _region(wb_b, file_b, ref_b)
    if (wb_a.sha256, ref_a) == (wb_b.sha256, ref_b):
        raise WorkbookError("relate_tables needs two different tables")
    con = connect()
    try:
        con.execute(f"ALTER TABLE {quote(load_region(wb_a, file_a, ref_a, con))} RENAME TO __a")
        con.execute(f"ALTER TABLE {quote(load_region(wb_b, file_b, ref_b, con))} RENAME TO __b")
        rows_a = con.execute("SELECT count(*) FROM __a").fetchall()[0][0]
        rows_b = con.execute("SELECT count(*) FROM __b").fetchall()[0][0]
        got = relate(con, "__a", "__b")
    finally:
        con.close()
    when = _stamp()
    shas = sorted({wb_a.sha256[:12], wb_b.sha256[:12]})
    head = (
        f"{label_a} ({rows_a} rows) vs {label_b} ({rows_b} rows): {len(got['pairs'])} column "
        f"pair(s) share values"
        + (
            f" (compared {got['checked']} of {got['pairs_total']} pairs)"
            if got["checked"] < got["pairs_total"]
            else ""
        )
        + f", sha {','.join(shas)}, at {when}"
    )
    body = [_pair_line(p) for p in got["pairs"]]
    if not body:
        body = ["no column pair shares a value (exact or ignoring spaces and letter case)"]
    return _fit(head, body, offset, budget)


def _pair_line(p: dict) -> str:
    a, b = _name(p["a"]), _name(p["b"])
    line = (
        f"{a} -> {b}: {p['cardinality']}, shared {p['overlap']} values; "
        f"{p['a_missing']} of {p['a_distinct']} {a} values not in {b}, "
        f"{p['b_missing']} of {p['b_distinct']} {b} values not in {a}"
    )
    if (more := p["overlap_folded"] - p["overlap"]) > 0:
        line += f"; +{more} after trim/case"
    for side, name in (("a_unmatched", a), ("b_unmatched", b)):
        if p[side]:
            shown = ", ".join(repr(_name(v)) for v in p[side])
            line += f"; {name} lacking: {shown}"
    return _clip(line)
