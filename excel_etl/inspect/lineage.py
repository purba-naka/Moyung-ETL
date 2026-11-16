"""Which sheets read which: references inside formulas, and a source/derived role from them.

A cell is *typed* when a person entered the number, *local* when its formula only reads
its own sheet (a total), and *cross* when its formula reads another sheet or file. Typed
cells are where data comes from; cross cells are where it is copied or recalculated.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from excel_etl.workbook import Workbook

type Role = Literal["empty", "source", "mixed", "derived"]

_WORD = r"[A-Za-z_\u00c0-\uffff][\w.\u00c0-\uffff]*"
_STRING = re.compile(r'"(?:[^"]|"")*"')
_PREFIX = re.compile(rf"'((?:[^']|'')+)'!|(?<![\w.'#$\]])((?:\[\d+\])?{_WORD}(?::{_WORD})?)!")
_TOKEN = re.compile(r"(?<![\w.!'#$\[])([A-Za-z_\\][\w.\\]*)(?![\w.(!\[])")
_NUMERIC = (int, float, bool, date, datetime, time, timedelta)


_MINOR = 0.1
_MAJOR = 0.5


@dataclass(frozen=True, slots=True)
class _Refs:
    sheets: tuple[str, ...]
    names: frozenset[str]


@dataclass(frozen=True, slots=True)
class Lineage:
    edges: dict[tuple[str, str], int]
    cross: dict[str, frozenset[tuple[int, int]]]


def _unquote(s: str) -> str:
    return s.replace("''", "'")


def _sheets_in(text: str) -> tuple[str, ...]:
    if "!" not in text:
        return ()
    out = []
    for m in _PREFIX.finditer(_STRING.sub('""', text)):
        out.append(_unquote(m[1]) if m[1] is not None else m[2])
    return tuple(out)


def _names_reaching_sheets(wb: Workbook) -> dict[str, dict[str | None, tuple[str, ...]]]:
    """UPPER name -> {scope sheet or None: sheets it points at}; names with no sheet are skipped."""
    out: dict[str, dict[str | None, tuple[str, ...]]] = {}
    for d in wb.defined_names():
        if sheets := _sheets_in(d.refers_to):
            out.setdefault(d.name.upper(), {})[d.sheet] = sheets
    return out


class _Resolver:
    """Turns formula text into the set of sheets it reads. Results are cached by text, because
    a shared formula repeats the same text in thousands of cells."""

    def __init__(self, wb: Workbook) -> None:
        self._order = [s.name for s in wb.sheets]
        self._pos = {n: i for i, n in enumerate(self._order)}
        self._names = _names_reaching_sheets(wb)
        self._cache: dict[str, _Refs] = {}
        self._reads: dict[tuple[str, str], frozenset[str]] = {}

    def _parse(self, text: str) -> _Refs:
        if (hit := self._cache.get(text)) is not None:
            return hit
        sheets = _sheets_in(text)
        tokens: frozenset[str] = frozenset()
        if self._names:
            rest = _PREFIX.sub(" ", _STRING.sub('""', text)).upper()
            tokens = frozenset(t for t in _TOKEN.findall(rest) if t in self._names)
        return self._cache.setdefault(text, _Refs(sheets, tokens))

    def _expand(self, ref: str) -> list[str]:
        if ref.startswith("[") or ref in self._pos:
            return [ref]
        if ":" in ref:
            a, _, b = ref.partition(":")
            if a in self._pos and b in self._pos:
                lo, hi = sorted((self._pos[a], self._pos[b]))
                return self._order[lo : hi + 1]
        return [ref]

    def reads(self, text: str, own: str) -> frozenset[str]:
        """Sheets (and `[file]` markers) a formula in `own` reads, own sheet excluded."""
        key = (text, own)
        if (hit := self._reads.get(key)) is None:
            hit = self._reads[key] = self._resolve(text, own)
        return hit

    def _resolve(self, text: str, own: str) -> frozenset[str]:
        refs = self._parse(text)
        found = list(refs.sheets)
        for token in refs.names:
            scopes = self._names[token]
            found += scopes.get(own) or scopes.get(None) or ()
        out: set[str] = set()
        for ref in found:
            for name in self._expand(ref):
                out.add(name if name in self._pos else "[file]")
        out.discard(own)
        return frozenset(out)


def lineage(wb: Workbook) -> Lineage:
    """Sheet-to-sheet reads for the whole workbook. Cached on the workbook."""
    if (hit := wb.memo.get("lineage")) is not None:
        return hit
    res = _Resolver(wb)
    edges: dict[tuple[str, str], int] = {}
    cross: dict[str, frozenset[tuple[int, int]]] = {}
    for sheet in wb.sheets:
        leaving = set()
        for pos, text in (wb.formulas(sheet.name) or {}).items():
            if reads := res.reads(text, sheet.name):
                leaving.add(pos)
                for dst in reads:
                    edges[sheet.name, dst] = edges.get((sheet.name, dst), 0) + 1
        cross[sheet.name] = frozenset(leaving)
    out = Lineage(edges, cross)
    wb.memo["lineage"] = out
    return out


def classify(typed: int, local: int, cross: int) -> Role:
    """source = people typed it; derived = copied or calculated; mixed = both."""
    total = typed + local + cross
    if total == 0:
        return "empty"
    typed_share, cross_share = typed / total, cross / total
    if cross_share >= _MAJOR:
        return "derived" if typed_share < _MINOR else "mixed"
    if cross_share >= _MINOR:
        return "mixed"
    return "source" if typed_share >= _MINOR else "derived"


class Votes:
    """Per-row column lists of typed, local and cross cells, so a region window is cheap."""

    def __init__(self, wb: Workbook, sheet: str) -> None:
        values = wb.values(sheet)
        formulas = wb.formula_cells(sheet) or frozenset()
        cross = lineage(wb).cross.get(sheet, frozenset())
        by_row: dict[int, list[int]] = {}
        for r, c in formulas:
            by_row.setdefault(r, []).append(c)
        self.typed: dict[int, list[int]] = {}
        self.local: dict[int, list[int]] = {}
        self.cross: dict[int, list[int]] = {}
        for r, row in enumerate(values):
            skip = set(by_row.get(r, ()))
            cols = [c for c, v in enumerate(row) if isinstance(v, _NUMERIC) and c not in skip]
            if cols:
                self.typed[r] = cols
        for r, cols in by_row.items():
            cols.sort()
            self.cross[r] = [c for c in cols if (r, c) in cross]
            self.local[r] = [c for c in cols if (r, c) not in cross]

    def count(self, rows: range, cols: range) -> tuple[int, int, int]:
        """(typed, local, cross) cells inside a row and column window."""

        def n(index: dict[int, list[int]]) -> int:
            hits = index if len(index) < len(rows) else (r for r in rows if r in index)
            return sum(
                bisect_left(index[r], cols.stop) - bisect_left(index[r], cols.start)
                for r in hits
                if r in rows
            )

        return n(self.typed), n(self.local), n(self.cross)


def votes(wb: Workbook, sheet: str) -> Votes:
    """Typed, local and cross cells of a sheet. Text cells are labels, so they do not vote."""
    key = f"votes:{sheet}"
    if (hit := wb.memo.get(key)) is None:
        hit = wb.memo[key] = Votes(wb, sheet)
    return hit


def sheet_role(wb: Workbook, sheet: str) -> Role:
    role = classify(*votes(wb, sheet).count(range(1 << 20), range(1 << 14)))
    if role != "empty":
        return role
    rows = wb.values(sheet)
    cells = [(r, c) for r, row in enumerate(rows) for c, v in enumerate(row) if v is not None]
    if not cells:
        return role
    box = (
        min(r for r, _ in cells),
        min(c for _, c in cells),
        max(r for r, _ in cells),
        max(c for _, c in cells),
    )
    return "source" if _is_text_table(wb, sheet, box) else role


def region_role(wb: Workbook, sheet: str, box: tuple[int, int, int, int]) -> Role:
    """Role of one table block, from the cells inside its box."""
    r0, c0, r1, c1 = box
    role = classify(*votes(wb, sheet).count(range(r0, r1 + 1), range(c0, c1 + 1)))
    return "source" if role == "empty" and _is_text_table(wb, sheet, box) else role


def _is_text_table(wb: Workbook, sheet: str, box: tuple[int, int, int, int]) -> bool:
    r0, c0, r1, c1 = box
    if r1 <= r0 or c1 <= c0:
        return False
    rows = wb.values(sheet)[r0 : r1 + 1]
    return sum(isinstance(v, str) for row in rows for v in row[c0 : c1 + 1]) >= 4
