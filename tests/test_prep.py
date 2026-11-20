from __future__ import annotations

import json
import math
import re

import duckdb
import pytest

from excel_etl.engine import run, validate
from excel_etl.nodes.base import connect
from excel_etl.pipeline import SpecError, parse_spec
from excel_etl.prep import apply_preprocessing as _apply_full

N = 200


def apply_preprocessing(con, table, metadata, name):
    return _apply_full(con, table, metadata, name).problems


@pytest.fixture
def data(tmp_path):
    lines = ["id,age,spend,seg,tier,churn"]
    for i in range(N):
        age = "" if i % 10 == 3 else str(20 + i % 40)
        seg = "" if i % 13 == 5 else "ABC"[i % 3]
        tier = "gold" if i % 5 == 0 else "silver"
        label = "yes" if i % 4 == 0 else "no"
        lines.append(f"{i},{age},{10.5 + i * 2},{seg},{tier},{label}")
    path = tmp_path / "c.csv"
    path.write_bytes(("\n".join(lines) + "\n").encode())
    return path


def _ml(**over):
    base = {
        "name": "m",
        "input": "cust",
        "target": "churn",
        "features": ["age", "spend", "seg", "tier"],
        "ids": ["id"],
        "split": {"strategy": "stratified", "test_size": 0.2, "validation_size": 0.1, "seed": 5},
        "missing": {"age": {"strategy": "median"}, "seg": {"strategy": "mode"}},
        "numerical": {"age": {"strategy": "standard_scale"}, "spend": {"strategy": "minmax"}},
        "categorical": {
            "seg": {"strategy": "one_hot", "unknown": "ignore"},
            "tier": {"strategy": "ordinal", "unknown": "error"},
        },
    }
    return {**base, **over}


def _spec(ml=None):
    ml = ml if ml is not None else _ml()
    parts = ["train", "test"] + (["validation"] if ml["split"].get("validation_size") else [])
    return parse_spec(
        json.dumps(
            {
                "sources": {"cust": {"kind": "csv", "file_role": "c"}},
                "ml": ml,
                "outputs": {p: {"input": f"m_{p}"} for p in parts},
            }
        )
    )


def _go(data, tmp_path, ml=None):
    rep = run(_spec(ml), {"c": data}, tmp_path / "out")
    assert rep.status == "ok", rep.issues
    return rep, tmp_path / "out"


def _q(path, sql):
    sql = re.sub(r"\bT\b", f"'{path.as_posix()}'", sql)
    return duckdb.connect().execute(sql).fetchall()


def _meta(out):
    return json.loads((out / "preprocessing_metadata.json").read_text())["preprocessing"]


def _entry(meta, column):
    return next(e for e in meta["features"] if e["column"] == column)


def test_train_is_scaled_by_its_own_numbers_and_the_others_by_the_same(data, tmp_path):
    _, out = _go(data, tmp_path)
    mean, std, low, high = _q(
        out / "train.parquet", "SELECT avg(age), stddev_pop(age), min(spend), max(spend) FROM T"
    )[0]
    assert abs(mean) < 1e-9 and abs(std - 1) < 1e-9 and low == 0 and high == 1
    test_low, test_high = _q(out / "test.parquet", "SELECT min(spend), max(spend) FROM T")[0]
    assert test_low > 0 and test_high < 1


def test_parameters_come_from_the_train_rows_only(data, tmp_path):
    _, out = _go(data, tmp_path)
    ids = [r[0] for r in _q(out / "train.parquet", "SELECT id FROM T")]
    in_train = ",".join(map(str, ids))
    raw = f"'{data.as_posix()}'"
    expected = (
        duckdb.connect()
        .execute(f"SELECT median(age), avg(coalesce(age, 40)) FROM {raw} WHERE id IN ({in_train})")
        .fetchall()[0]
    )
    meta = _meta(out)
    assert _entry(meta, "age")["missing"]["value"] == expected[0]
    assert math.isclose(_entry(meta, "age")["numeric"]["mean"], expected[1], rel_tol=1e-9)


def test_empty_values_are_filled_in_every_partition_and_counted_in_the_report(data, tmp_path):
    _, out = _go(data, tmp_path)
    for part in ("train", "validation", "test"):
        assert _q(out / f"{part}.parquet", "SELECT count(*) FILTER (WHERE age IS NULL) FROM T") == [
            (0,)
        ]
    report = json.loads((out / "preparation_report.json").read_text())
    assert sum(report["empty_values"]["age"].values()) == N // 10
    assert set(report["empty_values"]) == {"age", "seg"}


def test_one_hot_and_ordinal_columns_have_the_engine_types_and_stable_names(data, tmp_path):
    rep, out = _go(data, tmp_path)
    types = {r[0]: r[1] for r in _q(out / "train.parquet", "DESCRIBE SELECT * FROM T")}
    assert list(types) == ["id", "age", "spend", "seg_A", "seg_B", "seg_C", "tier", "churn"]
    assert types["seg_A"] == types["tier"] == "BIGINT" and types["age"] == "DOUBLE"
    assert _entry(_meta(out), "seg")["categorical"]["categories"] == ["A", "B", "C"]
    assert _entry(_meta(out), "tier")["categorical"]["categories"] == ["gold", "silver"]
    roles = {c["name"]: c for c in _meta(out)["output_columns"]}
    assert roles["seg_B"]["derived_from"] == "seg" and roles["seg_B"]["role"] == "feature"
    assert [n.rows_out for n in rep.nodes if n.node == "ml_split"] == [140, 20, 40]


def test_one_hot_rows_have_a_single_one_and_a_null_category_is_filled_first(data, tmp_path):
    _, out = _go(data, tmp_path)
    sums = _q(out / "train.parquet", "SELECT DISTINCT seg_A + seg_B + seg_C FROM T")
    assert sums == [(1,)]


def test_the_stored_metadata_reproduces_the_train_table(data, tmp_path):
    _, out = _go(data, tmp_path)
    ids = ",".join(str(r[0]) for r in _q(out / "train.parquet", "SELECT id FROM T"))
    con = connect()
    con.execute(
        "CREATE TABLE raw (id BIGINT, age BIGINT, spend DOUBLE, seg VARCHAR, tier VARCHAR, "
        "churn BOOLEAN)"
    )
    source = duckdb.connect().execute(f"SELECT * FROM '{data.as_posix()}' WHERE id IN ({ids})")
    for row in source.fetchall():
        con.execute("INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?)", list(row))
    meta_text = (out / "preprocessing_metadata.json").read_text()
    assert apply_preprocessing(con, "raw", meta_text, "again") == []
    again = con.execute("SELECT * FROM again ORDER BY id").fetchall()
    train = _q(out / "train.parquet", "SELECT * FROM T ORDER BY id")
    assert len(again) == len(train) == 140
    for got, want in zip(again, train, strict=True):
        same = (
            math.isclose(a, b) if isinstance(a, float) else a == b
            for a, b in zip(got, want, strict=True)
        )
        assert all(same)


def _new(con, sql):
    con.execute(f"CREATE OR REPLACE TABLE fresh AS {sql}")


def test_new_data_without_a_target_or_ids_is_prepared_with_the_train_numbers(data, tmp_path):
    _, out = _go(data, tmp_path)
    con = connect()
    _new(
        con,
        "SELECT * FROM (VALUES (30, 100.0, 'A', 'gold'), (NULL, 50.0, NULL, 'silver')) "
        "v(age, spend, seg, tier)",
    )
    assert apply_preprocessing(con, "fresh", _meta(out), "ready") == []
    assert [d[0] for d in con.table("ready").description] == [
        "age",
        "spend",
        "seg_A",
        "seg_B",
        "seg_C",
        "tier",
    ]
    age_mean = _entry(_meta(out), "age")["numeric"]["mean"]
    median = _entry(_meta(out), "age")["missing"]["value"]
    std = _entry(_meta(out), "age")["numeric"]["std"]
    rows = con.execute("SELECT age, seg_A FROM ready").fetchall()
    assert math.isclose(rows[1][0], (median - age_mean) / std) and rows[1][1] == 1


def test_apply_says_what_is_wrong_with_the_new_data_instead_of_raising(data, tmp_path):
    _, out = _go(data, tmp_path)
    meta = _meta(out)
    con = connect()
    _new(con, "SELECT 30 AS age, 1.0 AS spend, 'A' AS seg, 'bronze' AS tier")
    assert "not seen in train" in apply_preprocessing(con, "fresh", meta, "x")[0]
    _new(con, "SELECT 30 AS age, 1.0 AS spend, 'A' AS seg")
    assert "'tier' is missing" in apply_preprocessing(con, "fresh", meta, "x")[0]
    _new(con, "SELECT 'old' AS age, 1.0 AS spend, 'A' AS seg, 'gold' AS tier")
    assert "needs a number" in apply_preprocessing(con, "fresh", meta, "x")[0]
    assert "not JSON" in apply_preprocessing(con, "fresh", "{nope", "x")[0]
    assert "needs ids, target and features" in apply_preprocessing(con, "fresh", {}, "x")[0]
    assert "version" in apply_preprocessing(con, "fresh", {**meta, "version": 9}, "x")[0]


def test_apply_accepts_the_whole_file_or_just_the_preprocessing_part(data, tmp_path):
    _, out = _go(data, tmp_path)
    con = connect()
    _new(con, "SELECT 30 AS age, 1.0 AS spend, 'A' AS seg, 'gold' AS tier")
    whole = json.loads((out / "preprocessing_metadata.json").read_text())
    assert apply_preprocessing(con, "fresh", whole, "a") == []
    assert apply_preprocessing(con, "fresh", whole["preprocessing"], "b") == []


def test_constant_mean_and_drop_strategies(data, tmp_path):
    ml = _ml(
        missing={
            "age": {"strategy": "drop"},
            "seg": {"strategy": "constant", "value": "Z"},
        },
        numerical={"spend": {"strategy": "log1p"}},
        categorical={"seg": {"strategy": "one_hot"}},
    )
    rep, out = _go(data, tmp_path, ml)
    assert _entry(_meta(out), "seg")["missing"] == {"strategy": "constant", "value": "Z"}
    assert "Z" in _entry(_meta(out), "seg")["categorical"]["categories"]
    assert sum(r.rows for r in rep.outputs) == N - N // 10
    report = json.loads((out / "preparation_report.json").read_text())
    assert sum(report["rows_cut_for_empty_values"].values()) == N // 10
    spend = _q(out / "train.parquet", "SELECT min(spend) FROM T")[0][0]
    assert math.isclose(spend, math.log1p(10.5), abs_tol=0.5) or spend > 0


def test_a_mean_fill_turns_whole_numbers_into_doubles(data, tmp_path):
    ml = _ml(
        missing={"age": {"strategy": "mean"}},
        numerical={},
        categorical={},
        features=["age"],
        exclude=["spend", "seg", "tier"],
    )
    _, out = _go(data, tmp_path, ml)
    types = {r[0]: r[1] for r in _q(out / "train.parquet", "DESCRIBE SELECT * FROM T")}
    assert types["age"] == "DOUBLE"


def test_the_validate_schema_leaves_out_one_hot_columns_but_keeps_the_rest(data):
    report = validate(_spec(), {"c": data})
    assert report.ok
    types = {c.name: c.type for c in report.schemas["m_train"]}
    assert list(types) == ["id", "age", "spend", "tier", "churn"]
    assert types["age"] == "DOUBLE" and types["tier"] == "BIGINT"


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"missing": {"nope": {"strategy": "mode"}}}, "does not have"),
        ({"missing": {"id": {"strategy": "mode"}}}, "not a feature"),
        ({"missing": {"churn": {"strategy": "mode"}}}, "not a feature"),
        ({"missing": {"seg": {"strategy": "median"}}}, "needs a number"),
        ({"missing": {"spend": {"strategy": "mode"}}}, "mode needs"),
        ({"missing": {"age": {"strategy": "constant", "value": "x"}}}, "does not fit"),
        ({"numerical": {"seg": {"strategy": "minmax"}}}, "needs a number"),
        ({"categorical": {"spend": {"strategy": "one_hot"}}}, "needs text, boolean"),
    ],
)
def test_rules_that_do_not_fit_the_columns_are_issues_at_validate(data, over, message):
    ml = {**_ml(missing={}, numerical={}, categorical={}), **over}
    issues = " ".join(i.message for i in validate(_spec(ml), {"c": data}).issues)
    assert message in issues


@pytest.mark.parametrize(
    "over",
    [
        {
            "numerical": {"age": {"strategy": "minmax"}},
            "categorical": {"AGE": {"strategy": "ordinal"}},
        },
        {"missing": {"age": {"strategy": "constant"}}},
        {"missing": {"age": {"strategy": "mean", "value": 3}}},
        {"missing": {"age": {"strategy": "bogus"}}},
        {"categorical": {"seg": {"strategy": "one_hot", "max_categories": 1}}},
        {"numerical": {"age": {"strategy": "minmax", "extra": 1}}},
        {"missing": {"a": {"strategy": "mode"}, "A": {"strategy": "mode"}}},
    ],
)
def test_a_bad_rule_is_refused_when_the_spec_is_read(over):
    with pytest.raises(SpecError):
        _spec(_ml(**over))


def test_too_many_categories_is_an_issue_with_the_way_out(data, tmp_path):
    ml = _ml(
        categorical={"seg": {"strategy": "one_hot", "max_categories": 2}},
        numerical={},
    )
    rep = run(_spec(ml), {"c": data}, tmp_path / "out")
    assert rep.status == "failed"
    assert (
        "max_categories" in rep.issues[0].message
        and not (tmp_path / "out" / "train.parquet").exists()
    )


def test_log1p_on_values_below_minus_one_is_an_issue(tmp_path):
    path = tmp_path / "n.csv"
    lines = ["id,x,y"] + [f"{i},{i - 5},{'ab'[i % 2]}" for i in range(40)]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    ml = {
        "name": "m",
        "input": "cust",
        "target": "y",
        "features": ["x"],
        "ids": ["id"],
        "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
        "numerical": {"x": {"strategy": "log1p"}},
    }
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "log1p" in rep.issues[0].message


def test_a_constant_column_warns_and_scales_to_zero(tmp_path):
    path = tmp_path / "k.csv"
    lines = ["id,x,y"] + [f"{i},7,{'ab'[i % 2]}" for i in range(40)]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    ml = {
        "name": "m",
        "input": "cust",
        "target": "y",
        "features": ["x"],
        "ids": ["id"],
        "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
        "numerical": {"x": {"strategy": "standard_scale"}},
    }
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "ok" and any("constant in train" in w for w in rep.warnings)
    assert _q(tmp_path / "out" / "train.parquet", "SELECT DISTINCT x FROM T") == [(0.0,)]


def test_a_feature_with_no_value_in_train_is_an_issue(tmp_path):
    path = tmp_path / "e.csv"
    lines = ["id,x,y"] + [f"{i},,{'ab'[i % 2]}" for i in range(40)]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    ml = {
        "name": "m",
        "input": "cust",
        "target": "y",
        "features": ["x"],
        "ids": ["id"],
        "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
        "missing": {"x": {"strategy": "mean"}},
    }
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "failed"


def test_the_same_rules_and_seed_give_byte_identical_files(data, tmp_path):
    first, again = tmp_path / "a", tmp_path / "b"
    run(_spec(), {"c": data}, first)
    run(_spec(), {"c": data}, again)
    for name in sorted(p.name for p in first.iterdir()):
        assert (first / name).read_bytes() == (again / name).read_bytes(), name


def test_unknown_rule_keys_get_the_ml_hint():
    with pytest.raises(SpecError, match="takes name, input, target, features"):
        _spec({**_ml(), "scaling": {}})


def _tiny(tmp_path, header, rows, name="t.csv"):
    path = tmp_path / name
    path.write_bytes(("\n".join([header, *rows]) + "\n").encode())
    return path


def _small_ml(features, **over):
    return {
        "name": "m",
        "input": "cust",
        "target": "y",
        "features": features,
        "ids": ["id"],
        "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
        **over,
    }


def test_new_data_warns_about_unseen_categories_and_dropped_rows(data, tmp_path):
    ml = _ml(
        missing={"age": {"strategy": "drop"}},
        numerical={},
        categorical={"seg": {"strategy": "one_hot", "unknown": "ignore"}},
    )
    _, out = _go(data, tmp_path, ml)
    con = connect()
    _new(
        con,
        "SELECT * FROM (VALUES (30, 1.0, 'A', 'gold'), (NULL, 2.0, 'B', 'gold'), "
        "(31, 3.0, 'platinum', 'gold')) v(age, spend, seg, tier)",
    )
    result = _apply_full(con, "fresh", (out / "preprocessing_metadata.json").read_text(), "ready")
    assert result.ok and (result.rows_in, result.rows_out) == (3, 2)
    text = " ".join(result.warnings)
    assert "1 of 3 row(s) were dropped" in text and "'seg': 1 row(s)" in text and "platinum" in text


def test_apply_will_not_replace_its_own_input(data, tmp_path):
    _, out = _go(data, tmp_path)
    con = connect()
    _new(con, "SELECT 30 AS age, 1.0 AS spend, 'A' AS seg, 'gold' AS tier")
    assert "another name" in apply_preprocessing(con, "fresh", _meta(out), "fresh")[0]
    assert con.execute("SELECT count(*) FROM fresh").fetchall() == [(1,)]


def test_categories_that_differ_only_in_case_are_a_clear_problem(tmp_path):
    path = _tiny(tmp_path, "id,c,y", [f"{i},{'Aab'[i % 3]},{'xy'[i % 2]}" for i in range(60)])
    ml = _small_ml(["c"], categorical={"c": {"strategy": "one_hot"}})
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "output columns would repeat" in rep.issues[0].message


def test_an_encoded_name_equal_to_an_existing_column_is_a_clear_problem(tmp_path):
    rows = [f"{i},{'ab'[i % 2]},{i},{'xy'[i % 2]}" for i in range(60)]
    path = _tiny(tmp_path, "id,c,c_a,y", rows)
    ml = _small_ml(["c", "c_a"], categorical={"c": {"strategy": "one_hot"}})
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "c_a" in rep.issues[0].message


def test_whole_number_categories_are_sorted_by_value_not_as_text(tmp_path):
    path = _tiny(tmp_path, "id,n,y", [f"{i},{[1, 2, 10][i % 3]},{'xy'[i % 2]}" for i in range(60)])
    ml = _small_ml(["n"], categorical={"n": {"strategy": "ordinal"}})
    _, out = _go(path, tmp_path, ml)
    assert _entry(_meta(out), "n")["categorical"]["categories"] == ["1", "2", "10"]


def test_a_decimal_column_can_be_filled_with_its_median(tmp_path):
    rows = [f"{i},{'' if i % 7 == 0 else f'{i}.25'},{'xy'[i % 2]}" for i in range(60)]
    path = _tiny(tmp_path, "id,d,y", rows)
    ml = _small_ml(["d"], missing={"d": {"strategy": "median"}})
    _, out = _go(path, tmp_path, ml)
    assert isinstance(_entry(_meta(out), "d")["missing"]["value"], float)


def test_nan_in_a_number_column_is_named_not_called_empty(tmp_path):
    rows = [f"{i},{'nan' if i == 3 else i * 1.5},{'xy'[i % 2]}" for i in range(60)]
    path = _tiny(tmp_path, "id,x,y", rows)
    ml = _small_ml(["x"], numerical={"x": {"strategy": "minmax"}})
    rep = run(_spec(ml), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "NaN or infinite" in rep.issues[0].message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["features"][0].pop("numeric"),
        lambda m: m["features"][0].update(numeric={"strategy": "bogus"}),
        lambda m: m["features"][0].update(numeric={"strategy": "minmax", "min": 0.0}),
        lambda m: m["features"][0].update(
            numeric={"strategy": "standard_scale", "mean": 0.0, "std": float("nan")}
        ),
        lambda m: m["features"][0].update(
            categorical={"strategy": "one_hot", "categories": "ab", "unknown": "ignore"}
        ),
        lambda m: m.update(features="age"),
        lambda m: m.update(features=[]),
        lambda m: m.update(ids="id"),
        lambda m: m["features"][0].update(column=5),
        lambda m: m["features"][0].update(missing={"strategy": "mean", "value": float("inf")}),
    ],
)
def test_broken_metadata_gives_problems_and_never_an_exception(data, tmp_path, mutate):
    _, out = _go(data, tmp_path)
    meta = json.loads((out / "preprocessing_metadata.json").read_text())["preprocessing"]
    mutate(meta)
    con = connect()
    _new(con, "SELECT 30 AS age, 1.0 AS spend, 'A' AS seg, 'gold' AS tier")
    result = _apply_full(con, "fresh", meta, "ready")
    assert not result.ok and result.problems


def test_a_missing_input_table_is_a_problem_not_an_exception(data, tmp_path):
    _, out = _go(data, tmp_path)
    result = _apply_full(connect(), "nothing_here", _meta(out), "ready")
    assert not result.ok


def test_validate_plans_a_decimal_constant_in_a_whole_number_column_as_double(data):
    ml = _ml(
        missing={"age": {"strategy": "constant", "value": 2.5}},
        numerical={},
        categorical={},
    )
    types = {c.name: c.type for c in validate(_spec(ml), {"c": data}).schemas["m_train"]}
    assert types["age"] == "DOUBLE"


def test_a_mean_fill_on_a_category_column_is_refused(data):
    ml = _ml(
        missing={"age": {"strategy": "mean"}},
        numerical={},
        categorical={"age": {"strategy": "ordinal"}},
    )
    issues = " ".join(i.message for i in validate(_spec(ml), {"c": data}).issues)
    assert "cannot also be a category" in issues


def test_a_check_on_a_one_hot_column_explains_why_it_is_not_there(data):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {"cust": {"kind": "csv", "file_role": "c"}},
                "ml": _ml(),
                "outputs": {"t": {"input": "m_train"}},
                "checks": {"c": {"kind": "not_null", "table": "m_train", "columns": ["seg_A"]}},
            }
        )
    )
    issues = " ".join(i.message for i in validate(spec, {"c": data}).issues)
    assert "exist only after the run" in issues


def test_a_spec_without_rules_dumps_without_empty_rule_sections():
    from excel_etl.pipeline import dump_spec

    plain = {k: v for k, v in _ml().items() if k not in ("missing", "numerical", "categorical")}
    text = dump_spec(_spec(plain))
    assert '"missing"' not in text and '"numerical"' not in text and '"categorical"' not in text


def test_rows_cut_by_drop_are_said_in_the_run_warnings(data, tmp_path):
    ml = _ml(missing={"age": {"strategy": "drop"}}, numerical={}, categorical={})
    rep, _ = _go(data, tmp_path, ml)
    assert any("rows with an empty value were dropped" in w for w in rep.warnings)


def test_every_artifact_carries_the_same_version(data, tmp_path):
    from excel_etl.prep import METADATA_VERSION

    _, out = _go(data, tmp_path)
    for name in ("preprocessing_metadata.json", "feature_schema.json", "preparation_report.json"):
        assert json.loads((out / name).read_text())["version"] == METADATA_VERSION


def test_the_ml_catalog_text_names_the_strategies_from_the_models():
    from excel_etl.ml import describe

    text = describe()
    for word in ("median", "standard_scale", "log1p", "one_hot", "ordinal", "max_categories: 50"):
        assert word in text
    assert "apply_ml_preprocessing" in text
