"""Find table blocks in a sheet: range, header rows, noise above, totals, repeated column groups."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from excel_etl.workbook import Cell, Workbook, WorkbookError

type Box = tuple[int, int, int, int]  # (row0, col0, row1, col1), 0-based, inclusive
type Orientation = Literal["rows", "columns", "matrix"]

# ponytail: tuned on the BALANSITAS layouts. Headers up to 4 rows, column groups up to 8
# wide, data starts at the first run of 3 data rows; upgrade: make them arguments if a
# workbook needs more.
_MAX_HEADER = 4
_MAX_PERIOD = 8
_RUN = 3
# ponytail: English and Indonesian words only, so "Total Blue" also counts as a total;
# upgrade: bold/fill from Workbook.meta, or a per-workbook word list.
_TOTAL = re.compile(r"^\s*(grand\s+|sub\s*-?\s*)?(total|jumlah)\b", re.IGNORECASE)
_NUMERIC = (int, float, bool, date, datetime, time, timedelta)
# A merge bigger than this only marks its top-left cell. Real header merges are a few
# dozen cells; a file that merges a million cells should not cost a million dict entries.
_MAX_MERGE_CELLS = 10_000


@dataclass(frozen=True, slots=True)
class ColumnGroup:
    """`count` blocks of `period` columns from `start`, each with the same leaf labels."""

    start: int
    period: int
    count: int
    labels: tuple[str, ...]
    parents: tuple[str | None, ...]  # header row above the leaf, one per block


@dataclass(frozen=True, slots=True)
class Region:
    box: Box
    header_rows: tuple[int, ...]
    data_start: int  # first data row (after the header and any section labels)
    merged_header: tuple[Box, ...]
    total_rows: tuple[int, ...]
    orientation: Orientation
    groups: tuple[ColumnGroup, ...]
    noise: tuple[Box, ...]  # title/dashboard cells above the header


class SheetGrid:
    """One sheet seen as sets of positions, built once and shared by every view of it."""

    def __init__(self, wb: Workbook, sheet: str) -> None:
        self._wb, self._sheet = wb, sheet
        self.values = wb.values(sheet)
        self.merged = wb.merged(sheet)
        self.formulas = wb.formula_cells(sheet) or frozenset()
        # A formula with no cached value reads as empty, so it counts as a cell too.
        self.cells: set[tuple[int, int]] = {
            (r, c) for r, row in enumerate(self.values) for c, v in enumerate(row) if v is not None
        } | self.formulas
        rows = max((r for r, _ in self.cells), default=-1)
        cols = max((c for _, c in self.cells), default=-1)
        self.cover: dict[tuple[int, int], Cell] = {}
        for r0, c0, r1, c1 in self.merged:
            if r0 > rows or c0 > cols:
                continue  # merge sits entirely past the data
            r1, c1 = min(r1, rows), min(c1, cols)
            v = self.get(r0, c0)
            if (r1 - r0 + 1) * (c1 - c0 + 1) > _MAX_MERGE_CELLS:
                self.cover[r0, c0] = v
                continue
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    self.cover[r, c] = v
        self.filled = self.cells | self.cover.keys()

    @property
    def bounds(self) -> Box | None:
        """Smallest box around all cells, merged areas not included. None for an empty sheet."""
        if not self.cells:
            return None
        rows = [r for r, _ in self.cells]
        cols = [c for _, c in self.cells]
        return min(rows), min(cols), max(rows), max(cols)

    @property
    def extent(self) -> Box | None:
        """Like `bounds`, but widened to the merged areas that were kept in `cover`."""
        if not self.filled:
            return None
        rows = [r for r, _ in self.filled]
        cols = [c for _, c in self.filled]
        return min(rows), min(cols), max(rows), max(cols)

    def get(self, r: int, c: int) -> Cell:
        if r < 0 or c < 0 or r >= len(self.values):
            return None
        row = self.values[r]
        return row[c] if c < len(row) else None

    def is_bold(self, r: int, c0: int, c1: int) -> bool:
        """Whether every filled cell of the row in c0..c1 is bold. Reads the styles with
        openpyxl, which is slow, so ask only when the other signals did not settle it."""
        try:
            meta = self._wb.meta(self._sheet)
        except WorkbookError:
            return False
        cells = [c for c in range(c0, c1 + 1) if (r, c) in self.cells]
        return bool(cells) and all((m := meta.get((r, c))) is not None and m.bold for c in cells)

    def label(self, r: int, c: int) -> str | None:
        v = self.cover.get((r, c), self.get(r, c))
        return None if v is None else str(v).strip() or None

    def row(self, r: int, c0: int, c1: int) -> tuple[Cell, ...]:
        row = self.values[r] if 0 <= r < len(self.values) else ()
        cells = row[c0 : c1 + 1]
        return cells + (None,) * (c1 + 1 - c0 - len(cells))

    def is_data(self, r: int, box: Box) -> bool:
        cells = self.row(r, box[1], box[3])
        nums = sum(
            isinstance(v, _NUMERIC) if v is not None else (r, box[1] + i) in self.formulas
            for i, v in enumerate(cells)
        )
        texts = sum(isinstance(v, str) for v in cells)
        # ponytail: a header row of numeric years reads as data; upgrade: use bold/fill from meta
        return nums >= max(1, texts)


_NEIGHBORS = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc)


def _components(cells: set[tuple[int, int]]) -> list[Box]:
    """Bounding boxes of 8-connected filled cells, overlapping boxes joined."""
    todo, boxes = set(cells), []
    while todo:
        stack = [todo.pop()]
        seen = []
        while stack:
            r, c = cell = stack.pop()
            seen.append(cell)
            for dr, dc in _NEIGHBORS:
                if (n := (r + dr, c + dc)) in todo:
                    todo.remove(n)
                    stack.append(n)
        rows = [r for r, _ in seen]
        cols = [c for _, c in seen]
        boxes.append((min(rows), min(cols), max(rows), max(cols)))
    return _merge_overlaps(boxes)


def _merge_overlaps(boxes: list[Box]) -> list[Box]:
    """Join boxes that overlap until none do. Sweeps top to bottom, so only boxes that
    still reach the current row are compared (about linear when blocks are spread out)."""
    pending = sorted(boxes)
    while True:
        done: list[Box] = []
        active: list[Box] = []
        merged = False
        for b in pending:
            done += [o for o in active if o[2] < b[0]]
            active = [o for o in active if o[2] >= b[0]]
            for i, o in enumerate(active):
                if b[1] <= o[3] and o[1] <= b[3]:  # rows overlap already, so check columns
                    active[i] = (min(b[0], o[0]), min(b[1], o[1]), max(b[2], o[2]), max(b[3], o[3]))
                    merged = True
                    break
            else:
                active.append(b)
        pending = sorted(done + active)
        if not merged:
            return pending


def _groups(g: SheetGrid, leaf: int, left: int, right: int) -> tuple[ColumnGroup, ...]:
    labels = [g.label(leaf, c) for c in range(left, right + 1)]
    n, out, i = len(labels), [], 0
    while i < n:
        best = (0, 0)  # (count, period)
        for p in range(2, _MAX_PERIOD + 1):
            block = labels[i : i + p]
            if len(block) < p or None in block or len(set(block)) < p:
                continue
            k = i
            while k + p < n and labels[k] == labels[k + p]:
                k += 1
            count = (k - i + p) // p
            if count >= 2 and count * p > best[0] * best[1]:
                best = (count, p)
        count, p = best
        if not count:
            i += 1
            continue
        start = left + i
        parents = tuple(
            g.label(leaf - 1, start + j * p) if leaf > 0 else None for j in range(count)
        )
        out.append(ColumnGroup(start, p, count, tuple(labels[i : i + p]), parents))  # type: ignore[arg-type]
        i += count * p
    return tuple(out)


def _split_rows(g: SheetGrid, box: Box, first: int, header: tuple[int, ...]) -> list[int]:
    """Rows where a repeated copy of the leaf header starts a new table (NPK, then ORGANIK)."""
    _, left, bottom, right = box
    leaf = tuple(g.label(header[-1], c) for c in range(left, right + 1))
    if sum(x is not None for x in leaf) < 2:
        return []
    k = next(i for i, x in enumerate(leaf) if x is not None)
    splits: list[int] = []
    prev = first
    for s in range(first + 1, bottom + 1):
        # a table needs its header plus at least one data row, so skip back-to-back copies
        if (
            s - prev > len(header)
            and g.label(s, left + k) == leaf[k]
            and leaf == tuple(g.label(s, c) for c in range(left, right + 1))
        ):
            splits.append(s - len(header) + 1)
            prev = s
    return splits


_MIN_TEXT_ROWS = 4  # header + 3 records, so two lines of prose are not a table


def _text_header(g: SheetGrid, box: Box) -> bool:
    """Whether the top row of a block with no number in it is a header: a master or mapping
    table (code, name, category), where nothing numeric tells the header from the records.

    The top row must be all different texts; the rows below must be mostly full; and the top
    row must set itself apart: bold, or none of its texts is repeated lower in its column
    (a header names a column, a record is one of many in it).
    """
    top, left, bottom, right = box
    width = right - left + 1
    if bottom - top + 1 < _MIN_TEXT_ROWS or width < 2:
        return False
    # a block that holds any number is a slice of a numeric table (shipments, a total), whose
    # header is somewhere else: not a master table
    if any(
        isinstance(g.get(r, c), _NUMERIC)
        for r in range(top, bottom + 1)
        for c in range(left, right + 1)
    ):
        return False
    if any((r, c) in g.formulas for r in range(top, bottom + 1) for c in range(left, right + 1)):
        return False
    head = [g.label(top, c) for c in range(left, right + 1)]
    if None in head or len(set(head)) != width:
        return False
    body = range(top + 1, bottom + 1)
    full = sum(
        sum(g.label(r, c) is not None for c in range(left, right + 1)) * 2 >= width for r in body
    )
    if full * 4 < len(body) * 3:  # fewer than 3 of 4 body rows are mostly full
        return False
    if g.is_bold(top, left, right):
        return True
    return not any(g.label(r, c) == head[c - left] for r in body for c in range(left, right + 1))


def _analyze(g: SheetGrid, box: Box, split: bool = True) -> list[Region]:
    top, left, bottom, right = box
    data = [r for r in range(top, bottom + 1) if g.is_data(r, box)]
    if not data:
        if _text_header(g, box):
            return [Region(box, (top,), top + 1, (), (), "rows", (), ())]
        return [Region(box, (), top, (), (), "columns", (), ())]
    # a dashboard row of numbers above the header is not where the data starts: take the
    # first run of _RUN data rows (or the first data row in a tiny table)
    rows = set(data)
    first = next((r for r in data if all(r + i in rows for i in range(_RUN))), data[0])
    data = [r for r in data if r >= first]
    data_cols = {c for r in data for c in range(left, right + 1) if (r, c) in g.filled}

    def wide(r: int) -> bool:
        return 2 * sum((r, c) in g.filled for c in data_cols) >= len(data_cols)

    r = first - 1
    while r >= top and not wide(r):  # section labels between header and data
        r -= 1
    end = r
    groups = _groups(g, end, left, right) if end >= top else ()
    starts = {grp.start + j * grp.period for grp in groups for j in range(grp.count)}
    while r >= top and end - r < _MAX_HEADER and not g.is_data(r, box):
        # a sparse row still counts as a parent header when it labels the starts of the
        # repeated column groups below (unmerged JANUARI .. FEBRUARI over PKG..TOTAL)
        sparse_parent = sum(g.label(r, c) is not None for c in starts) >= 2
        if not (wide(r) or (r < end and sparse_parent)):
            break
        r -= 1
    header = tuple(range(r + 1, end + 1))
    start = header[0] if header else first

    if split and header:
        cuts = _split_rows(g, box, first, header)
        if cuts:
            # the first slice keeps `top`, so the noise above the first table is not lost
            edges = [top, *cuts, bottom + 1]
            return [
                reg
                for a, b in zip(edges, edges[1:], strict=False)
                for reg in _analyze(g, (a, left, b - 1, right), split=False)
            ]

    above = {(rr, c) for rr in range(top, start) for c in range(left, right + 1)}
    noise = _components(above & g.filled)
    lbl_col = min(data_cols)
    has_labels = sum(isinstance(g.get(rr, lbl_col), str) for rr in data) * 2 >= len(data)
    # ponytail: "columns" doubles as "no header found"; upgrade: a separate flag if a
    # headerless table needs to be told apart from a table whose columns are the records.
    orient: Orientation = "matrix" if header and has_labels else "rows" if header else "columns"
    totals = tuple(
        rr
        for rr in data
        if any(isinstance(v, str) and _TOTAL.match(v) for v in g.row(rr, left, right))
    )
    merged_hdr = tuple(
        m
        for m in g.merged
        if header and m[0] <= end and m[2] >= start and m[1] <= right and m[3] >= left
    )
    return [
        Region(
            (start, left, bottom, right),
            header,
            first,
            merged_hdr,
            totals,
            orient,
            groups if header else (),
            tuple(noise),
        )
    ]


def detect_regions(wb: Workbook, sheet: str, grid: SheetGrid | None = None) -> list[Region]:
    """Table blocks, ordered by the block of cells they sit in (top, then left).

    The order is what `F1.S12.R{n}` numbers. Blocks smaller than 2x2 are left out as notes.
    Pass `grid` when you already built one for this sheet.
    """
    g = grid or SheetGrid(wb, sheet)
    out = []
    for box in _components(g.filled):
        if box[2] > box[0] and box[3] > box[1]:
            out.extend(_analyze(g, box))
    return out
