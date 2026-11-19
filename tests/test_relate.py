from __future__ import annotations

import json

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import relate_tables
from excel_etl.nodes.base import NodeError, connect
from excel_etl.profile import profile_table
from excel_etl.relate import relate
from excel_etl.workbook import Workbook, WorkbookError


def _con():
    con = connect()
    con.execute(
        "CREATE TABLE cust AS SELECT * FROM "
        "(VALUES (1,'Ann'),(2,'Bob'),(3,'Cy'),(4,'Di')) v(id, name)"
    )
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (10,1,5.0),(11,1,7.0),(12,2,1.0),(13,9,2.0))"
        " v(order_id, cust_id, amt)"
    )
    return con


def test_finds_the_foreign_key_with_overlap_and_cardinality():
    con = _con()
    try:
        got = relate(con, "orders", "cust")
    finally:
        con.close()
    best = got["pairs"][0]
    assert (best["a"], best["b"]) == ("cust_id", "id")
    assert best["overlap"] == 2
    assert best["a_distinct"] == 3 and best["b_distinct"] == 4
    assert best["a_missing"] == 1 and best["b_missing"] == 2
    assert best["cardinality"] == "N:1"
    json.dumps(got)


def test_pairs_without_shared_values_or_of_other_type_families_are_left_out():
    con = _con()
    try:
        got = relate(con, "orders", "cust")
    finally:
        con.close()
    pairs = {(p["a"], p["b"]) for p in got["pairs"]}
    assert ("amt", "name") not in pairs and ("order_id", "name") not in pairs
    assert all(p["overlap"] > 0 for p in got["pairs"])


@pytest.mark.parametrize(
    ("a_rows", "b_rows", "want"),
    [
        ("(1),(2)", "(1),(2)", "1:1"),
        ("(1),(2)", "(1),(1),(2)", "1:N"),
        ("(1),(1),(2)", "(1),(2)", "N:1"),
        ("(1),(1),(2)", "(1),(1),(2)", "N:M"),
    ],
)
def test_cardinality_from_uniqueness_of_each_side(a_rows, b_rows, want):
    con = connect()
    try:
        con.execute(f"CREATE TABLE a AS SELECT * FROM (VALUES {a_rows}) v(k)")
        con.execute(f"CREATE TABLE b AS SELECT * FROM (VALUES {b_rows}) v(k)")
        assert relate(con, "a", "b")["pairs"][0]["cardinality"] == want
    finally:
        con.close()


def test_text_match_is_exact_and_near_matches_are_counted():
    con = connect()
    try:
        con.execute("CREATE TABLE a AS SELECT * FROM (VALUES ('PKG'),('pkc '),('X')) v(code)")
        con.execute("CREATE TABLE b AS SELECT * FROM (VALUES ('PKG'),('PKC'),('Y')) v(code)")
        pair = relate(con, "a", "b")["pairs"][0]
    finally:
        con.close()
    assert pair["overlap"] == 1
    assert pair["overlap_folded"] == 2


def test_results_are_deterministic_and_sorted_by_overlap():
    con = _con()
    try:
        a, b = relate(con, "orders", "cust"), relate(con, "orders", "cust")
    finally:
        con.close()
    assert a == b
    overlaps = [p["overlap"] for p in a["pairs"]]
    assert overlaps == sorted(overlaps, reverse=True)


def test_hostile_names_are_quoted():
    con = connect()
    try:
        con.execute('CREATE TABLE a ("k|1" INTEGER)')
        con.execute('CREATE TABLE b ("x""y" INTEGER)')
        con.execute("INSERT INTO a VALUES (1),(2)")
        con.execute("INSERT INTO b VALUES (2),(3)")
        pair = relate(con, "a", "b")["pairs"][0]
    finally:
        con.close()
    assert (pair["a"], pair["b"], pair["overlap"]) == ("k|1", 'x"y', 1)


def test_composite_key_candidate_when_no_single_column_is_unique():
    con = connect()
    try:
        con.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES ('A',1,5),('A',2,5),('B',1,5),('B',2,5)) "
            "v(site, month, qty)"
        )
        prof = profile_table(con, "t")
        con.execute("CREATE TABLE u AS SELECT * FROM (VALUES (1,'a'),(2,'a')) v(id, x)")
        single = profile_table(con, "u")
    finally:
        con.close()
    assert prof["key_candidates"] == [["site", "month"]]
    assert single["key_candidates"] == [["id"]]


def test_no_key_candidate_for_a_table_with_duplicate_rows():
    con = connect()
    try:
        con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1,1),(1,1)) v(a, b)")
        assert profile_table(con, "t")["key_candidates"] == []
    finally:
        con.close()


def _book() -> Workbook:
    return Workbook.open(
        make_xlsx(
            {
                "Cust": [["id", "name"], [1, "Ann"], [2, "Bob"], [3, "Cy"]],
                "Orders": [["order_id", "cust_id"], [10, 1], [11, 1], [12, 2], [13, 9]],
            }
        )
    )


def test_relate_tables_text_names_both_tables_and_the_best_pair_first():
    wb = _book()
    out = relate_tables(wb, "F1", "F1.S2.R1", wb, "F1", "F1.S1.R1")
    lines = out["text"].splitlines()
    assert lines[0].startswith("F1.S2.R1 Orders (4 rows) vs F1.S1.R1 Cust (3 rows)")
    assert "sha " in lines[0] and lines[0].endswith("Z")
    assert lines[1].startswith("cust_id -> id: N:1, shared 2 values")
    assert "1 of 3 cust_id values not in id" in lines[1]


def test_relate_tables_refuses_the_same_table_twice():
    wb = _book()
    with pytest.raises(WorkbookError, match="two different"):
        relate_tables(wb, "F1", "F1.S1.R1", wb, "F1", "F1.S1.R1")


def test_relate_tables_says_when_nothing_is_shared():
    wb = Workbook.open(
        make_xlsx({"A": [["k", "n"], ["x", 1], ["z", 2]], "B": [["j", "d"], ["y", 7], ["w", 8]]})
    )
    text = relate_tables(wb, "F1", "F1.S1.R1", wb, "F1", "F1.S2.R1")["text"]
    assert "no column pair shares a value" in text


def test_relate_with_unknown_table_is_a_node_error():
    con = _con()
    try:
        with pytest.raises(NodeError):
            relate(con, "orders", "nope")
    finally:
        con.close()
