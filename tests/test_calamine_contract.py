"""Pin the calamine behavior the library relies on (AGENTS.md section 4).

If a calamine upgrade changes any of this, this test fails first instead of
the region detector quietly going wrong.
"""

from __future__ import annotations

import io
from datetime import date

from conftest import make_xlsx
from python_calamine import CalamineWorkbook, SheetVisibleEnum


def test_calamine_contract() -> None:
    data = make_xlsx(
        {
            "Data": [["Head", None, 10], [date(2026, 1, 1), "x", 2.5]],
            "Secret": [["a"]],
        },
        merged={"Data": ["A1:B1"]},
        hidden=("Secret",),
    )
    wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
    sheet = wb.get_sheet_by_name("Data")
    rows = sheet.to_python(skip_empty_area=False)

    assert rows[0][1] == ""
    assert rows[0][2] == 10.0 and isinstance(rows[0][2], float)
    assert isinstance(rows[1][0], date)
    assert sheet.merged_cell_ranges == [((0, 0), (0, 1))]
    # SheetVisibleEnum is a Rust enum, not a Python Enum, so it has no .name; compare members.
    assert [m.visible for m in wb.sheets_metadata] == [
        SheetVisibleEnum.Visible,
        SheetVisibleEnum.Hidden,
    ]
