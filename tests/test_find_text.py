from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import find_text
from excel_etl.workbook import Workbook, WorkbookError


def _wb() -> Workbook:
    return Workbook.open(
        make_xlsx(
            {
                "Data": [
                    ["TANGGAL", "JANUARI  PRODUKSI", None],
                    [date(2026, 1, 5), 10, "catatan"],
                ],
                "Lain": [["Judul"], ["ﬀlow"], ["2026"]],
            }
        )
    )


def test_find_reports_positions() -> None:
    out = find_text(_wb(), "F1", "tanggal")
    lines = out["text"].splitlines()
    assert lines[0] == "F1: 'tanggal' matches 1 cells in 1 of 2 sheets. sheet: hits | cell=text"
    assert lines[1] == "F1.S1 Data: 1 hits | A1=TANGGAL"
    assert not out["truncated"] and out["next_offset"] is None


def test_find_ignores_case_spaces_and_unicode_lookalikes() -> None:
    # double space in the cell, non-breaking space, ligature ff
    out = find_text(_wb(), "F1", "januari  produksi")["text"]
    assert "B1=JANUARI PRODUKSI" in out
    assert "B1=JANUARI PRODUKSI" in find_text(_wb(), "F1", "JANUARI\u00a0PRODUKSI")["text"]
    assert "A2=ﬀlow" in find_text(_wb(), "F1", "FFLOW")["text"]


def test_find_matches_text_cells_only() -> None:
    out = find_text(_wb(), "F1", "2026")["text"]
    assert "A3=2026" in out  # the text cell, not the date 2026-01-05
    assert not find_text(_wb(), "F1", "10")["text"].splitlines()[1:]


def test_find_hidden_flag_and_paging() -> None:
    wb = Workbook.open(
        make_xlsx(
            {"A": [["TANGGAL"]], "B": [["TANGGAL"]], "C": [["TANGGAL"]]},
            hidden=("B",),
        )
    )
    lines, offset = [], 0
    while True:
        out = find_text(wb, "F1", "tanggal", offset=offset, budget=100)
        lines += out["text"].splitlines()[1:]
        if not out["truncated"]:
            break
        offset = out["next_offset"]
    assert lines == [
        "F1.S1 A: 1 hits | A1=TANGGAL",
        "F1.S2 B [hidden]: 1 hits | A1=TANGGAL",
        "F1.S3 C: 1 hits | A1=TANGGAL",
    ]


def test_find_clips_many_hits_per_sheet() -> None:
    wb = Workbook.open(make_xlsx({"S": [[f"tangent-{i}"] for i in range(9)]}))
    line = find_text(wb, "F1", "tangent")["text"].splitlines()[1]
    assert line.startswith("F1.S1 S: 9 hits | A1=tangent-0")
    assert line.endswith("+3")


def test_find_clips_a_huge_query_in_the_head() -> None:
    # the matches count must survive a query so long it would eat the whole head line
    out = find_text(_wb(), "F1", "tanggal " + "x" * 5000)["text"]
    head = out.splitlines()[0]
    assert "matches 0 cells in 0 of 2 sheets" in head
    assert len(head) < 120


@pytest.mark.parametrize("query", ["", "   ", 5, None])
def test_find_rejects_bad_query(query) -> None:
    with pytest.raises(WorkbookError):
        find_text(_wb(), "F1", query)


@pytest.mark.parametrize("kw", [{"offset": -1}, {"budget": 0}])
def test_find_rejects_bad_paging(kw) -> None:
    with pytest.raises(WorkbookError):
        find_text(_wb(), "F1", "x", **kw)


@pytest.mark.sample
def test_sample_find(sample_path) -> None:
    wb = Workbook.open(sample_path)
    out = find_text(wb, "F1", "tanggal")
    text = out["text"]
    assert len(text) / 4 < 1500
    assert "matches" in text.splitlines()[0] and "TANGGAL" in text
