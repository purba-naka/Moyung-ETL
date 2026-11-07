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


def test_a_grand_total_below_a_gap_is_warned_about_not_silently_dropped():
    import json

    from conftest import make_xlsx

    from excel_etl.engine import validate
    from excel_etl.pipeline import parse_spec

    rows = [["Item", "Qty"], ["pen", 2], ["ink", 1], [None, None], [None, None], ["Grand total", 3]]
    book = make_xlsx({"S": rows})

    def spec(end):
        src = {
            "file_role": "f",
            "sheet": {"name": "S"},
            "region": {"header_anchor": ["Qty"], "end": end},
        }
        return parse_spec(json.dumps({"sources": {"s": src}}))

    short = validate(spec("until_blank_rows(2)"), {"f": book})
    assert short.ok and any("1 more filled row(s) lie below the table" in w for w in short.warnings)
    assert any("until_blank_rows(N)" in w for w in short.warnings)
    wide = validate(spec("until_blank_rows(5)"), {"f": book})
    assert wide.ok and not wide.warnings  # nothing left below: no noise
    clean = validate(spec("until_blank_rows(2)"), {"f": make_xlsx({"S": rows[:3]})})
    assert not clean.warnings


def test_blank_gap_is_the_one_reading_of_end():
    from excel_etl.pipeline import RegionRef

    assert RegionRef(header_anchor=["a"], end="until_blank_rows(7)").blank_gap == 7
    assert RegionRef(header_anchor=["a"]).blank_gap == 2


def test_two_row_header_is_named_parent_pipe_leaf_as_the_docs_say():
    import json

    from conftest import make_xlsx

    from excel_etl.engine import validate
    from excel_etl.pipeline import RegionRef, parse_spec

    rows = [["Sales", None, "Cost", None], ["Qty", "Value", "Qty", "Value"], [1, 2, 3, 4]]
    src = {
        "file_role": "f",
        "sheet": {"name": "S"},
        "region": {"header_anchor": ["Sales"], "header_rows": 2},
    }
    rep = validate(parse_spec(json.dumps({"sources": {"s": src}})), {"f": make_xlsx({"S": rows})})
    assert [c.name for c in rep.schemas["s"]] == [
        "Sales|Qty",
        "Sales|Value",
        "Cost|Qty",
        "Cost|Value",
    ]
    doc = " ".join((RegionRef.__doc__ or "").split())
    assert "`Sales|Qty`" in doc and "PARENT|LEAF" in doc


def _book_with_errors(cells: dict[str, str], rows):
    """A workbook where the given cells hold Excel errors (t="e"), which openpyxl writes."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    for r in rows:
        ws.append(r)
    for ref, err in cells.items():
        ws[ref].value = err
        ws[ref].data_type = "e"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _spec_for(sheet="S", anchor="Qty"):
    import json

    from excel_etl.pipeline import parse_spec

    src = {"file_role": "f", "sheet": {"name": sheet}, "region": {"header_anchor": [anchor]}}
    return parse_spec(json.dumps({"sources": {"s": src}}))


def test_error_cells_in_the_table_are_counted_and_warned_about_not_silently_empty():
    from excel_etl.engine import validate

    rows = [["Item", "Qty"], ["a", 1], ["b", 2], ["c", 3]]
    book = _book_with_errors({"B2": "#DIV/0!", "B4": "#N/A"}, rows)
    rep = validate(_spec_for(), {"f": book})
    assert rep.ok
    (w,) = rep.warnings
    assert "2 cell(s) hold an Excel error" in w and "fill_missing" in w


def test_a_clean_table_and_errors_outside_the_table_give_no_warning():
    from excel_etl.engine import validate

    clean = _book_with_errors({}, [["Item", "Qty"], ["a", 1], ["b", 2]])
    assert validate(_spec_for(), {"f": clean}).warnings == ()
    rows = [["Item", "Qty"], ["a", 1], ["b", 2], [None, None], [None, None], ["x", None]]
    outside = _book_with_errors({"B6": "#REF!"}, rows)  # below the table: the `below` warning
    warns = validate(_spec_for(), {"f": outside}).warnings
    assert not any("Excel error" in w for w in warns)
    above = _book_with_errors({"A1": "#NAME?"}, [["Item", "Qty"], ["a", 1], ["b", 2]])
    assert not any("Excel error" in w for w in validate(_spec_for(), {"f": above}).warnings)


def test_error_cells_are_listed_by_the_workbook_and_formula_errors_are_not_confused_with_them():
    from excel_etl.workbook import Workbook

    book = _book_with_errors({"B2": "#DIV/0!"}, [["a", "b"], [1, 0], [2, 0]])
    wb = Workbook.open(book)
    assert wb.error_cells("S") == frozenset({(1, 1)})
    with_formula = _book_with_errors({}, [["a", "b"], [1, "=1/0"], [2, 0]])
    # a formula without a cached value is tracked as a formula, not as an error cell
    assert Workbook.open(with_formula).error_cells("S") == frozenset()


def test_an_error_cell_beside_the_table_is_not_counted():
    from excel_etl.engine import validate

    rows = [["Note", "Item", "Qty"], ["x", "a", 1], ["y", "b", 2]]
    book = _book_with_errors({"A2": "#N/A"}, rows)  # column A is outside the table (Item, Qty)
    spec = _spec_for(anchor="Qty")
    # anchor Qty alone selects the table that holds Qty; Note is a neighbour column or part of it,
    # so make the neighbour unambiguous with a gap column
    rows = [["Note", None, "Item", "Qty"], ["x", None, "a", 1], ["y", None, "b", 2]]
    book = _book_with_errors({"A2": "#N/A"}, rows)
    rep = validate(spec, {"f": book})
    assert rep.ok and not any("Excel error" in w for w in rep.warnings)
    inside = _book_with_errors({"D2": "#N/A"}, rows)
    assert any("1 cell(s) hold an Excel error" in w for w in validate(spec, {"f": inside}).warnings)
