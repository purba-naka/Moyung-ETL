from __future__ import annotations

import io
from datetime import date

import openpyxl
import pytest
from conftest import make_xlsx
from openpyxl.styles import Border, Font, PatternFill, Side

from excel_etl.workbook import CellMeta, Workbook, WorkbookError


def _styled() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "Head"
    ws["A1"].font = Font(bold=True)
    ws["B1"].fill = PatternFill("solid", fgColor="FFFF00")
    ws["A2"] = 3
    ws["B2"] = "=A2*2"
    ws["B2"].border = Border(bottom=Side(style="thin"))
    ws["C2"] = 0.5
    ws["C2"].number_format = "0%"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_values_merged_visibility() -> None:
    data = make_xlsx(
        {"Data": [["Head", None, 10, 2.5], [date(2026, 1, 1)]], "Secret": [["a"]]},
        merged={"Data": ["A1:B1"]},
        hidden=("Secret",),
    )
    wb = Workbook.open(data)

    assert [(s.name, s.visible) for s in wb.sheets] == [("Data", "visible"), ("Secret", "hidden")]
    rows = wb.values("Data")
    assert rows[0] == ("Head", None, 10, 2.5)
    assert type(rows[0][2]) is int
    assert rows[1][0] == date(2026, 1, 1)
    assert wb.merged("Data") == ((0, 0, 0, 1),)
    assert len(wb.sha256) == 64


def test_meta_formula_and_style() -> None:
    meta = Workbook.open(_styled()).meta("S")

    assert meta[(0, 0)] == CellMeta(bold=True)
    assert meta[(0, 1)] == CellMeta(fill=True)
    assert meta[(1, 1)] == CellMeta(formula="=A2*2", border=True)
    assert meta[(1, 2)] == CellMeta(number_format="0%")
    assert (1, 0) not in meta


def test_open_is_cached_by_content(tmp_path) -> None:
    data = make_xlsx({"A": [[1]]})
    path = tmp_path / "f.xlsx"
    path.write_bytes(data)
    assert Workbook.open(data) is Workbook.open(path)


@pytest.mark.parametrize(
    ("source", "sheet"),
    [(b"not excel", None), ("missing.xlsx", None), (make_xlsx({"A": [[1]]}), "Nope")],
)
def test_bad_input_raises_workbook_error(source, sheet) -> None:
    with pytest.raises(WorkbookError):
        Workbook.open(source).values(sheet)


def test_empty_sheet() -> None:
    wb = Workbook.open(make_xlsx({"E": []}))
    assert wb.values("E") == ()
    assert wb.meta("E") == {}


@pytest.mark.sample
def test_sample_reads_fast(sample_path) -> None:
    wb = Workbook.open(sample_path)
    assert len(wb.sheets) == 72
    assert sum(s.visible != "visible" for s in wb.sheets) == 4
    assert len(wb.values("(N) PKG")) >= 805
    assert any(m.formula for m in wb.meta("(N) PKG").values())
