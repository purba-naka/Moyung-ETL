"""rebind: find every source of a spec in new files and say whether the pipeline can run as is.

The spec holds anchors, not cell addresses, so "shifted" needs something to compare with: the
`BindRecord` an earlier bind returned. The host keeps the records (the library keeps no state).

  exact    contract holds, table is where it was (or there is no earlier record)
  shifted  contract holds, but the sheet name, the table's position or a column's type moved:
           safe widening only (BIGINT -> DOUBLE, DATE -> TIMESTAMP): run as is
  drift    the contract broke, the columns changed in a way the spec does not cover, or a
           column's type narrowed or turned into text: repair
  missing  sheet, header or file not found: look for candidates

Each record stores the duckdb type of every column, read the same way the pipeline reads it,
so a column that turned from numbers into text is caught here, before a run fails on a cast.
"""

from __future__ import annotations

from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from moyung.engine import FileInput, open_file
from moyung.inspect.encode import a1, few
from moyung.nodes.base import NodeError, quote, schema_of
from moyung.nodes.source import (
    Location,
    expect_hits,
    expect_label,
    locate_all,
    matching_sheets,
    missing_required,
    read_locations,
    read_table_file,
)
from moyung.pipeline import PipelineSpec, Source
from moyung.tablefile import TableFileError, as_table_file
from moyung.workbook import Workbook, WorkbookError

type Status = Literal["exact", "shifted", "drift", "missing"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class BindRecord(_Model):
    """Where a source's table was found. Rows and columns are 0-based. `types` maps a
    casefolded column name to the duckdb type the pipeline reads it as."""

    sheet: str
    header_row: int
    first_col: int
    columns: tuple[str, ...]
    types: dict[str, str] = {}
    sheets: int = 1
    sheet_columns: dict[str, tuple[str, ...]] = {}
    rows: int | None = None


class SourceBind(_Model):
    status: Status
    record: BindRecord | None
    diff: tuple[str, ...]
    message: str = ""


class BindReport(_Model):
    sources: dict[str, SourceBind]

    @property
    def ok(self) -> bool:
        """True when the pipeline can run as is (every source exact or shifted)."""
        return all(s.status in ("exact", "shifted") for s in self.sources.values())

    def accepted_records(self) -> dict[str, BindRecord]:
        """The records of every source that was found, drifted or not: what a person who
        approved the drift wants remembered as the new normal."""
        return {k: s.record for k, s in self.sources.items() if s.record is not None}

    def records(self) -> dict[str, BindRecord]:
        """The records to hand to the next `rebind` as `previous`. Sources that drifted or
        are missing are left out: a drifted place is not one to remember."""
        return {
            k: s.record
            for k, s in self.sources.items()
            if s.record is not None and s.status in ("exact", "shifted")
        }

    def text(self) -> str:
        lines = [f"rebind: {'ok' if self.ok else 'needs repair'}"]
        for name, s in self.sources.items():
            rec = s.record
            at = f" at {rec.sheet}!{a1(rec.header_row, rec.first_col)}" if rec and rec.sheet else ""
            lines.append(f"  {name}|{s.status}{at}")
            lines += [f"    {d}" for d in (*s.diff, *([s.message] if s.message else []))]
        return "\n".join(lines)


def _record(locs: list[Location], types: dict[str, str], rows: int | None) -> BindRecord:
    first = locs[0]
    columns_by_key: dict[str, str] = {}
    for loc in locs:
        for col in loc.names:
            columns_by_key.setdefault(col.casefold(), col)
    columns = tuple(columns_by_key.values())
    return BindRecord(
        sheet=first.sheet,
        header_row=first.header,
        first_col=first.left,
        columns=columns,
        types={c.casefold(): types[c.casefold()] for c in columns if c.casefold() in types},
        sheets=len(locs),
        sheet_columns={loc.sheet: tuple(loc.names) for loc in locs},
        rows=rows,
    )


def _covered(source: Source, name: str) -> bool:
    return any(expect_hits(w, [name]) for w in source.expect_columns)


_SAFE_CHANGES = {
    ("BIGINT", "DOUBLE"): "safe widening",
    ("DATE", "TIMESTAMP"): "safe widening",
    ("DOUBLE", "BIGINT"): "every value is a whole number this time; nothing is lost",
}


def _compare(
    source: Source,
    old: BindRecord,
    names: tuple[str, ...],
    types: dict[str, str],
    drift: list[str],
    moved: list[str],
) -> None:
    """What changed in the columns since the last bind: names that came or went, and types."""
    was = {n.casefold(): n for n in old.columns}
    now = {n.casefold(): n for n in names}
    added = [n for n in names if n.casefold() not in was]
    gone = [n for n in old.columns if n.casefold() not in now]
    recased = [f"{was[k]!r} -> {n!r}" for k, n in now.items() if k in was and was[k] != n]
    if recased:
        moved.append(f"columns whose letter case changed: {few(recased, 6)}")
    for label, cols in (("added", added), ("removed", gone)):
        if cols:
            moved.append(f"columns {label}: {few(cols, 6)}")
            loose = [n for n in cols if not _covered(source, n)]
            if loose:
                drift.append(
                    f"columns {label} that no expect_columns entry covers: {few(loose, 6)}; "
                    f"decide for each: add it to the steps that need it (a group_by agg, a "
                    f"select), or add an expect_columns entry to accept it as it is"
                )
    for key, was_type in old.types.items():
        if key not in types or was_type == types[key]:
            continue
        col = next((n for n in names if n.casefold() == key), key)
        if why := _SAFE_CHANGES.get((was_type, types[key])):
            moved.append(f"column {col!r} type {was_type} -> {types[key]} ({why})")
        else:
            drift.append(
                f"column {col!r} was {was_type}, is now {types[key]}: see the values first "
                f"(profile_region, sample_data). A cast that turns unreadable values into "
                f"NULL drops them silently: ask the person before casting. Otherwise "
                f"update expect_columns and the steps that read it"
            )


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0])


def _compare_sheets(
    source: Source, old: BindRecord, locs: list[Location], drift: list[str]
) -> None:
    """A source read from several same-shaped sheets: a sheet that went, or lost a column the
    union of all sheets still has, would otherwise change the data without a word."""
    if old.sheets <= 1 and len(locs) <= 1:
        return
    now = {loc.sheet: loc.names for loc in locs}
    if not old.sheet_columns:
        if old.sheets > len(locs):
            drift.append(
                f"{old.sheets} sheets matched this source before, {len(locs)} now: the pipeline "
                "reads fewer tables; check the file"
            )
        return
    gone = [s for s in old.sheet_columns if s not in now]
    if gone:
        drift.append(
            f"sheet(s) {few(gone, 6)} no longer match this source, so their rows are missing "
            "from the result; check the file, or accept the change"
        )
    for sheet, names in now.items():
        was = old.sheet_columns.get(sheet)
        if was is None:
            continue
        have = {n.casefold() for n in names}
        lost = [c for c in was if c.casefold() not in have and not _covered(source, c)]
        if lost:
            drift.append(
                f"sheet {sheet!r} lost column(s) {few(lost, 6)}: its rows would be empty there; "
                "check the file, or add an expect_columns entry to accept it"
            )


_DROP = 0.5
_DROP_MIN_ROWS = 20


def _compare_rows(old: BindRecord, rows: int | None, drift: list[str], moved: list[str]) -> None:
    if rows is None or old.rows is None or old.rows == 0:
        return
    if rows == 0:
        drift.append(f"the table had {old.rows} rows and now has none: check the file")
    elif old.rows >= _DROP_MIN_ROWS and rows < old.rows * _DROP:
        moved.append(f"row count fell from {old.rows} to {rows}")


def _judge(
    source: Source,
    locs: list[Location],
    old: BindRecord | None,
    types: dict[str, str],
    rows: int | None,
) -> SourceBind:
    rec = _record(locs, types, rows)
    if old is not None:
        present = {c.casefold() for c in rec.columns}
        kept = {k: t for k, t in old.types.items() if k in present and k not in rec.types}
        if kept or not types:
            rec = rec.model_copy(update={"types": {**kept, **rec.types} if types else old.types})
    loc = locs[0]
    names = rec.columns
    drift: list[str] = []
    moved: list[str] = []
    for want in missing_required(source, names):
        drift.append(
            f"required column {expect_label(want)} not found: find the new header text "
            f"(find_text, render_range), then change expect_columns and the steps that use it"
        )
    if loc.how == "fallback" and (old is None or old.sheet != loc.sheet):
        drift.append(
            f"sheet not found by name; {loc.sheet!r} found by its content: check it is the same "
            f"table (compare_sheets), then set sheet.name to it"
        )
    if old is not None:
        if old.sheet != loc.sheet and loc.how not in ("fallback", "name_regex"):
            moved.append(f"sheet {old.sheet!r} -> {loc.sheet!r}")
        if (old.header_row, old.first_col) != (loc.header, loc.left):
            moved.append(
                f"header {a1(old.header_row, old.first_col)} -> {a1(loc.header, loc.left)}"
            )
        _compare(source, old, names, types, drift, moved)
        _compare_sheets(source, old, locs, drift)
        _compare_rows(old, rows, drift, moved)
    status: Status = "drift" if drift else "shifted" if moved else "exact"
    return SourceBind(status=status, record=rec, diff=tuple([*drift, *moved]))


def _bind_table(
    con: duckdb.DuckDBPyConnection,
    name: str,
    source: Source,
    files: dict[str, FileInput],
    old: BindRecord | None,
) -> SourceBind:
    """Bind a CSV or Parquet source: there is no place to find, so only the columns and their
    types are judged against the last bind."""
    role = source.file_role
    if role not in files:
        known = ", ".join(sorted(files)) or "none"
        return SourceBind(
            status="missing",
            record=None,
            diff=(),
            message=f"no file for file_role {role!r}; files given: {known}",
        )
    try:
        tf = as_table_file(files[role], source.kind)
        rel = read_table_file(tf, source, con, name)
        schema = schema_of(rel)
        rows = _count(con, name)
    except (TableFileError, NodeError, duckdb.Error, TypeError) as e:
        return SourceBind(
            status="missing",
            record=None,
            diff=(),
            message=f"file {role!r}: {e}",
        )
    finally:
        con.execute(f"DROP TABLE IF EXISTS {quote(name)}")
    names = tuple(c.name for c in schema)
    types = {c.name.casefold(): c.type for c in schema}
    rec = BindRecord(sheet="", header_row=0, first_col=0, columns=names, types=types, rows=rows)
    drift: list[str] = [
        f"required column {expect_label(want)} not found: check the file's header, then change "
        f"expect_columns and the steps that use it"
        for want in missing_required(source, names)
    ]
    moved: list[str] = []
    if old is not None:
        _compare(source, old, names, types, drift, moved)
        _compare_rows(old, rows, drift, moved)
    status: Status = "drift" if drift else "shifted" if moved else "exact"
    return SourceBind(status=status, record=rec, diff=tuple([*drift, *moved]))


def _read_types(
    con: duckdb.DuckDBPyConnection, source: Source, locs: list[Location], name: str
) -> tuple[dict[str, str], int | None]:
    """The table's column types as the pipeline reads them (`{casefolded column: duckdb type}`)
    and its row count. Empty and None when the table cannot be read: the run will fail on it
    with the real error."""
    try:
        rel = read_locations(locs, source, con, name)
        schema = schema_of(rel)
        if not schema:
            return {}, _count(con, name)
        filled = con.execute(
            "SELECT "
            + ", ".join(f"count({quote(c.name)})" for c in schema)
            + f" FROM {quote(name)}"
        ).fetchall()[0]
        types = {c.name.casefold(): c.type for c, n in zip(schema, filled, strict=True) if n}
        return types, _count(con, name)
    except (NodeError, WorkbookError, duckdb.Error):
        return {}, None
    finally:
        con.execute(f"DROP TABLE IF EXISTS {quote(name)}")


def rebind(
    spec: PipelineSpec,
    files: dict[str, FileInput],
    previous: dict[str, BindRecord] | None = None,
) -> BindReport:
    """Find each source of `spec` in `files` (file_role -> path, bytes or Workbook).

    `previous` is the `records()` of an earlier report, to tell shifted from exact and to
    spot columns that came or went, and to diff each column's type against the last bind.
    Without it only the contract and the sheet are judged. Nothing is raised for a bad file:
    it shows up as `missing`.
    """
    previous = previous or {}
    bad_files = None if isinstance(files, dict) else "files must be a dict of file_role -> file"
    if bad_files:
        files = {}
    books: dict[str, Workbook | str] = {}
    out: dict[str, SourceBind] = {}
    con = duckdb.connect()
    try:
        for name, source in spec.sources.items():
            role = source.file_role
            if source.kind != "excel":
                out[name] = _bind_table(con, name, source, files, previous.get(name))
                continue
            if role not in books:
                if role not in files:
                    known = ", ".join(sorted(files)) or "none"
                    books[role] = bad_files or (
                        f"no file for file_role {role!r}; files given: {known}"
                    )
                else:
                    try:
                        books[role] = open_file(files[role])
                    except (WorkbookError, OSError, TypeError) as e:
                        books[role] = f"file {role!r}: {e}"
            wb = books[role]
            if isinstance(wb, str):
                out[name] = SourceBind(status="missing", record=None, diff=(), message=wb)
                continue
            try:
                locs = locate_all(wb, source)
            except (NodeError, WorkbookError) as e:
                matched = bool(matching_sheets(wb, source))
                out[name] = SourceBind(
                    status="drift" if matched else "missing",
                    record=None,
                    diff=(str(e),) if matched else (),
                    message="" if matched else str(e),
                )
                continue
            types, rows = _read_types(con, source, locs, name)
            out[name] = _judge(source, locs, previous.get(name), types, rows)
    finally:
        con.close()
    return BindReport(sources=out)


__all__ = ["BindRecord", "BindReport", "SourceBind", "rebind"]
