"""Text views of a workbook for an LLM. Each view is small on purpose; the agent zooms in.

Every view returns `{"text", "truncated", "next_offset"}`. When `truncated` is true, call
again with `offset=next_offset` for the rest. Bad input raises WorkbookError.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from excel_etl.inspect.lineage import lineage, region_role, sheet_role
from excel_etl.inspect.regions import Box, Region, SheetGrid, detect_regions
from excel_etl.workbook import Workbook, WorkbookError, col_index

type Role = Literal["empty", "source", "mixed", "derived"]

_TEXT_MAX = 30
_LINE_MAX = 600  # chars; one runaway line (16k columns wide) must not eat the whole budget
_MAX_ROW, _MAX_COL = 1_048_576, 16_384  # Excel's own limits


def col_letters(c: int) -> str:
    """0-based column index -> Excel letters (0 -> A, 26 -> AA)."""
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(r: int, c: int) -> str:
    return f"{col_letters(c)}{r + 1}"


def _box(b: Box) -> str:
    return f"{a1(b[0], b[1])}:{a1(b[2], b[3])}"


def _few(items: list[str], n: int = 3) -> str:
    return ",".join(items[:n]) + (f",+{len(items) - n}" if len(items) > n else "")


def _clip(s: str, n: int = _LINE_MAX) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _name(s: str) -> str:
    """A sheet name or label as it appears in a text line: one line, no column separator."""
    return " ".join(s.replace("|", "/").split())


def _paging(offset: int, limit: int | None = None) -> None:
    # Checked here, once, so an agent that sends offset=-1 or limit=0 gets an error instead
    # of a truncated page whose next_offset points at the same page forever.
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise WorkbookError(f"offset must be an integer >= 0, got {offset!r}")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise WorkbookError(f"limit must be an integer >= 1, got {limit!r}")


def _page(lines: list[str], offset: int, shown: int, total: int) -> dict:
    end = offset + shown
    truncated = end < total
    return {
        "text": "\n".join(lines),
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }


@dataclass(frozen=True, slots=True)
class SheetSummary:
    id: str
    name: str
    visible: str
    used: str | None  # "B3:FC807", None when the sheet has no cells
    cells: int
    regions: int
    formula_pct: int | None  # None when formulas cannot be read (.xls)
    role: Role


def _summary(wb: Workbook, file_id: str, i: int) -> SheetSummary:
    sheet = wb.sheets[i]
    g = SheetGrid(wb, sheet.name)
    pct = None
    if wb.formula_cells(sheet.name) is not None:
        pct = round(100 * len(g.formulas) / len(g.cells)) if g.cells else 0
    return SheetSummary(
        id=f"{file_id}.S{i + 1}",
        name=sheet.name,
        visible=sheet.visible,
        used=_box(g.bounds) if g.bounds else None,
        cells=len(g.cells),
        regions=len(detect_regions(wb, sheet.name, g)),
        formula_pct=pct,
        # .xls has no formula text to follow, so everything numeric there counts as typed
        role=sheet_role(wb, sheet.name),
    )


def _compact(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def workbook_map(wb: Workbook, file_id: str, offset: int = 0, limit: int = 200) -> dict:
    """List the sheets of a workbook, one line each. Use this first.

    Line format `id|name|used|cells|regions|formula%|role`:
    used = A1 range around all cells, cells = filled cells, regions = table-like blocks found
    (see sheet_regions), formula% = share of cells that are formulas (? for .xls),
    role = where the numbers come from: source = typed by people, derived = formulas that
    read other sheets or only add up this one, mixed = both, empty. Judged on numeric cells.
    Hidden sheets carry a `[hidden]` flag. `offset`/`limit` page over sheets.
    """
    _paging(offset, limit)
    total = len(wb.sheets)
    page = [_summary(wb, file_id, i) for i in range(offset, min(offset + limit, total))]
    lines = [f"{file_id} {total} sheets. id|name|used|cells|regions|formula%|role"]
    for s in page:
        flag = "" if s.visible == "visible" else f" [{s.visible}]"
        pct = "?" if s.formula_pct is None else str(s.formula_pct)
        lines.append(
            _clip(
                f"{s.id}|{_name(s.name)}{flag}|{s.used or '-'}|{_compact(s.cells)}"
                f"|{s.regions}|{pct}|{s.role}"
            )
        )
    return _page(lines, offset, len(page), total)


def formula_lineage(wb: Workbook, file_id: str, offset: int = 0, limit: int = 100) -> dict:
    """Show which sheets read which, from the formulas. Use to tell raw sheets from reports.

    One line per sheet that reads others: `F1.S9 Per Bulan <- F1.S59 (N) PKG x120, ...`,
    where x120 = formula cells that read that sheet. `[file]` = another workbook or a
    sheet that is not in this file. Sheets that nothing reads and that read nothing are
    left out, so a short list means the sheets are independent. `offset`/`limit` page over
    the reading sheets. Role (source/derived/mixed) is in workbook_map and sheet_regions.
    """
    _paging(offset, limit)
    ids = {s.name: f"{file_id}.S{i}" for i, s in enumerate(wb.sheets, start=1)}
    reads: dict[str, list[tuple[str, int]]] = {}
    for (src, dst), n in lineage(wb).edges.items():
        reads.setdefault(src, []).append((dst, n))
    readers = [s.name for s in wb.sheets if s.name in reads]
    page = readers[offset : offset + limit]
    lines = [f"{file_id} {len(readers)} sheets read other sheets. sheet <- sheet xN"]
    for name in page:
        # biggest source first: that is the one an agent should look at
        src = sorted(reads[name], key=lambda x: (-x[1], x[0]))
        parts = [f"{ids[d]} {_name(d)} x{n}" if d in ids else f"{d} x{n}" for d, n in src]
        more = f", +{len(parts) - 6} more" if len(parts) > 6 else ""
        lines.append(_clip(f"{ids[name]} {_name(name)} <- " + ", ".join(parts[:6]) + more))
    return _page(lines, offset, len(page), len(readers))


def _resolve(wb: Workbook, file_id: str, sheet: str) -> tuple[str, str]:
    """Sheet name or id (`F1.S12`) -> (id, name). An id wins over a sheet named like one."""
    if not isinstance(sheet, str):
        raise WorkbookError(f"sheet must be a string, got {sheet!r}")
    for i, s in enumerate(wb.sheets, start=1):
        if sheet == f"{file_id}.S{i}":
            return sheet, s.name
    for i, s in enumerate(wb.sheets, start=1):
        if sheet == s.name:
            return f"{file_id}.S{i}", s.name
    names = ", ".join(s.name for s in wb.sheets)
    raise WorkbookError(
        f"no sheet {sheet!r}; use a sheet name or id like {file_id}.S1. Sheets: {names}"
    )


def _region_line(rid: str, r: Region, role: Role) -> str:
    parts = [rid, _box(r.box)]
    if r.header_rows:
        h = r.header_rows
        parts.append(f"hdr {h[0] + 1}" + (f"-{h[-1] + 1}" if len(h) > 1 else ""))
    parts.append(f"{r.orientation}/{role}")
    if r.total_rows:
        parts.append("total " + _few([str(t + 1) for t in r.total_rows], 5))
    for g in r.groups:
        parents = [_fmt(p) for p in g.parents if p]
        span = f" ({parents[0]}..{parents[-1]})" if parents else ""
        labels = ",".join(_fmt(x) for x in g.labels)
        parts.append(f"group {col_letters(g.start)}:[{labels}]x{g.count}{span}")
    if r.merged_header:
        parts.append("merged " + _few([_box(m) for m in r.merged_header]))
    if r.noise:
        parts.append("noise " + _few([_box(n) for n in r.noise]))
    return _clip("|".join(parts))


def sheet_regions(wb: Workbook, file_id: str, sheet: str, offset: int = 0, limit: int = 50) -> dict:
    """List the tables in one sheet (name or id like F1.S12). Use after workbook_map.

    Line format `id|range|hdr rows|orientation/role|total rows|group|merged|noise`, rows 1-based:
    id = F1.S12.R3 (pass it to render_range), range = table incl. header,
    hdr = header row(s), orientation = rows (records per row), matrix (row labels in the
    first column), columns (no header found), role = source (numbers typed by people),
    derived (formulas) or mixed, judged on the cells inside the table,
    total = rows that look like totals,
    group = repeated column pattern `start:[labels]xN (first..last parent header)`,
    merged = merged header cells, noise = titles or dashboards above the header.
    The `headerless:` line lists small blocks with no header (notes, KPIs).
    `offset`/`limit` page over the tables only; R numbers stay the same on every page.
    """
    _paging(offset, limit)
    sid, name = _resolve(wb, file_id, sheet)
    regions = [(f"{sid}.R{i}", r) for i, r in enumerate(detect_regions(wb, name), start=1)]
    tables = [x for x in regions if x[1].header_rows]
    small = [x for x in regions if not x[1].header_rows]
    page = tables[offset : offset + limit]
    lines = [
        f"{sid} {_name(name)}: {len(tables)} tables, {len(small)} headerless blocks. "
        "id|range|hdr rows|orientation/role|total rows|group|merged|noise (rows 1-based)"
    ]
    lines += [_region_line(rid, r, region_role(wb, name, r.box)) for rid, r in page]
    if small and offset == 0:
        # ponytail: headerless blocks (notes, KPIs, side calcs) get one summary line;
        # upgrade: a `kind` filter if the agent needs to page through them.
        lines.append(
            "headerless: "
            + _few([f"{rid.rsplit('.', 1)[1]} {_box(r.box)}" for rid, r in small], 10)
        )
    return _page(lines, offset, len(page), len(tables))


_A1 = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})(?::([A-Z]{1,3})([0-9]{1,7}))?$", re.ASCII)
_REGION_ID = re.compile(r"^(.+\.S[0-9]{1,6})\.R([0-9]{1,6})$", re.ASCII)


def _parse_range(s: str) -> Box:
    if not isinstance(s, str):
        raise WorkbookError(f"range must be a string like 'B4:N40', got {s!r}")
    # upper() first would turn the ligature "ﬀ" into "FF", so only plain ASCII gets through
    m = _A1.fullmatch(s.strip().replace("$", "")) if s.isascii() else None
    if m is None and s.isascii():
        m = _A1.fullmatch(s.strip().replace("$", "").upper())
    if not m:
        raise WorkbookError(f"bad range {s!r}; use A1 style like 'B4:N40'")
    c0, r0 = col_index(m[1]), int(m[2]) - 1
    c1, r1 = (col_index(m[3]), int(m[4]) - 1) if m[3] else (c0, r0)
    box = min(r0, r1), min(c0, c1), max(r0, r1), max(c0, c1)
    if box[0] < 0 or box[2] >= _MAX_ROW or box[3] >= _MAX_COL:
        raise WorkbookError(
            f"range {s!r} is outside the sheet (rows 1..{_MAX_ROW}, columns A..XFD)"
        )
    return box


def _clamp(box: Box, bounds: Box) -> Box | None:
    """Cut a requested box down to where cells exist, so a huge range costs nothing extra."""
    r0, c0 = max(box[0], bounds[0]), max(box[1], bounds[1])
    r1, c1 = min(box[2], bounds[2]), min(box[3], bounds[3])
    return (r0, c0, r1, c1) if r0 <= r1 and c0 <= c1 else None


def _target(
    wb: Workbook, file_id: str, ref: str, rng: str | None
) -> tuple[str, str, Box | None, SheetGrid | None]:
    """ref (sheet name, sheet id or region id) + range -> (label, sheet name, box, grid).

    The grid comes back only when finding a region already built it.
    """
    if not isinstance(ref, str):
        raise WorkbookError(f"ref must be a string, got {ref!r}")
    names = {s.name for s in wb.sheets}
    m = _REGION_ID.match(ref)
    # a sheet that is literally named like a region id wins over reading it as one
    if not m or ref in names:
        sid, name = _resolve(wb, file_id, ref)
        return f"{sid} {_name(name)}", name, _parse_range(rng) if rng is not None else None, None
    if rng is not None:
        raise WorkbookError(f"{ref} is a region; give either a region id or a range, not both")
    label, name, region, g = _region(wb, file_id, ref)
    return label, name, region.box, g


def _region(wb: Workbook, file_id: str, ref: str) -> tuple[str, str, Region, SheetGrid]:
    """Region id (`F1.S12.R3`) -> (label, sheet name, region, grid)."""
    m = _REGION_ID.match(ref) if isinstance(ref, str) else None
    if m is None:
        raise WorkbookError(
            f"{ref!r} is not a region id like {file_id}.S12.R3; get one from sheet_regions"
        )
    _, name = _resolve(wb, file_id, m[1])
    g = SheetGrid(wb, name)
    regions = detect_regions(wb, name, g)
    i = int(m[2])
    if not 1 <= i <= len(regions):
        raise WorkbookError(f"{ref}: sheet {m[1]} has regions R1..R{len(regions)}")
    return f"{ref} {_name(name)}", name, regions[i - 1], g


def _kind(v: object) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int | float):
        return "num"
    if isinstance(v, datetime):
        return "datetime"
    return {date: "date", time: "time", timedelta: "duration"}.get(type(v), "text")


def _fmt(v: object, digits: int = 4) -> str:
    if isinstance(v, float):
        v = round(v, digits)
        return str(int(v)) if v.is_integer() else str(v)
    if isinstance(v, datetime) and v.time() == time():
        return v.date().isoformat()
    if isinstance(v, date | time):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    s = " ".join(str(v).split())  # newlines and runs of spaces in labels carry no meaning
    return s if len(s) <= _TEXT_MAX else s[: _TEXT_MAX - 1] + "…"


def _rects(runs: list[tuple[int, int, int]]) -> list[str]:
    """(col, row0, row1) runs -> A1 ranges, joining side-by-side columns with the same rows."""
    out: list[str] = []
    by_rows: dict[tuple[int, int], list[int]] = {}
    for c, r0, r1 in runs:
        by_rows.setdefault((r0, r1), []).append(c)
    for (r0, r1), cols in sorted(by_rows.items(), key=lambda x: (x[0][0], min(x[1]))):
        cols.sort()
        start = prev = cols[0]
        for c in [*cols[1:], None]:
            if c is not None and c == prev + 1:
                prev = c
                continue
            out.append(a1(r0, start) if (r0, start) == (r1, prev) else _box((r0, start, r1, prev)))
            if c is not None:
                start = prev = c
    return out


def _compressed(g: SheetGrid, box: Box) -> list[str]:
    top, left, bottom, right = box
    texts: dict[str, list[tuple[int, int]]] = {}
    # value runs per column: [kind, row0, row1, min, max, has uncached formula].
    # Blanks and text do not break a run; an uncached formula joins whatever run it is in.
    runs: list[tuple[str, int, int, int, object, object, bool]] = []
    for c in range(left, right + 1):
        cur: list | None = None
        for r in range(top, bottom + 1):
            v = g.get(r, c)
            if v is None:
                if (r, c) not in g.formulas:
                    continue
                if cur:
                    cur[2], cur[5] = r, True
                else:
                    cur = ["formula", r, r, None, None, True]
                continue
            kind = _kind(v)
            if kind == "text":
                texts.setdefault(_fmt(v), []).append((r, c))
                continue
            if cur and cur[0] == "formula":
                cur[0], cur[3], cur[4] = kind, v, v
            if cur and cur[0] == kind:
                cur[2] = r
                if kind != "bool":
                    # naive and timezone-aware datetimes do not compare; keep the old range
                    with contextlib.suppress(TypeError):
                        cur[3], cur[4] = min(cur[3], v), max(cur[4], v)
                continue
            if cur:
                runs.append((cur[0], cur[1], cur[2], c, cur[3], cur[4], cur[5]))
            cur = [kind, r, r, v, v, False]
        if cur:
            runs.append((cur[0], cur[1], cur[2], c, cur[3], cur[4], cur[5]))

    lines = []
    # text seen once: one line per row (`r4: A=TANGGAL|T=Total Produksi`), reads like the
    # header it usually is; text seen more than once: inverted index (`"Ton": B5,D5,..`)
    by_row: dict[int, list[str]] = {}
    for text, cells in texts.items():
        if len(cells) == 1:
            r, c = cells[0]
            by_row.setdefault(r, []).append(f"{col_letters(c)}={text}")
    # a row holding one lone label (month markers down column A) goes in a per-column line
    by_col: dict[str, list[str]] = {}
    for r in sorted(by_row):
        if len(by_row[r]) == 1:
            col, text = by_row[r][0].split("=", 1)
            by_col.setdefault(col, []).append(f"{r + 1}={text}")
        else:
            lines.append(f"r{r + 1}: " + "|".join(by_row[r]))
    lines += [f"col {col}: " + "|".join(items) for col, items in by_col.items()]
    for text, cells in sorted(texts.items(), key=lambda x: x[1][0]):
        if len(cells) == 1:
            continue
        col_runs = []
        for r, c in sorted(cells, key=lambda x: (x[1], x[0])):
            if col_runs and col_runs[-1][0] == c and col_runs[-1][2] == r - 1:
                col_runs[-1][2] = r
            else:
                col_runs.append([c, r, r])
        lines.append(f'"{text}": ' + _few(_rects([tuple(x) for x in col_runs]), 3))

    groups: dict[tuple[str, int, int, bool], list] = {}
    for kind, r0, r1, c, lo, hi, uncached in runs:
        grp = groups.setdefault((kind, r0, r1, uncached), [[], lo, hi])
        grp[0].append((c, r0, r1))
        if kind not in ("formula", "bool"):
            with contextlib.suppress(TypeError):
                grp[1], grp[2] = min(grp[1], lo), max(grp[2], hi)
    zeros: list[str] = []
    for (kind, _, _, uncached), (cols, lo, hi) in sorted(
        groups.items(), key=lambda x: (x[0][1], x[1][0][0][0])
    ):
        if kind == "num" and lo == hi == 0 and not uncached:
            zeros += _rects(cols)  # placeholder columns: list once, not one line each
            continue
        if kind == "formula":
            desc = "=?"
        else:
            lo_s, hi_s = _fmt(lo, 3), _fmt(hi, 3)  # a range only needs the scale
            span = "" if kind == "bool" else f" {lo_s}" + ("" if lo == hi else f"..{hi_s}")
            desc = kind + span + (" +=?" if uncached else "")
        lines.append(f"{_few(_rects(cols), 6)} {desc}")
    if zeros:
        lines.append("all 0: " + _few(zeros, 12))
    return [_clip(x) for x in lines]


def _raw(g: SheetGrid, box: Box):
    """Yield one line per non-empty row."""
    top, left, bottom, right = box
    anchors = {(m[0], m[1]) for m in g.merged}
    for r in range(top, bottom + 1):
        cells = []
        for c in range(left, right + 1):
            v = g.get(r, c)
            if (r, c) in g.cover and (r, c) not in anchors:
                cells.append("^")
            elif v is None:
                cells.append("=?" if (r, c) in g.formulas else "")
            else:
                cells.append(_fmt(v).replace("|", "/"))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            yield _clip(f"{r + 1}|" + "|".join(cells))


def render_range(
    wb: Workbook,
    file_id: str,
    ref: str,
    range: str | None = None,  # noqa: A002 - tool argument name the agent sees
    mode: Literal["compressed", "raw"] = "compressed",
    offset: int = 0,
    budget: int = 24_000,
) -> dict:
    """Show the cells of a sheet, region or A1 range as text. Use after sheet_regions.

    ref = sheet name, sheet id (F1.S12) or region id (F1.S12.R3, from sheet_regions);
    range = A1 range like 'B4:N40' inside a sheet (not with a region id); omit for all.
    mode compressed (start here): `r4: A=TANGGAL|T=Total` = texts that occur once, by row;
    `col A: 37=JAN|...` = lone labels down a column; `"Ton": B5,D5,+9` = repeated text and
    where it is; `A6:A806 date 2026-01-01..2027-12-31` = a run of values as range, type
    and min..max; `+=?` = run also holds formulas without a cached value; `all 0:` =
    columns holding only zeros. Rows are 1-based.
    mode raw: `row|A|B|...` with real values; `^` = covered by a merged cell, `=?` =
    formula without a cached value. Use on a small range.
    `budget` caps the text in chars (about 4 per token); `offset` continues a page.
    """
    _paging(offset)
    if mode not in ("compressed", "raw"):
        raise WorkbookError(f"mode must be 'compressed' or 'raw', got {mode!r}")
    _check_budget(budget)
    label, name, box, g = _target(wb, file_id, ref, range)
    g = g or SheetGrid(wb, name)
    extent = g.extent
    if extent is None:
        return _page([f"{label}: empty sheet"], 0, 0, 0)
    box = extent if box is None else _clamp(box, extent)
    if box is None:
        return _page([f"{label}: no cells in that range"], 0, 0, 0)
    head = f"{label} {_box(box)} {mode}, rows 1-based"
    if mode == "raw":
        head += f", first column {col_letters(box[1])}"
    body = _compressed(g, box) if mode == "compressed" else list(_raw(g, box))
    return _fit(head, body, offset, budget)


def _check_budget(budget: int) -> None:
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise WorkbookError(f"budget must be an integer >= 1, got {budget!r}")


def _fit(head: str, body: list[str], offset: int, budget: int) -> dict:
    """Head line plus as many body lines from `offset` as fit in `budget` chars (at least one)."""
    lines, size = [_clip(head)], len(head)
    for line in body[offset:]:
        if size + len(line) + 1 > budget and len(lines) > 1:
            break
        lines.append(line)
        size += len(line) + 1
    return _page(lines, offset, len(lines) - 1, len(body))


def _column_names(g: SheetGrid, region: Region) -> list[str]:
    """One name per column, header rows joined top to bottom: `JANUARI|PKG`."""
    _, left, _, right = region.box
    rows = region.header_rows
    # ponytail: a parent label that is not merged is carried right until the next label
    # (JANUARI over PKG..TOTAL); upgrade: stop at ColumnGroup edges if a sheet has a gap.
    filled: list[list[str | None]] = []
    for hr in rows[:-1]:
        last, row = None, []
        for c in range(left, right + 1):
            last = g.label(hr, c) or last
            row.append(last)
        filled.append(row)
    names = []
    for i, c in enumerate(range(left, right + 1)):
        parts = [row[i] for row in filled] + ([g.label(rows[-1], c)] if rows else [])
        out: list[str] = []
        for p in parts:
            # a vertical merge repeats its label on every header row, so keep it once
            if p and (not out or out[-1] != _name(_fmt(p))):
                out.append(_name(_fmt(p)))
        names.append("|".join(out) or "-")
    return names


def _rows(rows: list[int], n: int = 4) -> str:
    return _few([f"r{r + 1}" for r in rows], n)


def _profile_column(g: SheetGrid, c: int, rows: list[int]) -> str:
    kinds: dict[str, list[int]] = {}
    values: dict[str, list[object]] = {}
    for r in rows:
        v = g.get(r, c)
        if v is None:
            kind = "=?" if (r, c) in g.formulas else "null"
        else:
            kind = _kind(v)
            values.setdefault(kind, []).append(v)
        kinds.setdefault(kind, []).append(r)
    filled = {k: v for k, v in kinds.items() if k not in ("null", "=?")}
    if not filled:
        uncached = len(kinds.get("=?", ()))
        return f"=? x{uncached} (formulas, no cached value)" if uncached else "empty"
    main = max(filled, key=lambda k: (len(filled[k]), k))
    vals = values[main]
    parts = [main]
    if main in ("num", "date", "datetime", "time", "duration"):
        with contextlib.suppress(TypeError):  # naive vs aware datetimes
            lo, hi = min(vals), max(vals)
            parts.append(_fmt(lo, 3) if lo == hi else f"{_fmt(lo, 3)}..{_fmt(hi, 3)}")
    distinct = list(dict.fromkeys(vals))
    if main in ("text", "bool"):
        parts.append("e.g. " + ",".join(_fmt(v) for v in distinct[:3]))
    parts.append(f"distinct {len(distinct)}")
    if null := len(kinds.get("null", ())):
        parts.append(f"null {round(100 * null / len(rows))}%")
    if uncached := len(kinds.get("=?", ())):
        parts.append(f"=? {uncached}")
    for kind in sorted(filled, key=lambda k: -len(filled[k])):
        if kind != main:
            odd = filled[kind]
            sample = ",".join(_fmt(v) for v in list(dict.fromkeys(values[kind]))[:2])
            parts.append(f"odd {kind} {len(odd)} at {_rows(odd)} ({sample})")
    return " ".join(parts)


def profile_region(
    wb: Workbook, file_id: str, ref: str, offset: int = 0, budget: int = 24_000
) -> dict:
    """Describe each column of one table (region id from sheet_regions). Use before a pipeline.

    One line per column: `D JANUARI|PKG: num 0..523 distinct 31 null 4% odd text 2 at r25,r26 (x)`
    = column letter, name from the header rows, main type with min..max, distinct values,
    share of empty cells, `=? n` = formulas without a cached value, and `odd` = cells of
    another type and their rows (often subtotal or label rows to drop, like JAN in a date
    column). Total rows are left out of the numbers and listed in the first line.
    `offset` continues a page of columns; `budget` caps the text in chars.
    """
    _paging(offset)
    _check_budget(budget)
    label, _, region, g = _region(wb, file_id, ref)
    top, left, bottom, right = region.box
    totals = set(region.total_rows)
    rows = [r for r in range(region.data_start, bottom + 1) if r not in totals]
    hdr = region.header_rows
    head = (
        f"{label} {_box(region.box)}: {len(rows)} data rows"
        + (f" from r{rows[0] + 1}" if rows else "")
        + (f", header r{hdr[0] + 1}-r{hdr[-1] + 1}" if hdr else ", no header")
        + (f", totals left out: {_rows(sorted(totals), 6)}" if totals else "")
        + ". col name: type range|e.g. distinct null =? odd"
    )
    names = _column_names(g, region)
    body = [
        _clip(f"{col_letters(c)} {names[i]}: {_profile_column(g, c, rows) if rows else 'no data'}")
        for i, c in enumerate(range(left, right + 1))
    ]
    return _fit(head, body, offset, budget)


_WS = re.compile(r"\s+")


def _text_key(s: str) -> str:
    """Text ready to match: one casefolded line, unicode look-alikes folded (ﬀ = ff)."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", s).casefold()).strip()


def find_text(
    wb: Workbook, file_id: str, query: str, offset: int = 0, budget: int = 24_000
) -> dict:
    """Find text cells that contain a query, to locate a label or term before reading a sheet.

    Case, extra spaces and unicode look-alikes (ﬀ = ff, non-breaking space) are ignored.
    Text cells only: numbers and dates never match. One line per sheet with a match, in
    workbook order: `F1.S9 Per Bulan: 12 hits | A4=TANGGAL | ... +10`, positions in
    reading order, up to 6 shown per sheet, `+N` = further cells in that sheet.
    `offset` pages over the matching sheets; `budget` caps the text in chars.
    """
    _paging(offset)
    _check_budget(budget)
    if not isinstance(query, str):
        raise WorkbookError(f"query must be a string, got {query!r}")
    key = _text_key(query)
    if not key:
        raise WorkbookError(f"query {query!r} is empty once whitespace is trimmed")
    # values() is cached per sheet and the normalized texts are kept on the workbook, so
    # later pages of the same query cost one pass of containment checks.
    norm: dict[str, str] = wb.memo.setdefault("text_keys", {})

    def _key(v: str) -> str:
        k = norm.get(v)
        return k if k is not None else norm.setdefault(v, _text_key(v))

    body = []
    cells = 0
    for i, s in enumerate(wb.sheets, start=1):
        hits = [
            f"{a1(r, c)}={_name(_fmt(v))}"
            for r, row in enumerate(wb.values(s.name))
            for c, v in enumerate(row)
            if isinstance(v, str) and key in _key(v)
        ]
        if not hits:
            continue
        cells += len(hits)
        flag = "" if s.visible == "visible" else f" [{s.visible}]"
        shown = " | ".join(hits[:6])
        more = f" | +{len(hits) - 6}" if len(hits) > 6 else ""
        body.append(
            _clip(f"{file_id}.S{i} {_name(s.name)}{flag}: {len(hits)} hits | {shown}{more}")
        )
    head = (
        f"{file_id}: '{_name(query)}' matches {cells} cells in "
        f"{len(body)} of {len(wb.sheets)} sheets. sheet: hits | cell=text"
    )
    return _fit(head, body, offset, budget)
