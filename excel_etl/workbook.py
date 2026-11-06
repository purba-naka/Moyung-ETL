"""One handle per Excel file: fast values from calamine, formulas and styles from openpyxl.

Coordinates are 0-based (row, col) everywhere, the same as calamine.
"""

from __future__ import annotations

import hashlib
import html
import io
import os
import posixpath
import re
import warnings
import zipfile
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

import openpyxl
from python_calamine import CalamineError, CalamineWorkbook, SheetVisibleEnum

type Cell = str | int | float | bool | date | datetime | time | timedelta | None
type Grid = tuple[tuple[Cell, ...], ...]
type Visibility = Literal["visible", "hidden", "very_hidden"]


def _visibility(v: SheetVisibleEnum) -> Visibility:
    # calamine's enum is a Rust type: no .name and not hashable, so it cannot be a dict key.
    if v == SheetVisibleEnum.Visible:
        return "visible"
    return "hidden" if v == SheetVisibleEnum.Hidden else "very_hidden"


_CACHE_SIZE = 4
# calamine allocates the whole A1..last-cell rectangle, and aborts the process (not an
# exception) when it is huge, e.g. one value at XFD1048576. So we measure it in the XML first.
_MAX_CELLS = 25_000_000
_MAX_PART = 400_000_000  # unpacked bytes of one XML part; guards against zip bombs
_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, ET.ParseError, KeyError)
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# A cell with a first child tag holds something: f = formula, v = value, i = inline string.
# Self-closing <c .../> never matches because the next tag after it is <c> or </row>.
# ponytail: assumes the default namespace (no "x:" prefix), which is what Excel and openpyxl
# write, and that `r=` is the first attribute of <c>; upgrade: an iterparse scan if a
# producer breaks either.
_CELL = re.compile(rb'<c r="([A-Z]+)(\d+)"[^>]*><([fvi])')
# A formula cell: group 3 = <f> attributes, group 4 = formula text (empty for a shared child).
# Formula text never holds a raw `<` (it is written as &lt;), so [^<]* ends at </f> without the
# backtracking a lazy `.*?` would do over 100 MB of XML.
_FORMULA = re.compile(rb'<c r="([A-Z]+)(\d+)"[^>]*><f([^>]*?)(?:/>|>([^<]*)</f>)')
_SHARED_ID = re.compile(rb'\bsi="(\d+)"')


def _read_part(z: zipfile.ZipFile, name: str) -> bytes:
    if z.getinfo(name).file_size > _MAX_PART:
        raise WorkbookError(f"{name} is larger than {_MAX_PART // 10**6} MB unpacked")
    return z.read(name)


def _sheet_paths(data: bytes) -> dict[str, str]:
    """Sheet name -> worksheet XML path inside the zip. Chartsheets are left out."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        book = ET.fromstring(_read_part(z, "xl/workbook.xml"))
        rels = ET.fromstring(_read_part(z, "xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{_NS_PKG}Relationship")}
    out: dict[str, str] = {}
    for s in book.iter(f"{_NS_MAIN}sheet"):
        t = target.get(s.get(f"{_NS_REL}id"), "")
        # Targets are usually relative to xl/, but some writers use an absolute "/xl/..." path.
        path = t.lstrip("/") if t.startswith("/") else posixpath.normpath(f"xl/{t}")
        if "worksheets/" in path:
            out[s.get("name", "")] = path
    return out


def col_index(letters: str | bytes) -> int:
    """Excel column letters -> 0-based index (A -> 0, AA -> 26)."""
    n = 0
    for ch in letters.encode() if isinstance(letters, str) else letters:
        n = n * 26 + ch - 64
    return n - 1


class WorkbookError(ValueError):
    """A problem with the input file or a bad sheet name. Tools turn this into an error dict."""


@dataclass(frozen=True, slots=True)
class _Scan:
    formulas: frozenset[tuple[int, int]]
    cells: int  # size of the A1..last-cell rectangle, which is what calamine allocates


def _scan_sheet(xml: bytes) -> _Scan:
    formulas: set[tuple[int, int]] = set()
    letters: dict[bytes, int] = {}
    max_row = max_col = 0
    for m in _CELL.finditer(xml):
        row = int(m[2])
        col = letters.get(m[1])
        if col is None:
            col = letters[m[1]] = col_index(m[1])
        if m[3] == b"f":
            formulas.add((row - 1, col))
        max_row, max_col = max(max_row, row), max(max_col, col + 1)
    return _Scan(frozenset(formulas), max_row * max_col)


def _formula_texts(xml: bytes):
    shared: dict[bytes, str] = {}  # shared-formula id -> text of its first cell
    for m in _FORMULA.finditer(xml):
        body = m[4]
        si = _SHARED_ID.search(m[3]) if b't="shared"' in m[3] else None
        if body:
            text = body.decode("utf-8", "replace")
            if "&" in text:
                text = html.unescape(text)
            if si:
                shared[si[1]] = text
        elif si and si[1] in shared:
            text = shared[si[1]]
        else:
            continue  # data table, or a shared child whose first cell is missing
        yield int(m[2]) - 1, col_index(m[1]), text


@dataclass(frozen=True, slots=True)
class DefinedName:
    name: str
    refers_to: str
    sheet: str | None  # the sheet it is local to, None for a workbook-wide name


@dataclass(frozen=True, slots=True)
class Sheet:
    name: str
    visible: Visibility


@dataclass(frozen=True, slots=True)
class CellMeta:
    formula: str | None = None
    bold: bool = False
    fill: bool = False
    border: bool = False
    number_format: str | None = None


def _normalize(value: object) -> Cell:
    # calamine gives "" for empty cells and floats for every number, so 10 comes back
    # as 10.0. We fix both here once, and nothing downstream has to remember it.
    if value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value  # type: ignore[return-value]


class Workbook:
    """Use `Workbook.open`. Each sheet is parsed lazily, once, and the result is cached."""

    def __init__(self, data: bytes, sha256: str) -> None:
        self.sha256 = sha256
        self._data = data
        try:
            self._cal = CalamineWorkbook.from_filelike(io.BytesIO(data))
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                # .xlsb and .ods are zips too, but only .xlsx has the XML we scan.
                self._xlsx = "xl/workbook.xml" in z.namelist()
        except zipfile.BadZipFile:
            self._xlsx = False  # .xls
        except CalamineError as e:
            raise WorkbookError(f"not a readable Excel file: {e}") from e
        self.sheets: tuple[Sheet, ...] = tuple(
            Sheet(m.name, _visibility(m.visible)) for m in self._cal.sheets_metadata
        )
        self._names = {s.name for s in self.sheets}
        self._values: dict[str, Grid] = {}
        self._merged: dict[str, tuple[tuple[int, int, int, int], ...]] = {}
        self._meta: dict[str, dict[tuple[int, int], CellMeta]] = {}
        self._scans: dict[str, _Scan] = {}
        self._texts: dict[str, dict[tuple[int, int], str]] = {}
        self._names_def: tuple[DefinedName, ...] | None = None
        # Views built from this file (lineage, ...) keep their result here, so it lives and
        # dies with the cached Workbook.
        self.memo: dict[str, Any] = {}
        self._paths: dict[str, str] | None = None
        self._opx: openpyxl.Workbook | None = None

    @classmethod
    def open(cls, source: str | os.PathLike[str] | bytes) -> Workbook:
        """Open an Excel file from a path or from its bytes. Raises WorkbookError if unreadable."""
        if isinstance(source, bytes):
            data = source
        else:
            try:
                data = Path(source).read_bytes()
            except (OSError, TypeError, ValueError) as e:
                raise WorkbookError(f"cannot read file: {e}") from e
        sha = hashlib.sha256(data).hexdigest()
        if (wb := _cache.get(sha)) is not None:
            _cache.move_to_end(sha)
            return wb
        wb = cls(data, sha)
        _cache[sha] = wb
        # ponytail: keeps the last few files in memory per process; upgrade: size-based LRU
        # if hosts open many large files at once. Not thread-safe; add a lock if a host
        # opens files from several threads.
        if len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
        return wb

    def _check(self, sheet: str) -> None:
        if sheet not in self._names:
            names = ", ".join(s.name for s in self.sheets)
            raise WorkbookError(f"no sheet named {sheet!r}; sheets: {names}")

    def _scan(self, sheet: str) -> _Scan | None:
        """Formula positions and cell count from the sheet XML. None when not an .xlsx."""
        self._check(sheet)
        if not self._xlsx:
            return None
        if sheet not in self._scans:
            try:
                if self._paths is None:
                    self._paths = _sheet_paths(self._data)
                path = self._paths.get(sheet)
                if path is None:  # chartsheet: no cells
                    self._scans[sheet] = _Scan(frozenset(), 0)
                else:
                    with zipfile.ZipFile(io.BytesIO(self._data)) as z:
                        self._scans[sheet] = _scan_sheet(_read_part(z, path))
            except _ZIP_ERRORS as e:
                raise WorkbookError(f"damaged file, cannot read sheet {sheet!r}: {e!r}") from e
        return self._scans[sheet]

    def formula_cells(self, sheet: str) -> frozenset[tuple[int, int]] | None:
        """Positions of formula cells, read straight from the sheet XML. None for .xls.

        calamine shows a formula with no cached value as an empty cell, so `values`
        alone can make a calculated sheet look empty. This fills that gap without
        paying for openpyxl.
        """
        scan = self._scan(sheet)
        return None if scan is None else scan.formulas

    def formulas(self, sheet: str) -> dict[tuple[int, int], str] | None:
        """Formula text per cell, straight from the sheet XML (no `=`). None for .xls.

        A shared formula repeats its first cell's text for every cell in the group, so
        relative references in those copies are not shifted. That is fine for finding
        which sheets a formula reads, which is what this is for. Costs about as much as
        `formula_cells` and far less than `meta`.
        """
        scan = self._scan(sheet)
        if scan is None:
            return None
        if sheet not in self._texts:
            path = (self._paths or {}).get(sheet)
            try:
                if path is None:
                    self._texts[sheet] = {}
                else:
                    with zipfile.ZipFile(io.BytesIO(self._data)) as z:
                        xml = _read_part(z, path)
                    self._texts[sheet] = {(r, c): t for r, c, t in _formula_texts(xml)}
            except _ZIP_ERRORS as e:
                raise WorkbookError(f"damaged file, cannot read sheet {sheet!r}: {e!r}") from e
        return self._texts[sheet]

    def defined_names(self) -> tuple[DefinedName, ...]:
        """Named ranges and formulas from workbook.xml. Empty for .xls."""
        if self._names_def is None:
            out: list[DefinedName] = []
            if self._xlsx:
                try:
                    with zipfile.ZipFile(io.BytesIO(self._data)) as z:
                        book = ET.fromstring(_read_part(z, "xl/workbook.xml"))
                except _ZIP_ERRORS as e:
                    raise WorkbookError(f"damaged file, cannot read names: {e!r}") from e
                for d in book.iter(f"{_NS_MAIN}definedName"):
                    local = d.get("localSheetId")
                    sheet = (
                        self.sheets[int(local)].name
                        if local and int(local) < len(self.sheets)
                        else None
                    )
                    out.append(DefinedName(d.get("name", ""), (d.text or "").strip(), sheet))
            self._names_def = tuple(out)
        return self._names_def

    def values(self, sheet: str) -> Grid:
        """Cell values starting at A1. Rows can have different lengths; missing = None."""
        scan = self._scan(sheet)  # also checks the sheet name
        if sheet not in self._values:
            if scan is not None and scan.cells > _MAX_CELLS:
                raise WorkbookError(
                    f"sheet {sheet!r} spans {scan.cells:,} cells from A1 to its last cell "
                    f"(limit {_MAX_CELLS:,}); a stray value far from the data is the usual cause"
                )
            try:
                cs = self._cal.get_sheet_by_name(sheet)
                rows = cs.to_python(skip_empty_area=False)
            except CalamineError as e:
                raise WorkbookError(f"damaged file, cannot read sheet {sheet!r}: {e!r}") from e
            self._values[sheet] = tuple(tuple(_normalize(v) for v in row) for row in rows)
        return self._values[sheet]

    def merged(self, sheet: str) -> tuple[tuple[int, int, int, int], ...]:
        """Merged ranges as (row0, col0, row1, col1), inclusive."""
        self._check(sheet)
        if sheet not in self._merged:
            try:
                ranges = self._cal.get_sheet_by_name(sheet).merged_cell_ranges or []
            except CalamineError as e:
                raise WorkbookError(f"damaged file, cannot read sheet {sheet!r}: {e!r}") from e
            self._merged[sheet] = tuple(sorted((r0, c0, r1, c1) for (r0, c0), (r1, c1) in ranges))
        return self._merged[sheet]

    def meta(self, sheet: str) -> dict[tuple[int, int], CellMeta]:
        """Formula and basic style per cell. Plain cells are left out.

        This loads openpyxl, which is about 15x slower than calamine, so only call it
        when a tool really needs formulas or styles.
        """
        self._check(sheet)
        if sheet in self._meta:
            return self._meta[sheet]
        if not self._xlsx:
            # ponytail: openpyxl only reads xlsx/xlsm; upgrade: parse .xls BIFF if users need it.
            raise WorkbookError("formulas and styles need an .xlsx/.xlsm file")
        rows = self.values(sheet)
        out: dict[tuple[int, int], CellMeta] = {}
        if rows:
            # read_only sheets trust the file's <dimension> tag, which is sometimes wrong,
            # so we take the size from calamine instead.
            bounds = {"max_row": len(rows), "max_col": max(map(len, rows))}
            with warnings.catch_warnings():
                # openpyxl warns about parts it drops (conditional formatting extensions,
                # WMF images). In read_only mode it parses the sheet while we iterate, so
                # the filter has to cover the loop, not just load_workbook.
                warnings.simplefilter("ignore", UserWarning)
                ws = self._openpyxl()[sheet]
                for r, row in enumerate(ws.iter_rows(min_row=1, min_col=1, **bounds)):
                    for c, cell in enumerate(row):
                        if (m := _cell_meta(cell)) is not None:
                            out[(r, c)] = m
        self._meta[sheet] = out
        return out

    def _openpyxl(self) -> openpyxl.Workbook:
        if self._opx is None:
            self._opx = openpyxl.load_workbook(
                io.BytesIO(self._data), read_only=True, data_only=False
            )
        return self._opx


def _cell_meta(cell: object) -> CellMeta | None:
    if cell.font is None:  # read_only EmptyCell: no value and no style
        return None
    formula = None
    if cell.data_type == "f":
        v = cell.value
        formula = getattr(v, "text", None) or str(v)  # ArrayFormula keeps the text in .text
    f = cell.fill
    b = cell.border
    meta = CellMeta(
        formula=formula,
        bold=bool(cell.font.b),
        fill=f.tagname == "gradientFill" or f.fill_type not in (None, "none"),
        border=any(s is not None and s.style for s in (b.left, b.right, b.top, b.bottom)),
        number_format=None if cell.number_format == "General" else cell.number_format,
    )
    return None if meta == _PLAIN else meta


_PLAIN = CellMeta()
_cache: OrderedDict[str, Workbook] = OrderedDict()
