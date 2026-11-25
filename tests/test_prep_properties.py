from __future__ import annotations

import copy
import json
import math
import random
import statistics

import pytest
from test_nodes_differential import table

from moyung import prep
from moyung.ml import MlSpec, split
from moyung.nodes.base import schema_of

SEEDS = range(12)
SPLIT = {"strategy": "random", "test_size": 0.2, "validation_size": 0.1, "seed": 3}


def numeric_rows(seed, n=120, null_rate=0.15):
    rnd = random.Random(seed)
    return [
        (
            i,
            None if rnd.random() < null_rate else round(rnd.uniform(-50, 200), 3),
            "y" if i % 2 else "n",
        )
        for i in range(n)
    ]


def category_rows(seed, n=120, null_rate=0.1):
    rnd = random.Random(seed)
    pool = ["red", "green", "blue", "grey"]
    return [
        (i, None if rnd.random() < null_rate else rnd.choice(pool), "y" if i % 2 else "n")
        for i in range(n)
    ]


def prepare(con, rows, kind, **rules):
    cols = [("id", "BIGINT"), ("f", "DOUBLE" if kind == "num" else "VARCHAR"), ("t", "VARCHAR")]
    table(con, "d", cols, rows)
    ml = MlSpec(name="m", input="d", target="t", features=["f"], ids=["id"], split=SPLIT, **rules)
    result, problems = split(con, ml, schema_of(con.table("d")))
    parts = {}
    if result is not None:
        for part, name in ml.tables().items():
            cols_out = con.table(name).columns
            parts[part] = {
                r[0]: dict(zip(cols_out, r, strict=True)) for r in con.table(name).fetchall()
            }
    return result, problems, parts


def train_values(rows, parts):
    train = parts["train"]
    return [r[1] for r in rows if r[0] in train and r[1] is not None]


@pytest.mark.parametrize("seed", SEEDS)
class TestNumericScaling:
    def test_standard_scale_gives_train_mean_zero_and_unit_spread(self, con, seed):
        rows = numeric_rows(seed, null_rate=0.0)
        result, problems, parts = prepare(
            con, rows, "num", numerical={"f": {"strategy": "standard_scale"}}
        )
        assert not problems
        scaled = [r["f"] for r in parts["train"].values()]
        assert abs(statistics.fmean(scaled)) < 1e-9
        assert abs(statistics.pstdev(scaled) - 1) < 1e-9

    def test_every_partition_uses_the_train_mean_and_std(self, con, seed):
        rows = numeric_rows(seed, null_rate=0.0)
        result, problems, parts = prepare(
            con, rows, "num", numerical={"f": {"strategy": "standard_scale"}}
        )
        raw = train_values(rows, parts)
        mean, std = statistics.fmean(raw), statistics.pstdev(raw)
        by_id = {r[0]: r[1] for r in rows}
        for part in parts.values():
            for row_id, row in part.items():
                assert row["f"] == pytest.approx((by_id[row_id] - mean) / std, abs=1e-9)

    def test_minmax_maps_train_to_zero_one_and_others_by_the_train_range(self, con, seed):
        rows = numeric_rows(seed, null_rate=0.0)
        result, problems, parts = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        raw = train_values(rows, parts)
        lo, hi = min(raw), max(raw)
        by_id = {r[0]: r[1] for r in rows}
        scaled = [r["f"] for r in parts["train"].values()]
        assert min(scaled) == pytest.approx(0) and max(scaled) == pytest.approx(1)
        for part in parts.values():
            for row_id, row in part.items():
                assert row["f"] == pytest.approx((by_id[row_id] - lo) / (hi - lo), abs=1e-9)

    def test_log1p_is_the_natural_log_of_one_plus_x(self, con, seed):
        rows = [
            (i, abs(v) if v is not None else None, t)
            for i, v, t in numeric_rows(seed, null_rate=0.0)
        ]
        result, problems, parts = prepare(con, rows, "num", numerical={"f": {"strategy": "log1p"}})
        by_id = {r[0]: r[1] for r in rows}
        for part in parts.values():
            for row_id, row in part.items():
                assert row["f"] == pytest.approx(math.log1p(by_id[row_id]), abs=1e-9)


@pytest.mark.parametrize("seed", SEEDS)
class TestMissingValues:
    @pytest.mark.parametrize(
        "strategy, stat", [("mean", statistics.fmean), ("median", statistics.median)]
    )
    def test_the_fill_value_is_a_statistic_of_the_train_rows_only(self, con, seed, strategy, stat):
        rows = numeric_rows(seed, n=200, null_rate=0.3)
        result, problems, parts = prepare(con, rows, "num", missing={"f": {"strategy": strategy}})
        assert not problems
        fill = stat(train_values(rows, parts))
        by_id = {r[0]: r[1] for r in rows}
        for part in parts.values():
            for row_id, row in part.items():
                expected = fill if by_id[row_id] is None else by_id[row_id]
                assert row["f"] == pytest.approx(expected, abs=1e-9)

    def test_constant_fills_every_empty_cell_with_that_value(self, con, seed):
        rows = numeric_rows(seed, null_rate=0.3)
        _, _, parts = prepare(
            con, rows, "num", missing={"f": {"strategy": "constant", "value": -1}}
        )
        assert all(r["f"] is not None for part in parts.values() for r in part.values())
        by_id = {r[0]: r[1] for r in rows}
        assert all(
            r["f"] == -1 for part in parts.values() for i, r in part.items() if by_id[i] is None
        )

    def test_mode_picks_the_most_common_train_value(self, con, seed):
        rows = category_rows(seed, null_rate=0.3)
        result, problems, parts = prepare(con, rows, "cat", missing={"f": {"strategy": "mode"}})
        assert not problems
        counts = {}
        for value in train_values(rows, parts):
            counts[value] = counts.get(value, 0) + 1
        top = max(counts.values())
        by_id = {r[0]: r[1] for r in rows}
        filled = {r["f"] for part in parts.values() for i, r in part.items() if by_id[i] is None}
        assert len(filled) <= 1 and (not filled or counts[next(iter(filled))] == top)

    def test_drop_removes_the_empty_rows_from_every_partition(self, con, seed):
        rows = numeric_rows(seed, null_rate=0.3)
        result, problems, parts = prepare(con, rows, "num", missing={"f": {"strategy": "drop"}})
        kept = {i for part in parts.values() for i in part}
        assert kept == {r[0] for r in rows if r[1] is not None}


@pytest.mark.parametrize("seed", SEEDS)
class TestCategories:
    def test_one_hot_marks_exactly_the_matching_column(self, con, seed):
        rows = category_rows(seed, null_rate=0.0)
        result, problems, parts = prepare(
            con, rows, "cat", categorical={"f": {"strategy": "one_hot"}}
        )
        assert not problems
        by_id = {r[0]: r[1] for r in rows}
        for part in parts.values():
            for row_id, row in part.items():
                hot = {k for k, v in row.items() if k.startswith("f_") and v == 1}
                assert hot == {f"f_{by_id[row_id]}"} or not hot
                assert all(v in (0, 1) for k, v in row.items() if k.startswith("f_"))

    def test_the_encoded_columns_are_named_after_the_train_categories(self, con, seed):
        rows = category_rows(seed, null_rate=0.0)
        result, _, parts = prepare(con, rows, "cat", categorical={"f": {"strategy": "one_hot"}})
        seen = sorted(set(train_values(rows, parts)))
        columns = [c.name for c in result.columns if c.name.startswith("f_")]
        assert sorted(columns) == [f"f_{v}" for v in seen]

    def test_ordinal_uses_the_position_in_the_train_category_list(self, con, seed):
        rows = category_rows(seed, null_rate=0.0)
        result, _, parts = prepare(con, rows, "cat", categorical={"f": {"strategy": "ordinal"}})
        categories = result.metadata["features"][0]["categorical"]["categories"]
        by_id = {r[0]: r[1] for r in rows}
        for part in parts.values():
            for row_id, row in part.items():
                value = by_id[row_id]
                if value in categories:
                    assert row["f"] == categories.index(value)

    def test_a_category_unseen_in_train_is_ignored_or_refused_as_told(self, con, seed):
        rows = [(i, "common" if i < 100 else f"rare{i}", "y" if i % 2 else "n") for i in range(120)]
        random.Random(seed).shuffle(rows)
        ignored, problems, _ = prepare(con, rows, "cat", categorical={"f": {"strategy": "one_hot"}})
        assert ignored is not None or problems
        con.execute("DROP TABLE IF EXISTS d")
        for name in ("m_train", "m_validation", "m_test"):
            con.execute(f"DROP TABLE IF EXISTS {name}")
        strict, problems, _ = prepare(
            con, rows, "cat", categorical={"f": {"strategy": "one_hot", "unknown": "error"}}
        )
        assert strict is None or ignored is None
        if strict is None:
            assert any("not seen in train" in p or "no value" in p for p in problems)


@pytest.mark.parametrize("seed", range(6))
class TestApplyMatchesTheSplit:
    @pytest.mark.parametrize(
        "kind, rules",
        [
            (
                "num",
                {
                    "numerical": {"f": {"strategy": "standard_scale"}},
                    "missing": {"f": {"strategy": "mean"}},
                },
            ),
            (
                "num",
                {
                    "numerical": {"f": {"strategy": "minmax"}},
                    "missing": {"f": {"strategy": "median"}},
                },
            ),
            (
                "cat",
                {
                    "categorical": {"f": {"strategy": "one_hot"}},
                    "missing": {"f": {"strategy": "mode"}},
                },
            ),
            (
                "cat",
                {
                    "categorical": {"f": {"strategy": "ordinal"}},
                    "missing": {"f": {"strategy": "constant", "value": "red"}},
                },
            ),
        ],
    )
    def test_applying_the_saved_metadata_to_the_raw_table_reproduces_every_partition(
        self, con, seed, kind, rules
    ):
        rows = numeric_rows(seed) if kind == "num" else category_rows(seed)
        result, problems, parts = prepare(con, rows, kind, **rules)
        assert not problems
        assert prep.apply(con, "d", result.metadata, "again") == []
        cols = con.table("again").columns
        again = {r[0]: dict(zip(cols, r, strict=True)) for r in con.table("again").fetchall()}
        every = {i: row for part in parts.values() for i, row in part.items()}
        assert again.keys() == every.keys()
        for row_id, row in every.items():
            for name, value in row.items():
                if isinstance(value, float):
                    assert again[row_id][name] == pytest.approx(value, abs=1e-9)
                else:
                    assert again[row_id][name] == value

    def test_the_metadata_is_json_and_survives_a_round_trip(self, con, seed):
        rows = numeric_rows(seed)
        result, _, _ = prepare(
            con,
            rows,
            "num",
            numerical={"f": {"strategy": "standard_scale"}},
            missing={"f": {"strategy": "mean"}},
        )
        text = json.dumps(result.metadata, sort_keys=True)
        assert json.loads(text) == result.metadata
        assert prep.apply(con, "d", json.loads(text), "again") == []

    def test_fitting_twice_gives_identical_metadata(self, con, seed):
        rows = numeric_rows(seed)
        first, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "standard_scale"}})
        con.execute("DROP TABLE d")
        for name in ("m_train", "m_validation", "m_test"):
            con.execute(f"DROP TABLE {name}")
        second, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "standard_scale"}})
        assert first.metadata == second.metadata


class TestEdges:
    def test_a_constant_column_is_scaled_to_zero_with_a_warning(self, con):
        rows = [(i, 5.0, "y" if i % 2 else "n") for i in range(60)]
        result, problems, parts = prepare(
            con, rows, "num", numerical={"f": {"strategy": "standard_scale"}}
        )
        assert any("constant in train" in w for w in result.warnings)
        assert all(r["f"] == 0 for r in parts["train"].values())

    def test_a_column_with_no_train_value_cannot_be_learned_from(self, con):
        rows = [(i, None, "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(con, rows, "num", missing={"f": {"strategy": "mean"}})
        assert result is None and "no value in train" in problems[0]

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_values_are_refused(self, con, bad):
        rows = [(i, bad if i == 7 else 1.0, "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(
            con, rows, "num", numerical={"f": {"strategy": "standard_scale"}}
        )
        assert result is None and "NaN or infinite" in problems[0]

    @pytest.mark.parametrize("value", [-1.0, -2.0, -1e9])
    def test_log1p_needs_values_above_minus_one(self, con, value):
        rows = [(i, value if i == 7 else 1.0, "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "log1p"}})
        assert result is None and "above -1" in problems[0]

    @pytest.mark.xfail(
        strict=True, reason="BUG-020: finite 1e308 values are reported as NaN or infinite"
    )
    def test_a_finite_huge_value_is_not_called_infinite(self, con):
        rows = [(i, 1e308 * (1 if i % 2 else -1), "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(
            con, rows, "num", numerical={"f": {"strategy": "standard_scale"}}
        )
        assert not any("NaN or infinite" in p for p in problems)

    def test_too_many_categories_names_the_limit(self, con):
        rows = [(i, f"c{i}", "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(
            con, rows, "cat", categorical={"f": {"strategy": "one_hot", "max_categories": 5}}
        )
        assert result is None and "above max_categories 5" in problems[0]

    def test_a_category_column_with_only_empty_values_is_refused(self, con):
        rows = [(i, None, "y" if i % 2 else "n") for i in range(60)]
        result, problems, _ = prepare(con, rows, "cat", categorical={"f": {"strategy": "one_hot"}})
        assert result is None and "no value in train" in problems[0]

    def test_awkward_category_text_becomes_safe_column_names(self, con):
        pool = ["a b", "A/B", "é", 'x"y', "1", "drop;--"]
        rows = [(i, pool[i % len(pool)], "y" if i % 2 else "n") for i in range(120)]
        result, problems, parts = prepare(
            con, rows, "cat", categorical={"f": {"strategy": "one_hot"}}
        )
        assert not problems
        names = [c.name for c in result.columns]
        assert len(names) == len({n.casefold() for n in names})

    def test_apply_refuses_the_input_table_name_as_the_output(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        assert "another name" in prep.apply(con, "d", result.metadata, "D")[0]

    def test_apply_names_a_missing_column(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        table(con, "new", [("id", "BIGINT"), ("other", "DOUBLE")], [(1, 1.0)])
        assert "'f' is missing" in prep.apply(con, "new", result.metadata, "out")[0]

    def test_apply_works_on_new_data_without_a_target(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        table(con, "new", [("id", "BIGINT"), ("f", "DOUBLE")], [(1, 10.0), (2, 20.0)])
        assert prep.apply(con, "new", result.metadata, "out") == []
        assert con.table("out").columns == ["id", "f"]

    def test_apply_matches_columns_ignoring_letter_case(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        table(con, "new", [("ID", "BIGINT"), ("F", "DOUBLE")], [(1, 10.0)])
        assert prep.apply(con, "new", result.metadata, "out") == []

    def test_apply_refuses_a_column_of_another_type(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        table(con, "new", [("id", "BIGINT"), ("f", "VARCHAR")], [(1, "x")])
        assert prep.apply(con, "new", result.metadata, "out")

    def test_apply_does_not_change_the_metadata_it_is_given(self, con):
        rows = numeric_rows(1)
        result, _, _ = prepare(con, rows, "num", numerical={"f": {"strategy": "minmax"}})
        before = copy.deepcopy(result.metadata)
        prep.apply(con, "d", result.metadata, "out")
        assert result.metadata == before
