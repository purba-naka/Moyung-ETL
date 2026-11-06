from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import a1
from excel_etl.inspect.regions import detect_regions
from excel_etl.workbook import Workbook


def _ref(box: tuple[int, int, int, int]) -> str:
    return f"{a1(box[0], box[1])}:{a1(box[2], box[3])}"


def test_grouped_header_with_dashboard_noise() -> None:
    rows = [
        ["NPK PKG", "Penjualan", None, None, None],
        ["DASHBOARD", 10, 0, 20, 0],
        ["TANGGAL", "Phonska 1", None, "Phonska 2", None],
        [None, "Ton", "Formula", "Ton", "Formula"],
        *[[date(2026, 1, d), d, "sub c", d * 2, "sub c"] for d in range(1, 6)],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}, merged={"S": ["B3:C3", "D3:E3"]}))
    (r,) = detect_regions(wb, "S")
    assert _ref(r.box) == "A3:E9"
    assert r.header_rows == (2, 3)
    assert len(r.merged_header) == 2
    assert [_ref(n) for n in r.noise] == ["A1:E2"]
    (g,) = r.groups
    assert (g.start, g.period, g.count, g.labels) == (1, 2, 2, ("Ton", "Formula"))
    assert g.parents == ("Phonska 1", "Phonska 2")


def test_total_row_and_repeated_header_split() -> None:
    hdr = [["Deskripsi", "JANUARI", None, "FEBRUARI", None], [None, "PKG", "PKC", "PKG", "PKC"]]
    body = [["1.1. Subsidi", 1, 2, 3, 4], ["1.2. Retail", 5, 6, 7, 8], ["Total", 6, 8, 10, 12]]
    rows = [*hdr, *body, [None] * 5, *hdr, *body]
    wb = Workbook.open(make_xlsx({"S": rows}))
    a, b = detect_regions(wb, "S")
    assert (_ref(a.box), _ref(b.box)) == ("A1:E5", "A7:E11")
    assert a.header_rows == (0, 1) and b.header_rows == (6, 7)
    assert a.total_rows == (4,) and b.total_rows == (10,)
    assert a.orientation == "matrix"
    assert a.groups[0].parents == ("JANUARI", "FEBRUARI")


def test_side_by_side_tables_and_tiny_notes() -> None:
    rows = [
        ["a", "b", None, None, "x", "y"],
        [1, 2, None, None, 3, 4],
        [1, 2, None, None, 3, 4],
        [1, 2, None, None, 3, 4],
        [None] * 6,
        ["note"],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}))
    assert [_ref(r.box) for r in detect_regions(wb, "S")] == ["A1:B4", "E1:F4"]


def test_empty_sheet() -> None:
    wb = Workbook.open(make_xlsx({"S": []}))
    assert detect_regions(wb, "S") == []


@pytest.mark.sample
def test_sample_layouts(sample_path) -> None:
    wb = Workbook.open(sample_path)
    pkg = detect_regions(wb, "(N) PKG")[0]
    assert _ref(pkg.box).startswith("A4:") and pkg.header_rows == (3, 4)
    assert pkg.groups[0].labels == ("Ton", "Formula") and pkg.groups[0].count >= 9

    big = [r for r in detect_regions(wb, "Per Bulan") if r.header_rows]
    assert len(big) >= 3
    assert {_ref(r.box).split(":")[0] for r in big} >= {"A4", "A58"}
    assert big[0].groups[0].parents[:2] == ("JANUARI", "FEBRUARI")

    rkap = detect_regions(wb, "RKAP 2026")[0]
    assert _ref(rkap.box).startswith("B3:") and rkap.header_rows == (2, 3)

    rekap = detect_regions(wb, "REKAP BALANS")
    assert _ref(rekap[0].box) == "A2:N9" and rekap[0].header_rows == (1, 2)
    assert len(rekap[0].noise) == 1  # "Periode | Jul"
