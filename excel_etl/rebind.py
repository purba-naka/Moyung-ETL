"""rebind: find every source of a spec in new files and say whether the pipeline can run as is.

The spec holds anchors, not cell addresses, so "shifted" needs something to compare with: the
`BindRecord` a earlier bind returned. The host keeps the records (the library keeps no state).

  exact    contract holds, table is where it was (or there is no earlier record)
  shifted  contract holds, but the sheet name or the table's position moved: run as is
  drift    the contract broke or the columns changed in a way the spec does not cover: repair
  missing  sheet, header or file not found: look for candidates

ponytail: column types are not compared (a column that turns from numbers to text is not
drift yet); upgrade: store a type per column in the record and diff it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from excel_etl.engine import FileRef, open_file
from excel_etl.inspect.encode import _few, a1
from excel_etl.nodes.base import NodeError
from excel_etl.nodes.source import Location, expect_hits, expect_label, locate
from excel_etl.pipeline import PipelineSpec, Source
from excel_etl.workbook import Workbook, WorkbookError

type Status = Literal["exact", "shifted", "drift", "missing"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class BindRecord(_Model):
    """Where a source's table was found. Rows and columns are 0-based."""

    sheet: str
    header_row: int
    first_col: int
    columns: tuple[str, ...]


class SourceBind(_Model):
    status: Status
    record: BindRecord | None  # None when missing
    diff: tuple[str, ...]  # what changed against the earlier record, and what broke
    message: str = ""  # why it is missing


class BindReport(_Model):
    sources: dict[str, SourceBind]

    @property
    def ok(self) -> bool:
        """True when the pipeline can run as is (every source exact or shifted)."""
        return all(s.status in ("exact", "shifted") for s in self.sources.values())

    def records(self) -> dict[str, BindRecord]:
        """The records to hand to the next `rebind` as `previous`."""
        return {k: s.record for k, s in self.sources.items() if s.record is not None}

    def text(self) -> str:
        lines = [f"rebind: {'ok' if self.ok else 'needs repair'}"]
        for name, s in self.sources.items():
            rec = s.record
            at = f" at {rec.sheet}!{a1(rec.header_row, rec.first_col)}" if rec else ""
            lines.append(f"  {name}|{s.status}{at}")
            lines += [f"    {d}" for d in (*s.diff, *([s.message] if s.message else []))]
        return "\n".join(lines)


def _record(loc: Location) -> BindRecord:
    return BindRecord(sheet=loc.sheet, header_row=loc.header, first_col=loc.left, columns=loc.names)


def _covered(source: Source, name: str) -> bool:
    return any(expect_hits(w, [name]) for w in source.expect_columns)


def _judge(source: Source, loc: Location, old: BindRecord | None) -> SourceBind:
    rec = _record(loc)
    drift: list[str] = []
    moved: list[str] = []
    for want in source.expect_columns:
        if not expect_hits(want, loc.names) and want.required:
            drift.append(f"required column {expect_label(want)} not found")
    if loc.how == "fallback" and (old is None or old.sheet != loc.sheet):
        drift.append(f"sheet not found by name; {loc.sheet!r} found by its content")
    if old is not None:
        if old.sheet != loc.sheet and loc.how != "fallback":
            moved.append(f"sheet {old.sheet!r} -> {loc.sheet!r}")
        if (old.header_row, old.first_col) != (loc.header, loc.left):
            moved.append(
                f"header {a1(old.header_row, old.first_col)} -> {a1(loc.header, loc.left)}"
            )
        was, now = set(old.columns), set(loc.names)
        added = [n for n in loc.names if n not in was]
        gone = [n for n in old.columns if n not in now]
        for label, names in (("added", added), ("removed", gone)):
            if names:
                moved.append(f"columns {label}: {_few(names, 6)}")
                loose = [n for n in names if not _covered(source, n)]
                if loose:
                    drift.append(
                        f"columns {label} that no expect_columns entry covers: {_few(loose, 6)}"
                    )
    status: Status = "drift" if drift else "shifted" if moved else "exact"
    return SourceBind(status=status, record=rec, diff=tuple([*drift, *moved]))


def rebind(
    spec: PipelineSpec,
    files: dict[str, FileRef],
    previous: dict[str, BindRecord] | None = None,
) -> BindReport:
    """Find each source of `spec` in `files` (file_role -> path, bytes or Workbook).

    `previous` is the `records()` of an earlier report, to tell shifted from exact and to
    spot columns that came or went. Without it only the contract and the sheet are judged.
    Nothing is run and nothing is raised for a bad file: it shows up as `missing`.
    """
    previous = previous or {}
    books: dict[str, Workbook | str] = {}  # a str is the reason a file cannot be used
    out: dict[str, SourceBind] = {}
    for name, source in spec.sources.items():
        role = source.file_role
        if role not in books:
            if role not in files:
                known = ", ".join(sorted(files)) or "none"
                books[role] = f"no file for file_role {role!r}; files given: {known}"
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
            loc = locate(wb, source)
        except (NodeError, WorkbookError) as e:
            out[name] = SourceBind(status="missing", record=None, diff=(), message=str(e))
            continue
        out[name] = _judge(source, loc, previous.get(name))
    return BindReport(sources=out)


__all__ = ["BindRecord", "BindReport", "SourceBind", "rebind"]
