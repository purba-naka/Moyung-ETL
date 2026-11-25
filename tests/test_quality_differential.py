from __future__ import annotations

import collections
import random
import re

import pytest
from pydantic import TypeAdapter, ValidationError
from test_nodes_differential import table  # noqa: F401

from moyung.nodes.base import schema_of
from moyung.quality import Check, check_schema, run_checks

ADAPTER = TypeAdapter(Check)
KS = [None, 1, 2, 3, 10**12]
SS = [None, "a", "b", "B", "zz", "a b", "", "é", "A1"]
XS = [None, -1.0, 0.0, 1.0, 2.5, 5.0, 1e9]
SEEDS = range(25)


def rows_for(seed):
    rnd = random.Random(seed)
    n = rnd.choice([0, 1, 2, 5, 20, 60])
    return [(rnd.choice(KS), rnd.choice(SS), rnd.choice(XS)) for _ in range(n)]


def setup(con, rows):
    table(con, "t", [("k", "BIGINT"), ("s", "VARCHAR"), ("x", "DOUBLE")], rows)
    table(con, "r", [("k", "BIGINT")], [(1,), (2,), (9,)])
    return {name: schema_of(con.table(name)) for name in ("t", "r")}


def check(con, schemas, **spec):
    results, broken = run_checks({"c": ADAPTER.validate_python(spec)}, con, schemas)
    assert not broken, broken
    (result,) = results
    return result


@pytest.mark.parametrize("seed", SEEDS)
class TestAgainstAPythonOracle:
    def test_not_null_counts_rows_with_any_null(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="not_null", table="t", columns=["k", "s"])
        want = sum(1 for r in rows if r[0] is None or r[1] is None)
        assert res.bad_rows == want and res.passed == (want == 0)

    def test_unique_counts_repeated_keys_and_null_equals_null(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="unique", table="t", columns=["k", "s"])
        counts = collections.Counter((r[0], r[1]) for r in rows)
        assert res.bad_rows == sum(1 for n in counts.values() if n > 1)

    def test_range_is_inclusive_and_skips_null(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="range", table="t", column="x", min=0, max=2.5)
        want = sum(1 for r in rows if r[2] is not None and not 0 <= r[2] <= 2.5)
        assert res.bad_rows == want

    def test_a_range_with_only_a_minimum(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="range", table="t", column="x", min=1.0)
        assert res.bad_rows == sum(1 for r in rows if r[2] is not None and r[2] < 1.0)

    def test_accepted_values_text_skips_null(self, con, seed):
        rows = rows_for(seed)
        res = check(
            con,
            setup(con, rows),
            kind="accepted_values",
            table="t",
            column="s",
            values=["a", "b", ""],
        )
        want = sum(1 for r in rows if r[1] is not None and r[1] not in ("a", "b", ""))
        assert res.bad_rows == want

    def test_accepted_values_whole_numbers_are_exact(self, con, seed):
        rows = rows_for(seed)
        res = check(
            con,
            setup(con, rows),
            kind="accepted_values",
            table="t",
            column="k",
            values=[1, 2, 10**12],
        )
        want = sum(1 for r in rows if r[0] is not None and r[0] not in (1, 2, 10**12))
        assert res.bad_rows == want

    def test_regex_is_a_search_and_null_does_not_match(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="regex", table="t", column="s", pattern="^[a-z]+$")
        want = sum(1 for r in rows if r[1] is None or not re.search("^[a-z]+$", r[1]))
        assert res.bad_rows == want

    def test_regex_without_anchors_finds_a_match_anywhere(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="regex", table="t", column="s", pattern="a")
        want = sum(1 for r in rows if r[1] is None or "a" not in r[1])
        assert res.bad_rows == want

    def test_duplicate_rows_counts_the_surplus_rows(self, con, seed):
        rows = rows_for(seed)
        res = check(con, setup(con, rows), kind="duplicate_rows", table="t")
        counts = collections.Counter(rows)
        assert res.bad_rows == sum(n - 1 for n in counts.values())

    def test_foreign_key_counts_rows_and_skips_null(self, con, seed):
        rows = rows_for(seed)
        res = check(
            con,
            setup(con, rows),
            kind="foreign_key",
            table="t",
            column="k",
            ref_table="r",
            ref_column="k",
        )
        want = sum(1 for r in rows if r[0] is not None and r[0] not in (1, 2, 9))
        assert res.bad_rows == want

    def test_row_count_reports_the_shortfall_or_the_excess(self, con, seed):
        rows = rows_for(seed)
        schemas = setup(con, rows)
        low = check(con, schemas, kind="row_count", table="t", min=10)
        assert (low.passed, low.bad_rows) == (len(rows) >= 10, max(0, 10 - len(rows)))
        high = check(con, schemas, kind="row_count", table="t", max=10)
        assert (high.passed, high.bad_rows) == (len(rows) <= 10, max(0, len(rows) - 10))

    @pytest.mark.parametrize("agg", ["sum", "count", "min", "max", "avg"])
    def test_reconcile_a_table_with_itself_always_passes(self, con, seed, agg):
        rows = rows_for(seed)
        side = {"table": "t", "agg": agg, "column": "x"}
        res = check(con, setup(con, rows), kind="reconcile", left=side, right=side)
        assert res.passed

    def test_reconcile_a_sum_with_a_number_matches_python(self, con, seed):
        rows = rows_for(seed)
        total = sum(r[2] for r in rows if r[2] is not None)
        schemas = setup(con, rows)
        left = {"table": "t", "agg": "sum", "column": "x"}
        assert check(con, schemas, kind="reconcile", left=left, value=total, rel_tol=1e-9).passed
        assert not check(con, schemas, kind="reconcile", left=left, value=total + 1000).passed


class TestKnownCases:
    @pytest.fixture
    def schemas(self, con):
        rows = [(1, "a", 1.0), (1, "a", 5.0), (2, None, None), (None, "B", -1.0), (None, "b", 2.5)]
        return setup(con, rows)

    def test_a_passing_check_has_no_bad_rows_and_an_empty_message(self, con, schemas):
        res = check(con, schemas, kind="row_count", table="t", min=1)
        assert (res.passed, res.bad_rows, res.message) == (True, 0, "")

    def test_severity_is_carried_into_the_result(self, con, schemas):
        res = check(con, schemas, kind="not_null", table="t", columns=["k"], severity="warn")
        assert res.severity == "warn" and not res.passed

    def test_the_message_names_the_column_with_nulls(self, con, schemas):
        res = check(con, schemas, kind="not_null", table="t", columns=["k", "s"])
        assert "k: 2" in res.message and "s: 1" in res.message

    def test_unique_examples_show_the_repeat_count(self, con, schemas):
        res = check(con, schemas, kind="unique", table="t", columns=["k"])
        assert "x2" in res.message

    @pytest.mark.xfail(
        strict=True, reason="BUG-019: the message shows CAST(... AS HUGEINT) instead of the column"
    )
    def test_accepted_values_on_a_number_column_names_the_column(self, con, schemas):
        res = check(con, schemas, kind="accepted_values", table="t", column="k", values=[1])
        assert "HUGEINT" not in res.message and "CAST" not in res.message

    def test_a_number_column_accepts_a_float_value_that_is_whole(self, con, schemas):
        res = check(con, schemas, kind="accepted_values", table="t", column="k", values=[1.0, 2.0])
        assert res.bad_rows in (0, 0.0) or res.passed

    def test_a_sum_of_nothing_reconciles_with_zero(self, con, schemas):
        left = {"table": "t", "agg": "sum", "column": "x", "where": "x > 1000"}
        assert check(con, schemas, kind="reconcile", left=left, value=0).passed

    def test_reconcile_tolerances(self, con):
        table(con, "n", [("v", "DOUBLE")], [(100.0,)])
        table(con, "m", [("v", "DOUBLE")], [(100.5,)])
        schemas = {n: schema_of(con.table(n)) for n in ("n", "m")}
        left = {"table": "n", "agg": "sum", "column": "v"}
        right = {"table": "m", "agg": "sum", "column": "v"}
        cases = [
            ({}, False),
            ({"abs_tol": 0.5}, True),
            ({"abs_tol": 0.49}, False),
            ({"rel_tol": 0.005}, True),
            ({"rel_tol": 0.0049}, False),
            ({"abs_tol": 0.4, "rel_tol": 0.005}, True),
        ]
        for tol, ok in cases:
            res = check(con, schemas, kind="reconcile", left=left, right=right, **tol)
            assert res.passed is ok, tol

    def test_reconcile_by_group_counts_the_groups_that_differ(self, con):
        table(
            con, "n", [("g", "VARCHAR"), ("v", "INTEGER")], [("a", 1), ("a", 2), ("b", 5), ("c", 1)]
        )
        table(con, "m", [("g", "VARCHAR"), ("v", "INTEGER")], [("a", 3), ("b", 6), ("d", 1)])
        schemas = {n: schema_of(con.table(n)) for n in ("n", "m")}
        left = {"table": "n", "agg": "sum", "column": "v"}
        right = {"table": "m", "agg": "sum", "column": "v"}
        res = check(con, schemas, kind="reconcile", left=left, right=right, by=["g"])
        assert res.bad_rows == 3

    def test_integer_aggregates_are_compared_exactly(self, con):
        table(con, "n", [("v", "BIGINT")], [(10**15,)])
        table(con, "m", [("v", "BIGINT")], [(10**15 + 1,)])
        schemas = {n: schema_of(con.table(n)) for n in ("n", "m")}
        left = {"table": "n", "agg": "sum", "column": "v"}
        right = {"table": "m", "agg": "sum", "column": "v"}
        assert not check(con, schemas, kind="reconcile", left=left, right=right).passed

    def test_a_filter_in_a_side_is_applied(self, con):
        table(con, "n", [("v", "INTEGER")], [(1,), (2,), (3,)])
        schemas = {"n": schema_of(con.table("n"))}
        left = {"table": "n", "agg": "sum", "column": "v", "where": "v >= 2"}
        assert check(con, schemas, kind="reconcile", left=left, value=5).passed


class TestBrokenChecks:
    def test_a_missing_column_is_reported_without_running(self, con):
        schemas = setup(con, [(1, "a", 1.0)])
        checks = {
            "c": ADAPTER.validate_python({"kind": "not_null", "table": "t", "columns": ["zz"]})
        }
        assert "zz" in check_schema(checks, schemas)["c"]
        results, broken = run_checks(checks, con, schemas)
        assert results == [] and "zz" in broken["c"]

    def test_a_check_on_a_table_that_did_not_come_out_is_skipped(self, con):
        schemas = setup(con, [(1, "a", 1.0)])
        checks = {"c": ADAPTER.validate_python({"kind": "row_count", "table": "gone", "min": 1})}
        assert check_schema(checks, schemas) == {}
        assert run_checks(checks, con, schemas) == ([], {})

    def test_a_range_on_text_is_refused(self, con):
        schemas = setup(con, [(1, "a", 1.0)])
        checks = {
            "c": ADAPTER.validate_python({"kind": "range", "table": "t", "column": "s", "min": 1})
        }
        assert "c" in check_schema(checks, schemas)

    def test_a_regex_on_a_number_column_is_refused_or_reads_text(self, con):
        schemas = setup(con, [(1, "a", 1.0)])
        checks = {
            "c": ADAPTER.validate_python(
                {"kind": "regex", "table": "t", "column": "k", "pattern": "1"}
            )
        }
        problems = check_schema(checks, schemas)
        results, broken = run_checks(checks, con, schemas)
        assert problems or results or broken

    def test_a_foreign_key_to_a_missing_column_is_reported(self, con):
        schemas = setup(con, [(1, "a", 1.0)])
        spec = {
            "kind": "foreign_key",
            "table": "t",
            "column": "k",
            "ref_table": "r",
            "ref_column": "zz",
        }
        assert "zz" in check_schema({"c": ADAPTER.validate_python(spec)}, schemas)["c"]

    @pytest.mark.parametrize(
        "spec",
        [
            {"kind": "range", "table": "t", "column": "x"},
            {"kind": "range", "table": "t", "column": "x", "min": True},
            {"kind": "row_count", "table": "t"},
            {"kind": "row_count", "table": "t", "min": -1},
            {"kind": "not_null", "table": "t", "columns": []},
            {"kind": "unique", "table": "", "columns": ["k"]},
            {"kind": "regex", "table": "t", "column": "s", "pattern": "("},
            {"kind": "regex", "table": "t", "column": "s", "pattern": "(?=a)"},
            {"kind": "accepted_values", "table": "t", "column": "s", "values": []},
            {"kind": "reconcile", "left": {"table": "t", "agg": "sum", "column": "x"}},
            {"kind": "reconcile", "left": {"table": "t", "agg": "sum"}, "value": 1},
            {
                "kind": "reconcile",
                "left": {"table": "t", "agg": "sum", "column": "x"},
                "value": float("nan"),
            },
            {
                "kind": "reconcile",
                "left": {"table": "t", "agg": "sum", "column": "x"},
                "value": 1,
                "abs_tol": -1,
            },
            {"kind": "magic", "table": "t"},
            {"kind": "not_null", "table": "t", "columns": ["k"], "severity": "fatal"},
            {"kind": "not_null", "table": "t", "columns": ["k"], "extra": 1},
        ],
    )
    def test_a_malformed_check_is_refused_when_it_is_parsed(self, spec):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(spec)

    def test_a_where_that_is_not_a_safe_expression_is_refused(self):
        spec = {
            "kind": "reconcile",
            "left": {"table": "t", "agg": "sum", "column": "x", "where": "1) UNION SELECT 1 --"},
            "value": 1,
        }
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(spec)

    def test_a_column_name_with_a_quote_is_safe(self, con):
        table(con, "q", [('say "hi"', "INTEGER")], [(None,), (1,)])
        schemas = {"q": schema_of(con.table("q"))}
        res = check(con, schemas, kind="not_null", table="q", columns=['say "hi"'])
        assert res.bad_rows == 1

    def test_a_table_name_that_looks_like_sql_is_quoted(self, con):
        table(con, 'a"; DROP TABLE t; --', [("k", "INTEGER")], [(1,), (1,)])
        schemas = {'a"; DROP TABLE t; --': schema_of(con.table('a"; DROP TABLE t; --'))}
        res = check(con, schemas, kind="duplicate_rows", table='a"; DROP TABLE t; --')
        assert res.bad_rows == 1
