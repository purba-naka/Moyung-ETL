from __future__ import annotations

import contextlib

import duckdb
import pytest

from moyung.nodes.base import NodeError, build, check_arity, infer, schema_of


def table(con, name, columns, rows):
    quoted = '"' + name.replace('"', '""') + '"'
    cols = ", ".join('"' + n.replace('"', '""') + '" ' + t for n, t in columns)
    con.execute(f"CREATE TABLE {quoted} ({cols})")
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f"INSERT INTO {quoted} VALUES ({marks})", rows)


def apply(con, node, config, *tables):
    n = build(node, config)
    rels = [con.table(t) for t in tables]
    check_arity(n, len(rels))
    inferred = infer(n, [schema_of(r) for r in rels])
    out = n.run(rels)
    assert schema_of(out) == inferred, f"{node}: infer_schema promised another schema than run"
    return out.fetchall()


def oracle(con, sql):
    return con.execute(sql).fetchall()


def same_rows(a, b):
    key = lambda r: tuple((v is None, str(v)) for v in r)  # noqa: E731
    return sorted(a, key=key) == sorted(b, key=key)


KEYS = [("k", "INTEGER"), ("l", "VARCHAR")]
LEFT = [(1, "a"), (2, "b"), (2, "b2"), (None, "n"), (3, "c")]
RIGHT = [(1, "x"), (2, "y"), (2, "y2"), (None, "m"), (4, "z")]


class TestJoin:
    @pytest.fixture
    def tables(self, con):
        table(con, "L", [("k", "INTEGER"), ("a", "VARCHAR")], LEFT)
        table(con, "R", [("k", "INTEGER"), ("b", "VARCHAR")], RIGHT)

    @pytest.mark.parametrize("how", ["inner", "left", "right", "full"])
    def test_every_kind_matches_plain_sql(self, con, tables, how):
        sql_kind = {"inner": "INNER", "left": "LEFT", "right": "RIGHT", "full": "FULL"}[how]
        got = apply(con, "join", {"on": ["k"], "how": how}, "L", "R")
        select = "coalesce(L.k, R.k)" if how in ("right", "full") else "L.k"
        want = oracle(con, f"SELECT {select}, a, b FROM L {sql_kind} JOIN R ON L.k = R.k")
        assert same_rows(got, want)

    def test_null_keys_match_nothing_by_default(self, con, tables):
        got = apply(con, "join", {"on": ["k"], "how": "inner"}, "L", "R")
        assert all(row[0] is not None for row in got)

    def test_null_equal_joins_null_to_null(self, con, tables):
        got = apply(con, "join", {"on": ["k"], "null_equal": True}, "L", "R")
        assert (None, "n", "m") in got

    def test_duplicate_keys_fan_out(self, con, tables):
        got = apply(con, "join", {"on": ["k"]}, "L", "R")
        assert len([r for r in got if r[0] == 2]) == 4

    def test_a_left_join_keeps_unmatched_left_rows_with_null(self, con, tables):
        got = apply(con, "join", {"on": ["k"], "how": "left"}, "L", "R")
        assert (3, "c", None) in got and (None, "n", None) in got and len(got) == 7

    def test_a_full_join_key_comes_from_the_side_that_has_the_row(self, con, tables):
        got = apply(con, "join", {"on": ["k"], "how": "full"}, "L", "R")
        assert (4, None, "z") in got

    def test_different_key_names_keep_both_columns(self, con):
        table(con, "L", [("id", "INTEGER"), ("a", "VARCHAR")], [(1, "x")])
        table(con, "R", [("code", "INTEGER"), ("b", "VARCHAR")], [(1, "y")])
        got = apply(con, "join", {"on": [{"left": "id", "right": "code"}]}, "L", "R")
        assert got == [(1, "x", 1, "y")]

    def test_two_keys_must_both_match(self, con):
        table(
            con,
            "L",
            [("a", "INTEGER"), ("b", "INTEGER"), ("x", "VARCHAR")],
            [(1, 1, "p"), (1, 2, "q")],
        )
        table(con, "R", [("a", "INTEGER"), ("b", "INTEGER"), ("y", "VARCHAR")], [(1, 2, "z")])
        assert apply(con, "join", {"on": ["a", "b"]}, "L", "R") == [(1, 2, "q", "z")]

    def test_a_shared_non_key_column_is_an_error_without_suffixes(self, con):
        table(con, "L", [("k", "INTEGER"), ("v", "INTEGER")], [(1, 1)])
        table(con, "R", [("k", "INTEGER"), ("v", "INTEGER")], [(1, 2)])
        with pytest.raises(NodeError):
            apply(con, "join", {"on": ["k"]}, "L", "R")

    def test_suffixes_rename_the_shared_columns(self, con):
        table(con, "L", [("k", "INTEGER"), ("v", "INTEGER")], [(1, 1)])
        table(con, "R", [("k", "INTEGER"), ("v", "INTEGER")], [(1, 2)])
        got = apply(con, "join", {"on": ["k"], "suffixes": ["_l", "_r"]}, "L", "R")
        assert got == [(1, 1, 2)]

    def test_equal_suffixes_are_refused(self):
        with pytest.raises(NodeError):
            build("join", {"on": ["k"], "suffixes": ["_x", "_x"]})

    def test_a_missing_key_is_named(self, con, tables):
        with pytest.raises(NodeError, match="zz"):
            apply(con, "join", {"on": ["zz"]}, "L", "R")

    def test_an_empty_side(self, con):
        table(con, "L", [("k", "INTEGER")], [])
        table(con, "R", [("k", "INTEGER"), ("b", "VARCHAR")], [(1, "y")])
        assert apply(con, "join", {"on": ["k"]}, "L", "R") == []
        assert apply(con, "join", {"on": ["k"], "how": "right"}, "L", "R") == [(1, "y")]

    def test_three_inputs_are_refused(self):
        with pytest.raises(NodeError):
            check_arity(build("join", {"on": ["k"]}), 3)

    def test_one_input_is_refused(self):
        with pytest.raises(NodeError):
            check_arity(build("join", {"on": ["k"]}), 1)

    def test_keys_of_unlike_types_are_an_error_or_coerce_but_never_crash(self, con):
        table(con, "L", [("k", "INTEGER")], [(1,)])
        table(con, "R", [("k", "VARCHAR"), ("b", "INTEGER")], [("1", 5), ("x", 6)])
        with contextlib.suppress(NodeError):
            apply(con, "join", {"on": ["k"]}, "L", "R")


class TestGroupBy:
    @pytest.fixture
    def sales(self, con):
        rows = [
            ("b", 5, "x"),
            ("a", 1, "y"),
            ("b", None, "z"),
            (None, 7, "w"),
            ("a", 3, None),
            (None, None, None),
        ]
        table(con, "S", [("g", "VARCHAR"), ("v", "INTEGER"), ("t", "VARCHAR")], rows)

    def test_groups_come_out_in_order_of_first_appearance(self, con, sales):
        got = apply(
            con, "group_by", {"by": ["g"], "aggs": [{"name": "n", "fn": "count_rows"}]}, "S"
        )
        assert [r[0] for r in got] == ["b", "a", None]

    def test_null_is_one_group(self, con, sales):
        got = dict(
            apply(con, "group_by", {"by": ["g"], "aggs": [{"name": "n", "fn": "count_rows"}]}, "S")
        )
        assert got[None] == 2

    def test_count_skips_empty_and_count_rows_does_not(self, con, sales):
        aggs = [{"name": "c", "fn": "count", "column": "v"}, {"name": "r", "fn": "count_rows"}]
        got = {r[0]: r[1:] for r in apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S")}
        assert got == {"b": (1, 2), "a": (2, 2), None: (1, 2)}

    def test_a_sum_of_nothing_is_null(self, con):
        table(con, "S", [("g", "VARCHAR"), ("v", "INTEGER")], [("a", None), ("a", None)])
        got = apply(
            con, "group_by", {"by": ["g"], "aggs": [{"name": "s", "fn": "sum", "column": "v"}]}, "S"
        )
        assert got == [("a", None)]

    def test_whole_number_sums_are_bigint(self, con, sales):
        n = build("group_by", {"by": ["g"], "aggs": [{"name": "s", "fn": "sum", "column": "v"}]})
        schema = infer(n, [schema_of(con.table("S"))])
        assert schema[1].type == "BIGINT"

    def test_avg_ignores_nulls(self, con, sales):
        got = dict(
            apply(
                con,
                "group_by",
                {"by": ["g"], "aggs": [{"name": "m", "fn": "avg", "column": "v"}]},
                "S",
            )
        )
        assert got == {"b": 5.0, "a": 2.0, None: 7.0}

    def test_min_max_on_text(self, con, sales):
        aggs = [
            {"name": "lo", "fn": "min", "column": "t"},
            {"name": "hi", "fn": "max", "column": "t"},
        ]
        got = {r[0]: r[1:] for r in apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S")}
        assert got["b"] == ("x", "z") and got["a"] == ("y", "y")

    def test_first_and_last_follow_table_order_and_skip_empty(self, con, sales):
        aggs = [
            {"name": "f", "fn": "first", "column": "v"},
            {"name": "l", "fn": "last", "column": "v"},
        ]
        got = {r[0]: r[1:] for r in apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S")}
        assert got["b"] == (5, 5) and got["a"] == (1, 3) and got[None] == (7, 7)

    def test_join_text_keeps_table_order_and_skips_empty(self, con, sales):
        aggs = [{"name": "j", "fn": "join_text", "column": "t", "separator": "|"}]
        got = dict(apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S"))
        assert got["b"] == "x|z" and got["a"] == "y" and got[None] == "w"

    def test_count_distinct(self, con):
        table(con, "S", [("g", "INTEGER"), ("v", "INTEGER")], [(1, 1), (1, 1), (1, 2), (1, None)])
        aggs = [{"name": "d", "fn": "count_distinct", "column": "v"}]
        assert apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S") == [(1, 2)]

    def test_an_empty_by_summarises_the_whole_table(self, con, sales):
        got = apply(con, "group_by", {"aggs": [{"name": "n", "fn": "count_rows"}]}, "S")
        assert got == [(6,)]

    def test_an_empty_table_with_no_by_gives_one_row(self, con):
        table(con, "S", [("v", "INTEGER")], [])
        got = apply(
            con,
            "group_by",
            {
                "aggs": [
                    {"name": "n", "fn": "count_rows"},
                    {"name": "s", "fn": "sum", "column": "v"},
                ]
            },
            "S",
        )
        assert got == [(0, None)]

    def test_an_empty_table_with_by_gives_no_rows(self, con):
        table(con, "S", [("g", "INTEGER")], [])
        assert (
            apply(con, "group_by", {"by": ["g"], "aggs": [{"name": "n", "fn": "count_rows"}]}, "S")
            == []
        )

    def test_a_bigint_sum_overflow_is_an_error_not_a_wrong_number(self, con):
        table(con, "S", [("v", "BIGINT")], [(2**62,), (2**62,), (2**62,)])
        with pytest.raises(duckdb.Error, match="(?i)overflow|out of range"):
            apply(con, "group_by", {"aggs": [{"name": "s", "fn": "sum", "column": "v"}]}, "S")

    def test_sum_of_text_is_refused_when_the_spec_is_checked(self, con, sales):
        with pytest.raises(NodeError):
            apply(
                con,
                "group_by",
                {"by": ["g"], "aggs": [{"name": "s", "fn": "sum", "column": "t"}]},
                "S",
            )

    def test_two_outputs_with_one_name_are_refused(self, con, sales):
        aggs = [{"name": "n", "fn": "count_rows"}, {"name": "n", "fn": "count", "column": "v"}]
        with pytest.raises(NodeError):
            apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S")

    def test_an_output_named_like_a_group_column_is_refused(self, con, sales):
        with pytest.raises(NodeError):
            apply(con, "group_by", {"by": ["g"], "aggs": [{"name": "g", "fn": "count_rows"}]}, "S")

    def test_count_rows_with_a_column_is_refused(self):
        with pytest.raises(NodeError):
            build("group_by", {"aggs": [{"name": "n", "fn": "count_rows", "column": "v"}]})

    def test_a_sum_without_a_column_is_refused(self, con, sales):
        with pytest.raises(NodeError):
            apply(con, "group_by", {"aggs": [{"name": "s", "fn": "sum"}]}, "S")

    def test_a_group_by_matches_plain_sql_on_many_rows(self, con):
        rows = [(i % 7, i % 3, i) for i in range(500)]
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER"), ("v", "INTEGER")], rows)
        aggs = [
            {"name": "s", "fn": "sum", "column": "v"},
            {"name": "m", "fn": "max", "column": "v"},
        ]
        got = apply(con, "group_by", {"by": ["a", "b"], "aggs": aggs}, "S")
        want = oracle(con, "SELECT a, b, sum(v), max(v) FROM S GROUP BY a, b")
        assert same_rows(got, want)


class TestUnion:
    def test_by_name_fills_the_missing_columns_with_null(self, con):
        table(con, "A", [("x", "INTEGER"), ("y", "VARCHAR")], [(1, "p")])
        table(con, "B", [("y", "VARCHAR"), ("z", "INTEGER")], [("q", 9)])
        got = apply(con, "union", {}, "A", "B")
        assert same_rows(got, [(1, "p", None), (None, "q", 9)])

    def test_by_position_takes_names_from_the_first_table(self, con):
        table(con, "A", [("x", "INTEGER"), ("y", "VARCHAR")], [(1, "p")])
        table(con, "B", [("q", "INTEGER"), ("r", "VARCHAR")], [(2, "s")])
        n = build("union", {"by": "position"})
        schema = infer(n, [schema_of(con.table("A")), schema_of(con.table("B"))])
        assert [c.name for c in schema] == ["x", "y"]
        assert apply(con, "union", {"by": "position"}, "A", "B") == [(1, "p"), (2, "s")]

    def test_by_position_with_unequal_widths_is_refused(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,)])
        table(con, "B", [("x", "INTEGER"), ("y", "INTEGER")], [(1, 2)])
        with pytest.raises(NodeError):
            apply(con, "union", {"by": "position"}, "A", "B")

    def test_numbers_of_different_types_become_double(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,)])
        table(con, "B", [("x", "DOUBLE")], [(2.5,)])
        n = build("union", {})
        schema = infer(n, [schema_of(con.table("A")), schema_of(con.table("B"))])
        assert schema[0].type == "DOUBLE"
        assert sorted(apply(con, "union", {}, "A", "B")) == [(1.0,), (2.5,)]

    def test_a_number_and_a_text_become_text(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,)])
        table(con, "B", [("x", "VARCHAR")], [("a",)])
        got = apply(con, "union", {}, "A", "B")
        assert sorted(got) == [("1",), ("a",)]

    def test_source_column_numbers_the_inputs_from_zero(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,)])
        table(con, "B", [("x", "INTEGER")], [(2,)])
        got = apply(con, "union", {"source_column": "src"}, "A", "B")
        assert sorted(got) == [(1, 0), (2, 1)]

    def test_distinct_drops_repeated_rows_including_null_rows(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,), (None,)])
        table(con, "B", [("x", "INTEGER")], [(1,), (None,)])
        got = apply(con, "union", {"distinct": True}, "A", "B")
        assert sorted(got, key=lambda r: (r[0] is None, r[0])) == [(1,), (None,)]

    def test_without_distinct_duplicates_stay(self, con):
        table(con, "A", [("x", "INTEGER")], [(1,)])
        table(con, "B", [("x", "INTEGER")], [(1,)])
        assert apply(con, "union", {}, "A", "B") == [(1,), (1,)]

    def test_three_tables(self, con):
        for name, v in (("A", 1), ("B", 2), ("C", 3)):
            table(con, name, [("x", "INTEGER")], [(v,)])
        assert apply(con, "union", {}, "A", "B", "C") == [(1,), (2,), (3,)]

    def test_one_table_is_refused(self, con):
        with pytest.raises(NodeError):
            check_arity(build("union", {}), 1)

    def test_the_source_column_may_not_clash_with_a_column(self, con):
        table(con, "A", [("src", "INTEGER")], [(1,)])
        table(con, "B", [("src", "INTEGER")], [(2,)])
        with pytest.raises(NodeError):
            apply(con, "union", {"source_column": "src"}, "A", "B")

    def test_column_names_that_differ_only_in_case_are_one_column_by_name(self, con):
        table(con, "A", [("Qty", "INTEGER")], [(1,)])
        table(con, "B", [("qty", "INTEGER")], [(2,)])
        got = apply(con, "union", {}, "A", "B")
        assert sorted(got) == [(1,), (2,)]


class TestDedupe:
    def test_the_first_row_is_kept_and_order_stays(self, con):
        table(
            con, "S", [("k", "INTEGER"), ("v", "VARCHAR")], [(2, "a"), (1, "b"), (2, "c"), (1, "d")]
        )
        assert apply(con, "dedupe", {"keys": ["k"]}, "S") == [(2, "a"), (1, "b")]

    def test_null_keys_count_as_equal(self, con):
        table(con, "S", [("k", "INTEGER"), ("v", "VARCHAR")], [(None, "a"), (None, "b")])
        assert apply(con, "dedupe", {"keys": ["k"]}, "S") == [(None, "a")]

    def test_no_keys_means_whole_row(self, con):
        table(
            con,
            "S",
            [("k", "INTEGER"), ("v", "VARCHAR")],
            [(1, "a"), (1, "a"), (1, None), (1, None)],
        )
        assert apply(con, "dedupe", {}, "S") == [(1, "a"), (1, None)]

    def test_a_table_without_repeats_is_unchanged(self, con):
        rows = [(i, str(i)) for i in range(50)]
        table(con, "S", [("k", "INTEGER"), ("v", "VARCHAR")], rows)
        assert apply(con, "dedupe", {}, "S") == rows

    def test_an_unknown_key_is_named(self, con):
        table(con, "S", [("k", "INTEGER")], [(1,)])
        with pytest.raises(NodeError, match="zz"):
            apply(con, "dedupe", {"keys": ["zz"]}, "S")

    def test_a_repeated_key_is_refused(self, con):
        table(con, "S", [("k", "INTEGER")], [(1,)])
        with pytest.raises(NodeError):
            apply(con, "dedupe", {"keys": ["k", "k"]}, "S")

    def test_an_empty_table(self, con):
        table(con, "S", [("k", "INTEGER")], [])
        assert apply(con, "dedupe", {}, "S") == []


class TestSort:
    def test_nulls_go_last_ascending_and_descending(self, con):
        table(con, "S", [("v", "INTEGER")], [(2,), (None,), (1,), (3,)])
        assert apply(con, "sort", {"by": ["v"]}, "S") == [(1,), (2,), (3,), (None,)]
        assert apply(con, "sort", {"by": ["-v"]}, "S") == [(3,), (2,), (1,), (None,)]

    def test_ties_are_a_permutation_ordered_by_the_key_and_the_same_every_run(self, con):
        rows = [(i % 3, i) for i in range(60)]
        table(con, "S", [("k", "INTEGER"), ("i", "INTEGER")], rows)
        first = apply(con, "sort", {"by": ["k"]}, "S")
        assert sorted(first, key=lambda r: r[1]) == sorted(rows, key=lambda r: r[1])
        assert [r[0] for r in first] == sorted(r[0] for r in rows)
        assert apply(con, "sort", {"by": ["k"]}, "S") == first

    def test_two_keys_with_mixed_direction(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, 1), (1, 2), (2, 1), (2, 2)])
        assert apply(con, "sort", {"by": ["a", "-b"]}, "S") == [(1, 2), (1, 1), (2, 2), (2, 1)]

    def test_text_sorts_by_bytes_not_by_locale(self, con):
        table(con, "S", [("t", "VARCHAR")], [("b",), ("B",), ("a",), ("A",)])
        assert [r[0] for r in apply(con, "sort", {"by": ["t"]}, "S")] == ["A", "B", "a", "b"]

    def test_a_column_that_really_starts_with_a_dash(self, con):
        table(con, "S", [("-x", "INTEGER")], [(1,), (2,)])
        try:
            got = apply(con, "sort", {"by": ["-x"]}, "S")
        except NodeError:
            return
        assert sorted(got) == [(1,), (2,)]

    def test_an_unknown_column_is_named(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,)])
        with pytest.raises(NodeError, match="zz"):
            apply(con, "sort", {"by": ["zz"]}, "S")


class TestFilter:
    def test_a_null_condition_drops_the_row(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,), (None,), (3,)])
        assert apply(con, "filter", {"where": "v > 1"}, "S") == [(3,)]

    def test_not_of_null_is_still_null_so_the_row_is_dropped(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,), (None,), (3,)])
        assert apply(con, "filter", {"where": "NOT (v > 1)"}, "S") == [(1,)]

    def test_is_null_finds_the_empty_rows(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,), (None,)])
        assert apply(con, "filter", {"where": "v IS NULL"}, "S") == [(None,)]

    def test_a_quoted_column_with_spaces_and_a_pipe(self, con):
        table(con, "S", [("JAN|Ton", "INTEGER")], [(1,), (5,)])
        assert apply(con, "filter", {"where": '"JAN|Ton" > 2'}, "S") == [(5,)]

    def test_a_filter_that_matches_nothing(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,)])
        assert apply(con, "filter", {"where": "v > 99"}, "S") == []

    def test_an_unknown_column_is_a_node_error(self, con):
        table(con, "S", [("v", "INTEGER")], [(1,)])
        with pytest.raises(NodeError):
            apply(con, "filter", {"where": "zz > 1"}, "S")

    def test_a_filter_that_is_not_a_condition_still_runs_or_fails_cleanly(self, con):
        table(con, "S", [("v", "INTEGER")], [(0,), (1,), (2,)])
        try:
            got = apply(con, "filter", {"where": "v"}, "S")
        except NodeError:
            return
        assert got in ([(1,), (2,)], [])


class TestDerive:
    def test_a_new_column_goes_last_and_an_existing_one_is_replaced_in_place(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, 2)])
        n = build("derive", {"name": "c", "expr": "a + b"})
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["a", "b", "c"]
        n = build("derive", {"name": "a", "expr": "a * 10"})
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["a", "b"]
        assert apply(con, "derive", {"name": "a", "expr": "a * 10"}, "S") == [(10, 2)]

    def test_division_by_zero_is_inf_and_nullif_turns_it_into_null(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, 0)])
        assert apply(con, "derive", {"name": "c", "expr": "a / b"}, "S")[0][2] == float("inf")
        assert apply(con, "derive", {"name": "c", "expr": "a / nullif(b, 0)"}, "S")[0][2] is None

    def test_null_in_arithmetic_gives_null_and_coalesce_fixes_it(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, None)])
        assert apply(con, "derive", {"name": "c", "expr": "a + b"}, "S")[0][2] is None
        assert apply(con, "derive", {"name": "c", "expr": "a + coalesce(b, 0)"}, "S")[0][2] == 1

    def test_type_casts_the_result(self, con):
        table(con, "S", [("a", "INTEGER")], [(1,)])
        n = build("derive", {"name": "c", "expr": "a", "type": "VARCHAR"})
        assert infer(n, [schema_of(con.table("S"))])[-1].type == "VARCHAR"

    def test_a_new_name_that_differs_only_in_case_is_refused(self, con):
        table(con, "S", [("Qty", "INTEGER")], [(1,)])
        with pytest.raises(NodeError):
            apply(con, "derive", {"name": "qty", "expr": "1"}, "S")

    def test_integer_division_keeps_the_fraction(self, con):
        table(con, "S", [("a", "INTEGER")], [(7,)])
        assert apply(con, "derive", {"name": "c", "expr": "a / 2"}, "S")[0][1] == 3.5


class TestRenameSelect:
    def test_rename_keeps_order_and_other_columns(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER"), ("c", "INTEGER")], [(1, 2, 3)])
        n = build("rename", {"mapping": {"b": "B2"}})
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["a", "B2", "c"]

    def test_rename_into_an_existing_name_is_refused(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, 2)])
        with pytest.raises(NodeError):
            apply(con, "rename", {"mapping": {"a": "b"}}, "S")

    def test_swapping_two_names_is_allowed_or_refused_but_never_corrupts(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER")], [(1, 2)])
        try:
            got = apply(con, "rename", {"mapping": {"a": "b", "b": "a"}}, "S")
        except NodeError:
            return
        assert got == [(1, 2)]

    def test_rename_of_an_unknown_column(self, con):
        table(con, "S", [("a", "INTEGER")], [(1,)])
        with pytest.raises(NodeError, match="zz"):
            apply(con, "rename", {"mapping": {"zz": "y"}}, "S")

    def test_select_orders_and_drops(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "INTEGER"), ("c", "INTEGER")], [(1, 2, 3)])
        assert apply(con, "select", {"columns": ["c", "a"]}, "S") == [(3, 1)]

    def test_select_of_one_column_twice_is_refused_when_the_spec_is_checked(self, con):
        table(con, "S", [("a", "INTEGER")], [(1,)])
        with pytest.raises(NodeError, match="listed twice"):
            infer(build("select", {"columns": ["a", "a"]}), [schema_of(con.table("S"))])

    def test_select_nothing_is_refused(self):
        with pytest.raises(NodeError):
            build("select", {"columns": []})

    def test_a_name_with_a_double_quote(self, con):
        table(con, "S", [('say "hi"', "INTEGER")], [(1,)])
        assert apply(con, "select", {"columns": ['say "hi"']}, "S") == [(1,)]
        assert apply(con, "rename", {"mapping": {'say "hi"': "x"}}, "S") == [(1,)]


class TestCastFill:
    def test_a_value_that_does_not_fit_becomes_null_unless_strict(self, con):
        table(con, "S", [("v", "VARCHAR")], [("1",), ("x",), ("2.5",)])
        got = apply(con, "cast", {"types": {"v": "BIGINT"}}, "S")
        assert got[0] == (1,) and got[1] == (None,)

    def test_strict_fails_on_the_first_bad_value(self, con):
        table(con, "S", [("v", "VARCHAR")], [("1",), ("x",)])
        with pytest.raises(duckdb.Error):
            apply(con, "cast", {"types": {"v": "BIGINT"}, "strict": True}, "S")

    def test_the_decimal_comma_drops_dots_and_moves_the_comma(self, con):
        table(con, "S", [("v", "VARCHAR")], [("1.234,56",), ("2.000",)])
        got = apply(con, "cast", {"types": {"v": "DOUBLE"}, "decimal": ","}, "S")
        assert got == [(1234.56,), (2000.0,)]

    def test_the_decimal_option_does_not_touch_real_numbers(self, con):
        table(con, "S", [("v", "DOUBLE")], [(1234.5,)])
        assert apply(con, "cast", {"types": {"v": "DOUBLE"}, "decimal": ","}, "S") == [(1234.5,)]

    def test_text_to_date_reads_iso_only(self, con):
        table(con, "S", [("d", "VARCHAR")], [("2026-01-31",), ("31/01/2026",)])
        got = apply(con, "cast", {"types": {"d": "DATE"}}, "S")
        assert got[1] == (None,) and got[0][0].isoformat() == "2026-01-31"

    def test_a_big_value_into_a_small_type_is_null_or_an_error(self, con):
        table(con, "S", [("v", "BIGINT")], [(10**12,)])
        try:
            got = apply(con, "cast", {"types": {"v": "TINYINT"}}, "S")
        except Exception:
            return
        assert got == [(None,)]

    def test_an_unknown_type_is_a_node_error(self, con):
        table(con, "S", [("v", "VARCHAR")], [("1",)])
        with pytest.raises(NodeError):
            apply(con, "cast", {"types": {"v": "NOTATYPE"}}, "S")

    def test_fill_missing_replaces_only_null(self, con):
        table(con, "S", [("v", "INTEGER"), ("t", "VARCHAR")], [(None, None), (5, "")])
        got = apply(con, "fill_missing", {"values": {"v": 0, "t": "KG"}}, "S")
        assert got == [(0, "KG"), (5, "")]

    def test_fill_missing_with_the_wrong_kind_of_value_is_refused(self, con):
        table(con, "S", [("v", "INTEGER")], [(None,)])
        with pytest.raises(NodeError):
            apply(con, "fill_missing", {"values": {"v": "text"}}, "S")

    def test_fill_missing_with_a_decimal_into_an_integer_column(self, con):
        table(con, "S", [("v", "INTEGER")], [(None,)])
        try:
            got = apply(con, "fill_missing", {"values": {"v": 1.5}}, "S")
        except NodeError:
            return
        assert got in (
            [
                (1,),
            ],
            [(2,)],
            [(1.5,)],
        )


class TestWindow:
    @pytest.fixture
    def data(self, con):
        rows = [("a", 1, 10), ("a", 2, 20), ("b", 1, 5), ("a", 3, None), ("b", 2, 5)]
        table(con, "S", [("g", "VARCHAR"), ("o", "INTEGER"), ("v", "INTEGER")], rows)

    def test_a_running_sum_per_group(self, con, data):
        cfg = {"name": "r", "fn": "sum", "column": "v", "partition_by": ["g"], "order_by": ["o"]}
        got = apply(con, "window", cfg, "S")
        assert [r[3] for r in got] == [10, 30, 5, 30, 10]

    def test_the_rows_and_their_order_stay(self, con, data):
        cfg = {"name": "r", "fn": "row_number", "partition_by": ["g"], "order_by": ["o"]}
        got = apply(con, "window", cfg, "S")
        assert [r[:3] for r in got] == [
            ("a", 1, 10),
            ("a", 2, 20),
            ("b", 1, 5),
            ("a", 3, None),
            ("b", 2, 5),
        ]
        assert [r[3] for r in got] == [1, 2, 1, 3, 2]

    def test_no_order_by_gives_the_group_total_on_every_row(self, con, data):
        got = apply(
            con, "window", {"name": "t", "fn": "sum", "column": "v", "partition_by": ["g"]}, "S"
        )
        assert [r[3] for r in got] == [30, 30, 10, 30, 10]

    def test_rank_shares_numbers_for_equal_rows(self, con, data):
        cfg = {"name": "k", "fn": "rank", "partition_by": ["g"], "order_by": ["-v"]}
        got = apply(con, "window", cfg, "S")
        assert [r[3] for r in got if r[0] == "b"] == [1, 1]
        cfg["fn"] = "dense_rank"
        assert [r[3] for r in apply(con, "window", cfg, "S") if r[0] == "a"] == [2, 1, 3]

    def test_rank_without_order_by_is_refused(self, con, data):
        with pytest.raises(NodeError):
            apply(con, "window", {"name": "k", "fn": "rank", "partition_by": ["g"]}, "S")

    def test_lag_and_lead_are_null_at_the_edge(self, con, data):
        cfg = {"name": "p", "fn": "lag", "column": "v", "partition_by": ["g"], "order_by": ["o"]}
        got = apply(con, "window", cfg, "S")
        assert [r[3] for r in got] == [None, 10, None, 20, 5]
        cfg.update(fn="lead")
        assert [r[3] for r in apply(con, "window", cfg, "S")] == [20, None, 5, None, None]

    def test_lag_with_an_offset(self, con, data):
        cfg = {"name": "p", "fn": "lag", "column": "v", "order_by": ["g", "o"], "offset": 2}
        got = apply(con, "window", cfg, "S")
        assert [r[3] for r in got] == [None, None, 20, 10, None]

    def test_share_is_null_when_the_group_sum_is_zero(self, con):
        table(
            con, "S", [("g", "VARCHAR"), ("v", "INTEGER")], [("a", 0), ("a", 0), ("b", 2), ("b", 6)]
        )
        got = apply(
            con, "window", {"name": "s", "fn": "share", "column": "v", "partition_by": ["g"]}, "S"
        )
        assert [r[2] for r in got] == [None, None, 0.25, 0.75]

    def test_first_and_last_value_skip_empty(self, con, data):
        cfg = {
            "name": "f",
            "fn": "last_value",
            "column": "v",
            "partition_by": ["g"],
            "order_by": ["o"],
        }
        got = apply(con, "window", cfg, "S")
        assert [r[3] for r in got if r[0] == "a"] == [20, 20, 20] or [
            r[3] for r in got if r[0] == "a"
        ] == [10, 20, 20]

    def test_replacing_an_existing_column_keeps_its_place(self, con, data):
        n = build("window", {"name": "v", "fn": "row_number", "order_by": ["o"]})
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["g", "o", "v"]

    def test_a_window_over_an_empty_table(self, con):
        table(con, "S", [("g", "VARCHAR"), ("v", "INTEGER")], [])
        assert apply(con, "window", {"name": "r", "fn": "row_number", "order_by": ["v"]}, "S") == []

    def test_sum_of_text_is_refused(self, con, data):
        with pytest.raises(NodeError):
            apply(con, "window", {"name": "r", "fn": "sum", "column": "g", "order_by": ["o"]}, "S")


class TestPivot:
    @pytest.fixture
    def long(self, con):
        rows = [
            ("a", "jan", 1),
            ("a", "feb", 2),
            ("b", "jan", 3),
            ("a", "jan", 10),
            ("c", "mar", 9),
            ("b", None, 7),
        ]
        table(con, "S", [("g", "VARCHAR"), ("m", "VARCHAR"), ("v", "INTEGER")], rows)

    def test_duplicates_are_aggregated_with_sum(self, con, long):
        got = apply(
            con, "pivot", {"by": ["g"], "column": "m", "value": "v", "values": ["jan", "feb"]}, "S"
        )
        assert {r[0]: r[1:] for r in got} == {"a": (11, 2), "b": (3, None), "c": (None, None)}

    def test_groups_come_out_in_order_of_first_appearance(self, con, long):
        got = apply(
            con, "pivot", {"by": ["g"], "column": "m", "value": "v", "values": ["jan"]}, "S"
        )
        assert [r[0] for r in got] == ["a", "b", "c"]

    def test_a_row_whose_column_value_is_not_listed_is_left_out(self, con, long):
        got = apply(
            con, "pivot", {"by": ["g"], "column": "m", "value": "v", "values": ["mar"]}, "S"
        )
        assert {r[0]: r[1] for r in got}["c"] == 9

    def test_prefix_names_the_new_columns(self, con, long):
        n = build(
            "pivot", {"by": ["g"], "column": "m", "value": "v", "values": ["jan"], "prefix": "m_"}
        )
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["g", "m_jan"]

    @pytest.mark.parametrize("fn", ["sum", "avg", "min", "max", "count", "first", "last"])
    def test_every_function_is_known_and_runs(self, con, long, fn):
        cfg = {"by": ["g"], "column": "m", "value": "v", "values": ["jan"], "fn": fn}
        assert len(apply(con, "pivot", cfg, "S")) == 3

    def test_a_listed_value_twice_is_refused(self, con, long):
        with pytest.raises(NodeError):
            apply(
                con,
                "pivot",
                {"by": ["g"], "column": "m", "value": "v", "values": ["jan", "jan"]},
                "S",
            )

    def test_a_new_column_that_collides_with_a_by_column_is_refused(self, con, long):
        with pytest.raises(NodeError):
            apply(con, "pivot", {"by": ["g"], "column": "m", "value": "v", "values": ["g"]}, "S")

    def test_values_that_look_the_same_but_differ_in_case(self, con):
        table(con, "S", [("m", "VARCHAR"), ("v", "INTEGER")], [("Jan", 1), ("jan", 2)])
        try:
            got = apply(con, "pivot", {"column": "m", "value": "v", "values": ["Jan", "jan"]}, "S")
        except NodeError:
            return
        assert got == [(1, 2)]

    def test_numeric_values(self, con):
        table(con, "S", [("y", "INTEGER"), ("v", "INTEGER")], [(2025, 1), (2026, 2)])
        got = apply(con, "pivot", {"column": "y", "value": "v", "values": [2025, 2026]}, "S")
        assert got == [(1, 2)]
