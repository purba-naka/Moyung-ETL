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

from excel_etl.engine import FileInput, open_file
from excel_etl.inspect.encode import a1, few
from excel_etl.nodes.base import NodeError, quote, schema_of
from excel_etl.nodes.source import (
    Location,
    expect_hits,
    expect_label,
    locate_all,
    matching_sheets,
    missing_required,
    read_locations,
)
from excel_etl.pipeline import PipelineSpec, Source
from excel_etl.workbook import Workbook, WorkbookError

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
            at = f" at {rec.sheet}!{a1(rec.header_row, rec.first_col)}" if rec else ""
            lines.append(f"  {name}|{s.status}{at}")
            lines += [f"    {d}" for d in (*s.diff, *([s.message] if s.message else []))]
        return "\n".join(lines)


def _record(locs: list[Location], types: dict[str, str]) -> BindRecord:
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
    )


def _covered(source: Source, name: str) -> bool:
    return any(expect_hits(w, [name]) for w in source.expect_columns)


_WIDENS = {("BIGINT", "DOUBLE"), ("DATE", "TIMESTAMP")}


def _judge(
    source: Source, locs: list[Location], old: BindRecord | None, types: dict[str, str]
) -> SourceBind:
    rec = _record(locs, types)
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
        was, now = set(old.columns), set(names)
        added = [n for n in names if n not in was]
        gone = [n for n in old.columns if n not in now]
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
            if (was_type, types[key]) in _WIDENS:
                moved.append(f"column {col!r} type {was_type} -> {types[key]} (safe widening)")
            else:
                drift.append(
                    f"column {col!r} was {was_type}, is now {types[key]}: see the values first "
                    f"(profile_region, sample_data), then add a cast step, or update "
                    f"expect_columns and the steps that read it"
                )
    status: Status = "drift" if drift else "shifted" if moved else "exact"
    return SourceBind(status=status, record=rec, diff=tuple([*drift, *moved]))


def _read_types(
    con: duckdb.DuckDBPyConnection, source: Source, locs: list[Location], name: str
) -> dict[str, str]:
    """The table's column types as the pipeline reads them (`{casefolded column: duckdb type}`).
    Empty when the table cannot be read: the run will fail on it with the real error."""
    try:
        rel = read_locations(locs, source, con, name)
        return {c.name.casefold(): c.type for c in schema_of(rel)}
    except (NodeError, WorkbookError, duckdb.Error):
        return {}
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
            types = _read_types(con, source, locs, name)
            out[name] = _judge(source, locs, previous.get(name), types)
    finally:
        con.close()
    return BindReport(sources=out)


__all__ = ["BindRecord", "BindReport", "SourceBind", "rebind"]
