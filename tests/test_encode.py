from __future__ import annotations

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import (
    col_letters,
    formula_lineage,
    propose_datasets,
    sheet_regions,
    workbook_map,
)
from excel_etl.workbook import Workbook, WorkbookError


@pytest.mark.parametrize(("c", "s"), [(0, "A"), (25, "Z"), (26, "AA"), (158, "FC"), (701, "ZZ")])
def test_col_letters(c: int, s: str) -> None:
    assert col_letters(c) == s


def test_workbook_map_roles_and_formula_without_cache() -> None:
    # openpyxl writes formulas with no cached value, so calamine sees those cells as empty.
    # The map must still count them, or a calculated sheet looks blank.
    data = make_xlsx(
        {
            "Input": [["Tanggal", "Ton"], ["2026-01-01", 5], ["2026-01-02", 7]],
            "Calc": [[None, None], [None, "=Input!B2*2"], [None, "=Input!B3*2"]],
            "Blank": [],
            "Old": [[7]],
        },
        hidden=("Old",),
    )
    out = workbook_map(Workbook.open(data), "F1")

    assert out["text"].splitlines() == [
        "F1 4 sheets. id|name|used|cells|regions|formula%|role",
        "F1.S1|Input|A1:B3|6|1|0|source",
        "F1.S2|Calc|B2:B3|2|0|100|derived",
        "F1.S3|Blank|-|0|0|0|empty",
        "F1.S4|Old [hidden]|A1:A1|1|0|0|source",
    ]
    assert out["truncated"] is False


def test_workbook_map_paginates() -> None:
    wb = Workbook.open(make_xlsx({f"S{i}": [[i]] for i in range(5)}))
    first = workbook_map(wb, "F1", limit=2)
    last = workbook_map(wb, "F1", offset=4, limit=2)

    assert (first["truncated"], first["next_offset"]) == (True, 2)
    assert first["text"].count("\n") == 2
    assert (last["truncated"], last["next_offset"]) == (False, None)


@pytest.mark.parametrize(
    "call",
    [
        lambda wb: workbook_map(wb, "F1", limit=None),
        lambda wb: formula_lineage(wb, "F1", limit=None),
        lambda wb: sheet_regions(wb, "F1", "S", limit=None),
        lambda wb: propose_datasets(wb, "F1", limit=None),
    ],
)
def test_paged_tools_reject_a_null_limit(call) -> None:
    # a host that sends explicit null for an omitted arg gets one clear error, not a TypeError
    from datetime import date

    wb = Workbook.open(make_xlsx({"S": [["TANGGAL", "PKG"], [date(2026, 1, 5), 10]]}))
    with pytest.raises(WorkbookError):
        call(wb)


def test_no_sheet_error_lists_only_a_few_names() -> None:
    wb = Workbook.open(make_xlsx({f"S{i}": [[i]] for i in range(7)}))
    with pytest.raises(WorkbookError, match=r"S4,\+2$"):
        sheet_regions(wb, "F1", "nope")


@pytest.mark.sample
def test_sample_workbook_map_budget(sample_path) -> None:
    wb = Workbook.open(sample_path)
    text = workbook_map(wb, "F1")["text"]

    assert text.count("\n") == 72
    assert len(text) / 4 < 2000
    assert sum(len(wb.formula_cells(s.name) or ()) for s in wb.sheets) >= 542_358


def _same_table_sheets(names) -> Workbook:
    return Workbook.open(make_xlsx({name: [["Item", "Qty"], ["a", 1], ["b", 2]] for name in names}))


def test_workbook_map_groups_same_shape_without_changing_sheet_ids() -> None:
    names = ["m01", "m02", "m03", "m04", "m05"]
    wb = Workbook.open(
        make_xlsx(
            {
                **{name: [["Item", "Qty"], ["a", 1], ["b", 2]] for name in names},
                "other": [["Code", "Value"], [1, 2]],
                "last": [["When", "Amount"], ["now", 3]],
            }
        )
    )
    text = workbook_map(wb, "F1")["text"]
    assert text.count("\n") == 3
    assert "F1.S1|m01" in text and "+4 same shape: F1.S1..F1.S5" in text
    assert "name_regex m[0-9]{2}" in text
    assert "F1.S6|other" in text and "F1.S7|last" in text
    assert "F1.S2|m02" not in text


def test_shape_group_with_different_name_patterns_lists_the_names() -> None:
    text = workbook_map(_same_table_sheets(["alpha", "beta", "gamma"]), "F1")["text"]
    assert "+2 same shape" in text
    assert "name_regex (?:alpha/beta/gamma)" in text  # `|` is shown as `/` in the map
    assert "names differ" not in text


def test_shape_group_with_too_many_odd_names_says_names_differ() -> None:
    names = [f"sheet_{chr(97 + i % 26)}{i}_" + "x" * 16 for i in range(30)]  # max 31 chars
    text = workbook_map(_same_table_sheets(names), "F1")["text"]
    assert "same shape" in text and "names differ" in text


def test_two_same_shape_sheets_are_not_grouped() -> None:
    text = workbook_map(_same_table_sheets(["m01", "m02"]), "F1")["text"]
    assert "same shape" not in text and "F1.S2|m02" in text


def test_pattern_matching_a_sheet_outside_the_group_is_not_proposed() -> None:
    wb = Workbook.open(
        make_xlsx(
            {
                **{name: [["Item", "Qty"], ["a", 1]] for name in ["m01", "m02", "m03"]},
                "m10": [["Different", "Headers"], [1, 2]],
            }
        )
    )
    text = workbook_map(wb, "F1")["text"]
    assert "names differ" in text and "name_regex" not in text


def test_workbook_map_grouping_preserves_original_sheet_paging() -> None:
    wb = _same_table_sheets([f"m{i:02}" for i in range(5)])
    out = workbook_map(wb, "F1", offset=4, limit=2)
    assert out["text"].startswith("F1 5 sheets") and not out["truncated"]
    assert out["text"].count("\n") == 0  # m05 is represented by the group on the first page
    assert "F1.S1..F1.S5" in workbook_map(wb, "F1")["text"]


def test_generated_group_pattern_does_not_break_pipe_delimited_fields() -> None:
    text = workbook_map(_same_table_sheets(["m|01", "m|02", "m|03"]), "F1")["text"]
    line = text.splitlines()[1]
    assert len(line.split("|")) == 8
    assert "name_regex m\\/[0-9]{2}" in line


def test_workbook_map_150_same_shape_sheets_stays_under_budget() -> None:
    wb = _same_table_sheets([f"m{i:03}" for i in range(150)])
    out = workbook_map(wb, "F1")
    assert len(out["text"]) < 1500 and "+149 same shape" in out["text"]


def test_sheet_regions_text_and_lookup() -> None:
    from datetime import date

    from excel_etl.inspect.encode import sheet_regions
    from excel_etl.workbook import WorkbookError

    rows = [
        ["Judul"],
        ["TANGGAL", "Phonska 1", None, "Phonska 2", None, None, "catatan", "y"],
        [None, "Ton", "Formula", "Ton", "Formula", None, "x", "z"],
        *[[date(2026, 1, d), d, "sub c", d, "sub c"] for d in range(1, 5)],
        ["Total", 10, None, 10, None],
    ]
    wb = Workbook.open(make_xlsx({"A": [[1]], "S": rows}, merged={"S": ["B2:C2", "D2:E2"]}))
    out = sheet_regions(wb, "F1", "F1.S2")
    assert out["text"].splitlines() == [
        "F1.S2 S: 1 tables, 1 headerless blocks. "
        "id|range|hdr rows|orientation/role|total rows|group|merged|noise (rows 1-based)",
        "F1.S2.R1|A2:E8|hdr 2-3|rows/source|total 8|group B:[Ton,Formula]x2 (Phonska 1..Phonska 2)"
        "|merged B2:C2,D2:E2|noise A1:A1",
        "headerless: R2 G2:H3",
    ]
    assert sheet_regions(wb, "F1", "S") == out
    with pytest.raises(WorkbookError):
        sheet_regions(wb, "F1", "nope")


@pytest.mark.sample
def test_sample_sheet_regions_budget(sample_path) -> None:
    from excel_etl.inspect.encode import sheet_regions

    wb = Workbook.open(sample_path)
    for name in ("(N) PKG", "Per Bulan", "RKAP 2026", "REKAP BALANS", "(U) PKT"):
        text = sheet_regions(wb, "F1", name)["text"]
        assert len(text) / 4 < 1000, (name, len(text))
    assert "hdr 4-5" in sheet_regions(wb, "F1", "(N) PKG")["text"]


def test_template_sheets_with_the_same_layout_do_not_spoil_the_dated_pattern() -> None:
    names = ["Template", "Copy of Template"] + [f"2026-01-{d:02d}" for d in range(1, 21)]
    text = workbook_map(_same_table_sheets(names), "F1")["text"]
    assert "name_regex [0-9]{4}" in text.replace("\\-", "-"), text
    assert "Template" not in text.split("same shape")[1]
