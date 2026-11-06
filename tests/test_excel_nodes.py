from __future__ import annotations

from datetime import date

import duckdb
import pytest
from conftest import make_xlsx

from excel_etl.nodes import NodeError, build, infer, schema_of
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.pipeline import Source
from excel_etl.workbook import Workbook


def _rel(sql: str, con: duckdb.DuckDBPyConnection | None = None):
    return (con or connect()).sql(sql)


def _run(name: str, config: dict, rel):
    n = build(name, config)
    out = n.run([rel])
    assert infer(n, [schema_of(rel)]) == schema_of(out)  # the contract, on every case
    return out


_DAYS = """select * from (values
    ('2026-01-01', 1), ('JAN', 9), ('2026-01-02', 2), ('.', 0), (NULL, NULL)
) v("TANGGAL", "Ton")"""


def test_drop_rows_blank_only() -> None:
    rel = _rel("select * from (values (1, 'a'), (NULL, NULL), (NULL, 'b')) v(x, y)")
    assert _run("drop_rows", {}, rel).fetchall() == [(1, "a"), (None, "b")]


def test_drop_rows_keep_type_removes_subtotal_labels() -> None:
    rel = _rel(_DAYS)
    out = _run("drop_rows", {"column": "TANGGAL", "keep_type": "date"}, rel)
    assert out.fetchall() == [("2026-01-01", 1), ("2026-01-02", 2)]
    assert [c.type for c in schema_of(out)] == ["VARCHAR", "INTEGER"]  # type left alone


def test_drop_rows_regex_and_null_cells() -> None:
    rel = _rel("select * from (values ('Total A', 1), ('x', 2), (NULL, 3)) v(label, n)")
    out = _run("drop_rows", {"column": "label", "drop_regex": "^Total", "blank": False}, rel)
    assert out.fetchall() == [("x", 2), (None, 3)]  # NULL does not match, so it stays


def test_drop_rows_odd_column_name_and_quote_in_regex() -> None:
    rel = _rel("select * from (values ('it''s', 1), ('x', 2)) v(\"JAN|PKG\", n)")
    out = _run("drop_rows", {"column": "JAN|PKG", "drop_regex": "it's"}, rel)
    assert out.fetchall() == [("x", 2)]


@pytest.mark.parametrize(
    "config",
    [
        {"keep_type": "date"},
        {"column": "x"},
        {"column": "x", "drop_regex": "(["},
        {"column": "x", "keep_type": "money"},
        {"blank": "maybe"},
    ],
)
def test_drop_rows_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("drop_rows", config)


def test_drop_rows_names_a_missing_column() -> None:
    n = build("drop_rows", {"column": "zzz", "keep_type": "date"})
    with pytest.raises(NodeError, match="no column 'zzz'; columns: TANGGAL, Ton"):
        infer(n, [schema_of(_rel(_DAYS))])


def test_fill_down_fills_blanks_from_above_and_keeps_order() -> None:
    rel = _rel(
        "select * from (values (1, 'A', 1), (2, NULL, 2), (3, NULL, 3), (4, 'B', 4), (5, NULL, 5))"
        " v(i, grp, n) order by i"
    )
    out = _run("fill_down", {"columns": ["grp"]}, rel)
    assert out.fetchall() == [(1, "A", 1), (2, "A", 2), (3, "A", 3), (4, "B", 4), (5, "B", 5)]
    assert out.columns == ["i", "grp", "n"]  # same column order


def test_fill_down_leading_nulls_stay_null_and_several_columns() -> None:
    rel = _rel("select * from (values (1, NULL, NULL), (2, 'A', 'x'), (3, NULL, NULL)) v(i, a, b)")
    out = _run("fill_down", {"columns": ["a", "b", "a"]}, rel)
    assert out.fetchall() == [(1, None, None), (2, "A", "x"), (3, "A", "x")]


def test_fill_down_config_and_missing_column() -> None:
    with pytest.raises(NodeError):
        build("fill_down", {"columns": []})
    with pytest.raises(NodeError, match="no column 'q'"):
        infer(build("fill_down", {"columns": ["q"]}), [schema_of(_rel(_DAYS))])


def test_workbook_to_clean_table() -> None:
    # month labels sit among the dates, the group name is merged down over blank cells
    rows = [
        ["Grup", "TANGGAL", "Ton"],
        ["Urea", date(2026, 1, 1), 5],
        [None, date(2026, 1, 2), 6],
        [None, "JAN", 11],
        ["NPK", date(2026, 2, 1), 7],
        [None, "FEB", 7],
    ]
    wb = Workbook.open(make_xlsx({"Data": rows}))
    con = connect()
    src = Source.model_validate(
        {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["TANGGAL"]}}
    )
    rel = read_region(wb, src, con, "raw")
    rel = _run("fill_down", {"columns": ["Grup"]}, rel)
    rel = _run("drop_rows", {"column": "TANGGAL", "keep_type": "date"}, rel)
    assert rel.fetchall() == [
        ("Urea", "2026-01-01", 5),
        ("Urea", "2026-01-02", 6),
        ("NPK", "2026-02-01", 7),
    ]


@pytest.mark.sample
def test_sample_drop_subtotals(sample_path) -> None:
    wb = Workbook.open(sample_path)
    src = Source.model_validate(
        {
            "file_role": "b",
            "sheet": {"name_like": "(N) PKG"},
            "region": {"header_anchor": ["TANGGAL", "Phonska 1"], "header_rows": 2},
        }
    )
    con = connect()
    rel = read_region(wb, src, con, "pkg")
    clean = _run("drop_rows", {"column": "TANGGAL", "keep_type": "date"}, rel)
    n = clean.aggregate("count(*)").limit(1).fetchall()[0][0]
    assert n == 730  # every real day; the 70 subtotal and blank rows are gone
