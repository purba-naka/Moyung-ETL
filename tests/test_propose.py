from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import propose_datasets
from excel_etl.workbook import Workbook, WorkbookError


def _wb() -> Workbook:
    return Workbook.open(
        make_xlsx(
            {
                # typed numbers only: a source table
                "Data": [
                    ["TANGGAL", "PKG", "PKC"],
                    *[[date(2026, 1, d), d, d * 2] for d in range(1, 6)],
                ],
                # formulas reading Data: derived
                "Rekap": [
                    ["BULAN", "TOTAL"],
                    ["JAN", "=Data!B2+Data!C2"],
                ],
                # notes next to the table, no header: not a dataset
                "Side": [["catatan"], ["KPI 1"]],
            }
        )
    )


def test_propose_lists_source_tables() -> None:
    out = propose_datasets(_wb(), "F1")
    lines = out["text"].splitlines()
    assert lines[0] == (
        "F1: 1 datasets to choose from (1 source, 0 mixed, 1 derived tables left out) "
        "in 3 sheets. id|sheet|range|role|rows|cols|columns"
    )
    assert lines[1] == "F1.S1.R1|Data|A1:C6|source|5|3|TANGGAL, PKG (+1)"
    assert (
        lines[2] == "derived sheets to check against (what they read: formula_lineage): "
        "F1.S2 Rekap: 1 derived tables <- Data x1"
    )
    assert not out["truncated"] and out["next_offset"] is None


def test_propose_mixed_when_typed_and_cross() -> None:
    wb = Workbook.open(
        make_xlsx(
            {
                "Data": [["TANGGAL", "PKG"], [date(2026, 1, 5), 10]],
                # typed prognosis next to a cross formula: mixed
                "Mix": [
                    ["TANGGAL", "RKAP", "ACTUAL"],
                    [date(2026, 1, 5), 7, "=Data!B2"],
                ],
            }
        )
    )
    out = propose_datasets(wb, "F1")
    assert "F1.S2.R1|Mix|A1:C2|mixed|1|3|TANGGAL, RKAP (+1)" in out["text"]


def test_propose_flags_hidden_and_pages() -> None:
    sheets = {name: [[f"Header {name}", "PKG"], [date(2026, 1, 5), 10]] for name in ("A", "B", "C")}
    wb = Workbook.open(make_xlsx(sheets, hidden=("B",)))
    first = propose_datasets(wb, "F1", limit=1)
    assert first["truncated"] and first["next_offset"] == 1
    rest = propose_datasets(wb, "F1", offset=1, limit=1)
    assert "F1.S2.R1|B [hidden]|A1:B2|source|1|2" in rest["text"]
    assert "F1.S3.R1|C|A1:B2|source|1|2" in propose_datasets(wb, "F1", offset=2)["text"]


def test_propose_groups_repeated_sheets_into_one_dataset() -> None:
    sheets = {
        **{
            name: [["Item", "Qty"], ["a", 1], ["b", 2]]
            for name in ["m01", "m02", "m03", "m04", "m05"]
        },
        "other": [["Code", "Value"], [1, 2], [3, 4]],
    }
    out = propose_datasets(Workbook.open(make_xlsx(sheets)), "F1")
    assert out["text"].count("\n") == 2
    assert "F1.S1.R1|m01" in out["text"]
    assert "+4 same shape: F1.S1..F1.S5, name_regex m[0-9]{2}" in out["text"]
    assert "F1.S6.R1|other" in out["text"]
    assert "m02" not in out["text"]


def test_grouped_sheets_keep_unrelated_second_table_proposals() -> None:
    sheets = {
        name: [
            ["Item", "Qty"],
            ["a", 1],
            [],
            [],
            [f"Other {name}", "Value"],
            ["x", 2],
        ]
        for name in ["m01", "m02", "m03"]
    }
    out = propose_datasets(Workbook.open(make_xlsx(sheets)), "F1")
    assert len(out["text"].splitlines()) == 5
    assert "F1.S2.R2|m02" in out["text"] and "F1.S3.R2|m03" in out["text"]


def test_same_header_derived_sheets_are_not_grouped_away() -> None:
    sheets = {
        "m01": [["Item", "Qty"], ["a", 1]],
        **{name: [["Item", "Qty"], ["a", "=m01!B2"]] for name in ["m02", "m03", "m04"]},
    }
    out = propose_datasets(Workbook.open(make_xlsx(sheets)), "F1")
    assert "1 source" in out["text"] and "3 derived tables left out" in out["text"]
    assert all(f"F1.S{i}" in out["text"] for i in (2, 3, 4))


def test_propose_no_datasets() -> None:
    # a 2x1 block is under detect_regions' size cut, so a table must be at least 2x2
    wb = Workbook.open(make_xlsx({"Rekap": [["X", "Y"], ["=1+1", "=2+2"]]}))
    out = propose_datasets(wb, "F1")
    lines = out["text"].splitlines()
    assert lines[0] == (
        "F1: 0 datasets to choose from (0 source, 0 mixed, 1 derived tables left out) "
        "in 1 sheets. id|sheet|range|role|rows|cols|columns"
    )
    assert len(lines) == 1  # local-only formulas: no sheet to check against


@pytest.mark.parametrize("kw", [{"offset": -1}, {"limit": 0}, {"limit": None}])
def test_propose_rejects_bad_paging(kw) -> None:
    with pytest.raises(WorkbookError):
        propose_datasets(_wb(), "F1", **kw)


def test_propose_leaves_total_rows_out_of_the_count() -> None:
    # the same data-row count profile_region reports: the total row is not a data row
    wb = Workbook.open(
        make_xlsx(
            {
                "Data": [
                    ["TANGGAL", "PKG"],
                    *[[date(2026, 1, d), d] for d in range(1, 6)],
                    ["Total", "=B2+B3+B4+B5+B6"],
                ]
            }
        )
    )
    assert "F1.S1.R1|Data|A1:B7|source|5|2|TANGGAL, PKG" in propose_datasets(wb, "F1")["text"]


def test_propose_stray_kpi_does_not_name_a_check_sheet() -> None:
    # the derived table only totals its own sheet; the cross cell is a stray KPI outside
    # every table, so it must not put Rekap on the check-against list
    wb = Workbook.open(
        make_xlsx(
            {
                "Data": [["TANGGAL", "PKG"], [date(2026, 1, 5), 10]],
                "Rekap": [
                    ["BULAN", "TOTAL"],
                    ["JAN", "=B2*2"],
                    [],
                    [],
                    [],
                    ["KPI", "=Data!B2"],
                ],
            }
        )
    )
    assert "derived sheets to check against" not in propose_datasets(wb, "F1")["text"]


@pytest.mark.sample
def test_sample_propose(sample_path) -> None:
    wb = Workbook.open(sample_path)
    first = propose_datasets(wb, "F1")
    lines = first["text"].splitlines()
    assert len(first["text"]) / 4 < 6000
    assert first["truncated"] and first["next_offset"]
    assert "|source|" in lines[1] or "|mixed|" in lines[1]
    assert "derived sheets to check against" in lines[-1]
    second = propose_datasets(wb, "F1", offset=first["next_offset"])
    assert second["text"].splitlines()[1] != lines[1]
