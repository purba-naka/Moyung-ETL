from __future__ import annotations

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import formula_lineage
from excel_etl.workbook import Workbook, WorkbookError


def test_formula_lineage_lines() -> None:
    sheets = {
        "Input": [[1], [2]],
        "Other, with comma": [[5]],
        "Report": [["=Input!A1+Input!A2"], ["='Other, with comma'!A1"], ["=[1]Ext!A1"]],
        "Alone": [["=SUM(A2:A3)"], [1]],
    }
    out = formula_lineage(Workbook.open(make_xlsx(sheets)), "F1")
    assert out["text"].splitlines() == [
        "F1 1 sheets read other sheets. sheet <- sheet xN",
        "F1.S3 Report <- F1.S1 Input x1, F1.S2 Other, with comma x1, [file] x1",
    ]
    assert not out["truncated"]


def test_formula_lineage_pages_and_validates() -> None:
    sheets = {"Src": [[1]]}
    for i in range(5):
        sheets[f"R{i}"] = [["=Src!A1"]]
    wb = Workbook.open(make_xlsx(sheets))
    first = formula_lineage(wb, "F1", limit=2)
    assert first["truncated"] and first["next_offset"] == 2
    rest = formula_lineage(wb, "F1", offset=2, limit=10)
    assert not rest["truncated"] and len(rest["text"].splitlines()) == 4
    with pytest.raises(WorkbookError):
        formula_lineage(wb, "F1", limit=0)


def test_many_sources_are_cut_with_a_count() -> None:
    sheets = {f"S{i}": [[1]] for i in range(9)}
    sheets["R"] = [[f"=S{i}!A1"] for i in range(9)]
    line = formula_lineage(Workbook.open(make_xlsx(sheets)), "F1")["text"].splitlines()[1]
    assert line.endswith("+3 more") and line.count(" x1") == 6


@pytest.mark.sample
def test_sample_lineage_tool_budget(sample_path) -> None:
    out = formula_lineage(Workbook.open(sample_path), "F1")
    assert len(out["text"]) / 4 < 6000 and not out["truncated"]
    assert "(BB) CR" in out["text"]
