from __future__ import annotations

import json

import duckdb
import pytest
from conftest import make_xlsx
from pydantic import TypeAdapter, ValidationError

from excel_etl.engine import run, validate
from excel_etl.nodes import schema_of
from excel_etl.pipeline import SpecError, parse_spec
from excel_etl.quality import Check, check_schema, run_checks

_CHECK = TypeAdapter(Check)


def chk(**kw):
    return _CHECK.validate_python(kw)


def world(**tables: str):
    """tables: name -> 'CREATE TABLE ...; INSERT ...' pieces joined; returns (con, schemas)."""
    con = duckdb.connect()
    for sql in tables.values():
        con.execute(sql)
    return con, {n: schema_of(con.table(n)) for n in tables}


def one(checks, con, schemas):
    results, broken = run_checks(checks, con, schemas)
    return results, broken


# --- not_null / unique -----------------------------------------------------------------------


def test_not_null_counts_rows_and_names_columns():
    con, sc = world(
        t="CREATE TABLE t AS SELECT * FROM (VALUES (1,'a'),(NULL,'b'),(3,NULL),(NULL,NULL)) v(id,\"Nama Produk\")"
    )
    (r,), broken = one(
        {"c": chk(kind="not_null", table="t", columns=["ID", "nama produk"])}, con, sc
    )
    assert not broken and not r.passed and r.bad_rows == 3  # rows with any NULL, not NULL cells
    assert "id: 2" in r.message and "Nama Produk: 2" in r.message


def test_not_null_passes_on_clean_and_on_empty_table():
    con, sc = world(t="CREATE TABLE t AS SELECT 1 AS a", e="CREATE TABLE e(a INTEGER)")
    res, _ = one(
        {
            "x": chk(kind="not_null", table="t", columns=["a"]),
            "y": chk(kind="not_null", table="e", columns=["a"]),
        },
        con,
        sc,
    )
    assert [r.passed for r in res] == [True, True] and res[0].message == ""


def test_unique_single_and_composite_keys_with_null():
    con, sc = world(
        t="CREATE TABLE t AS SELECT * FROM (VALUES ('a',1),('a',1),('a',2),('b',1),(NULL,1),(NULL,1)) v(k,n)"
    )
    (r1,), _ = one({"c": chk(kind="unique", table="t", columns=["k"])}, con, sc)
    assert r1.bad_rows == 2 and "'a'" in r1.message and "x3" in r1.message  # 'a' x3 and NULL x2
    (r2,), _ = one({"c": chk(kind="unique", table="t", columns=["k", "n"])}, con, sc)
    assert r2.bad_rows == 2  # ('a',1) x2 and (NULL,1) x2
    (r3,), _ = one({"c": chk(kind="unique", table="t", columns=["n", "k"])}, con, sc)
    assert r3.bad_rows == 2


def test_unique_passes_when_distinct():
    con, sc = world(t="CREATE TABLE t AS SELECT range AS i FROM range(5)")
    (r,), _ = one({"c": chk(kind="unique", table="t", columns=["i"])}, con, sc)
    assert r.passed and r.bad_rows == 0


# --- range -----------------------------------------------------------------------------------


def test_range_int_bounds_inclusive_and_nulls_skipped():
    con, sc = world(t="CREATE TABLE t AS SELECT * FROM (VALUES (-1),(0),(5),(10),(11),(NULL)) v(x)")
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", min=0, max=10)}, con, sc)
    assert r.bad_rows == 2 and "lowest -1, highest 11" in r.message
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", min=0)}, con, sc)
    assert r.bad_rows == 1
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", max=10)}, con, sc)
    assert r.bad_rows == 1


def test_range_fractional_bound_on_integer_column_is_not_rounded():
    con, sc = world(t="CREATE TABLE t AS SELECT * FROM (VALUES (2),(3)) v(x)")
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", min=2.5)}, con, sc)
    assert r.bad_rows == 1


def test_range_dates_and_doubles():
    con, sc = world(
        d="CREATE TABLE d AS SELECT * FROM (VALUES (DATE '2026-01-01'),(DATE '2026-02-15'),(DATE '2025-12-31')) v(day)",
        f="CREATE TABLE f AS SELECT * FROM (VALUES (0.5),(1.5),(-0.1)) v(p)",
    )
    (r,), _ = one(
        {"c": chk(kind="range", table="d", column="day", min="2026-01-01", max="2026-01-31")},
        con,
        sc,
    )
    assert r.bad_rows == 2
    (r,), _ = one({"c": chk(kind="range", table="f", column="p", min=0, max=1)}, con, sc)
    assert r.bad_rows == 2


def test_range_rejects_text_column_and_missing_bound():
    con, sc = world(t="CREATE TABLE t AS SELECT 'a' AS s")
    _, broken = run_checks({"c": chk(kind="range", table="t", column="s", min=0)}, con, sc)
    assert "needs a number or date column" in broken["c"]
    with pytest.raises(ValidationError, match="min, max or both"):
        chk(kind="range", table="t", column="s")


# --- accepted_values -------------------------------------------------------------------------


def test_accepted_values_text_number_and_null():
    con, sc = world(
        t="CREATE TABLE t AS SELECT * FROM (VALUES ('KG'),('TON'),('kg'),(NULL),('LB')) v(unit)",
        n="CREATE TABLE n AS SELECT * FROM (VALUES (1),(2),(3)) v(x)",
    )
    (r,), _ = one(
        {"c": chk(kind="accepted_values", table="t", column="unit", values=["KG", "TON"])}, con, sc
    )
    assert r.bad_rows == 2 and "LB" in r.message and "kg" in r.message
    (r,), _ = one({"c": chk(kind="accepted_values", table="n", column="x", values=[1, 2])}, con, sc)
    assert r.bad_rows == 1


def test_accepted_values_quotes_in_values_are_data_not_sql():
    con, sc = world(t="CREATE TABLE t AS SELECT * FROM (VALUES ('a'' OR 1=1 --'),('b')) v(s)")
    (r,), _ = one({"c": chk(kind="accepted_values", table="t", column="s", values=["b"])}, con, sc)
    assert r.bad_rows == 1


# --- reconcile -------------------------------------------------------------------------------

DAILY = """CREATE TABLE daily AS SELECT * FROM (VALUES
  ('jan', 10.0), ('jan', 20.0), ('feb', 5.0), ('feb', 5.5)) v(month, ton)"""
MONTHLY = """CREATE TABLE monthly AS SELECT * FROM (VALUES
  ('jan', 30.0), ('feb', 10.0)) v(bulan, total)"""


def rec(**kw):
    return chk(kind="reconcile", **kw)


def test_reconcile_totals_equal_and_off():
    con, sc = world(daily=DAILY, monthly=MONTHLY)
    ok = rec(
        left={"table": "daily", "column": "ton"}, right={"table": "monthly", "column": "total"}
    )
    (r,), _ = one({"c": ok}, con, sc)
    assert not r.passed and "= 40.5" in r.message and "= 40.0" in r.message
    ok.abs_tol = 0.5
    assert one({"c": ok}, con, sc)[0][0].passed
    ok.abs_tol, ok.rel_tol = 0.4, 0.0125  # 0.5 off a 40.5 total is 1.23%
    assert one({"c": ok}, con, sc)[0][0].passed
    ok.rel_tol = 0.012
    assert not one({"c": ok}, con, sc)[0][0].passed


def test_reconcile_by_group_with_different_column_names():
    con, sc = world(daily=DAILY, monthly=MONTHLY)
    c = rec(
        left={"table": "daily", "column": "ton"},
        right={"table": "monthly", "column": "total"},
        by=["month"],
    )
    # by must exist on both sides under the same name: bulan != month
    _, broken = run_checks({"c": c}, con, sc)
    assert "no column 'month'" in broken["c"]
    con.execute("CREATE TABLE m2 AS SELECT bulan AS month, total FROM monthly")
    sc["m2"] = schema_of(con.table("m2"))
    c.right.table = "m2"
    (r,), _ = one({"c": c}, con, sc)
    assert not r.passed and r.bad_rows == 1 and "feb" in r.message and "10.5 vs 10.0" in r.message


def test_reconcile_group_missing_on_one_side_counts_as_zero_for_sum():
    con, sc = world(
        a="CREATE TABLE a AS SELECT * FROM (VALUES ('x',1.0),('y',2.0)) v(k,v)",
        b="CREATE TABLE b AS SELECT * FROM (VALUES ('x',1.0)) v(k,v)",
    )
    c = rec(left={"table": "a", "column": "v"}, right={"table": "b", "column": "v"}, by=["k"])
    (r,), _ = one({"c": c}, con, sc)
    assert r.bad_rows == 1 and "'y'" in r.message
    c.right.table = "a"
    assert one({"c": c}, con, sc)[0][0].passed
    # and in the other direction (group only on the right)
    c2 = rec(left={"table": "b", "column": "v"}, right={"table": "a", "column": "v"}, by=["k"])
    assert one({"c": c2}, con, sc)[0][0].bad_rows == 1


def test_reconcile_null_group_key_matches_null():
    con, sc = world(
        a="CREATE TABLE a AS SELECT * FROM (VALUES (NULL,1.0),('x',2.0)) v(k,v)",
        b="CREATE TABLE b AS SELECT * FROM (VALUES (NULL,1.0),('x',2.0)) v(k,v)",
    )
    c = rec(left={"table": "a", "column": "v"}, right={"table": "b", "column": "v"}, by=["k"])
    assert one({"c": c}, con, sc)[0][0].passed


def test_reconcile_against_number_and_count():
    con, sc = world(daily=DAILY)
    c = rec(left={"table": "daily", "column": "ton"}, value=40.5)
    assert one({"c": c}, con, sc)[0][0].passed
    c = rec(left={"table": "daily", "agg": "count"}, value=5)
    r = one({"c": c}, con, sc)[0][0]
    assert not r.passed and "count(*) of daily = 4.0 but 5" in r.message


def test_reconcile_sum_of_empty_table_is_zero_and_avg_of_empty_differs():
    con, sc = world(e="CREATE TABLE e(v DOUBLE)")
    assert one({"c": rec(left={"table": "e", "column": "v"}, value=0)}, con, sc)[0][0].passed
    r = one({"c": rec(left={"table": "e", "agg": "avg", "column": "v"}, value=0)}, con, sc)[0][0]
    assert not r.passed  # no average exists; it is not silently 0


def test_reconcile_integer_sum_beyond_double_does_not_crash():
    con, sc = world(t="CREATE TABLE t AS SELECT 9223372036854775807::BIGINT AS v FROM range(3)")
    r = one({"c": rec(left={"table": "t", "column": "v"}, value=1.0)}, con, sc)[0][0]
    assert not r.passed


def test_reconcile_shape_errors():
    for kw, msg in [
        ({"left": {"table": "a", "column": "v"}}, "either right"),
        (
            {
                "left": {"table": "a", "column": "v"},
                "right": {"table": "b", "column": "v"},
                "value": 1,
            },
            "either right",
        ),
        ({"left": {"table": "a"}, "value": 1}, "needs a column"),
        ({"left": {"table": "a", "column": "v"}, "value": 1, "by": ["k"]}, "cannot be used"),
        (
            {"left": {"table": "a", "column": "v"}, "value": 1, "abs_tol": -1},
            "greater than or equal",
        ),
    ]:
        with pytest.raises(ValidationError, match=msg):
            rec(**kw)


def test_reconcile_text_column_sum_is_reported_not_crash():
    con, sc = world(t="CREATE TABLE t AS SELECT 'a' AS s")
    _, broken = run_checks({"c": rec(left={"table": "t", "column": "s"}, value=0)}, con, sc)
    assert "needs a number column" in broken["c"]


# --- planning errors -------------------------------------------------------------------------


def test_unknown_column_and_table_are_reported_with_alternatives():
    con, sc = world(t="CREATE TABLE t AS SELECT 1 AS a, 2 AS b")
    probs = check_schema(
        {
            "c1": chk(kind="not_null", table="t", columns=["zz"]),
            "c2": chk(kind="unique", table="gone", columns=["a"]),
        },
        sc,
    )
    assert "columns: a, b" in probs["c1"] and "c2" not in probs  # missing table: skipped here
    _, broken = run_checks({"c2": chk(kind="unique", table="gone", columns=["a"])}, con, sc)
    assert broken == {}  # skipped, an earlier step failed


def test_extra_keys_and_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        chk(kind="not_null", table="t", columns=["a"], colums=["b"])
    with pytest.raises(ValidationError):
        chk(kind="sorted", table="t")
    with pytest.raises(ValidationError):
        chk(kind="not_null", table="t", columns=[])
    with pytest.raises(ValidationError):
        chk(kind="not_null", table="t", columns=["a"], severity="fatal")


def test_check_results_order_follows_checks():
    con, sc = world(t="CREATE TABLE t AS SELECT 1 AS a")
    names = ["z", "a", "m"]
    res, _ = one({n: chk(kind="not_null", table="t", columns=["a"]) for n in names}, con, sc)
    assert [r.name for r in res] == names


# --- through the engine ----------------------------------------------------------------------


def _book():
    return make_xlsx(
        {
            "Daily": [["Month", "Ton"], ["jan", 10], ["jan", 20], ["feb", 5], ["feb", None]],
            "Monthly": [["Month", "Total"], ["jan", 30], ["feb", 5]],
        }
    )


def _spec(checks, outputs=True):
    src = lambda sheet, a: {  # noqa: E731
        "file_role": "f",
        "sheet": {"name": sheet},
        "region": {"header_anchor": [a]},
    }
    return parse_spec(
        json.dumps(
            {
                "sources": {"daily": src("Daily", "Ton"), "monthly": src("Monthly", "Total")},
                "outputs": {"out": {"input": "daily"}} if outputs else {},
                "checks": checks,
            }
        )
    )


def test_engine_failed_error_check_blocks_all_output(tmp_path):
    spec = _spec(
        {
            "ton_filled": {"kind": "not_null", "table": "daily", "columns": ["Ton"]},
            "sums": {
                "kind": "reconcile",
                "left": {"table": "daily", "column": "Ton"},
                "right": {"table": "monthly", "column": "Total"},
                "by": ["Month"],
            },
        }
    )
    rep = run(spec, {"f": _book()}, tmp_path)
    assert rep.status == "failed" and rep.outputs == ()
    assert [i.where for i in rep.issues] == ["checks.ton_filled"]
    assert list(tmp_path.iterdir()) == []
    assert [(c.name, c.passed) for c in rep.checks] == [("ton_filled", False), ("sums", True)]
    assert "check ton_filled|not_null|FAIL" in rep.text()


def test_engine_warn_check_reports_but_writes(tmp_path):
    spec = _spec(
        {
            "ton_filled": {
                "kind": "not_null",
                "table": "daily",
                "columns": ["Ton"],
                "severity": "warn",
            }
        }
    )
    rep = run(spec, {"f": _book()}, tmp_path)
    assert rep.status == "ok" and (tmp_path / "out.parquet").exists()
    assert any("checks.ton_filled" in w for w in rep.warnings)
    assert "|warn:" in rep.text()


def test_engine_all_pass_lists_results(tmp_path):
    spec = _spec({"u": {"kind": "unique", "table": "monthly", "columns": ["Month"]}})
    rep = run(spec, {"f": _book()}, tmp_path)
    assert rep.status == "ok" and [(c.name, c.passed) for c in rep.checks] == [("u", True)]


def test_engine_validate_catches_bad_check_without_running(tmp_path):
    spec = _spec({"u": {"kind": "unique", "table": "monthly", "columns": ["Nope"]}})
    rep = validate(spec, {"f": _book()})
    assert [i.where for i in rep.issues] == ["checks.u"] and "columns: Month, Total" in rep.issues[
        0
    ].message
    assert run(spec, {"f": _book()}, tmp_path).status == "failed"
    assert validate(spec).ok  # no files: nothing to check against


def test_spec_rejects_check_on_unknown_table():
    with pytest.raises(SpecError, match="reads 'ghost'"):
        _spec({"u": {"kind": "unique", "table": "ghost", "columns": ["a"]}})
    with pytest.raises(SpecError, match="reads 'ghost'"):
        _spec(
            {
                "r": {
                    "kind": "reconcile",
                    "left": {"table": "daily", "column": "Ton"},
                    "right": {"table": "ghost", "column": "x"},
                }
            }
        )


def test_spec_round_trip_keeps_checks():
    from excel_etl.pipeline import dump_spec

    spec = _spec({"r": {"kind": "range", "table": "daily", "column": "Ton", "min": 0}})
    assert parse_spec(dump_spec(spec)) == spec


def test_check_on_failed_step_is_not_reported_twice(tmp_path):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "daily": {
                        "file_role": "f",
                        "sheet": {"name": "Daily"},
                        "region": {"header_anchor": ["Ton"]},
                    }
                },
                "steps": [
                    {
                        "id": "bad",
                        "node": "filter",
                        "inputs": ["daily"],
                        "config": {"where": "nope > 1"},
                    }
                ],
                "checks": {"c": {"kind": "not_null", "table": "bad", "columns": ["Ton"]}},
            }
        )
    )
    rep = validate(spec, {"f": _book()})
    assert [i.where for i in rep.issues] == ["steps.bad"]


def test_range_bound_below_one_unit_is_exact_on_integers():
    con, sc = world(t="CREATE TABLE t AS SELECT * FROM (VALUES (2),(3)) v(x)")
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", min=2.4)}, con, sc)
    assert r.bad_rows == 1
    (r,), _ = one({"c": chk(kind="range", table="t", column="x", max=2.6)}, con, sc)
    assert r.bad_rows == 1


def test_reconcile_relative_tolerance_uses_the_larger_side():
    con, sc = world(a="CREATE TABLE a AS SELECT 100.0 AS v", b="CREATE TABLE b AS SELECT 90.0 AS v")
    c = rec(left={"table": "a", "column": "v"}, right={"table": "b", "column": "v"}, rel_tol=0.1)
    assert one({"c": c}, con, sc)[0][0].passed  # 10 off, 10% of the larger (100)
    c.rel_tol = 0.099
    assert not one({"c": c}, con, sc)[0][0].passed


def test_reconcile_missing_group_equals_a_zero_group_for_sum_and_count_only():
    con, sc = world(
        a="CREATE TABLE a AS SELECT * FROM (VALUES ('x',1.0),('y',0.0)) v(k,v)",
        b="CREATE TABLE b AS SELECT * FROM (VALUES ('x',1.0)) v(k,v)",
    )
    for side in ("left", "right"):
        lt, rt = ("a", "b") if side == "left" else ("b", "a")
        c = rec(left={"table": lt, "column": "v"}, right={"table": rt, "column": "v"}, by=["k"])
        assert one({"c": c}, con, sc)[0][0].passed
    # for max a missing group is a difference, not 0
    c = rec(
        left={"table": "a", "agg": "max", "column": "v"},
        right={"table": "b", "agg": "max", "column": "v"},
        by=["k"],
    )
    assert one({"c": c}, con, sc)[0][0].bad_rows == 1
