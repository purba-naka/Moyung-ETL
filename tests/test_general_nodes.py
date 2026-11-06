from __future__ import annotations

import pytest
from conftest import make_xlsx

from excel_etl.nodes import NodeError, build, infer, schema_of
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.nodes.sqlexpr import check_type
from excel_etl.pipeline import Source
from excel_etl.workbook import Workbook


def _rel(sql: str):
    return connect().sql(sql)


def _run(name: str, config: dict, rel):
    n = build(name, config)
    out = n.run([rel])
    assert infer(n, [schema_of(rel)]) == schema_of(out)  # the contract, on every case
    return out


_T = """select * from (values
    (1, 'a', 2.5, date '2026-01-01'), (2, NULL, NULL, NULL), (3, 'c', 1.0, date '2026-02-01')
) v(id, label, "JAN|Ton", day)"""


# --- check_type ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "spelled"),
    [
        ("integer", "INTEGER"),
        ("DOUBLE", "DOUBLE"),
        ("decimal(10, 2)", "DECIMAL(10,2)"),
        ("varchar(5)", "VARCHAR"),
        ("  timestamp  ", "TIMESTAMP"),
        ("INTEGER[]", "INTEGER[]"),
        ("timestamp with time zone", "TIMESTAMP WITH TIME ZONE"),
    ],
)
def test_check_type_accepts_plain_names(given: str, spelled: str) -> None:
    assert check_type(given) == spelled


@pytest.mark.parametrize(
    "bad",
    [
        "INTEGER); DROP TABLE x; --",
        "INTEGER) , (SELECT 1",
        "STRUCT(a INT)",
        "ENUM('a','b')",
        "NOTATYPE",
        "",
        "  ",
        "int'",
        'in"t',
        "INT/*",
        "A" * 40,
        "DECIMAL(1,2,3)",
        5,
        None,
    ],
)
def test_check_type_refuses_the_rest(bad) -> None:
    with pytest.raises(NodeError):
        check_type(bad)


# --- rename ----------------------------------------------------------------------------------


def test_rename_keeps_order_and_other_columns() -> None:
    out = _run("rename", {"mapping": {"JAN|Ton": "ton", "id": "no"}}, _rel(_T))
    assert out.columns == ["no", "label", "ton", "day"]
    assert out.fetchall()[0] == (1, "a", 2.5, __import__("datetime").date(2026, 1, 1))


def test_rename_swap_and_odd_names() -> None:
    out = _run("rename", {"mapping": {"a": "b", "b": 'a "x"'}}, _rel("select 1 as a, 2 as b"))
    assert out.columns == ["b", 'a "x"'] and out.fetchall() == [(1, 2)]


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"mapping": {"zzz": "a"}}, "no column 'zzz'"),
        ({"mapping": {"id": "label"}}, "output column 'label' is listed twice"),
        ({"mapping": {"id": "x", "label": "x"}}, "'x' is listed twice"),
    ],
)
def test_rename_errors(config: dict, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        infer(build("rename", config), [schema_of(_rel(_T))])


@pytest.mark.parametrize("config", [{}, {"mapping": {}}, {"mapping": {"a": ""}}])
def test_rename_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("rename", config)


# --- cast ------------------------------------------------------------------------------------


def test_cast_lenient_turns_misfits_into_null() -> None:
    rel = _rel("select * from (values ('1'), ('2'), ('JAN'), (NULL)) v(x)")
    out = _run("cast", {"types": {"x": "integer"}}, rel)
    assert [c.type for c in schema_of(out)] == ["INTEGER"]
    assert out.fetchall() == [(1,), (2,), (None,), (None,)]


def test_cast_strict_fails_and_decimal_date() -> None:
    rel = _rel("select * from (values ('1'), ('JAN')) v(x)")
    strict = build("cast", {"types": {"x": "integer"}, "strict": True})
    with pytest.raises(Exception, match="(?i)conversion|could not"):
        strict.run([rel]).fetchall()
    out = _run(
        "cast",
        {"types": {"x": "decimal(5,1)", "d": "date"}},
        _rel("select '1.25' x, '2026-03-04' d"),
    )
    assert [c.type for c in schema_of(out)] == ["DECIMAL(5,1)", "DATE"]


def test_cast_errors() -> None:
    with pytest.raises(NodeError, match="no column 'q'"):
        infer(build("cast", {"types": {"q": "date"}}), [schema_of(_rel(_T))])
    for bad in ({"types": {"id": "NOTATYPE"}}, {"types": {"id": "int); drop"}}, {"types": {}}):
        with pytest.raises(NodeError):
            build("cast", bad)


# --- filter ----------------------------------------------------------------------------------


def test_filter_keeps_true_rows_only() -> None:
    out = _run("filter", {"where": '"JAN|Ton" > 1.5 OR label IS NULL'}, _rel(_T))
    assert [r[0] for r in out.fetchall()] == [1, 2]
    out = _run("filter", {"where": '"JAN|Ton" > 0'}, _rel(_T))
    assert [r[0] for r in out.fetchall()] == [1, 3]  # the NULL row is dropped, as in SQL


def test_filter_text_and_date_conditions() -> None:
    assert _run("filter", {"where": "label = 'a'"}, _rel(_T)).fetchall()[0][0] == 1
    out = _run("filter", {"where": "day >= DATE '2026-02-01'"}, _rel(_T))
    assert [r[0] for r in out.fetchall()] == [3]
    assert _run("filter", {"where": "label LIKE 'it''s%'"}, _rel(_T)).fetchall() == []


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"where": "zzz > 1"}, "filter:.*zzz"),
        ({"where": "id + 1"}, "not true/false"),
        ({"where": "label"}, "not true/false"),
        ({"where": "sum(id) > 1"}, "aggregate 'sum' is not allowed"),
    ],
)
def test_filter_schema_errors(config: dict, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        infer(build("filter", config), [schema_of(_rel(_T))])


@pytest.mark.parametrize(
    "where",
    ["(SELECT 1) = 1", "id IN (SELECT 1)", "1=1) UNION SELECT 2 --", "read_csv('x') IS NULL", ""],
)
def test_filter_refuses_unsafe_expressions_when_built(where: str) -> None:
    with pytest.raises(NodeError):
        build("filter", {"where": where})


# --- derive ----------------------------------------------------------------------------------


def test_derive_adds_a_column_last() -> None:
    out = _run("derive", {"name": "double", "expr": '"JAN|Ton" * 2'}, _rel(_T))
    assert out.columns == ["id", "label", "JAN|Ton", "day", "double"]
    assert [r[4] for r in out.fetchall()] == [5.0, None, 2.0]


def test_derive_replaces_in_place_and_casts() -> None:
    out = _run("derive", {"name": "id", "expr": "id * 10", "type": "bigint"}, _rel(_T))
    assert out.columns == ["id", "label", "JAN|Ton", "day"]
    assert [c.type for c in schema_of(out)][0] == "BIGINT"
    assert [r[0] for r in out.fetchall()] == [10, 20, 30]


def test_derive_case_and_functions_and_chaining() -> None:
    rel = _rel(_T)
    rel = _run(
        "derive", {"name": "kind", "expr": "CASE WHEN id > 1 THEN 'big' ELSE 'small' END"}, rel
    )
    rel = _run("derive", {"name": "tag", "expr": "upper(kind) || '-' || CAST(id AS VARCHAR)"}, rel)
    rel = _run("derive", {"name": "yr", "expr": "year(day)"}, rel)
    assert [r[-2:] for r in rel.fetchall()] == [("SMALL-1", 2026), ("BIG-2", None), ("BIG-3", 2026)]


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"name": "x", "expr": "zzz + 1"}, "derive:.*zzz"),
        ({"name": "x", "expr": "sum(id)"}, "aggregate 'sum' is not allowed"),
        ({"name": "x", "expr": "label + 1"}, "derive:"),
    ],
)
def test_derive_schema_errors(config: dict, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        infer(build("derive", config), [schema_of(_rel(_T))])


@pytest.mark.parametrize(
    "config",
    [
        {"name": "", "expr": "1"},
        {"name": "x", "expr": "(select 1)"},
        {"name": "x", "expr": "random()"},
        {"name": "x", "expr": "1", "type": "int); drop"},
        {"name": "x"},
    ],
)
def test_derive_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("derive", config)


# --- fill_missing ----------------------------------------------------------------------------


def test_fill_missing_numbers_text_and_bool() -> None:
    out = _run("fill_missing", {"values": {"JAN|Ton": 0, "label": "n/a"}}, _rel(_T))
    assert [(r[1], r[2]) for r in out.fetchall()] == [("a", 2.5), ("n/a", 0.0), ("c", 1.0)]
    b = _run(
        "fill_missing", {"values": {"f": True}}, _rel("select * from (values (NULL), (false)) v(f)")
    )
    assert b.fetchall() == [(True,), (False,)]


def test_fill_missing_quote_in_value_and_odd_column() -> None:
    rel = _rel("select * from (values (NULL), ('x')) v(\"it's\")")
    out = _run("fill_missing", {"values": {"it's": "o'clock"}}, rel)
    assert out.fetchall() == [("o'clock",), ("x",)]


def test_fill_missing_value_must_fit_the_column() -> None:
    with pytest.raises(NodeError, match="fill_missing"):
        infer(build("fill_missing", {"values": {"id": "abc"}}), [schema_of(_rel(_T))])
    with pytest.raises(NodeError, match="no column 'q'"):
        infer(build("fill_missing", {"values": {"q": 0}}), [schema_of(_rel(_T))])
    for bad in ({}, {"values": {}}, {"values": {"id": [1]}}, {"values": {"id": None}}):
        with pytest.raises(NodeError):
            build("fill_missing", bad)
    with pytest.raises(NodeError, match="nan"):
        infer(build("fill_missing", {"values": {"JAN|Ton": float("nan")}}), [schema_of(_rel(_T))])


# --- sort ------------------------------------------------------------------------------------


def test_sort_directions_and_nulls_last() -> None:
    rel = _rel(_T)
    assert [r[0] for r in _run("sort", {"by": ["JAN|Ton"]}, rel).fetchall()] == [3, 1, 2]
    assert [r[0] for r in _run("sort", {"by": ["-JAN|Ton"]}, rel).fetchall()] == [1, 3, 2]
    assert [r[0] for r in _run("sort", {"by": ["label", "-id"]}, rel).fetchall()] == [1, 3, 2]


def test_sort_column_that_starts_with_a_dash() -> None:
    rel = _rel('select * from (values (2, 1), (1, 2)) v("-x", y)')
    assert [r[0] for r in _run("sort", {"by": ["-x"]}, rel).fetchall()] == [1, 2]  # real column
    assert [r[0] for r in _run("sort", {"by": ["-y"]}, rel).fetchall()] == [1, 2]  # y descending


def test_sort_errors() -> None:
    with pytest.raises(NodeError, match="no column 'zzz'"):
        infer(build("sort", {"by": ["-zzz"]}), [schema_of(_rel(_T))])
    with pytest.raises(NodeError):
        build("sort", {"by": []})


# --- dedupe ----------------------------------------------------------------------------------


def test_dedupe_all_columns_and_by_keys() -> None:
    rel = _rel("select * from (values (1,'a'),(1,'a'),(1,'b'),(NULL,'z'),(NULL,'z')) v(k, v)")
    assert sorted(_run("dedupe", {}, rel).fetchall(), key=str) == [(1, "a"), (1, "b"), (None, "z")]
    out = _run("dedupe", {"keys": ["k"]}, rel)
    got = out.fetchall()
    assert len(got) == 2 and {r[0] for r in got} == {1, None}  # NULL counts as one key value


def test_dedupe_keeps_the_first_after_sort() -> None:
    rel = _rel("select * from (values (1,'late',2),(1,'early',1),(2,'only',1)) v(k, note, seq)")
    ordered = _run("sort", {"by": ["seq"]}, rel)
    out = _run("dedupe", {"keys": ["k"]}, ordered)
    assert sorted(out.fetchall()) == [(1, "early", 1), (2, "only", 1)]


def test_dedupe_errors() -> None:
    with pytest.raises(NodeError, match="no column 'zzz'"):
        infer(build("dedupe", {"keys": ["zzz"]}), [schema_of(_rel(_T))])
    with pytest.raises(NodeError, match="listed twice"):
        infer(build("dedupe", {"keys": ["id", "id"]}), [schema_of(_rel(_T))])
    with pytest.raises(NodeError):
        build("dedupe", {"keys": []})


# --- together, on a workbook shaped unlike any other test ------------------------------------


def test_workbook_clean_up_chain() -> None:
    rows = [
        ["Kode", "Qty", "Harga", "Status"],
        ["A1", "10", 2.5, "ok"],
        ["A1", "10", 2.5, "ok"],
        ["B2", "x", None, "ok"],
        ["C3", "7", 4.0, None],
        ["D4", "0", 1.0, "void"],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}))
    src = Source.model_validate(
        {"file_role": "f", "sheet": {"name": "S"}, "region": {"header_anchor": ["Kode"]}}
    )
    rel = read_region(wb, src, connect(), "raw")
    rel = _run("dedupe", {}, rel)
    rel = _run("cast", {"types": {"Qty": "integer"}}, rel)
    rel = _run("fill_missing", {"values": {"Status": "?", "Harga": 0}}, rel)
    rel = _run("filter", {"where": "Qty > 0 AND Status <> 'void'"}, rel)
    rel = _run("derive", {"name": "Nilai", "expr": "Qty * Harga"}, rel)
    rel = _run("rename", {"mapping": {"Kode": "code"}}, rel)
    rel = _run("sort", {"by": ["-Nilai"]}, rel)
    assert rel.columns == ["code", "Qty", "Harga", "Status", "Nilai"]
    assert rel.fetchall() == [("C3", 7, 4.0, "?", 28.0), ("A1", 10, 2.5, "ok", 25.0)]
    assert [(r[0], r[4]) for r in rel.fetchall()] == [("C3", 28.0), ("A1", 25.0)]


def test_dedupe_first_means_first_in_table_order_on_a_large_table() -> None:
    # on small tables any plan gives the same answer; with 200k rows a parallel scan reaches
    # keys in another order, and a plain row_number() keeps the wrong row
    rel = _rel("select i % 50 as k, i as seq from range(200000) t(i)")
    out = _run("dedupe", {"keys": ["k"]}, rel).fetchall()
    assert [r[1] for r in out] == list(range(50))  # row k first appears at seq k, in order


def test_dedupe_without_keys_keeps_the_first_copy_in_table_order() -> None:
    rel = _rel("select (i * 7919 % 100003) % 50000 as k from range(300000) t(i)")
    got = [r[0] for r in _run("dedupe", {}, rel).fetchall()]
    seen: dict[int, None] = {}
    for (k,) in rel.fetchall():
        seen.setdefault(k, None)
    assert got == list(seen)  # each value once, in order of first appearance


def test_sort_puts_nulls_last_in_both_directions() -> None:
    rel = _rel("select * from (values (2), (NULL), (1)) v(x)")
    assert _run("sort", {"by": ["x"]}, rel).fetchall() == [(1,), (2,), (None,)]
    assert _run("sort", {"by": ["-x"]}, rel).fetchall() == [(2,), (1,), (None,)]
