from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import profile_region
from excel_etl.workbook import Workbook, WorkbookError


def _wb() -> Workbook:
    rows = [
        ["Judul"],
        ["TANGGAL", "JANUARI", None, "FEBRUARI", None],
        [None, "PKG", "PKC", "PKG", "PKC"],
        *[[date(2026, 1, d), d, d * 2, None, "=B4*2"] for d in range(1, 6)],
        ["JAN", 15, 30, 0, 0],  # a subtotal row inside the date column
        *[[date(2026, 2, d), d, 1, 2, 3] for d in range(1, 4)],
        ["Total", 21, 33, 2, 3],
    ]
    # TANGGAL is merged down over both header rows, months are not merged
    return Workbook.open(make_xlsx({"S": rows}, merged={"S": ["A2:A3"]}))


def _lines(**kw) -> list[str]:
    return profile_region(_wb(), "F1", "F1.S1.R1", **kw)["text"].splitlines()


def test_profile_columns() -> None:
    lines = _lines()
    assert lines[0].startswith("F1.S1.R1 S A2:E13: 9 data rows from r4, header r2-r3")
    assert "totals left out: r13" in lines[0]
    assert lines[1:] == [
        "A TANGGAL: date 2026-01-01..2026-02-03 distinct 8 odd text 1 at r9 (JAN)",
        "B JANUARI|PKG: num 1..15 distinct 6",
        "C JANUARI|PKC: num 1..30 distinct 7",
        "D FEBRUARI|PKG: num 0..2 distinct 2 null 56%",
        "E FEBRUARI|PKC: num 0..3 distinct 2 =? 5",
    ]


def test_profile_pages_by_column() -> None:
    first = profile_region(_wb(), "F1", "F1.S1.R1", budget=200)
    assert first["truncated"] and len(first["text"]) <= 200 + 100  # head line always shows
    rest = profile_region(_wb(), "F1", "F1.S1.R1", offset=first["next_offset"])
    assert not rest["truncated"]
    shown = first["text"].splitlines()[1:] + rest["text"].splitlines()[1:]
    assert [x[0] for x in shown] == list("ABCDE")


@pytest.mark.parametrize("ref", ["S", "F1.S1", "F1.S1.R9", None, "F1.S1.R1x"])
def test_profile_needs_a_region_id(ref) -> None:
    with pytest.raises(WorkbookError):
        profile_region(_wb(), "F1", ref)


@pytest.mark.sample
def test_sample_profile(sample_path) -> None:
    wb = Workbook.open(sample_path)
    pkg = profile_region(wb, "F1", "F1.S59.R1")["text"]
    assert len(pkg) / 4 < 1500
    assert "A TANGGAL: date 2026-01-01..2026-07-31" in pkg and "(JAN,.JAN)" in pkg
    assert "B Phonska 1|Ton: num" in pkg
    per_bulan = profile_region(wb, "F1", "F1.S56.R1")["text"]
    assert "D JANUARI|PKG: num" in per_bulan and len(per_bulan) / 4 < 6000
