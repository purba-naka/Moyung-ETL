"""Regression tests from the phase 1 adversarial review (P6). Inputs are small and fixed."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import render_range, sheet_regions, workbook_map
from excel_etl.inspect.regions import detect_regions
from excel_etl.workbook import Workbook, WorkbookError


def _table(n: int = 5, shift: int = 0) -> list[list]:
    hdr = [["Item", "JAN", None, "FEB", None], [None, "PKG", "PKC", "PKG", "PKC"]]
    return [*hdr, *[[f"r{i + shift}", 1, 2, 3, 4] for i in range(n)]]


def _zip_replace(data: bytes, name_part: str, fn) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            dst.writestr(item, fn(body) if name_part in item.filename else body)
    return out.getvalue()


# --- input that must end as WorkbookError, never another exception -------------------------


@pytest.mark.parametrize(
    "rng",
    ["", "A0", "A1:A0", "B4:", "ZZZZ1", "A1048577", "A" + "9" * 5000, "ﬀ1", "A1:B2\nX", "1:2"],
)
def test_bad_range_is_workbook_error(rng: str) -> None:
    wb = Workbook.open(make_xlsx({"S": _table()}))
    with pytest.raises(WorkbookError):
        render_range(wb, "F1", "S", rng)


@pytest.mark.parametrize(
    "ref", ["nope", "F1.S9", "F1.S1.R0", "F1.S1.R99", "F1.S1.R" + "9" * 5000, "", "F1.S1.R-1"]
)
def test_bad_ref_is_workbook_error(ref: str) -> None:
    wb = Workbook.open(make_xlsx({"S": _table()}))
    with pytest.raises(WorkbookError):
        render_range(wb, "F1", ref)


@pytest.mark.parametrize(
    "kwargs",
    [{"offset": -1}, {"offset": 1.5}, {"budget": 0}, {"mode": "xx"}, {"range": 5}, {"ref": None}],
)
def test_bad_render_arguments(kwargs: dict) -> None:
    wb = Workbook.open(make_xlsx({"S": _table()}))
    with pytest.raises(WorkbookError):
        render_range(wb, **{"file_id": "F1", "ref": "S", **kwargs})


@pytest.mark.parametrize(("offset", "limit"), [(-1, 5), (0, 0), (0, -1), (0, 1.5)])
def test_bad_paging(offset: int, limit: int) -> None:
    wb = Workbook.open(make_xlsx({"S": _table()}))
    with pytest.raises(WorkbookError):
        workbook_map(wb, "F1", offset, limit)
    with pytest.raises(WorkbookError):
        sheet_regions(wb, "F1", "S", offset, limit)


def test_region_id_and_range_together_is_an_error() -> None:
    wb = Workbook.open(make_xlsx({"S": _table()}))
    with pytest.raises(WorkbookError, match="either"):
        render_range(wb, "F1", "F1.S1.R1", "A1:B2")


@pytest.mark.parametrize("source", [None, bytearray(b"x"), 5, "bad\0path"])
def test_open_odd_sources(source) -> None:
    with pytest.raises(WorkbookError):
        Workbook.open(source)


def test_sheet_named_like_an_id_does_not_shadow_the_id() -> None:
    wb = Workbook.open(make_xlsx({"A": [[1, 2], [3, 4]], "F1.S1": [[5, 6], [7, 8]]}))
    assert render_range(wb, "F1", "F1.S1", mode="raw")["text"].splitlines()[1] == "1|1|2"


# --- damaged or hostile files ---------------------------------------------------------------


def test_far_away_cell_is_refused_before_calamine_can_abort() -> None:
    wb = Workbook.open(make_xlsx({"S": [[1], [2]], "Far": [[1]]}))
    far = _zip_replace(
        make_xlsx({"S": [[1], [2]]}),
        "sheet1.xml",
        lambda b: b.replace(b'<c r="A1"', b'<c r="XFD1048576"', 1),
    )
    assert wb.values("S")  # the normal file is fine
    with pytest.raises(WorkbookError, match="far from the data"):
        Workbook.open(far).values("S")


def test_corrupt_sheet_xml_is_workbook_error() -> None:
    bad = _zip_replace(make_xlsx({"S": _table()}), "sheet1.xml", lambda b: b[: len(b) // 2])
    wb = Workbook.open(bad)
    with pytest.raises(WorkbookError):
        render_range(wb, "F1", "S")


def test_non_xlsx_zip_has_no_formula_scan() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", "<x/>")
    with pytest.raises(WorkbookError):
        Workbook.open(buf.getvalue())


# --- big or odd shapes stay cheap -----------------------------------------------------------


def test_huge_range_and_huge_merge_cost_nothing() -> None:
    # openpyxl would build a million cells for this merge, so write the XML tag ourselves
    data = _zip_replace(
        make_xlsx({"S": _table()}),
        "sheet1.xml",
        lambda b: b.replace(
            b"</sheetData>",
            b'</sheetData><mergeCells count="1"><mergeCell ref="G1:XFD1048576"/></mergeCells>',
        ),
    )
    wb = Workbook.open(data)
    assert wb.merged("S") == ((0, 6, 1048575, 16383),)
    out = render_range(wb, "F1", "S", "A1:XFD1048576")
    assert out["text"].startswith("F1.S1 S A1:") and not out["truncated"]
    assert [r.box for r in detect_regions(wb, "S") if r.header_rows] == [(0, 0, 6, 4)]
    assert render_range(wb, "F1", "S", "Z100:AA105")["text"].endswith("no cells in that range")


def test_one_wide_line_cannot_blow_the_budget() -> None:
    row = [f"col{i}" for i in range(3000)]
    wb = Workbook.open(make_xlsx({"S": [row, row]}))
    for mode in ("raw", "compressed"):
        out = render_range(wb, "F1", "S", mode=mode, budget=2000)
        assert all(len(line) <= 600 for line in out["text"].splitlines())


def test_many_stacked_tables_do_not_recurse() -> None:
    rows: list[list] = []
    for i in range(1200):
        rows += _table(2, i * 10) + [[None] * 5]
    wb = Workbook.open(make_xlsx({"S": rows}))
    regions = [r for r in detect_regions(wb, "S") if r.header_rows]
    assert len(regions) == 1200


# --- pagination walks -----------------------------------------------------------------------


def _walk(call) -> list[str]:
    lines, offset = [], 0
    for _ in range(200):
        out = call(offset)
        lines += out["text"].splitlines()[1:]
        if not out["truncated"]:
            return lines
        assert out["next_offset"] > offset
        offset = out["next_offset"]
    raise AssertionError("pagination did not finish")


def test_render_pagination_matches_one_big_call() -> None:
    wb = Workbook.open(
        make_xlsx({"S": [[date(2026, 1, 1 + d % 28), d, f"t{d}"] for d in range(1, 40)]})
    )
    for mode in ("raw", "compressed"):
        whole = render_range(wb, "F1", "S", mode=mode)["text"].splitlines()[1:]
        paged = _walk(lambda o, m=mode: render_range(wb, "F1", "S", mode=m, offset=o, budget=120))
        assert paged == whole


def test_sheet_regions_pagination_and_stable_ids() -> None:
    rows: list[list] = []
    for i in range(6):
        rows += _table(3, i) + [[None] * 5]
    wb = Workbook.open(make_xlsx({"S": rows}))
    whole = sheet_regions(wb, "F1", "S")["text"].splitlines()
    paged = _walk(lambda o: sheet_regions(wb, "F1", "S", offset=o, limit=2))
    assert [x.split("|")[0] for x in paged] == [x.split("|")[0] for x in whole[1:]]
    assert [x.split("|")[0] for x in whole[1:]] == [f"F1.S1.R{i}" for i in range(1, 7)]


def test_region_id_means_the_same_in_both_tools() -> None:
    # a headerless block comes first, so R numbers must count it in both views
    rows = [["note", "x"], ["note", "y"], [None] * 5, *[[None] * 5 for _ in range(2)]]
    rows += [[None, None, None, "a", "b"], [None, None, None, 1, 2], [None, None, None, 3, 4]]
    rows += [[None] * 5, *_table(3)]
    wb = Workbook.open(make_xlsx({"S": rows}))
    lines = sheet_regions(wb, "F1", "S")["text"].splitlines()
    line = next(x for x in lines if x.startswith("F1.S1.R"))
    rid, rng = line.split("|")[:2]
    assert render_range(wb, "F1", rid)["text"].startswith(f"{rid} S {rng} compressed")


def test_labels_cannot_break_line_format() -> None:
    rows = [["x|y\nz", "b|c"], [1, 2], [3, 4], [5, 6]]
    wb = Workbook.open(make_xlsx({"x|y\nz": rows}))
    text = workbook_map(wb, "F1")["text"].splitlines()
    assert len(text) == 2 and text[1].split("|")[1] == "x/y z"
    assert len(render_range(wb, "F1", "x|y\nz")["text"].splitlines()[0].split("|")) == 1


# --- budget: the synthetic version of the BALANSITAS check ----------------------------------


def test_compressed_800x160_sheet_stays_under_1000_tokens() -> None:
    hdr1 = [None] + [f"P{i // 2}" if i % 2 == 0 else None for i in range(158)]
    hdr2 = ["TANGGAL"] + ["Ton", "Formula"] * 79
    body = [
        [date(2026, 1, 1 + d % 28)] + [d + c if c % 2 == 0 else "sub c" for c in range(158)]
        for d in range(798)
    ]
    wb = Workbook.open(make_xlsx({"S": [hdr1, hdr2, *body]}))
    out = render_range(wb, "F1", "S")
    assert not out["truncated"] and len(out["text"]) / 4 < 1000, len(out["text"]) / 4
