from __future__ import annotations

import duckdb
import pytest
from pydantic import TypeAdapter, ValidationError

from moyung.nodes.base import NodeError, connect, schema_of
from moyung.nodes.sqlexpr import check_expression
from moyung.quality import Check, run_checks

_ADAPTER = TypeAdapter(Check)


def _verdict(setup: list[str], check: dict) -> str:
    con = duckdb.connect()
    for sql in setup:
        con.execute(sql)
    names = [
        r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    ]
    schemas = {t: schema_of(con.table(t)) for t in names}
    results, broken = run_checks({"c": _ADAPTER.validate_python(check)}, con, schemas)
    if broken:
        return f"broken: {broken}"
    return "pass" if results[0].passed else "fail"


def _sum(table: str) -> dict:
    return {"table": table, "agg": "sum", "column": "v"}


@pytest.mark.parametrize("bad", ["'nan'::DOUBLE", "'inf'::DOUBLE", "'-inf'::DOUBLE"])
def test_reconcile_fails_when_a_total_is_not_finite(bad):
    setup = [f"CREATE TABLE a AS SELECT {bad} v", "CREATE TABLE b AS SELECT 5.0::DOUBLE v"]
    for left, right in (("a", "b"), ("b", "a")):
        check = {"kind": "reconcile", "left": _sum(left), "right": _sum(right)}
        assert _verdict(setup, check) == "fail"


@pytest.mark.parametrize("field", ["value", "abs_tol", "rel_tol"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_reconcile_refuses_a_non_finite_number_in_the_spec(field, bad):
    check = {"kind": "reconcile", "left": _sum("a"), "value": 1.0, field: bad}
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(check)


def test_reconcile_relative_tolerance_uses_the_larger_side():
    setup = ["CREATE TABLE a AS SELECT 100.0 v", "CREATE TABLE b AS SELECT 111.0 v"]
    check = {"kind": "reconcile", "left": _sum("a"), "right": _sum("b"), "rel_tol": 0.1}
    assert _verdict(setup, check) == "pass"


def test_reconcile_counts_a_group_missing_on_one_side_as_zero():
    setup = [
        "CREATE TABLE a AS SELECT 'x' g, NULL::INTEGER v",
        "CREATE TABLE b AS SELECT 'y' g, NULL::INTEGER v",
    ]
    side = {"agg": "count", "column": "v"}
    check = {"kind": "reconcile", "left": {"table": "a", **side}, "right": {"table": "b", **side}}
    assert _verdict(setup, {**check, "by": ["g"]}) == "pass"


@pytest.mark.parametrize("bound", ["min", "max"])
def test_range_fails_a_nan_whichever_bound_is_set(bound):
    setup = ["CREATE TABLE a AS SELECT 'nan'::DOUBLE v"]
    assert _verdict(setup, {"kind": "range", "table": "a", "column": "v", bound: 5}) == "fail"


def test_range_compares_big_integers_and_decimals_exactly():
    big = ["CREATE TABLE a AS SELECT 9007199254740993::BIGINT v"]
    over = {"kind": "range", "table": "a", "column": "v", "max": 9007199254740992}
    assert _verdict(big, over) == "fail"
    assert _verdict(big, {**over, "max": 9007199254740993}) == "pass"
    dec = ["CREATE TABLE a AS SELECT 1.0000000000000001::DECIMAL(20,19) v"]
    assert _verdict(dec, {"kind": "range", "table": "a", "column": "v", "max": 1}) == "fail"
    assert _verdict(dec, {"kind": "range", "table": "a", "column": "v", "min": 1}) == "pass"


def test_range_with_a_fractional_bound_on_integers_still_works():
    setup = ["CREATE TABLE a AS SELECT unnest([1, 2, 3]) v"]
    check = {"kind": "range", "table": "a", "column": "v", "min": 1.5}
    assert _verdict(setup, check) == "fail"


def test_range_refuses_a_true_false_bound():
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"kind": "range", "table": "a", "column": "v", "max": True})


def test_accepted_values_compares_big_integers_exactly_and_skips_fractions():
    setup = ["CREATE TABLE a AS SELECT 9007199254740993::BIGINT v"]
    base = {"kind": "accepted_values", "table": "a", "column": "v"}
    assert _verdict(setup, {**base, "values": [9007199254740992]}) == "fail"
    assert _verdict(setup, {**base, "values": [9007199254740993]}) == "pass"
    assert _verdict(setup, {**base, "values": [1.5]}) == "fail"


@pytest.mark.parametrize(
    "expr",
    [
        "unnest([1, 2, 3])",
        "format('{:>15}', 1)",
        "printf('%5d', 1)",
        "current_date",
        "current_timestamp",
        "localtime",
        "current_localtime()",
        "now()",
    ],
)
def test_expressions_that_grow_rows_memory_or_follow_the_clock_are_refused(expr):
    with pytest.raises(NodeError):
        check_expression(expr)


def test_a_deeply_nested_expression_is_a_node_error_not_a_crash():
    with pytest.raises(NodeError, match="nested too deeply"):
        check_expression("NOT " * 990 + "true")


def test_every_connection_has_a_memory_limit():
    con = connect()
    limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert limit.endswith("GiB") and limit.startswith("3.7")
    assert (
        connect(memory_limit="1GB")
        .execute("SELECT current_setting('memory_limit')")
        .fetchone()[0]
        .startswith("953")
    )
