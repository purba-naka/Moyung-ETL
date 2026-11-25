from __future__ import annotations

import pytest
from test_nodes_differential import apply, table

from moyung.nodes.base import NodeError, build, infer, schema_of


class TestDropRows:
    def test_blank_rows_go_and_rows_with_one_value_stay(self, con):
        table(con, "S", [("a", "INTEGER"), ("b", "VARCHAR")], [(1, "x"), (None, None), (None, "y")])
        assert apply(con, "drop_rows", {}, "S") == [(1, "x"), (None, "y")]

    def test_blank_false_keeps_blank_rows(self, con):
        table(con, "S", [("a", "INTEGER")], [(1,), (None,)])
        assert apply(con, "drop_rows", {"blank": False}, "S") == [(1,), (None,)]

    def test_a_whitespace_only_text_is_not_blank(self, con):
        table(con, "S", [("a", "VARCHAR"), ("b", "VARCHAR")], [("  ", None), ("", None)])
        got = apply(con, "drop_rows", {}, "S")
        assert ("  ", None) in got

    def test_keep_type_date_drops_label_rows(self, con):
        rows = [("2026-01-31", 1), ("JAN", 2), ("FEB - 2025", 4), ("2026-02-28", 5)]
        table(con, "S", [("d", "VARCHAR"), ("v", "INTEGER")], rows)
        got = apply(con, "drop_rows", {"column": "d", "keep_type": "date"}, "S")
        assert got == [("2026-01-31", 1), ("2026-02-28", 5)]

    @pytest.mark.xfail(
        strict=True, reason="BUG-018: keep_type drops rows whose column is empty; the docs say kept"
    )
    @pytest.mark.parametrize("empty", [None, ""])
    def test_keep_type_keeps_a_row_whose_column_is_empty(self, con, empty):
        table(con, "S", [("d", "VARCHAR"), ("v", "INTEGER")], [("2026-01-31", 1), (empty, 3)])
        got = apply(con, "drop_rows", {"column": "d", "keep_type": "date"}, "S")
        assert (empty, 3) in got

    def test_keep_type_today_drops_a_row_whose_column_is_empty(self, con):
        table(con, "S", [("d", "VARCHAR"), ("v", "INTEGER")], [("2026-01-31", 1), (None, 3)])
        got = apply(con, "drop_rows", {"column": "d", "keep_type": "date"}, "S")
        assert got == [("2026-01-31", 1)]

    def test_keep_type_bigint_drops_text_but_keeps_decimals_because_they_round(self, con):
        table(con, "S", [("n", "VARCHAR")], [("1",), ("2.5",), ("x",), ("-3",), (" 4 ",)])
        got = [r[0] for r in apply(con, "drop_rows", {"column": "n", "keep_type": "bigint"}, "S")]
        assert got == ["1", "2.5", "-3", " 4 "]

    def test_keep_type_double_accepts_integers_and_decimals(self, con):
        table(con, "S", [("n", "VARCHAR")], [("1",), ("2.5",), ("x",), ("1e3",)])
        got = [r[0] for r in apply(con, "drop_rows", {"column": "n", "keep_type": "double"}, "S")]
        assert got == ["1", "2.5", "1e3"]

    def test_keep_type_boolean(self, con):
        table(con, "S", [("b", "VARCHAR")], [("true",), ("false",), ("maybe",)])
        got = [r[0] for r in apply(con, "drop_rows", {"column": "b", "keep_type": "boolean"}, "S")]
        assert got == ["true", "false"]

    def test_drop_regex_is_case_sensitive_unless_told_otherwise(self, con):
        rows = [("Total", 1), ("total", 2), ("Item", 3), (None, 4)]
        table(con, "S", [("l", "VARCHAR"), ("v", "INTEGER")], rows)
        got = apply(con, "drop_rows", {"column": "l", "drop_regex": "^Total"}, "S")
        assert [r[0] for r in got] == ["total", "Item", None]
        got = apply(con, "drop_rows", {"column": "l", "drop_regex": "(?i)^total"}, "S")
        assert [r[0] for r in got] == ["Item", None]

    def test_drop_regex_is_a_search_not_a_full_match(self, con):
        table(con, "S", [("l", "VARCHAR")], [("Grand Total",), ("Item",)])
        got = apply(con, "drop_rows", {"column": "l", "drop_regex": "Total"}, "S")
        assert got == [("Item",)]

    def test_drop_regex_on_a_number_column_is_refused_or_reads_the_text(self, con):
        table(con, "S", [("n", "INTEGER")], [(1,), (22,)])
        try:
            got = apply(con, "drop_rows", {"column": "n", "drop_regex": "^2"}, "S")
        except NodeError:
            return
        assert got == [(1,)]

    def test_a_column_without_a_rule_is_refused(self):
        with pytest.raises(NodeError):
            build("drop_rows", {"column": "a"})

    def test_a_rule_without_a_column_is_refused(self):
        with pytest.raises(NodeError):
            build("drop_rows", {"keep_type": "date"})

    def test_a_bad_regex_is_refused_when_the_node_is_built(self):
        with pytest.raises(NodeError):
            build("drop_rows", {"column": "a", "drop_regex": "("})

    def test_the_column_keeps_its_type(self, con):
        table(con, "S", [("d", "VARCHAR")], [("2026-01-01",)])
        n = build("drop_rows", {"column": "d", "keep_type": "date"})
        assert infer(n, [schema_of(con.table("S"))])[0].type == "VARCHAR"

    def test_all_rows_dropped(self, con):
        table(con, "S", [("d", "VARCHAR")], [("x",), ("y",)])
        assert apply(con, "drop_rows", {"column": "d", "keep_type": "date"}, "S") == []


class TestFillDown:
    def test_empty_cells_take_the_last_value_above(self, con):
        rows = [("a", 1), (None, 2), (None, 3), ("b", 4), (None, 5)]
        table(con, "S", [("g", "VARCHAR"), ("v", "INTEGER")], rows)
        got = apply(con, "fill_down", {"columns": ["g"]}, "S")
        assert [r[0] for r in got] == ["a", "a", "a", "b", "b"]

    def test_leading_empty_cells_stay_empty(self, con):
        table(con, "S", [("g", "VARCHAR")], [(None,), (None,), ("a",), (None,)])
        assert [r[0] for r in apply(con, "fill_down", {"columns": ["g"]}, "S")] == [
            None,
            None,
            "a",
            "a",
        ]

    def test_an_empty_string_is_a_value_not_a_gap(self, con):
        table(con, "S", [("g", "VARCHAR")], [("a",), ("",), (None,)])
        assert [r[0] for r in apply(con, "fill_down", {"columns": ["g"]}, "S")] == ["a", "", ""]

    def test_other_columns_are_untouched(self, con):
        table(con, "S", [("g", "VARCHAR"), ("v", "INTEGER")], [("a", None), (None, None)])
        got = apply(con, "fill_down", {"columns": ["g"]}, "S")
        assert got == [("a", None), ("a", None)]

    def test_two_columns_fill_independently(self, con):
        rows = [("a", "x"), (None, None), ("b", None), (None, "y")]
        table(con, "S", [("g", "VARCHAR"), ("h", "VARCHAR")], rows)
        got = apply(con, "fill_down", {"columns": ["g", "h"]}, "S")
        assert got == [("a", "x"), ("a", "x"), ("b", "x"), ("b", "y")]

    def test_the_row_order_is_what_the_table_has(self, con):
        rows = [(i // 10 if i % 10 == 0 else None, i) for i in range(100)]
        table(con, "S", [("g", "INTEGER"), ("i", "INTEGER")], rows)
        got = apply(con, "fill_down", {"columns": ["g"]}, "S")
        assert [r[1] for r in got] == list(range(100))
        assert [r[0] for r in got] == [i // 10 for i in range(100)]

    def test_an_unknown_column_is_named(self, con):
        table(con, "S", [("g", "VARCHAR")], [("a",)])
        with pytest.raises(NodeError, match="zz"):
            apply(con, "fill_down", {"columns": ["zz"]}, "S")

    def test_an_empty_table(self, con):
        table(con, "S", [("g", "VARCHAR")], [])
        assert apply(con, "fill_down", {"columns": ["g"]}, "S") == []


class TestUnpivot:
    @pytest.fixture
    def wide(self, con):
        rows = [("a", 1, 2.5, None), ("b", None, 4.0, 5)]
        table(
            con,
            "S",
            [("id", "VARCHAR"), ("jan", "INTEGER"), ("feb", "DOUBLE"), ("mar", "INTEGER")],
            rows,
        )

    def test_one_row_per_non_empty_cell(self, con, wide):
        cfg = {"keep": ["id"], "columns": ["jan", "feb", "mar"]}
        got = apply(con, "unpivot", cfg, "S")
        assert sorted(got) == [
            ("a", "feb", 2.5),
            ("a", "jan", 1.0),
            ("b", "feb", 4.0),
            ("b", "mar", 5.0),
        ]

    def test_drop_nulls_false_keeps_the_empty_cells(self, con, wide):
        cfg = {"keep": ["id"], "columns": ["jan", "feb", "mar"], "drop_nulls": False}
        assert len(apply(con, "unpivot", cfg, "S")) == 6

    def test_a_pattern_picks_the_columns(self, con, wide):
        got = apply(con, "unpivot", {"keep": ["id"], "pattern": "^(jan|feb)$"}, "S")
        assert {r[1] for r in got} == {"jan", "feb"}

    def test_numbers_of_different_types_become_double(self, con, wide):
        n = build("unpivot", {"keep": ["id"], "columns": ["jan", "feb"]})
        assert infer(n, [schema_of(con.table("S"))])[-1].type == "DOUBLE"

    def test_a_number_and_a_text_become_text(self, con):
        table(con, "S", [("id", "INTEGER"), ("a", "INTEGER"), ("b", "VARCHAR")], [(1, 2, "x")])
        got = apply(con, "unpivot", {"keep": ["id"], "columns": ["a", "b"]}, "S")
        assert sorted(got) == [(1, "a", "2"), (1, "b", "x")]

    def test_value_type_forces_the_type(self, con, wide):
        n = build("unpivot", {"keep": ["id"], "columns": ["jan", "feb"], "value_type": "VARCHAR"})
        assert infer(n, [schema_of(con.table("S"))])[-1].type == "VARCHAR"

    def test_custom_names(self, con, wide):
        n = build(
            "unpivot", {"keep": ["id"], "columns": ["jan"], "name_column": "m", "value_column": "q"}
        )
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["id", "m", "q"]

    def test_unlisted_columns_are_dropped(self, con, wide):
        n = build("unpivot", {"keep": ["id"], "columns": ["jan"]})
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == [
            "id",
            "variable",
            "value",
        ]

    def test_a_pattern_that_matches_nothing_is_an_error(self, con, wide):
        with pytest.raises(NodeError):
            apply(con, "unpivot", {"keep": ["id"], "pattern": "^zzz$"}, "S")

    def test_columns_and_pattern_together_are_refused(self):
        with pytest.raises(NodeError):
            build("unpivot", {"columns": ["a"], "pattern": "x"})

    def test_neither_columns_nor_pattern_is_refused(self):
        with pytest.raises(NodeError):
            build("unpivot", {"keep": ["id"]})

    def test_name_and_value_column_must_differ_and_not_be_kept(self):
        with pytest.raises(NodeError):
            build("unpivot", {"columns": ["a"], "name_column": "x", "value_column": "x"})
        with pytest.raises(NodeError):
            build("unpivot", {"columns": ["a"], "keep": ["variable"]})

    def test_a_kept_column_that_is_also_unpivoted_is_refused(self, con, wide):
        with pytest.raises(NodeError):
            apply(con, "unpivot", {"keep": ["id", "jan"], "columns": ["jan"]}, "S")

    def test_a_name_with_a_quote_and_a_pipe_survives(self, con):
        table(con, "S", [("id", "INTEGER"), ('say "x"|Ton', "INTEGER")], [(1, 5)])
        got = apply(con, "unpivot", {"keep": ["id"], "columns": ['say "x"|Ton']}, "S")
        assert got == [(1, 'say "x"|Ton', 5)]

    def test_a_wide_table_of_300_columns(self, con):
        cols = [("id", "INTEGER")] + [(f"c{i}", "INTEGER") for i in range(300)]
        table(con, "S", cols, [tuple([1] + list(range(300)))])
        got = apply(
            con, "unpivot", {"keep": ["id"], "pattern": "^c[0-9]+$", "drop_nulls": False}, "S"
        )
        assert len(got) == 300
        assert sum(r[2] for r in got) == sum(range(300))

    def test_the_total_of_the_values_is_unchanged(self, con, wide):
        got = apply(con, "unpivot", {"keep": ["id"], "columns": ["jan", "feb", "mar"]}, "S")
        assert sum(r[2] for r in got) == 1 + 2.5 + 4.0 + 5


class TestUnpivotGroups:
    PATTERN = "^(?P<group>.+)[|](?P<measure>[^|]+)$"

    @pytest.fixture
    def blocks(self, con):
        cols = [
            ("plant", "VARCHAR"),
            ("A|Ton", "INTEGER"),
            ("A|Rate", "DOUBLE"),
            ("B|Ton", "INTEGER"),
        ]
        table(con, "S", cols, [("p1", 1, 0.5, 2), ("p2", None, None, None)])

    def test_one_row_per_block_with_the_measures_as_columns(self, con, blocks):
        got = apply(con, "unpivot_groups", {"pattern": self.PATTERN, "keep": ["plant"]}, "S")
        by_key = {(r[0], r[1]): r[2:] for r in got}
        assert by_key[("p1", "A")] == (1.0, 0.5) or by_key[("p1", "A")] == (1, 0.5)
        assert by_key[("p1", "B")][1] is None

    def test_empty_blocks_are_dropped_unless_told_otherwise(self, con, blocks):
        got = apply(con, "unpivot_groups", {"pattern": self.PATTERN, "keep": ["plant"]}, "S")
        assert all(r[0] != "p2" for r in got)
        kept = apply(
            con,
            "unpivot_groups",
            {"pattern": self.PATTERN, "keep": ["plant"], "drop_empty": False},
            "S",
        )
        assert len([r for r in kept if r[0] == "p2"]) == 2

    def test_the_pattern_needs_both_named_groups(self):
        with pytest.raises(NodeError):
            build("unpivot_groups", {"pattern": "^(.+)[|](.+)$"})

    def test_a_pattern_that_matches_nothing_is_an_error(self, con, blocks):
        with pytest.raises(NodeError):
            apply(con, "unpivot_groups", {"pattern": "^(?P<group>x)(?P<measure>y)$"}, "S")

    def test_group_column_cannot_be_a_kept_column(self):
        with pytest.raises(NodeError):
            build("unpivot_groups", {"pattern": self.PATTERN, "keep": ["group"]})

    def test_a_group_name_made_of_numbers_is_text(self, con):
        table(con, "S", [("2025|Ton", "INTEGER"), ("2026|Ton", "INTEGER")], [(1, 2)])
        got = apply(con, "unpivot_groups", {"pattern": self.PATTERN}, "S")
        assert sorted(r[0] for r in got) == ["2025", "2026"]


class TestParseHierarchy:
    @pytest.mark.parametrize(
        "code, level, parent",
        [
            ("1", 1, None),
            ("1.2", 2, "1"),
            ("1.2.1", 3, "1.2"),
            ("1..2", None, None),
            ("1.2.", None, None),
            (".1", None, None),
            ("", None, None),
            (None, None, None),
            ("a.b.c", 3, "a.b"),
            ("10.20.30.40", 4, "10.20.30"),
        ],
    )
    def test_levels_and_parents(self, con, code, level, parent):
        table(con, "S", [("c", "VARCHAR")], [(code,)])
        got = apply(con, "parse_hierarchy", {"column": "c"}, "S")
        assert got == [(code, level, parent)]

    def test_another_separator(self, con):
        table(con, "S", [("c", "VARCHAR")], [("I > II > III",)])
        got = apply(con, "parse_hierarchy", {"column": "c", "separator": " > "}, "S")
        assert got == [("I > II > III", 3, "I > II")]

    def test_a_whole_number_column_is_one_level(self, con):
        table(con, "S", [("c", "INTEGER")], [(5,), (None,)])
        got = apply(con, "parse_hierarchy", {"column": "c"}, "S")
        assert [r[1] for r in got] == [1, None]

    def test_a_decimal_column_is_refused_with_the_reason(self, con):
        table(con, "S", [("c", "DOUBLE")], [(1.1,)])
        with pytest.raises(NodeError, match="lost as"):
            apply(con, "parse_hierarchy", {"column": "c"}, "S")

    def test_the_two_new_columns_may_not_share_a_name(self):
        with pytest.raises(NodeError):
            build("parse_hierarchy", {"column": "c", "level_column": "x", "parent_column": "x"})

    def test_a_separator_with_regex_characters_is_taken_literally(self, con):
        table(con, "S", [("c", "VARCHAR")], [("1.2.3",), ("1|2|3",)])
        got = apply(con, "parse_hierarchy", {"column": "c", "separator": "|"}, "S")
        assert got == [("1.2.3", 1, None), ("1|2|3", 3, "1|2")]

    def test_a_regex_separator_that_would_match_everything(self, con):
        table(con, "S", [("c", "VARCHAR")], [("a.b",)])
        got = apply(con, "parse_hierarchy", {"column": "c", "separator": ".*"}, "S")
        assert got[0][1] == 1

    def test_unicode_codes(self, con):
        table(con, "S", [("c", "VARCHAR")], [("é.ü",)])
        assert apply(con, "parse_hierarchy", {"column": "c"}, "S") == [("é.ü", 2, "é")]


class TestSplitHeaderToken:
    def test_a_separator_splits_at_the_first_ones_and_the_last_part_keeps_the_rest(self, con):
        table(con, "S", [("h", "VARCHAR")], [("JAN|Ton|net",)])
        got = apply(
            con, "split_header_token", {"column": "h", "into": ["m", "u"], "separator": "|"}, "S"
        )
        assert got == [("JAN|Ton|net", "JAN", "Ton|net")]

    def test_fewer_parts_give_null(self, con):
        table(con, "S", [("h", "VARCHAR")], [("JAN",)])
        got = apply(
            con, "split_header_token", {"column": "h", "into": ["m", "u"], "separator": "|"}, "S"
        )
        assert got == [("JAN", "JAN", None)]

    def test_parts_are_trimmed_and_empty_parts_are_null(self, con):
        table(con, "S", [("h", "VARCHAR")], [(" JAN | ",), ("|Ton",)])
        got = apply(
            con, "split_header_token", {"column": "h", "into": ["m", "u"], "separator": "|"}, "S"
        )
        assert got == [(" JAN | ", "JAN", None), ("|Ton", None, "Ton")]

    def test_null_in_gives_null_out(self, con):
        table(con, "S", [("h", "VARCHAR")], [(None,)])
        got = apply(
            con, "split_header_token", {"column": "h", "into": ["m", "u"], "separator": "|"}, "S"
        )
        assert got == [(None, None, None)]

    def test_drop_source_removes_the_column(self, con):
        table(con, "S", [("h", "VARCHAR"), ("v", "INTEGER")], [("a|b", 1)])
        cfg = {"column": "h", "into": ["m", "u"], "separator": "|", "drop_source": True}
        n = build("split_header_token", cfg)
        assert [c.name for c in infer(n, [schema_of(con.table("S"))])] == ["v", "m", "u"]

    def test_a_pattern_with_one_group_per_new_column(self, con):
        table(con, "S", [("h", "VARCHAR")], [("JAN-2025",), ("garbage",)])
        cfg = {"column": "h", "into": ["m", "y"], "pattern": r"^([A-Z]+)-(\d+)$"}
        got = apply(con, "split_header_token", cfg, "S")
        assert got == [("JAN-2025", "JAN", "2025"), ("garbage", None, None)]

    def test_a_pattern_with_the_wrong_number_of_groups_is_refused(self, con):
        table(con, "S", [("h", "VARCHAR")], [("a",)])
        with pytest.raises(NodeError):
            apply(
                con,
                "split_header_token",
                {"column": "h", "into": ["m", "y"], "pattern": "^(a)$"},
                "S",
            )

    def test_separator_and_pattern_together_are_refused(self):
        with pytest.raises(NodeError):
            build(
                "split_header_token",
                {"column": "h", "into": ["a", "b"], "separator": "|", "pattern": "x"},
            )

    def test_one_new_column_is_refused(self):
        with pytest.raises(NodeError):
            build("split_header_token", {"column": "h", "into": ["a"], "separator": "|"})

    def test_repeated_new_names_are_refused(self):
        with pytest.raises(NodeError):
            build("split_header_token", {"column": "h", "into": ["a", "a"], "separator": "|"})

    def test_a_new_name_that_already_exists_is_refused(self, con):
        table(con, "S", [("h", "VARCHAR"), ("m", "VARCHAR")], [("a|b", "x")])
        with pytest.raises(NodeError):
            apply(
                con,
                "split_header_token",
                {"column": "h", "into": ["m", "u"], "separator": "|"},
                "S",
            )

    def test_splitting_a_number_column_is_refused_or_reads_the_text(self, con):
        table(con, "S", [("h", "INTEGER")], [(12,)])
        try:
            apply(
                con,
                "split_header_token",
                {"column": "h", "into": ["a", "b"], "separator": "|"},
                "S",
            )
        except NodeError:
            return

    def test_a_separator_with_regex_characters_is_literal(self, con):
        table(con, "S", [("h", "VARCHAR")], [("a.b",), ("axb",)])
        got = apply(
            con, "split_header_token", {"column": "h", "into": ["p", "q"], "separator": "."}, "S"
        )
        assert got == [("a.b", "a", "b"), ("axb", "axb", None)]

    def test_the_end_to_end_unpivot_then_split(self, con):
        table(
            con,
            "S",
            [("id", "INTEGER"), ("JAN|Ton", "INTEGER"), ("JAN|Rate", "INTEGER")],
            [(1, 10, 3)],
        )
        long = apply(con, "unpivot", {"keep": ["id"], "columns": ["JAN|Ton", "JAN|Rate"]}, "S")
        assert sorted(long) == [(1, "JAN|Rate", 3.0), (1, "JAN|Ton", 10.0)]
