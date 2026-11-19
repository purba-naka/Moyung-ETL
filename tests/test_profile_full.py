from __future__ import annotations

import json
import re
from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import profile_region
from excel_etl.nodes.base import connect
from excel_etl.profile import column_line, profile_table
from excel_etl.workbook import Workbook, WorkbookError


def _wb() -> Workbook:
    rows = [["id", "city", "amount", "day", "note"]]
    for i in range(1, 21):
        rows.append([i, "A" if i % 2 else "B", i * 10.0, date(2026, 1, i), None])
    rows.append([21, "A", 10000.0, date(2026, 1, 21), None])
    rows.append([21, "A", 10000.0, date(2026, 1, 21), None])
    rows.append([22, None, 20.0, date(2026, 1, 22), None])
    return Workbook.open(make_xlsx({"S": rows}))


def _full(**kw) -> list[str]:
    return profile_region(_wb(), "F1", "F1.S1.R1", depth="full", **kw)["text"].splitlines()


def test_full_head_has_provenance_and_duplicates():
    wb = _wb()
    head = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()[0]
    assert head.startswith("F1.S1.R1 S A1:E24: 23 data rows, 5 columns, duplicate rows 1")
    assert "basis all rows" in head
    assert f"sha {wb.sha256[:12]}" in head and re.search(r", at 20\d\d-\d\d-\d\dT[0-9:]{8}Z", head)


def test_numeric_column_has_distribution_and_outlier_candidate():
    line = next(x for x in _full() if x.startswith("amount:"))
    assert "BIGINT" in line
    for part in ("min 10", "max 10000", "median", "mean", "std", "q25", "q75", "outliers 2"):
        assert part in line, line


def test_text_column_has_top_values_and_length():
    line = next(x for x in _full() if x.startswith("city:"))
    assert "VARCHAR" in line and "null 1 (4%)" in line
    assert "top A:12" in line and "len 1..1" in line


def test_all_null_column_and_key_candidate():
    lines = _full()
    assert "all null" in next(x for x in lines if x.startswith("note:"))
    assert "unique" not in next(x for x in lines if x.startswith("id:"))


def test_unique_non_null_column_is_flagged_as_key_candidate():
    rows = [["k", "v"], *[[f"k{i}", 1] for i in range(5)]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    line = next(
        x
        for x in profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()
        if x.startswith("k:")
    )
    assert "unique key candidate" in line


def test_full_head_lists_key_candidates():
    rows = [["site", "month", "qty"], ["A", 1, 5], ["A", 2, 5], ["B", 1, 5], ["B", 2, 5]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    head = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()[0]
    assert head.endswith("; key candidates: site+month")
    assert _full()[0].endswith("; key candidates: none")


def test_quick_depth_is_unchanged_default():
    quick = profile_region(_wb(), "F1", "F1.S1.R1")["text"]
    assert quick == profile_region(_wb(), "F1", "F1.S1.R1", depth="quick")["text"]
    assert "basis" not in quick


def test_bad_depth_is_a_workbook_error():
    with pytest.raises(WorkbookError, match="depth"):
        profile_region(_wb(), "F1", "F1.S1.R1", depth="deep")


def test_empty_table_says_no_data():
    con = connect()
    try:
        con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        prof = profile_table(con, "t")
    finally:
        con.close()
    assert prof["rows"] == 0 and prof["duplicate_rows"] == 0
    assert [c["null"] for c in prof["columns"]] == [0, 0]
    assert column_line(prof["columns"][0], 0) == "INTEGER no data"


def test_header_only_sheet_has_no_region_to_profile():
    wb = Workbook.open(make_xlsx({"S": [["a", "b"]]}))
    with pytest.raises(WorkbookError, match="no regions"):
        profile_region(wb, "F1", "F1.S1.R1", depth="full")


def test_total_row_is_flagged_because_a_pipeline_reads_it_as_data():
    rows = [["k", "v"], *[[f"k{i}", i] for i in range(1, 5)], ["Total", 10]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    head = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()[0]
    assert "5 data rows" in head and "total rows r6 included in these numbers" in head


def test_budget_truncates_and_offset_continues():
    first = profile_region(_wb(), "F1", "F1.S1.R1", depth="full", budget=400)
    assert first["truncated"] is True
    rest = profile_region(_wb(), "F1", "F1.S1.R1", depth="full", offset=first["next_offset"])
    assert not rest["truncated"]


def test_profile_table_is_deterministic_and_json_serialisable():
    con = connect()
    try:
        con.execute("CREATE TABLE t AS SELECT range AS x, 'v' || (range % 3) AS s FROM range(50)")
        a = profile_table(con, "t")
        b = profile_table(con, "t")
    finally:
        con.close()
    assert a == b
    assert a["rows"] == 50
    assert [c["name"] for c in a["columns"]] == ["x", "s"]
    json.dumps(a)
    s = a["columns"][1]
    assert s["distinct"] == 3 and s["top"][0][1] in (17, 17, 16)
    assert s["null"] == 0


def test_hostile_column_names_are_quoted():
    con = connect()
    try:
        con.execute('CREATE TABLE t ("a|b" INTEGER, "x""y" VARCHAR)')
        con.execute("INSERT INTO t VALUES (1, 'q'), (2, NULL)")
        prof = profile_table(con, "t")
    finally:
        con.close()
    assert [c["name"] for c in prof["columns"]] == ["a|b", 'x"y']


def test_unsigned_integers_get_numeric_stats():
    con = connect()
    try:
        con.execute("CREATE TABLE t AS SELECT 5::UBIGINT AS a UNION ALL SELECT 9::UBIGINT")
        col = profile_table(con, "t")["columns"][0]
    finally:
        con.close()
    assert col["min"] == 5 and col["max"] == 9 and col["mean"] == 7


def test_nan_and_infinity_never_reach_the_result():
    con = connect()
    try:
        con.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES (1.0), ('inf'::DOUBLE), ('nan'::DOUBLE)) v(x)"
        )
        prof = profile_table(con, "t")
    finally:
        con.close()
    json.dumps(prof, allow_nan=False)
    assert prof["columns"][0]["non_finite"] == 2


def test_huge_doubles_print_in_short_form():
    line = column_line(
        {
            "name": "x", "type": "DOUBLE", "null": 0, "distinct": 2, "unique": False,
            "min": 1.0, "max": 1e300, "mean": 5e299, "median": 5e299, "std": 5e299,
            "q25": 1.0, "q75": 1e300,
        },
        2,
    )  # fmt: skip
    assert len(line) < 160 and "e+" in line


def test_null_is_counted_not_only_a_rounded_percent():
    rows = [["a", "b"], *[[i, None if i < 3 else "x"] for i in range(300)]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    text = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"]
    assert "null 3 (1%)" in text


def test_text_distinct_says_how_many_remain_after_trim_and_lower():
    values = ["JKT", "jkt", " JKT ", "BDG", "bdg", "SBY", "JKT"]
    rows = [["code", "n"], *[[v, i] for i, v in enumerate(values)]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    lines = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()
    line = next(x for x in lines if x.startswith("code:"))
    assert "distinct 6 (3 after trim+lower)" in line


def test_text_distinct_is_left_plain_when_trim_and_lower_change_nothing():
    rows = [["code", "n"], ["A", 1], ["B", 2], ["C", 3]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    lines = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()
    assert "after trim+lower" not in next(x for x in lines if x.startswith("code:"))


def test_line_breaking_text_values_stay_on_one_line():
    rows = [["k", "n"], ["a\nb|c", 1], ["a\nb|c", 2], ["x", 3]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    lines = profile_region(wb, "F1", "F1.S1.R1", depth="full")["text"].splitlines()
    assert len(lines) == 3 and lines[1].startswith("k: VARCHAR top a b/c:2")


def test_a_loader_crash_is_a_plain_workbook_error():
    wb = Workbook.open(make_xlsx({"S": [["id", "m"], [1, 1e308], [2, 1.5]]}))
    with pytest.raises(WorkbookError, match="F1.S1.R1"):
        profile_region(wb, "F1", "F1.S1.R1", depth="full")


def test_loader_failure_message_has_no_internal_error_marker():
    wb = Workbook.open(make_xlsx({"S": [["id", "m"], [1, 1e308], [2, 1.5]]}))
    with pytest.raises(WorkbookError) as caught:
        profile_region(wb, "F1", "F1.S1.R1", depth="full")
    assert "internal error" not in str(caught.value)
