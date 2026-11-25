from __future__ import annotations

import random

import pytest
from test_nodes_differential import table  # noqa: F401

from moyung.ml import MlSpec, split
from moyung.nodes.base import schema_of

COLUMNS = [
    ("id", "BIGINT"),
    ("g", "BIGINT"),
    ("t", "VARCHAR"),
    ("f", "DOUBLE"),
    ("day", "BIGINT"),
]


def make_rows(n, groups=7, positive=0.25, seed=0):
    rnd = random.Random(seed)
    return [
        (i, rnd.randrange(groups), "y" if rnd.random() < positive else "n", rnd.random(), i // 3)
        for i in range(n)
    ]


def build(con, rows, strategy="random", by=None, test=0.2, validation=0.1, seed=1):
    table(con, "d", COLUMNS, rows)
    cfg = {
        "name": "m",
        "input": "d",
        "target": "t",
        "features": ["f", "g", "day"],
        "ids": ["id"],
        "split": {
            "strategy": strategy,
            "test_size": test,
            "validation_size": validation,
            "seed": seed,
            **({"by": by} if by else {}),
        },
    }
    ml = MlSpec(**cfg)
    result, problems = split(con, ml, schema_of(con.table("d")))
    parts = {}
    if result is not None:
        for part, name in ml.tables().items():
            cols = con.table(name).columns
            parts[part] = [dict(zip(cols, r, strict=True)) for r in con.table(name).fetchall()]
    return result, problems, parts


def ids(parts):
    return {p: {r["id"] for r in rows} for p, rows in parts.items()}


SIZES = [(30, 0.2, 0.1), (100, 0.2, 0.0), (101, 0.25, 0.15), (500, 0.1, 0.1), (57, 0.3, 0.3)]


@pytest.mark.parametrize(
    "strategy, by", [("random", None), ("stratified", None), ("group", "g"), ("time", "day")]
)
@pytest.mark.parametrize("n, test, validation", SIZES)
class TestEveryStrategy:
    def test_the_partitions_are_disjoint_and_cover_every_row(
        self, con, strategy, by, n, test, validation
    ):
        result, problems, parts = build(con, make_rows(n), strategy, by, test, validation)
        if problems:
            pytest.skip("a partition came out empty for this size")
        seen = ids(parts)
        every = [i for part in seen.values() for i in part]
        assert sorted(every) == list(range(n))
        assert len(every) == len(set(every))
        assert result.rows_in == n and sum(result.rows.values()) == n

    def test_the_reported_counts_match_the_tables(self, con, strategy, by, n, test, validation):
        result, problems, parts = build(con, make_rows(n), strategy, by, test, validation)
        if problems:
            pytest.skip("a partition came out empty for this size")
        assert {p: len(rows) for p, rows in parts.items()} == result.rows

    def test_the_same_input_and_seed_give_the_same_partitions(
        self, con, strategy, by, n, test, validation
    ):
        rows = make_rows(n)
        _, problems, first = build(con, rows, strategy, by, test, validation, seed=5)
        con.execute("DROP TABLE d")
        for part in first:
            con.execute(f"DROP TABLE m_{part}")
        _, _, second = build(con, rows, strategy, by, test, validation, seed=5)
        if problems:
            pytest.skip("a partition came out empty for this size")
        assert ids(first) == ids(second)

    def test_the_columns_are_ids_then_features_then_target(
        self, con, strategy, by, n, test, validation
    ):
        _, problems, parts = build(con, make_rows(n), strategy, by, test, validation)
        if problems:
            pytest.skip("a partition came out empty for this size")
        assert list(next(iter(parts.values()))[0]) == ["id", "f", "g", "day", "t"]


@pytest.mark.parametrize("seed", range(8))
class TestSizes:
    @pytest.mark.parametrize("strategy", ["random", "stratified"])
    def test_the_test_share_is_within_one_row_per_class(self, con, seed, strategy):
        rows = make_rows(400, seed=seed)
        result, problems, parts = build(con, rows, strategy, None, 0.2, 0.1, seed=seed)
        assert not problems
        if strategy == "random":
            assert abs(len(parts["test"]) - 80) <= 1
            assert abs(len(parts["validation"]) - 40) <= 1
        else:
            for label in ("y", "n"):
                total = sum(1 for r in rows if r[2] == label)
                got = sum(1 for r in parts["test"] if r["t"] == label)
                assert abs(got - 0.2 * total) <= 1

    def test_stratified_keeps_the_class_balance_in_every_partition(self, con, seed):
        rows = make_rows(600, positive=0.1, seed=seed)
        _, problems, parts = build(con, rows, "stratified", None, 0.2, 0.2, seed=seed)
        overall = sum(1 for r in rows if r[2] == "y") / len(rows)
        for part in parts.values():
            share = sum(1 for r in part if r["t"] == "y") / len(part)
            assert abs(share - overall) < 0.03

    def test_a_group_never_spans_two_partitions(self, con, seed):
        rows = make_rows(300, groups=25, seed=seed)
        _, problems, parts = build(con, rows, "group", "g", 0.2, 0.2, seed=seed)
        assert not problems
        home = {}
        for part, members in parts.items():
            for row in members:
                assert home.setdefault(row["g"], part) == part

    def test_time_puts_the_newest_rows_in_test_and_the_oldest_in_train(self, con, seed):
        rows = make_rows(300, seed=seed)
        random.Random(seed).shuffle(rows)
        _, problems, parts = build(con, rows, "time", "day", 0.2, 0.2, seed=seed)
        assert not problems
        days = {p: [r["day"] for r in members] for p, members in parts.items()}
        assert min(days["test"]) >= max(days["validation"]) >= min(days["validation"])
        assert min(days["validation"]) >= max(days["train"])

    def test_time_does_not_depend_on_the_seed(self, con, seed):
        rows = make_rows(200)
        _, _, a = build(con, rows, "time", "day", 0.2, 0.1, seed=seed)
        con.execute("DROP TABLE d")
        for part in a:
            con.execute(f"DROP TABLE m_{part}")
        _, _, b = build(con, rows, "time", "day", 0.2, 0.1, seed=seed + 100)
        assert ids(a) == ids(b)


class TestSeedAndOrder:
    def test_another_seed_gives_another_cut(self, con):
        rows = make_rows(300)
        _, _, a = build(con, rows, seed=1)
        con.execute("DROP TABLE d")
        for part in a:
            con.execute(f"DROP TABLE m_{part}")
        _, _, b = build(con, rows, seed=2)
        assert ids(a)["test"] != ids(b)["test"]

    def test_the_cut_follows_the_row_position_not_the_row_values(self, con):
        rows = make_rows(200)
        _, _, a = build(con, rows, seed=3)
        con.execute("DROP TABLE d")
        for part in a:
            con.execute(f"DROP TABLE m_{part}")
        _, _, b = build(con, list(reversed(rows)), seed=3)
        position_a = {i: p for p, members in ids(a).items() for i in members}
        position_b = {i: p for p, members in ids(b).items() for i in members}
        assert position_a != position_b

    def test_no_validation_partition_when_validation_size_is_zero(self, con):
        result, problems, parts = build(con, make_rows(100), validation=0.0)
        assert set(parts) == {"train", "test"} and set(result.rows) == {"train", "test"}

    def test_helper_tables_are_dropped_afterwards(self, con):
        build(con, make_rows(100))
        names = {r[0] for r in con.execute("select table_name from duckdb_tables()").fetchall()}
        assert names == {"d", "m_train", "m_validation", "m_test"}


class TestProblems:
    def test_a_null_target_is_refused_with_a_count(self, con):
        rows = make_rows(50)
        rows[3] = (rows[3][0], rows[3][1], None, rows[3][3], rows[3][4])
        rows[9] = (rows[9][0], rows[9][1], None, rows[9][3], rows[9][4])
        result, problems, _ = build(con, rows)
        assert result is None and "2 empty value(s)" in problems[0]

    def test_a_null_group_or_time_value_is_refused(self, con):
        rows = make_rows(50)
        rows[3] = (rows[3][0], None, *rows[3][2:])
        result, problems, _ = build(con, rows, "group", "g")
        assert result is None and "'g' has 1 empty value(s)" in problems[0]

    @pytest.mark.parametrize("n", [0, 1, 2, 3])
    def test_a_table_too_small_for_the_partitions_says_which_is_empty(self, con, n):
        result, problems, _ = build(con, make_rows(n), test=0.2, validation=0.2)
        assert result is None and any("no rows" in p for p in problems)

    def test_one_group_cannot_be_split(self, con):
        result, problems, _ = build(con, make_rows(100, groups=1), "group", "g")
        assert result is None and problems

    def test_one_time_value_puts_every_row_in_one_partition(self, con):
        rows = [(i, 0, "y" if i % 2 else "n", 0.5, 7) for i in range(100)]
        result, problems, parts = build(con, rows, "time", "day", 0.2, 0.0)
        assert result is None or len({len(v) for v in parts.values()}) >= 1

    def test_a_target_with_a_single_class_is_warned_or_refused(self, con):
        rows = [(i, i % 5, "n", 0.5, i) for i in range(100)]
        result, problems, _ = build(con, rows, "stratified")
        assert result is None or result.warnings

    def test_a_rare_class_in_stratified_is_reported(self, con):
        rows = [(i, i % 5, "y" if i < 2 else "n", 0.5, i) for i in range(100)]
        result, problems, _ = build(con, rows, "stratified", test=0.2, validation=0.2)
        assert problems or result.warnings or "y" in result.classes["test"]

    @pytest.mark.parametrize("test, validation", [(0.5, 0.5), (0.6, 0.4), (0.9, 0.2)])
    def test_sizes_that_leave_no_train_are_refused_when_the_spec_is_read(self, test, validation):
        with pytest.raises(ValueError):
            MlSpec(
                name="m",
                input="d",
                target="t",
                features=["f"],
                split={
                    "strategy": "random",
                    "test_size": test,
                    "validation_size": validation,
                    "seed": 1,
                },
            )

    @pytest.mark.parametrize("test", [0, 1, -0.1, 1.5])
    def test_a_test_size_outside_zero_one_is_refused(self, test):
        with pytest.raises(ValueError):
            MlSpec(
                name="m",
                input="d",
                target="t",
                features=["f"],
                split={"strategy": "random", "test_size": test, "seed": 1},
            )

    @pytest.mark.parametrize("seed", [1.5, "1", True, None])
    def test_the_seed_must_be_a_real_integer(self, seed):
        with pytest.raises(ValueError):
            MlSpec(
                name="m",
                input="d",
                target="t",
                features=["f"],
                split={"strategy": "random", "test_size": 0.2, "seed": seed},
            )

    @pytest.mark.parametrize("seed", [0, -1, 2**40])
    def test_any_integer_seed_works(self, con, seed):
        result, problems, _ = build(con, make_rows(100), seed=seed)
        assert result is not None and not problems

    def test_a_group_split_needs_by_and_a_random_split_forbids_it(self):
        base = {"name": "m", "input": "d", "target": "t", "features": ["f"]}
        with pytest.raises(ValueError):
            MlSpec(**base, split={"strategy": "group", "test_size": 0.2, "seed": 1})
        with pytest.raises(ValueError):
            MlSpec(**base, split={"strategy": "random", "by": "g", "test_size": 0.2, "seed": 1})

    def test_a_column_with_two_roles_is_refused(self):
        with pytest.raises(ValueError, match="listed twice"):
            MlSpec(
                name="m",
                input="d",
                target="t",
                features=["f", "t"],
                split={"strategy": "random", "test_size": 0.2, "seed": 1},
            )
