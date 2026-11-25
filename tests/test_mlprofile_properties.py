from __future__ import annotations

import random

import pytest
from test_nodes_differential import table

from moyung.mlprofile import profile

TYPES = ["INTEGER", "VARCHAR", "DOUBLE", "BOOLEAN", "DATE"]


def make_table(con, seed, rows=None, width=None):
    rnd = random.Random(seed)
    n = rnd.choice([0, 1, 5, 30, 100]) if rows is None else rows
    w = rnd.randrange(1, 9) if width is None else width
    cols = [(f"c{i}", rnd.choice(TYPES)) for i in range(w)]

    def value(kind):
        if rnd.random() < 0.2:
            return None
        return {
            "INTEGER": lambda: rnd.randrange(5),
            "VARCHAR": lambda: rnd.choice(["a", "b", "c|d", "x\ny", "é"]),
            "DOUBLE": lambda: rnd.choice([0.5, 1.5, 2.5]),
            "BOOLEAN": lambda: rnd.random() < 0.5,
            "DATE": lambda: f"2026-01-{rnd.randrange(1, 28):02d}",
        }[kind]()

    data = [tuple(value(k) for _, k in cols) for _ in range(n)]
    table(con, "t", cols, data)
    return cols, data


def column_lines(lines):
    start = next(i for i, line in enumerate(lines) if line.startswith("column|type"))
    out = []
    for line in lines[start + 1 :]:
        if line.startswith(("suggested", "target")):
            break
        out.append(line)
    return out


@pytest.mark.parametrize("seed", range(30))
class TestAgainstPython:
    def test_the_counts_of_empty_and_distinct_values_are_right(self, con, seed):
        cols, data = make_table(con, seed)
        lines, _ = profile(con, "t", "", 0, 100)
        for i, line in enumerate(column_lines(lines)):
            name, kind, empty, distinct, _notes = line.split("|", 4)
            column = [r[i] for r in data]
            assert name == cols[i][0] and kind == cols[i][1]
            assert int(empty) == sum(v is None for v in column)
            assert int(distinct) == len({v for v in column if v is not None})

    def test_the_header_line_counts_rows_and_columns(self, con, seed):
        cols, data = make_table(con, seed)
        lines, _ = profile(con, "t", "", 0, 100)
        assert lines[0] == f"{len(data)} rows|{len(cols)} columns|columns 1-{len(cols)}"

    def test_pages_cover_every_column_once(self, con, seed):
        cols, _ = make_table(con, seed, width=7)
        seen, offset = [], 0
        while offset is not None:
            lines, offset = profile(con, "t", "", offset, 3)
            seen += [line.split("|")[0] for line in column_lines(lines)]
        assert seen == [c for c, _ in cols]


class TestNotes:
    def test_a_constant_column_is_excluded(self, con):
        table(con, "t", [("a", "INTEGER")], [(1,)] * 30)
        lines, _ = profile(con, "t", "", 0, 10)
        assert "constant" in lines[2] and "exclude: a" in " ".join(lines)

    def test_a_column_empty_except_for_one_value_warns_about_leaking(self, con):
        table(con, "t", [("a", "INTEGER")], [(1,)] + [(None,)] * 29)
        lines, _ = profile(con, "t", "", 0, 10)
        assert "one value, empty otherwise" in lines[2]

    def test_a_mostly_empty_column_is_excluded_with_its_share(self, con):
        table(con, "t", [("a", "INTEGER")], [(1,), (2,)] + [(None,)] * 28)
        lines, _ = profile(con, "t", "", 0, 10)
        assert "93% empty" in lines[2]

    def test_a_key_like_column_is_suggested_as_an_id(self, con):
        table(con, "t", [("k", "VARCHAR")], [(f"u{i}",) for i in range(50)])
        lines, _ = profile(con, "t", "", 0, 10)
        assert "ids: k" in " ".join(lines)

    def test_a_decimal_column_is_never_an_id_however_distinct(self, con):
        table(con, "t", [("x", "DOUBLE")], [(i + 0.5,) for i in range(50)])
        lines, _ = profile(con, "t", "", 0, 10)
        assert "ids:" not in " ".join(lines)

    def test_a_date_column_gets_the_split_by_hint(self, con):
        table(con, "t", [("d", "DATE")], [(f"2026-01-{i % 28 + 1:02d}",) for i in range(30)])
        lines, _ = profile(con, "t", "", 0, 10)
        assert "split `by`" in lines[2]

    def test_below_twenty_rows_nothing_is_called_an_id(self, con):
        table(con, "t", [("k", "VARCHAR")], [(f"u{i}",) for i in range(10)])
        lines, _ = profile(con, "t", "", 0, 10)
        assert "ids:" not in " ".join(lines)

    def test_a_pipe_or_newline_in_a_column_name_cannot_break_the_table(self, con):
        table(con, "t", [("a|b\nc", "INTEGER")], [(1,), (2,)])
        lines, _ = profile(con, "t", "", 0, 10)
        assert lines[2].split("|")[0] == "a/b c" and len(lines[2].split("|")) == 5


class TestTarget:
    @pytest.fixture
    def data(self, con):
        table(
            con,
            "t",
            [("y", "VARCHAR"), ("v", "DOUBLE")],
            [("a", 1.5)] * 120 + [("b", 2.5)] * 79 + [("c", 3.5)],
        )

    def test_classes_are_listed_most_common_first(self, con, data):
        lines, _ = profile(con, "t", "y", 0, 10)
        assert "classes a=120, b=79, c=1" in lines[-1]

    def test_a_rare_class_is_named(self, con, data):
        lines, _ = profile(con, "t", "y", 0, 10)
        assert "rare class(es) c" in lines[-1]

    def test_the_target_is_found_ignoring_letter_case(self, con, data):
        assert profile(con, "t", "Y", 0, 10)[0][-1].startswith("target 'y'")

    def test_the_target_is_left_out_of_the_suggestions(self, con):
        table(con, "t", [("y", "INTEGER"), ("z", "INTEGER")], [(1, 1)] * 30)
        lines, _ = profile(con, "t", "y", 0, 10)
        suggestion = next(line for line in lines if line.startswith("suggested"))
        assert "z" in suggestion and "y" not in suggestion.split(":", 1)[1].replace("exclude", "")

    def test_an_unknown_target_raises_value_error_with_the_columns(self, con, data):
        with pytest.raises(ValueError, match="no column 'zz'; columns: y, v"):
            profile(con, "t", "zz", 0, 10)

    def test_a_decimal_target_is_a_regression(self, con):
        table(con, "t", [("y", "DOUBLE")], [(i * 0.1,) for i in range(30)])
        assert "regression" in profile(con, "t", "y", 0, 10)[0][-1]

    def test_a_target_with_many_values_is_not_a_few_classes(self, con):
        table(con, "t", [("y", "INTEGER")], [(i,) for i in range(100)])
        assert "not a few classes" in profile(con, "t", "y", 0, 10)[0][-1]

    def test_a_single_class_says_nothing_to_learn(self, con):
        table(con, "t", [("y", "INTEGER")], [(1,)] * 30)
        assert "nothing to learn" in profile(con, "t", "y", 0, 10)[0][-1]

    def test_empty_target_values_are_counted(self, con):
        table(con, "t", [("y", "INTEGER")], [(1,), (None,), (2,), (None,)])
        assert "2 empty value(s)" in profile(con, "t", "y", 0, 10)[0][-1]

    def test_an_all_empty_target(self, con):
        table(con, "t", [("y", "INTEGER")], [(None,)] * 5)
        assert "no values" in profile(con, "t", "y", 0, 10)[0][-1]

    def test_an_empty_table(self, con):
        table(con, "t", [("y", "INTEGER")], [])
        lines, nxt = profile(con, "t", "y", 0, 10)
        assert lines[0].startswith("0 rows") and nxt is None and "no values" in lines[-1]

    def test_an_offset_past_the_end_gives_a_header_and_no_columns(self, con, data):
        lines, nxt = profile(con, "t", "", 50, 10)
        assert column_lines(lines) == [] and nxt is None
