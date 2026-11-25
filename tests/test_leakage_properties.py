from __future__ import annotations

import random

import pytest
from test_nodes_differential import table

from moyung.ml import MlSpec, split
from moyung.nodes.base import schema_of

LEAK_WORDS = (
    "equal or fully determine",
    "are empty almost fixes",
    "different value in almost every row",
    "fall after",
    "share their ids",
    "identical in every column",
)
COLUMNS = [
    ("id", "BIGINT"),
    ("cust", "VARCHAR"),
    ("f", "DOUBLE"),
    ("g", "VARCHAR"),
    ("t", "VARCHAR"),
    ("day", "DATE"),
]


def rows_for(seed, n=200):
    rnd = random.Random(seed)
    return [
        (
            i,
            f"c{rnd.randrange(n)}",
            rnd.random(),
            rnd.choice(["a", "b", "c"]),
            rnd.choice(["y", "n"]),
            f"2026-01-{1 + i % 28:02d}",
        )
        for i in range(n)
    ]


def run_split(con, rows, features=("f", "g"), ids=("id",), strategy="random", by=None, test=0.2):
    table(con, "d", COLUMNS, [(*r[:5], r[5]) for r in rows])
    cfg = {
        "name": "m",
        "input": "d",
        "target": "t",
        "features": list(features),
        "ids": list(ids),
        "exclude": [c for c, _ in COLUMNS if c not in {*features, *ids, "t"}],
        "split": {
            "strategy": strategy,
            "test_size": test,
            "validation_size": 0.1,
            "seed": 4,
            **({"by": by} if by else {}),
        },
    }
    result, problems = split(con, MlSpec(**cfg), schema_of(con.table("d")))
    assert result is not None, problems
    return result.warnings


def leak_warnings(warnings):
    return [w for w in warnings if any(word in w for word in LEAK_WORDS)]


@pytest.mark.parametrize("seed", range(10))
class TestCleanData:
    def test_random_independent_data_raises_no_leak_warning(self, con, seed):
        assert leak_warnings(run_split(con, rows_for(seed))) == []

    def test_a_stratified_split_of_clean_data_is_quiet(self, con, seed):
        assert leak_warnings(run_split(con, rows_for(seed), strategy="stratified")) == []


@pytest.mark.parametrize("seed", range(6))
class TestLeaks:
    def test_a_feature_that_copies_the_target_is_named(self, con, seed):
        rows = [(i, c, f, t, t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(con, rows, features=("f", "g"))
        assert any("'g'" in w and "equal or fully determine" in w for w in warnings)

    def test_a_feature_that_is_a_recoding_of_the_target_is_named(self, con, seed):
        rows = [(i, c, f, "P" if t == "y" else "Q", t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(con, rows, features=("f", "g"))
        assert any("'g'" in w and "determine" in w for w in warnings)

    def test_a_feature_empty_exactly_when_the_target_is_positive_is_named(self, con, seed):
        rows = [(i, c, f if t == "y" else None, g, t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(con, rows, features=("f", "g"))
        assert any("'f'" in w and "empty" in w for w in warnings)

    def test_a_feature_unique_per_row_is_called_an_id(self, con, seed):
        rows = [(i, f"u{i}", f, g, t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(con, rows, features=("f", "cust"))
        assert any("'cust'" in w and "almost every row" in w for w in warnings)

    def test_a_customer_seen_in_two_partitions_is_reported_by_the_id(self, con, seed):
        rows = [(i, f"c{i % 15}", f, g, t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(con, rows, features=("f", "g"), ids=("cust",))
        assert any("share their ids" in w for w in warnings)

    def test_a_group_split_on_that_id_removes_the_id_warning(self, con, seed):
        rows = [(i, f"c{i % 15}", f, g, t, d) for i, c, f, g, t, d in rows_for(seed)]
        warnings = run_split(
            con, rows, features=("f", "g", "cust"), ids=(), strategy="group", by="cust"
        )
        assert not any("share their ids" in w for w in warnings)

    def test_identical_rows_in_two_partitions_are_reported(self, con, seed):
        base = rows_for(seed, n=40)
        rows = [(i, "c", 1.0, "a", "y" if i % 2 else "n", "2026-01-01") for i in range(len(base))]
        warnings = run_split(con, rows, features=("f", "g"), ids=("cust",))
        assert any("identical in every column" in w or "share their ids" in w for w in warnings)

    def test_a_time_split_by_a_number_works_with_a_date_feature(self, con, seed):
        table(con, "d", COLUMNS, rows_for(seed))
        cfg = {
            "name": "m",
            "input": "d",
            "target": "t",
            "features": ["f", "day"],
            "ids": ["id"],
            "exclude": ["cust", "g"],
            "split": {"strategy": "time", "by": "id", "test_size": 0.2, "seed": 1},
        }
        result, problems = split(con, MlSpec(**cfg), schema_of(con.table("d")))
        assert result is not None, problems

    def test_a_time_split_by_a_date_flags_a_date_feature_that_comes_after_it(self, con, seed):
        table(con, "d", [*COLUMNS, ("sent", "DATE")], [(*r, "2026-12-31") for r in rows_for(seed)])
        cfg = {
            "name": "m",
            "input": "d",
            "target": "t",
            "features": ["f", "sent"],
            "ids": ["id"],
            "exclude": ["cust", "g", "day"],
            "split": {"strategy": "time", "by": "day", "test_size": 0.2, "seed": 1},
        }
        result, problems = split(con, MlSpec(**cfg), schema_of(con.table("d")))
        assert result is not None and any("fall after" in w for w in result.warnings)


class TestSmallAndOddInputs:
    @pytest.mark.parametrize("n", [10, 19])
    def test_below_the_minimum_row_count_the_id_heuristic_stays_quiet(self, con, n):
        rows = [(i, f"u{i}", 0.5, "a", "y" if i % 2 else "n", "2026-01-01") for i in range(n)]
        warnings = run_split(con, rows, features=("f", "cust"), test=0.3)
        assert not any("almost every row" in w for w in warnings)

    def test_every_warning_is_a_single_readable_sentence(self, con):
        rows = [(i, f"u{i}", f, t, t, d) for i, c, f, g, t, d in rows_for(1)]
        for warning in run_split(con, rows, features=("f", "cust", "g")):
            assert warning and "\n" not in warning and len(warning) < 600

    def test_the_warnings_name_at_most_five_columns_and_count_the_rest(self, con):
        cols = [("id", "BIGINT"), ("t", "VARCHAR")] + [(f"c{i}", "VARCHAR") for i in range(8)]
        rows = [
            (i, "y" if i % 2 else "n", *(("y" if i % 2 else "n") for _ in range(8)))
            for i in range(100)
        ]
        table(con, "d", cols, rows)
        cfg = {
            "name": "m",
            "input": "d",
            "target": "t",
            "features": [f"c{i}" for i in range(8)],
            "ids": ["id"],
            "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
        }
        result, problems = split(con, MlSpec(**cfg), schema_of(con.table("d")))
        text = " ".join(result.warnings)
        assert "and 3 more" in text
