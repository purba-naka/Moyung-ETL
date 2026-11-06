from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import col_index, render_range
from excel_etl.workbook import Workbook, WorkbookError


def _wb() -> Workbook:
    rows = [
        ["Judul"],
        ["TANGGAL", "Phonska 1", None, "Phonska 2", None],
        [None, "Ton", "Formula", "Ton", "Formula"],
        *[[date(2026, 1, d), d * 1.5, "sub c", 0, "sub c"] for d in range(1, 5)],
        ["Total", "=SUM(B4:B7)", None, 0, None],
    ]
    return Workbook.open(make_xlsx({"S": rows}, merged={"S": ["B2:C2", "D2:E2"]}))


@pytest.mark.parametrize(("s", "c"), [("A", 0), ("Z", 25), ("AA", 26), ("FC", 158)])
def test_col_index(s: str, c: int) -> None:
    assert col_index(s) == c


def test_compressed_summarizes_runs_and_texts() -> None:
    lines = render_range(_wb(), "F1", "S")["text"].splitlines()
    assert lines[0] == "F1.S1 S A1:E8 compressed, rows 1-based"
    assert "r2: A=TANGGAL|B=Phonska 1|D=Phonska 2" in lines
    assert "col A: 1=Judul|8=Total" in lines
    assert '"Ton": B3,D3' in lines
    assert '"sub c": C4:C7,E4:E7' in lines
    assert "A4:A7 date 2026-01-01..2026-01-04" in lines
    assert "B4:B8 num 1.5..6 +=?" in lines  # uncached SUM joins the run
    assert "all 0: D4:D8" in lines


def test_raw_marks_merged_and_uncached() -> None:
    out = render_range(_wb(), "F1", "F1.S1", "A2:E3", mode="raw")
    assert out["text"].splitlines() == [
        "F1.S1 S A2:E3 raw, rows 1-based, first column A",
        "2|TANGGAL|Phonska 1|^|Phonska 2|^",
        "3||Ton|Formula|Ton|Formula",
    ]
    assert render_range(_wb(), "F1", "S", "B8", mode="raw")["text"].endswith("8|=?")


def test_region_ref_budget_and_errors() -> None:
    wb = _wb()
    region = render_range(wb, "F1", "F1.S1.R1", mode="raw")
    assert region["text"].splitlines()[0].startswith("F1.S1.R1 S A2:E8 raw")

    page = render_range(wb, "F1", "S", mode="raw", budget=120)
    assert page["truncated"] and len(page["text"]) <= 120
    nxt = page["next_offset"]
    rest = render_range(wb, "F1", "S", mode="raw", offset=nxt)
    assert rest["text"].splitlines()[1].startswith(f"{nxt + 1}|")  # no blank rows in S

    for ref, rng in (("F1.S1.R9", None), ("S", "B4:"), ("nope", None)):
        with pytest.raises(WorkbookError):
            render_range(wb, "F1", ref, rng)


@pytest.mark.sample
def test_sample_compressed_budget(sample_path) -> None:
    wb = Workbook.open(sample_path)
    pkg = render_range(wb, "F1", "(N) PKG")
    # sheetwise needs ~34k tokens for this sheet (IMPLEMENTATION §Temuan)
    assert not pkg["truncated"] and len(pkg["text"]) / 4 < 1000
    assert "A6:A806 date 2026-01-01..2027-12-31" in pkg["text"]
    for name in ("Per Bulan", "RKAP 2026", "(U) PKT"):
        assert len(render_range(wb, "F1", name)["text"]) / 4 < 6000, name
