from __future__ import annotations

import pytest
from conftest import make_xlsx

from excel_etl.nodes import NodeError, build, infer, schema_of
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.pipeline import Source
from excel_etl.workbook import Workbook


def _run(name: str, config: dict, *rels):
    n = build(name, config)
    out = n.run(list(rels))
    assert infer(n, [schema_of(r) for r in rels]) == schema_of(out)  # the contract
    return out


def _schema_error(name: str, config: dict, *rels) -> str:
    with pytest.raises(NodeError) as e:
        infer(build(name, config), [schema_of(r) for r in rels])
    return str(e.value)


@pytest.fixture
def con():
    return connect()


@pytest.fixture
def lr(con):
    left = con.sql("select * from (values (1,'x',10),(2,'y',20),(NULL,'n',30)) v(id, name, v)")
    right = con.sql(
        "select * from (values (1,'p',100),(1,'q',101),(3,'z',300),(NULL,'m',400)) v(id, label, w)"
    )
    return left, right


# --- join ------------------------------------------------------------------------------------


def test_join_inner_one_key_column(lr) -> None:
    out = _run("join", {"on": ["id"]}, *lr)
    assert out.columns == ["id", "name", "v", "label", "w"]
    assert sorted(out.fetchall()) == [(1, "x", 10, "p", 100), (1, "x", 10, "q", 101)]


@pytest.mark.parametrize(
    ("how", "ids"),
    [
        ("left", [1, 1, 2, None]),
        ("right", [1, 1, 3, None]),
        ("full", [1, 1, 2, 3, None, None]),
    ],
)
def test_join_outer_kinds_keep_the_key_from_either_side(lr, how: str, ids: list) -> None:
    out = _run("join", {"on": ["id"], "how": how}, *lr)
    assert sorted((r[0] for r in out.fetchall()), key=lambda x: (x is None, x)) == ids


def test_join_null_keys_match_only_when_asked(lr) -> None:
    plain = _run("join", {"on": ["id"]}, *lr).fetchall()
    assert all(r[0] is not None for r in plain)
    safe = _run("join", {"on": ["id"], "null_equal": True}, *lr).fetchall()
    assert (None, "n", 30, "m", 400) in safe


def test_join_keys_with_different_names_both_stay(con) -> None:
    a = con.sql("select * from (values (1,'x')) v(code, n)")
    b = con.sql("select * from (values (1,9)) v(kode, w)")
    out = _run("join", {"on": [{"left": "code", "right": "kode"}]}, a, b)
    assert out.columns == ["code", "n", "kode", "w"] and out.fetchall() == [(1, "x", 1, 9)]


def test_join_shared_non_key_column_needs_suffixes(con) -> None:
    a = con.sql("select * from (values (1,'x')) v(id, name)")
    b = con.sql("select * from (values (1,'q')) v(id, name)")
    msg = _schema_error("join", {"on": ["id"]}, a, b)
    assert "'name' is in both tables" in msg and "suffixes" in msg
    out = _run("join", {"on": ["id"], "suffixes": ["_l", "_r"]}, a, b)
    assert out.columns == ["id", "name_l", "name_r"] and out.fetchall() == [(1, "x", "q")]


def test_join_two_keys_and_odd_names(con) -> None:
    a = con.sql('select * from (values (1,\'A\',5)) v("it\'s", "JAN|PKG", v)')
    b = con.sql('select * from (values (1,\'A\',7)) v("it\'s", "JAN|PKG", w)')
    out = _run("join", {"on": ["it's", "JAN|PKG"]}, a, b)
    assert out.fetchall() == [(1, "A", 5, 7)]


def test_join_schema_errors(con, lr) -> None:
    assert "no column 'zzz'" in _schema_error("join", {"on": ["zzz"]}, *lr)
    a = con.sql("select 1 as k")
    b = con.sql("select '1' as k")
    assert "cast one side" in _schema_error("join", {"on": ["k"]}, a, b)
    clash = con.sql("select 1 as id, 2 as name_r")
    other = con.sql("select 1 as id, 3 as name, 4 as name_r2")
    with_name = con.sql("select 1 as id, 'q' as name, 5 as name_r")
    msg = _schema_error("join", {"on": ["id"], "suffixes": ["", "_r"]}, with_name, other)
    assert "appears twice" in msg or "both tables" in msg
    assert clash  # the fixture tables above are only there to build the clash case
    reserved = con.sql("select 1 as id, 2 as __pos")
    assert "reserved" in _schema_error("join", {"on": ["id"]}, reserved, lr[1])


def test_join_arity_and_config_errors(lr) -> None:
    with pytest.raises(NodeError, match="takes 2 input"):
        infer(build("join", {"on": ["id"]}), [schema_of(lr[0])])
    for bad in (
        {},
        {"on": []},
        {"on": ["id"], "how": "outer"},
        {"on": ["id"], "suffixes": ["_x", "_x"]},
    ):
        with pytest.raises(NodeError):
            build("join", bad)


# --- union -----------------------------------------------------------------------------------


def test_union_by_name_fills_missing_columns_with_null(con) -> None:
    a = con.sql("select * from (values (1,'x')) v(id, n)")
    b = con.sql("select * from (values ('q', 2, 7)) v(n, id, extra)")
    out = _run("union", {}, a, b)
    assert out.columns == ["id", "n", "extra"]
    assert out.fetchall() == [(1, "x", None), (2, "q", 7)]


def test_union_by_position_takes_the_first_names(con) -> None:
    a = con.sql("select 1 as id, 'x' as n")
    b = con.sql("select 2 as code, 'y' as label")
    out = _run("union", {"by": "position"}, a, b)
    assert out.columns == ["id", "n"] and out.fetchall() == [(1, "x"), (2, "y")]
    c = con.sql("select 3 as one")
    assert "same number" in _schema_error("union", {"by": "position"}, a, c)


def test_union_types_three_inputs_source_and_distinct(con) -> None:
    a = con.sql("select 1 as v, 'x' as t")
    b = con.sql("select 2.5 as v, 'x' as t")
    c = con.sql("select 'n/a' as v, 'x' as t")
    nums = _run("union", {}, a, b)
    assert [col.type for col in schema_of(nums)][0] == "DOUBLE"
    mixed = _run("union", {"source_column": "src"}, a, b, c)
    assert [col.type for col in schema_of(mixed)] == ["VARCHAR", "VARCHAR", "BIGINT"]
    assert mixed.fetchall() == [("1", "x", 0), ("2.5", "x", 1), ("n/a", "x", 2)]
    assert len(_run("union", {"distinct": True}, a, a).fetchall()) == 1


def test_union_errors(con) -> None:
    a = con.sql("select 1 as src")
    assert "already a column" in _schema_error("union", {"source_column": "src"}, a, a)
    with pytest.raises(NodeError, match="takes 2\\+ input"):
        infer(build("union", {}), [schema_of(a)])
    with pytest.raises(NodeError):
        build("union", {"by": "rows"})


# --- group_by --------------------------------------------------------------------------------

_SALES = """select * from (values
    ('b', 'x', 1, 'p'), ('a', 'y', 2, NULL), ('b', 'x', 3, 'q'),
    ('a', 'x', NULL, 'r'), ('c', 'y', 5, 's')
) v(region, kind, qty, note)"""


def test_group_by_functions_and_first_appearance_order(con) -> None:
    rel = con.sql(_SALES)
    aggs = [
        {"name": "total", "fn": "sum", "column": "qty"},
        {"name": "rows", "fn": "count_rows"},
        {"name": "filled", "fn": "count", "column": "qty"},
        {"name": "kinds", "fn": "count_distinct", "column": "kind"},
        {"name": "mean", "fn": "avg", "column": "qty"},
        {"name": "lo", "fn": "min", "column": "qty"},
        {"name": "hi", "fn": "max", "column": "qty"},
        {"name": "first_note", "fn": "first", "column": "note"},
        {"name": "last_note", "fn": "last", "column": "note"},
        {"name": "notes", "fn": "join_text", "column": "note", "separator": "|"},
    ]
    out = _run("group_by", {"by": ["region"], "aggs": aggs}, rel)
    assert [c.type for c in schema_of(out)][:3] == ["VARCHAR", "BIGINT", "BIGINT"]
    assert out.fetchall() == [
        ("b", 4, 2, 2, 1, 2.0, 1, 3, "p", "q", "p|q"),
        ("a", 2, 2, 1, 2, 2.0, 2, 2, "r", "r", "r"),
        ("c", 5, 1, 1, 1, 5.0, 5, 5, "s", "s", "s"),
    ]


def test_group_by_whole_table_and_several_keys(con) -> None:
    rel = con.sql(_SALES)
    one = _run("group_by", {"aggs": [{"name": "t", "fn": "sum", "column": "qty"}]}, rel)
    assert one.fetchall() == [(11,)]
    two = _run(
        "group_by",
        {"by": ["region", "kind"], "aggs": [{"name": "n", "fn": "count_rows"}]},
        rel,
    )
    assert two.fetchall() == [("b", "x", 2), ("a", "y", 1), ("a", "x", 1), ("c", "y", 1)]


def test_group_by_order_and_first_hold_on_a_large_table(con) -> None:
    rel = con.sql("select (i % 50) as k, i as seq from range(200000) t(i)")
    out = _run(
        "group_by",
        {
            "by": ["k"],
            "aggs": [
                {"name": "first", "fn": "first", "column": "seq"},
                {"name": "last", "fn": "last", "column": "seq"},
            ],
        },
        rel,
    ).fetchall()
    assert [r[0] for r in out] == list(range(50))  # groups in order of first appearance
    assert all(r[1] == r[0] and r[2] == 199950 + r[0] for r in out)


def test_group_by_sum_types(con) -> None:
    rel = con.sql("select 1::integer as i, 1.5::double as d, 2.25::decimal(5,2) as m")
    out = _run(
        "group_by",
        {
            "aggs": [
                {"name": "si", "fn": "sum", "column": "i"},
                {"name": "sd", "fn": "sum", "column": "d"},
                {"name": "sm", "fn": "sum", "column": "m"},
            ]
        },
        rel,
    )
    assert [c.type for c in schema_of(out)] == ["BIGINT", "DOUBLE", "DECIMAL(38,2)"]


def test_group_by_sum_overflow_is_an_error_not_a_wrong_number(con) -> None:
    rel = con.sql("select 9223372036854775807::bigint as x union all select 5::bigint")
    n = build("group_by", {"aggs": [{"name": "s", "fn": "sum", "column": "x"}]})
    with pytest.raises(Exception, match="(?i)overflow|can't be cast|out of range"):
        n.run([rel]).fetchall()


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"by": ["zzz"], "aggs": [{"name": "n", "fn": "count_rows"}]}, "no column 'zzz'"),
        ({"aggs": [{"name": "s", "fn": "sum", "column": "note"}]}, "needs a number column"),
        ({"aggs": [{"name": "s", "fn": "avg", "column": "region"}]}, "needs a number column"),
        ({"by": ["region"], "aggs": [{"name": "region", "fn": "count_rows"}]}, "appears twice"),
        (
            {"aggs": [{"name": "a", "fn": "count_rows"}, {"name": "a", "fn": "count_rows"}]},
            "appears twice",
        ),
        (
            {"by": ["region", "region"], "aggs": [{"name": "n", "fn": "count_rows"}]},
            "appears twice",
        ),
    ],
)
def test_group_by_schema_errors(con, config: dict, needle: str) -> None:
    assert needle in _schema_error("group_by", config, con.sql(_SALES))


@pytest.mark.parametrize(
    "config",
    [
        {"aggs": []},
        {"aggs": [{"name": "x", "fn": "median", "column": "qty"}]},
        {"aggs": [{"name": "x", "fn": "sum"}]},
        {"aggs": [{"name": "x", "fn": "count_rows", "column": "qty"}]},
        {"aggs": [{"name": "", "fn": "count_rows"}]},
        {"aggs": [{"name": "x", "fn": "sum", "column": "qty", "expr": "qty * 2"}]},
    ],
)
def test_group_by_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("group_by", config)


# --- pivot -----------------------------------------------------------------------------------

_LONG = """select * from (values
    ('a', 'x', 1), ('a', 'y', 2), ('b', 'x', 3), ('a', 'x', 4), ('c', 'w', 9)
) v(k, c, m)"""


def test_pivot_listed_values_become_columns(con) -> None:
    out = _run(
        "pivot",
        {"by": ["k"], "column": "c", "value": "m", "values": ["x", "y", "z"]},
        con.sql(_LONG),
    )
    assert out.columns == ["k", "x", "y", "z"]
    assert out.fetchall() == [("a", 5, 2, None), ("b", 3, None, None), ("c", None, None, None)]


@pytest.mark.parametrize(
    ("fn", "a_x"),
    [("sum", 5), ("min", 1), ("max", 4), ("count", 2), ("first", 1), ("last", 4), ("avg", 2.5)],
)
def test_pivot_functions(con, fn: str, a_x) -> None:
    cfg = {"by": ["k"], "column": "c", "value": "m", "values": ["x"], "fn": fn}
    assert _run("pivot", cfg, con.sql(_LONG)).fetchall()[0] == ("a", a_x)


def test_pivot_number_and_date_keys_with_prefix(con) -> None:
    rel = con.sql("select * from (values ('a', 2025, 1), ('a', 2026, 2)) v(k, yr, m)")
    out = _run(
        "pivot",
        {"by": ["k"], "column": "yr", "value": "m", "values": [2025, 2026], "prefix": "y"},
        rel,
    )
    assert out.columns == ["k", "y2025", "y2026"] and out.fetchall() == [("a", 1, 2)]
    days = con.sql("select * from (values ('a', date '2026-01-01', 7)) v(k, d, m)")
    out = _run("pivot", {"by": ["k"], "column": "d", "value": "m", "values": ["2026-01-01"]}, days)
    assert out.fetchall() == [("a", 7)]


def test_pivot_value_that_cannot_fit_the_column_is_an_error(con) -> None:
    rel = con.sql("select * from (values ('a', 2025, 1)) v(k, yr, m)")
    msg = _schema_error(
        "pivot", {"by": ["k"], "column": "yr", "value": "m", "values": ["abc"]}, rel
    )
    assert "'abc'" in msg and "yr" in msg


def test_pivot_quote_in_value_and_no_by(con) -> None:
    rel = con.sql("select * from (values ('it''s', 1), ('b', 2)) v(c, m)")
    out = _run("pivot", {"column": "c", "value": "m", "values": ["it's", "b"]}, rel)
    assert out.columns == ["it's", "b"] and out.fetchall() == [(1, 2)]


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"column": "zzz", "value": "m", "values": ["x"]}, "no column 'zzz'"),
        ({"by": ["k"], "column": "c", "value": "c", "values": ["x"]}, "must be different"),
        ({"by": ["c"], "column": "c", "value": "m", "values": ["x"]}, "must be different"),
        (
            {"by": ["k"], "column": "c", "value": "k", "values": ["x"], "fn": "sum"},
            "must be different",
        ),
        ({"by": ["k"], "column": "m", "value": "c", "values": [1], "fn": "sum"}, "needs a number"),
        ({"by": ["k"], "column": "c", "value": "m", "values": ["k"]}, "also a `by` column"),
    ],
)
def test_pivot_schema_errors(con, config: dict, needle: str) -> None:
    assert needle in _schema_error("pivot", config, con.sql(_LONG))


@pytest.mark.parametrize(
    "config",
    [
        {"column": "c", "value": "m"},
        {"column": "c", "value": "m", "values": []},
        {"column": "c", "value": "m", "values": ["x", "x"]},
        {"column": "c", "value": "m", "values": [1, "1"]},
        {"column": "c", "value": "m", "values": ["x"], "fn": "median"},
    ],
)
def test_pivot_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("pivot", config)


# --- a chain over two workbooks unlike any other test ---------------------------------------


def test_two_sheets_joined_grouped_and_pivoted() -> None:
    orders = [
        ["Order", "Item", "Qty"],
        ["o1", "pen", 2],
        ["o2", "ink", 1],
        ["o3", "pen", 5],
        ["o4", "pad", 3],
    ]
    items = [
        ["Item", "Group", "Price"],
        ["pen", "write", 1.5],
        ["ink", "write", 4.0],
        ["pad", "paper", 2.0],
    ]
    wb = Workbook.open(make_xlsx({"Orders": orders, "Items": items}))
    con = connect()

    def src(sheet: str, anchor: str) -> Source:
        return Source.model_validate(
            {"file_role": "f", "sheet": {"name": sheet}, "region": {"header_anchor": [anchor]}}
        )

    o = read_region(wb, src("Orders", "Order"), con, "orders")
    i = read_region(wb, src("Items", "Group"), con, "items")
    rel = _run("join", {"on": ["Item"], "how": "left"}, o, i)
    rel = build("derive", {"name": "Value", "expr": "Qty * Price"}).run([rel])
    summary = _run(
        "group_by",
        {
            "by": ["Group"],
            "aggs": [
                {"name": "Qty", "fn": "sum", "column": "Qty"},
                {"name": "Value", "fn": "sum", "column": "Value"},
            ],
        },
        rel,
    )
    assert summary.fetchall() == [("write", 8, 14.5), ("paper", 3, 6.0)]
    wide = _run(
        "pivot",
        {"column": "Item", "value": "Qty", "values": ["pen", "ink", "pad"]},
        rel,
    )
    assert wide.fetchall() == [(7, 1, 3)]
