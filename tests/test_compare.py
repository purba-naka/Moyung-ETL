from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx
from test_hardening import _zip_replace

from excel_etl.inspect.encode import compare_sheets
from excel_etl.workbook import Workbook, WorkbookError


def _cached(data: bytes) -> bytes:
    # openpyxl writes formulas with an empty cached value; Excel saves one
    return data.replace(b"<f>B2+C2</f><v />", b"<f>B2+C2</f><v>30</v>").replace(
        b"<f>B2*2</f><v />", b"<f>B2*2</f><v>20</v>"
    )


def _wb(**sheets: list[list[object]]) -> Workbook:
    return Workbook.open(make_xlsx(dict(sheets)))


def test_compare_same_values() -> None:
    rows = [["TANGGAL", "PKG"], [date(2026, 1, 5), 10], ["Total", 15]]
    out = compare_sheets(_wb(A=rows, B=[r[:] for r in rows]), "F1", "A", "B")
    assert out["text"].splitlines() == [
        "F1.S1 A vs F1.S2 B: same values",
        "shape 3x2 both; formulas 0 vs 0; regions 1 vs 1 same ranges",
    ]
    assert not out["truncated"] and out["next_offset"] is None


def test_compare_pasted_copy_has_no_formulas() -> None:
    rows = [
        ["TANGGAL", "PKG", "PKC"],
        [date(2026, 1, 5), 10, 20],
        ["Total", "=B2+C2", "=B2*2"],
    ]
    pasted = [["TANGGAL", "PKG", "PKC"], [date(2026, 1, 5), 10, 20], ["Total", 30, 20]]
    data = _zip_replace(make_xlsx({"Live": rows, "Pasted": pasted}), "sheet1.xml", _cached)
    lines = compare_sheets(Workbook.open(data), "F1", "Live", "Pasted")["text"].splitlines()
    assert lines[0].endswith(": same values")
    assert "formulas 2 vs 0" in lines[1]


def test_compare_values_differ() -> None:
    a = [
        ["TANGGAL", "PKG"],
        [date(2026, 1, 5), 10],
        [date(2026, 1, 6), 20],
        ["Total", 15],
    ]
    b = [r[:] for r in a]
    b[2][1] = 99
    b[3][1] = 16
    out = compare_sheets(_wb(A=a, B=b), "F1", "F1.S1", "F1.S2")
    assert out["text"].splitlines() == [
        "F1.S1 A vs F1.S2 B: values differ",
        "shape 4x2 both; 2 of 8 filled cells differ; formulas 0 vs 0; regions 1 vs 1 same ranges",
        "diffs: B3 20|99, B4 15|16; rows: 2 of 4 (r3,r4)",
    ]


def test_compare_shapes_differ() -> None:
    a = [["TANGGAL", "PKG"], [date(2026, 1, 5), 10]]
    b = [["Judul"], *a, ["Total", 15]]
    out = compare_sheets(_wb(A=a, B=b), "F1", "A", "B")
    lines = out["text"].splitlines()
    assert lines[0].endswith(": shapes differ")
    assert lines[1] == (
        "shape 2x2 vs 4x2; on the shared part 4 of 4 filled cells differ; "
        "formulas 0 vs 0; regions 1 vs 1 ranges differ"
    )
    assert lines[2] == (
        "diffs: A1 TANGGAL|Judul, B1 PKG|-, A2 2026-01-05|TANGGAL, B2 10|PKG; rows: 2 of 2 (r1,r2)"
    )


def test_compare_bool_is_not_one() -> None:
    out = compare_sheets(_wb(A=[[True]], B=[[1]]), "F1", "A", "B")
    assert "1 of 1 filled cells differ" in out["text"]


@pytest.mark.parametrize("a,b", [("A", "A"), ("F1.S1", "F1.S1"), ("A", "Nope"), ("Nope", "A")])
def test_compare_rejects_bad_sheets(a, b) -> None:
    with pytest.raises(WorkbookError):
        compare_sheets(_wb(A=[[1, 2], [3, 4]], B=[[1, 2], [3, 4]]), "F1", a, b)


def test_compare_rejects_bad_budget() -> None:
    with pytest.raises(WorkbookError):
        compare_sheets(_wb(A=[[1, 2], [3, 4]], B=[[1, 2], [3, 4]]), "F1", "A", "B", budget=0)


@pytest.mark.sample
def test_sample_compare(sample_path) -> None:
    wb = Workbook.open(sample_path)
    out = compare_sheets(wb, "F1", "supplyinout", "supplyinout (2)")
    lines = out["text"].splitlines()
    assert len(out["text"]) / 4 < 300
    assert lines[0].startswith("F1.")
    assert "shape" in lines[1] and "regions" in lines[1]
