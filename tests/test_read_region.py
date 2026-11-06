from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.nodes import NodeError
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.pipeline import Source
from excel_etl.workbook import Workbook


def _rows(n: int = 4, shift: int = 0) -> list[list]:
    return [
        ["Laporan produksi"],
        [],
        ["TANGGAL", "JANUARI", None, "FEBRUARI", None],
        [None, "PKG", "PKC", "PKG", "PKC"],
        *[[date(2026, 1, d + 1), d + shift, d * 2.5, None, "x"] for d in range(n)],
        [],
        [],
        [],
        ["catatan di bawah"],
    ]


def _wb(**sheets) -> Workbook:
    return Workbook.open(make_xlsx(sheets or {"Data": _rows()}, merged={"Data": ["A3:A4"]}))


def _src(**over) -> Source:
    spec = {
        "file_role": "f",
        "sheet": {"name": "Data"},
        "region": {"header_anchor": ["TANGGAL", "JANUARI"], "header_rows": 2},
    }
    spec.update(over)
    return Source.model_validate(spec)


def test_reads_header_joined_and_typed() -> None:
    con = connect()
    rel = read_region(_wb(), _src(), con, "data")
    assert rel.columns == ["TANGGAL", "JANUARI|PKG", "JANUARI|PKC", "FEBRUARI|PKG", "FEBRUARI|PKC"]
    assert [str(t) for t in rel.types] == ["DATE", "BIGINT", "DOUBLE", "VARCHAR", "VARCHAR"]
    assert rel.fetchall()[0] == (date(2026, 1, 1), 0, 0.0, None, "x")
    assert len(rel.fetchall()) == 4  # stops at the blank rows, not at the note below


def test_gap_setting_decides_where_the_table_ends() -> None:
    wide = _src(
        region={"header_anchor": ["TANGGAL"], "header_rows": 2, "end": "until_blank_rows(9)"}
    )
    assert (
        len(read_region(_wb(), wide, connect(), "d").fetchall()) == 8
    )  # 4 rows, 3 blank, the note


def test_finds_the_sheet_by_name_like_and_fallback() -> None:
    wb = _wb()
    like = _src(sheet={"name_like": " data "})
    assert read_region(wb, like, connect(), "d").columns[0] == "TANGGAL"
    moved = _src(sheet={"name_like": "Old name", "fallback_contains": ["febru", "tanggal"]})
    assert read_region(wb, moved, connect(), "d").columns[0] == "TANGGAL"


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"sheet": {"name": "Nope"}}, "no sheet matches 'Nope'"),
        ({"sheet": {"name_like": "Nope", "fallback_contains": ["zzz"]}}, "no sheet matches"),
        (
            {"region": {"header_anchor": ["TANGGAL", "MARET"], "header_rows": 2}},
            "not found on sheet 'Data'",
        ),
        (
            {"region": {"header_anchor": ["catatan di bawah"], "header_rows": 1}},
            "but not inside a table",
        ),
    ],
)
def test_missing_pieces_say_what_to_check(over: dict, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        read_region(_wb(), _src(**over), connect(), "d")


def test_two_sheets_matching_the_fallback_is_an_error() -> None:
    wb = Workbook.open(make_xlsx({"A": _rows(), "B": _rows()}))
    src = _src(sheet={"fallback_contains": ["TANGGAL"]})
    with pytest.raises(NodeError, match="2 sheets hold all of"):
        read_region(wb, src, connect(), "d")


def test_mixed_column_is_text_and_nothing_is_lost() -> None:
    rows = [
        ["TANGGAL", "PKG"],
        [date(2026, 1, 1), 5],
        ["JAN", 6],
        [date(2026, 2, 1), 7],
    ]
    wb = Workbook.open(make_xlsx({"Data": rows}))
    src = _src(region={"header_anchor": ["TANGGAL"]})
    rel = read_region(wb, src, connect(), "d")
    assert [str(t) for t in rel.types] == ["VARCHAR", "BIGINT"]
    assert rel.fetchall() == [("2026-01-01", 5), ("JAN", 6), ("2026-02-01", 7)]


def test_duplicate_headers_stay_distinct_and_odd_names_work() -> None:
    rows = [["Item", "Ton", "Ton", 'a"b'], ["x", 1, 2, 3], ["y", 4, 5, 6]]
    wb = Workbook.open(make_xlsx({"Data": rows}))
    rel = read_region(wb, _src(region={"header_anchor": ["Item"]}), connect(), "d")
    assert rel.columns == ["Item", "Ton", "Ton (2)", 'a"b']
    assert rel.fetchall() == [("x", 1, 2, 3), ("y", 4, 5, 6)]


def test_formula_without_cached_value_is_null() -> None:
    rows = [["Item", "Ton", "Dobel"], ["x", 1, "=B2*2"], ["y", 4, "=B3*2"]]
    wb = Workbook.open(make_xlsx({"Data": rows}))
    rel = read_region(wb, _src(region={"header_anchor": ["Item"]}), connect(), "d")
    assert rel.fetchall() == [("x", 1, None), ("y", 4, None)]


def test_rereading_replaces_the_table_and_con_cannot_read_files(tmp_path) -> None:
    con = connect()
    read_region(_wb(), _src(), con, "d")
    read_region(_wb(Data=_rows(2)), _src(), con, "d")
    assert len(con.table("d").fetchall()) == 2
    (tmp_path / "x.csv").write_text("a\n1\n")
    with pytest.raises(Exception, match="(?i)disabled|permission"):
        con.sql(f"select * from read_csv('{(tmp_path / 'x.csv').as_posix()}')").fetchall()


def test_anchors_must_sit_together_in_one_table() -> None:
    # `TANGGAL` is in the left table and `Ton` only in the one on the right: no table has both
    rows = [
        ["TANGGAL", "Phonska 1", "Total", None, "Phonska 1", "Ton"],
        *[[date(2026, 1, d), d, d, None, d, d] for d in range(1, 5)],
    ]
    wb = Workbook.open(make_xlsx({"Data": rows}))
    both = _src(region={"header_anchor": ["TANGGAL", "Ton"]})
    with pytest.raises(NodeError, match="but not inside a table"):
        read_region(wb, both, connect(), "d")
    rel = read_region(wb, _src(region={"header_anchor": ["Phonska 1", "Ton"]}), connect(), "d")
    assert rel.columns == ["Phonska 1", "Ton"]  # the right-hand table, not the left one


@pytest.mark.sample
def test_sample_read_region(sample_path) -> None:
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
    assert rel.columns[:3] == ["TANGGAL", "Phonska 1|Ton", "Phonska 1|Formula"]
    assert len(rel.columns) == 29
    assert con.sql("select count(*) from pkg").fetchall()[0][0] > 700
