from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest
from conftest import make_xlsx

from excel_etl.engine import run, validate
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.pipeline import SpecError, parse_spec
from excel_etl.workbook import Workbook


def _spec(pattern: str = "d[0-9]", column: str = "origin") -> object:
    return parse_spec(
        json.dumps(
            {
                "sources": {
                    "items": {
                        "file_role": "f",
                        "sheet": {"name_regex": pattern, "sheet_column": column},
                        "region": {"header_anchor": ["Item"]},
                    }
                },
                "steps": [],
                "outputs": {"result": {"input": "items"}},
            }
        )
    )


def _run(spec: object, sheets: dict[str, list[list]], tmp_path):
    return run(spec, {"f": Workbook.open(make_xlsx(sheets))}, tmp_path)


def _rows(tmp_path) -> list[tuple]:
    return (
        duckdb.connect()
        .execute(f"SELECT * FROM read_parquet('{(tmp_path / 'result.parquet').as_posix()}')")
        .fetchall()
    )


def test_reads_matching_sheets_in_workbook_and_row_order(tmp_path) -> None:
    sheets = {
        "d1": [["Item", "Qty"], ["a", 1], ["b", 2]],
        "notes": [["Item", "Qty"], ["ignored", 9]],
        "d2": [["Item", "Qty"], ["c", 3]],
        "d3": [["Item", "Qty"], ["d", 4]],
    }
    rep = _run(_spec(), sheets, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "a", 1), ("d1", "b", 2), ("d2", "c", 3), ("d3", "d", 4)]
    assert rep.nodes[0].note == "3 sheets: d1..d3"


def test_pattern_must_match_the_whole_sheet_name(tmp_path) -> None:
    sheets = {name: [["Item", "Qty"], [name, 1]] for name in ["d1", "xd1", "d1x"]}
    rep = _run(_spec(), sheets, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "d1", 1)]


def test_extra_columns_are_null_in_sheets_without_them(tmp_path) -> None:
    rep = _run(
        _spec(),
        {
            "d1": [["Item", "Qty"], ["a", 1]],
            "d2": [["Item", "Qty", "Extra"], ["b", 2, "x"]],
        },
        tmp_path,
    )
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "a", 1, None), ("d2", "b", 2, "x")]


def test_different_column_types_use_a_common_text_type(tmp_path) -> None:
    sheets = {"d1": [["Item", "Qty"], [1, 1]], "d2": [["Item", "Qty"], ["x", 2]]}
    rep = _run(_spec(), sheets, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "1", 1), ("d2", "x", 2)]
    checked = validate(_spec(), {"f": make_xlsx(sheets)})
    assert str(checked.schemas["items"][1].type) == "VARCHAR"


def test_date_and_number_columns_become_text_instead_of_failing(tmp_path) -> None:
    sheets = {
        "d1": [["Item", "Qty"], ["a", date(2026, 1, 2)]],
        "d2": [["Item", "Qty"], ["b", 3]],
    }
    rep = _run(_spec(), sheets, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "a", "2026-01-02"), ("d2", "b", "3")]


def test_one_matching_sheet_without_the_table_fails_the_run(tmp_path) -> None:
    rep = _run(
        _spec(),
        {"d1": [["Item", "Qty"], ["a", 1]], "d2": [["Other", "Qty"], ["b", 2]]},
        tmp_path,
    )
    assert rep.status == "failed"
    assert "1 sheet(s) failed" in rep.issues[0].message
    assert "d2" in rep.issues[0].message
    assert list(tmp_path.iterdir()) == []


def test_no_matching_sheets_fails_and_names_the_pattern(tmp_path) -> None:
    rep = _run(_spec(), {"other": [["Item"], ["a"]]}, tmp_path)
    assert rep.status == "failed"
    assert "d[0-9]" in rep.issues[0].message
    assert "other" in rep.issues[0].message


def test_sheet_column_cannot_clash_with_a_table_column(tmp_path) -> None:
    rep = _run(_spec(column="item"), {"d1": [["Item", "Qty"], ["a", 1]]}, tmp_path)
    assert rep.status == "failed"
    assert "'item'" in rep.issues[0].message and "'Item'" in rep.issues[0].message


@pytest.mark.parametrize(
    "sheet",
    [
        {"name_regex": "d[0-9]", "name": "d1", "sheet_column": "origin"},
        {"name_regex": "d[0-9]"},
        {"name": "d1", "sheet_column": "origin"},
        {"name_regex": "(", "sheet_column": "origin"},
    ],
)
def test_invalid_sheet_references_are_rejected(sheet: dict) -> None:
    with pytest.raises(SpecError):
        _spec_from_sheet(sheet)


def _spec_from_sheet(sheet: dict) -> object:
    return parse_spec(
        json.dumps(
            {
                "sources": {
                    "items": {
                        "file_role": "f",
                        "sheet": sheet,
                        "region": {"header_anchor": ["Item"]},
                    }
                },
                "steps": [],
                "outputs": {},
            }
        )
    )


@pytest.mark.parametrize("column", ["", "  ", "__private", "x" * 101])
def test_sheet_column_names_are_constrained(column: str) -> None:
    with pytest.raises(SpecError):
        _spec_from_sheet({"name_regex": "d[0-9]", "sheet_column": column})


def test_two_row_headers_are_read_on_every_sheet(tmp_path) -> None:
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "items": {
                        "file_role": "f",
                        "sheet": {"name_regex": "d[0-9]", "sheet_column": "origin"},
                        "region": {"header_anchor": ["Parent", "Leaf"], "header_rows": 2},
                    }
                },
                "steps": [],
                "outputs": {"result": {"input": "items"}},
            }
        )
    )
    rep = _run(
        spec,
        {
            "d1": [["Parent", None], ["Item", "Leaf"], ["a", 1]],
            "d2": [["Parent", None], ["Item", "Leaf"], ["b", 2]],
        },
        tmp_path,
    )
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d1", "a", 1), ("d2", "b", 2)]


def test_quote_in_sheet_name_is_safe(tmp_path) -> None:
    rep = _run(_spec(pattern="d'1"), {"d'1": [["Item", "Qty"], ["a", 1]]}, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [("d'1", "a", 1)]


def test_rereading_replaces_a_multi_sheet_table() -> None:
    con = connect()
    spec = _spec()
    first = Workbook.open(make_xlsx({"d1": [["Item", "Qty"], ["a", 1]]}))
    second = Workbook.open(make_xlsx({"d2": [["Item", "Qty"], ["b", 2]]}))
    read_region(first, spec.sources["items"], con, "items")
    read_region(second, spec.sources["items"], con, "items")
    assert con.table("items").fetchall() == [("d2", "b", 2)]


def test_order_is_stable_across_large_sheets(tmp_path, monkeypatch) -> None:
    sheets = {
        f"d{i}": [["Item", "Seq"], *[[f"{i}-{j}", j] for j in range(3000)]] for i in range(1, 4)
    }
    original = duckdb.DuckDBPyConnection.execute

    def execute(con, query, *args, **kwargs):
        # DuckDB often happens to preserve UNION order. Simulate another legal plan so this
        # test still catches a missing ORDER BY after a DuckDB upgrade or on another machine.
        if " UNION ALL BY NAME " in query and " ORDER BY __k, __r" not in query:
            assert query.endswith(")")
            query = query[:-1] + " ORDER BY ALL DESC) ORDER BY ALL DESC"
        return original(con, query, *args, **kwargs)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", execute)
    rep = _run(_spec(), sheets, tmp_path)
    assert rep.status == "ok", rep.text()
    assert _rows(tmp_path) == [(f"d{i}", f"{i}-{j}", j) for i in range(1, 4) for j in range(3000)]
