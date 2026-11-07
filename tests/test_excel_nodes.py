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


# --- unpivot / unpivot_groups: several unrelated shapes, none tied to one workbook ----------


def test_unpivot_listed_columns_and_all_numbers_become_double() -> None:
    rel = _rel("select * from (values ('a', 1, 2.5), ('b', 3, NULL)) v(id, q1, q2)")
    out = _run("unpivot", {"keep": ["id"], "columns": ["q1", "q2"]}, rel)
    assert out.columns == ["id", "variable", "value"]
    assert [c.type for c in schema_of(out)] == ["VARCHAR", "VARCHAR", "DOUBLE"]
    assert sorted(out.fetchall()) == [("a", "q1", 1.0), ("a", "q2", 2.5), ("b", "q1", 3.0)]


def test_unpivot_by_pattern_custom_names_keep_nulls() -> None:
    rel = _rel("select * from (values (1, 10, NULL, 99), (2, 20, 5, 98)) v(k, m_jan, m_feb, other)")
    cfg = {"keep": ["k"], "pattern": "^m_", "name_column": "bulan", "value_column": "ton"}
    assert sorted(_run("unpivot", cfg, rel).fetchall()) == [
        (1, "m_jan", 10.0),
        (2, "m_feb", 5.0),
        (2, "m_jan", 20.0),
    ]
    out = _run("unpivot", {**cfg, "drop_nulls": False}, rel)
    assert (1, "m_feb", None) in out.fetchall() and len(out.fetchall()) == 4


def test_unpivot_mixed_types_become_text_unless_forced() -> None:
    rel = _rel("select * from (values (1, 'x', 5, date '2026-01-01')) v(k, a, b, c)")
    out = _run("unpivot", {"keep": ["k"], "columns": ["a", "b", "c"]}, rel)
    assert [c.type for c in schema_of(out)][-1] == "VARCHAR"
    assert sorted(out.fetchall()) == [(1, "a", "x"), (1, "b", "5"), (1, "c", "2026-01-01")]
    rel = _rel("select * from (values (1, '3', '4')) v(k, a, b)")
    forced = _run("unpivot", {"keep": ["k"], "columns": ["a", "b"], "value_type": "BIGINT"}, rel)
    assert sorted(forced.fetchall()) == [(1, "a", 3), (1, "b", 4)]


def test_unpivot_odd_column_names() -> None:
    rel = _rel('select * from (values (1, 2, 3)) v("id ""x""", "JAN|PKG", "it\'s")')
    out = _run("unpivot", {"keep": ['id "x"'], "columns": ["JAN|PKG", "it's"]}, rel)
    assert sorted(out.fetchall()) == [(1, "JAN|PKG", 2.0), (1, "it's", 3.0)] or sorted(
        out.fetchall()
    ) == [(1, "JAN|PKG", 2), (1, "it's", 3)]


@pytest.mark.parametrize(
    "config",
    [
        {"columns": ["a"], "pattern": "x"},
        {},
        {"pattern": "(["},
        {"columns": []},
        {"columns": ["a"], "name_column": "v", "value_column": "v"},
        {"columns": ["a"], "keep": ["variable"]},
        {"columns": ["a"], "value_type": "MONEY"},
    ],
)
def test_unpivot_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("unpivot", config)


def test_unpivot_schema_errors() -> None:
    schema = schema_of(_rel("select 1 as a, 2 as b"))
    for cfg, needle in [
        ({"columns": ["zzz"]}, "no column 'zzz'"),
        ({"keep": ["zzz"], "columns": ["a"]}, "no column 'zzz'"),
        ({"keep": ["a"], "columns": ["a"]}, "both kept and unpivoted"),
        ({"pattern": "^q"}, "no column to unpivot"),
    ]:
        with pytest.raises(NodeError, match=needle):
            infer(build("unpivot", cfg), [schema])


_WIDE = """select * from (values
    ('r1', 1, 10.0, 2, 20.0), ('r2', NULL, NULL, 4, 40.0), ('r3', NULL, NULL, NULL, NULL)
) v(id, "A|n", "A|rate", "B|n", "B|rate")"""
_BLOCKS = {"pattern": "^(?P<group>.+)[|](?P<measure>[^|]+)$", "keep": ["id"]}


def test_unpivot_groups_blocks_to_rows() -> None:
    out = _run("unpivot_groups", _BLOCKS, _rel(_WIDE))
    assert out.columns == ["id", "group", "n", "rate"]
    assert sorted(out.fetchall()) == [
        ("r1", "A", 1, 10.0),
        ("r1", "B", 2, 20.0),
        ("r2", "B", 4, 40.0),  # r2 has nothing in block A, r3 nothing anywhere
    ]


def test_unpivot_groups_keep_empty_blocks_and_rename_group() -> None:
    out = _run(
        "unpivot_groups", {**_BLOCKS, "drop_empty": False, "group_column": "grp"}, _rel(_WIDE)
    )
    assert out.columns == ["id", "grp", "n", "rate"]
    assert len(out.fetchall()) == 6


def test_unpivot_groups_other_separator_and_different_measure_sets() -> None:
    # blocks need not hold the same measures; one separator is as good as another
    cols = '"id", "north_units", "north_note", "south_units"'
    rel = _rel(f"select * from (values (1, 5, 'x', 7)) v({cols})")
    out = _run(
        "unpivot_groups",
        {"pattern": "^(?P<group>[a-z]+)_(?P<measure>.+)$", "keep": ["id"]},
        rel,
    )
    assert out.columns == ["id", "group", "units", "note"]
    assert sorted(out.fetchall(), key=str) == [(1, "north", 5, "x"), (1, "south", 7, None)]


def test_unpivot_groups_odd_group_names() -> None:
    rel = _rel('select * from (values (1, 2)) v("it\'s|n", "a b|n")')
    out = _run("unpivot_groups", {"pattern": "^(?P<group>.+)[|](?P<measure>.+)$"}, rel)
    assert sorted(out.fetchall()) == [("a b", 2), ("it's", 1)]


def test_unpivot_groups_measure_types_are_unified() -> None:
    rel = _rel('select * from (values (1, \'x\')) v("A|n", "B|n")')
    out = _run("unpivot_groups", {"pattern": "^(?P<group>.+)[|](?P<measure>.+)$"}, rel)
    assert [c.type for c in schema_of(out)] == ["VARCHAR", "VARCHAR"]
    assert sorted(out.fetchall()) == [("A", "1"), ("B", "x")]


@pytest.mark.parametrize(
    "config",
    [
        {"pattern": "(["},
        {"pattern": "^(.+)[|](.+)$"},  # no named groups
        {"pattern": "(?P<group>.)(?P<measure>.)", "group_column": "id", "keep": ["id"]},
        {},
    ],
)
def test_unpivot_groups_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("unpivot_groups", config)


def test_unpivot_groups_schema_errors() -> None:
    schema = schema_of(_rel(_WIDE))
    with pytest.raises(NodeError, match="pattern matches no column"):
        infer(build("unpivot_groups", {"pattern": "^(?P<group>x)(?P<measure>y)$"}), [schema])
    with pytest.raises(NodeError, match="no column 'zzz'"):
        infer(build("unpivot_groups", {**_BLOCKS, "keep": ["zzz"]}), [schema])
    twice = schema_of(_rel('select 1 as "A|n", 2 as "A.n"'))
    with pytest.raises(NodeError, match="both group 'A', measure 'n'"):
        infer(build("unpivot_groups", {"pattern": "^(?P<group>A)[|.](?P<measure>n)$"}), [twice])
    clash = schema_of(_rel('select 1 as "A|id", 2 as id'))
    with pytest.raises(NodeError, match="clashes"):
        infer(build("unpivot_groups", {**_BLOCKS}), [clash])


def test_workbook_with_a_different_shape_end_to_end() -> None:
    # two header rows, a different layout: regions as rows, quarters x (sales, cost) as blocks
    rows = [
        ["Region", "Q1", None, "Q2", None, "Q3", None],
        [None, "Sales", "Cost", "Sales", "Cost", "Sales", "Cost"],
        ["North", 10, 4, 12, 5, 9, 3],
        ["South", 7, 2, None, None, 8, 3],
    ]
    wb = Workbook.open(make_xlsx({"Sheet 1": rows}))
    src = Source.model_validate(
        {
            "file_role": "f",
            "sheet": {"name": "Sheet 1"},
            "region": {"header_anchor": ["Region", "Q1"], "header_rows": 2},
        }
    )
    rel = read_region(wb, src, connect(), "raw")
    rel = _run("fill_down", {"columns": ["Region"]}, rel)
    rel = _run(
        "unpivot_groups", {"pattern": "^(?P<group>Q.)[|](?P<measure>.+)$", "keep": ["Region"]}, rel
    )
    assert rel.columns == ["Region", "group", "Sales", "Cost"]
    assert sorted(rel.fetchall()) == [
        ("North", "Q1", 10, 4),
        ("North", "Q2", 12, 5),
        ("North", "Q3", 9, 3),
        ("South", "Q1", 7, 2),
        ("South", "Q3", 8, 3),
    ]


@pytest.mark.sample
def test_sample_unpivot_groups_sanity(sample_path) -> None:
    # sanity only: a header read from a real file feeds unpivot_groups without a crash and
    # keeps every filled cell. The rule itself is tested on the synthetic shapes above.
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
    rel = _run("drop_rows", {"column": "TANGGAL", "keep_type": "date"}, rel)
    cfg = {"pattern": "^(?P<group>.+)[|](?P<measure>[^|]+)$", "keep": ["TANGGAL"]}
    long = _run("unpivot_groups", cfg, rel)
    assert long.columns[:2] == ["TANGGAL", "group"] and len(long.columns) > 2
    n = long.aggregate("count(*)").limit(1).fetchall()[0][0]
    assert n > 1000


def test_unpivot_groups_pattern_also_matches_columns_you_did_not_mean() -> None:
    # a total block with the same `x|y` naming is a block like any other: narrow the pattern
    # (or select first) to leave it out
    rel = _rel('select * from (values (1, 2, 3)) v("A|n", "B|n", "Total|n")')
    cfg = {"pattern": "^(?P<group>.+)[|](?P<measure>.+)$"}
    assert sorted(_run("unpivot_groups", cfg, rel).fetchall()) == [("A", 1), ("B", 2), ("Total", 3)]
    narrow = {"pattern": "^(?P<group>(?!Total).+)[|](?P<measure>.+)$"}
    assert sorted(_run("unpivot_groups", narrow, rel).fetchall()) == [("A", 1), ("B", 2)]


def test_unpivot_groups_missing_measure_is_typed_null() -> None:
    # B has no `note`; its NULL must take note's type so the blocks can be stacked
    rel = _rel('select * from (values (1, 2, \'x\')) v("A|n", "B|n", "A|note")')
    out = _run("unpivot_groups", {"pattern": "^(?P<group>.)[|](?P<measure>.+)$"}, rel)
    assert [c.type for c in schema_of(out)] == ["VARCHAR", "INTEGER", "VARCHAR"]
    assert sorted(out.fetchall(), key=str) == [("A", 1, "x"), ("B", 2, None)]


def test_drop_regex_and_keep_type_keep_a_row_whose_column_is_empty():
    import duckdb

    from excel_etl.nodes import build

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('pen'),(NULL),('Total'),('ink')) v(\"Item\")"
    )
    out = build("drop_rows", {"blank": False, "column": "Item", "drop_regex": "^Total$"}).run(
        [con.table("t")]
    )
    assert sorted(r[0] or "" for r in out.fetchall()) == ["", "ink", "pen"]  # the NULL stays
    ci = build("drop_rows", {"blank": False, "column": "Item", "drop_regex": "(?i)^total$"})
    con.execute("CREATE TABLE u AS SELECT * FROM (VALUES ('TOTAL'),('pen')) v(\"Item\")")
    assert [r[0] for r in ci.run([con.table("u")]).fetchall()] == ["pen"]
    from excel_etl.nodes import catalog

    assert "A row whose `column` is empty is kept" in " ".join(catalog("drop_rows").split())
