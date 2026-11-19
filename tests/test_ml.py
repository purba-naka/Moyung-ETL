from __future__ import annotations

import json

import duckdb
import pytest

from excel_etl.engine import run, validate
from excel_etl.pipeline import SpecError, dump_spec, parse_spec, patch_spec

ROWS = 100


@pytest.fixture
def data(tmp_path):
    lines = ["id,cust,age,spend,seg,churn,day"]
    for i in range(ROWS):
        label = "yes" if i % 4 == 0 else "no"
        lines.append(
            f"{i},c{i % 20},{20 + i % 40},{10.5 + i},{'AB'[i % 2]},{label},2026-01-{1 + i % 28:02d}"
        )
    path = tmp_path / "c.csv"
    path.write_bytes(("\n".join(lines) + "\n").encode())
    return path


def _ml(**over):
    base = {
        "name": "churn",
        "input": "cust",
        "target": "churn",
        "features": ["age", "spend", "seg", "day"],
        "ids": ["id", "cust"],
        "split": {"strategy": "random", "test_size": 0.2, "validation_size": 0.1, "seed": 42},
    }
    return {**base, **over}


def _spec(ml=None, outputs=None, **extra):
    spec = {
        "sources": {"cust": {"kind": "csv", "file_role": "c"}},
        "ml": ml if ml is not None else _ml(),
        "outputs": outputs
        if outputs is not None
        else {
            "train": {"input": "churn_train"},
            "validation": {"input": "churn_validation"},
            "test": {"input": "churn_test"},
        },
        **extra,
    }
    return parse_spec(json.dumps(spec))


def _split(**fields):
    return _ml(split={"strategy": "random", "test_size": 0.2, "seed": 1, **fields})


def _read(path, sql="*"):
    return duckdb.connect().execute(f"SELECT {sql} FROM '{path.as_posix()}'").fetchall()


@pytest.mark.parametrize(
    "ml",
    [
        _split(strategy="time"),
        _split(by="age"),
        _split(strategy="stratified", by="churn"),
        _split(strategy="group"),
        _split(test_size=0.7, validation_size=0.3),
        _split(test_size=0),
        _ml(features=[]),
        _ml(features=["age", "AGE"]),
        _ml(exclude=["age"]),
        _ml(target="id", ids=["id"]),
        _ml(name="bad name"),
        _ml(split={"strategy": "random", "test_size": 0.2}),
        _ml(split={"strategy": "bogus", "test_size": 0.2, "seed": 1}),
        {**_ml(), "extra": 1},
    ],
)
def test_a_bad_ml_declaration_is_refused_when_the_spec_is_read(ml):
    with pytest.raises(SpecError):
        _spec(ml=ml, outputs={})


@pytest.mark.parametrize(
    ("ml", "outputs", "message"),
    [
        (_ml(input="nope"), {}, "ml reads 'nope'"),
        (_ml(), {"o": {"input": "churn_extra"}}, "not defined"),
        (
            _ml(split={"strategy": "random", "test_size": 0.2, "seed": 1}),
            {"o": {"input": "churn_validation"}},
            "not defined",
        ),
    ],
)
def test_ml_tables_are_part_of_the_spec_wiring(ml, outputs, message):
    with pytest.raises(SpecError, match=message):
        _spec(ml=ml, outputs=outputs)


def test_ml_tables_can_be_read_by_outputs_and_checks():
    spec = _spec(checks={"enough": {"kind": "row_count", "table": "churn_train", "min": 1}})
    assert set(spec.ml.tables()) == {"train", "validation", "test"}
    no_validation = _spec(ml=_split(), outputs={"t": {"input": "churn_test"}})
    assert set(no_validation.ml.tables()) == {"train", "test"}


def test_the_ml_section_round_trips_and_is_left_out_when_absent():
    spec = _spec()
    assert parse_spec(dump_spec(spec)) == spec
    plain = parse_spec(json.dumps({"sources": {}, "outputs": {}}))
    assert "ml" not in dump_spec(plain)


def test_edit_spec_can_set_and_remove_the_ml_section():
    plain = parse_spec(
        json.dumps({"sources": {"cust": {"kind": "csv", "file_role": "c"}}, "outputs": {}})
    )
    added = patch_spec(plain, json.dumps({"ml": _ml()}))
    assert added.ml is not None and added.ml.name == "churn"
    assert patch_spec(added, json.dumps({"ml": None})).ml is None


def test_validate_checks_the_declaration_against_the_columns(data):
    def issues(ml):
        return " ".join(i.message for i in validate(_spec(ml=ml, outputs={}), {"c": data}).issues)

    assert "columns not placed: day" in issues(_ml(features=["age", "spend", "seg"]))
    assert "'nope', which 'cust' does not have" in issues(_ml(features=["age", "nope", "day"]))
    assert "needs a category target" in issues(
        _ml(
            target="spend",
            features=["age", "seg", "day"],
            split={"strategy": "stratified", "test_size": 0.2, "seed": 1},
        )
    )
    assert "time split needs a date or number" in issues(
        _ml(split={"strategy": "time", "by": "seg", "test_size": 0.2, "seed": 1})
    )
    assert issues(_ml()) == ""


def test_validate_gives_the_partition_tables_the_declared_columns(data):
    report = validate(_spec(), {"c": data})
    assert report.ok
    names = [c.name for c in report.schemas["churn_train"]]
    assert names == ["id", "cust", "age", "spend", "seg", "day", "churn"]
    assert report.schemas["churn_test"] == report.schemas["churn_validation"]


def _ids(path):
    return [r[0] for r in _read(path, "id")]


def test_random_split_is_complete_disjoint_and_sized(data, tmp_path):
    rep = run(_spec(), {"c": data}, tmp_path / "out")
    assert rep.status == "ok", rep.issues
    parts = {n: _ids(tmp_path / "out" / f"{n}.parquet") for n in ("train", "validation", "test")}
    assert sorted(sum(parts.values(), [])) == list(range(ROWS))
    assert {n: len(v) for n, v in parts.items()} == {"train": 70, "validation": 10, "test": 20}


def test_the_same_seed_gives_the_same_files_and_another_seed_another_cut(data, tmp_path):
    first, again, other = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    run(_spec(), {"c": data}, first)
    run(_spec(), {"c": data}, again)
    reseeded = _ml(
        split={"strategy": "random", "test_size": 0.2, "validation_size": 0.1, "seed": 7}
    )
    run(_spec(ml=reseeded), {"c": data}, other)
    for name in sorted(p.name for p in first.iterdir()):
        assert (first / name).read_bytes() == (again / name).read_bytes(), name
    assert _ids(first / "test.parquet") != _ids(other / "test.parquet")


def test_stratified_split_keeps_the_class_balance(data, tmp_path):
    ml = _ml(split={"strategy": "stratified", "test_size": 0.2, "validation_size": 0.1, "seed": 3})
    run(_spec(ml=ml), {"c": data}, tmp_path / "out")
    for part, size in (("train", 70), ("validation", 10), ("test", 20)):
        yes = [r for r in _read(tmp_path / "out" / f"{part}.parquet", "churn") if r[0] is True]
        assert abs(len(yes) / size - 0.25) <= 0.05, part


def test_time_split_puts_the_newest_rows_in_test(data, tmp_path):
    ml = _ml(split={"strategy": "time", "by": "day", "test_size": 0.2, "seed": 1})
    run(
        _spec(ml=ml, outputs={"tr": {"input": "churn_train"}, "te": {"input": "churn_test"}}),
        {"c": data},
        tmp_path / "out",
    )
    newest_train = _read(tmp_path / "out" / "tr.parquet", "max(day)")[0][0]
    oldest_test = _read(tmp_path / "out" / "te.parquet", "min(day)")[0][0]
    assert newest_train <= oldest_test


def test_group_split_never_spans_a_group_over_two_partitions(data, tmp_path):
    ml = _ml(
        split={
            "strategy": "group",
            "by": "cust",
            "test_size": 0.2,
            "validation_size": 0.1,
            "seed": 7,
        }
    )
    run(_spec(ml=ml), {"c": data}, tmp_path / "out")
    groups = {
        n: {r[0] for r in _read(tmp_path / "out" / f"{n}.parquet", "cust")}
        for n in ("train", "validation", "test")
    }
    assert not (groups["train"] & groups["test"]) and not (groups["train"] & groups["validation"])
    assert not (groups["validation"] & groups["test"])


def test_a_null_where_the_split_needs_a_value_is_an_issue(tmp_path):
    path = tmp_path / "n.csv"
    path.write_bytes(b"id,day,y\n1,2026-01-01,a\n2,,b\n3,2026-01-03,a\n4,2026-01-04,b\n")
    ml = {
        "name": "d",
        "input": "cust",
        "target": "y",
        "features": ["day"],
        "ids": ["id"],
        "split": {"strategy": "time", "by": "day", "test_size": 0.25, "seed": 1},
    }
    rep = run(_spec(ml=ml, outputs={"o": {"input": "d_train"}}), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "empty value" in rep.issues[0].message
    assert not (tmp_path / "out" / "o.parquet").exists()


def test_a_split_that_leaves_a_partition_empty_is_an_issue(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(b"id,y\n1,a\n")
    ml = {
        "name": "d",
        "input": "cust",
        "target": "y",
        "features": ["id"],
        "split": {"strategy": "random", "test_size": 0.01, "seed": 1},
    }
    rep = run(_spec(ml=ml, outputs={}), {"c": path}, tmp_path / "out")
    assert rep.status == "failed" and "leaves" in rep.issues[0].message


def test_the_run_writes_three_deterministic_artifacts(data, tmp_path):
    rep = run(_spec(), {"c": data}, tmp_path / "out")
    names = sorted(a.rsplit("\\", 1)[-1] for a in rep.artifacts)
    assert names == [
        "feature_schema.json",
        "preparation_report.json",
        "preprocessing_metadata.json",
    ]
    assert "preprocessing_metadata.json|artifact" in rep.text()
    schema = json.loads((tmp_path / "out" / "feature_schema.json").read_text())
    roles = {c["name"]: c["role"] for c in schema["columns"]}
    assert roles == {
        "id": "id",
        "cust": "id",
        "age": "feature",
        "spend": "feature",
        "seg": "feature",
        "day": "feature",
        "churn": "target",
    }
    meta = json.loads((tmp_path / "out" / "preprocessing_metadata.json").read_text())
    assert meta["split"]["seed"] == 42 and meta["split"]["strategy"] == "random"
    report = json.loads((tmp_path / "out" / "preparation_report.json").read_text())
    assert report["rows_in"] == ROWS and report["partitions"]["train"] == 70


def test_a_failed_run_writes_no_artifact(data, tmp_path):
    spec = _spec(checks={"never": {"kind": "row_count", "table": "churn_train", "min": 1000}})
    rep = run(spec, {"c": data}, tmp_path / "out")
    assert rep.status == "failed" and rep.artifacts == ()
    assert not list((tmp_path / "out").glob("*.json*"))


def test_ml_exclude_counts_as_a_deliberate_drop_in_verify(data, tmp_path):
    ml = _ml(features=["age", "seg", "day"], exclude=["spend"])
    rep = run(_spec(ml=ml), {"c": data}, tmp_path / "out")
    assert rep.status == "ok" and rep.verify is not None
    assert rep.verify.status != "unexplained"
    assert any("spend" in e and "ml declaration" in e for e in rep.verify.excluded)
    report = json.loads((tmp_path / "out" / "preparation_report.json").read_text())
    assert report["dropped_columns"] == ["spend"]


def test_an_ml_table_name_cannot_reuse_a_source_or_step_name():
    sources = {
        "cust": {"kind": "csv", "file_role": "c"},
        "churn_train": {"kind": "csv", "file_role": "c"},
    }
    with pytest.raises(SpecError, match="same name as a source or step"):
        _spec(sources=sources, outputs={})


def _csv(tmp_path, text):
    path = tmp_path / "x.csv"
    path.write_bytes(text.encode())
    return path


def _plain(features, split, columns, target="y", **over):
    return {
        "name": "d",
        "input": "cust",
        "target": target,
        "features": features,
        "split": split,
        **over,
    }


@pytest.mark.parametrize("helper", ["part", "u", "g", "row"])
def test_a_column_named_like_an_internal_helper_does_not_break_any_split(tmp_path, helper):
    lines = [f"k,{helper},y"] + [f"k{i % 10},{i},{'ab'[i % 2]}" for i in range(60)]
    path = _csv(tmp_path, "\n".join(lines) + "\n")
    for strategy, by in (("random", None), ("stratified", None), ("time", helper), ("group", "k")):
        split = {"strategy": strategy, "test_size": 0.2, "validation_size": 0.1, "seed": 1}
        if by:
            split["by"] = by
        feature = [helper] if by != helper else []
        ml = _plain(feature + ["k"] if by != "k" else feature, split, None)
        if by == helper:
            ml["ids"] = [helper]
            ml["features"] = ["k"]
        if by == "k":
            ml["features"] = [helper]
            ml["ids"] = ["k"]
        spec = _spec(ml=ml, outputs={"t": {"input": "d_train"}})
        rep = run(spec, {"c": path}, tmp_path / f"o_{helper}_{strategy}")
        assert rep.status == "ok", (strategy, rep.issues)


def test_a_time_split_never_cuts_through_rows_that_share_a_timestamp(tmp_path):
    lines = ["id,day,y"] + [f"{i},2026-01-{1 + i // 4:02d},a" for i in range(20)]
    path = _csv(tmp_path, "\n".join(lines) + "\n")
    split = {"strategy": "time", "by": "day", "test_size": 0.25, "seed": 1}
    ml = {
        "name": "d",
        "input": "cust",
        "target": "y",
        "features": ["day"],
        "ids": ["id"],
        "split": split,
    }
    outs = {"tr": {"input": "d_train"}, "te": {"input": "d_test"}}
    rep = run(_spec(ml=ml, outputs=outs), {"c": path}, tmp_path / "out")
    assert rep.status == "ok", rep.issues
    train = {r[0] for r in _read(tmp_path / "out" / "tr.parquet", "day")}
    test = {r[0] for r in _read(tmp_path / "out" / "te.parquet", "day")}
    assert not (train & test)


def test_a_one_row_class_lands_in_train_and_a_missing_class_is_warned_about(tmp_path):
    lines = ["id,y"] + [f"{i},a" for i in range(19)] + ["19,b"]
    path = _csv(tmp_path, "\n".join(lines) + "\n")
    split = {"strategy": "stratified", "test_size": 0.25, "seed": 1}
    ml = {"name": "d", "input": "cust", "target": "y", "features": ["id"], "split": split}
    outs = {"tr": {"input": "d_train"}}
    rep = run(_spec(ml=ml, outputs=outs), {"c": path}, tmp_path / "out")
    assert rep.status == "ok"
    assert [r[0] for r in _read(tmp_path / "out" / "tr.parquet", "y") if r[0] == "b"] == ["b"]
    assert not any("not in train" in w for w in rep.warnings)
    only_test = {"strategy": "stratified", "test_size": 0.9, "seed": 1}
    ml = {"name": "d", "input": "cust", "target": "y", "features": ["id"], "split": only_test}
    rep = run(_spec(ml=ml, outputs=outs), {"c": path}, tmp_path / "out2")
    assert any("not in train" in w for w in rep.warnings)


def test_the_target_in_the_schema_file_uses_the_input_casing(tmp_path):
    path = _csv(tmp_path, "ID,Y\n1,a\n2,b\n3,a\n4,b\n5,a\n6,b\n7,a\n8,b\n9,a\n10,b\n")
    ml = {
        "name": "d",
        "input": "cust",
        "target": "y",
        "features": ["id"],
        "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
    }
    run(_spec(ml=ml, outputs={}), {"c": path}, tmp_path / "out")
    schema = json.loads((tmp_path / "out" / "feature_schema.json").read_text())
    assert schema["target"] == "Y" and [c["name"] for c in schema["columns"]] == ["ID", "Y"]


@pytest.mark.parametrize("seed", [True, "7", 1.5])
def test_the_seed_must_be_a_real_integer(seed):
    ml = _ml(split={"strategy": "random", "test_size": 0.2, "seed": seed})
    with pytest.raises(SpecError):
        _spec(ml=ml, outputs={})


def test_columns_that_differ_only_in_case_are_refused_by_the_column_check():
    from excel_etl.ml import MlSpec, check_columns
    from excel_etl.nodes.base import Column

    schema = (Column("a", "BIGINT"), Column("A", "BIGINT"), Column("y", "VARCHAR"))
    ml = MlSpec.model_validate(_ml(target="y", features=["a"], ids=[], split=_split()["split"]))
    assert "differ only in letter case" in check_columns(ml, schema)[0]


def test_ml_exclude_only_explains_the_sources_behind_the_ml_input(tmp_path):
    other = tmp_path / "o.csv"
    other.write_bytes(b"k,spend\n1,5\n2,6\n")
    main = _csv(
        tmp_path,
        "k,spend,y\n1,1,a\n2,2,b\n3,3,a\n4,4,b\n5,5,a\n6,6,b\n7,7,a\n8,8,b\n9,9,a\n10,10,b\n",
    )
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "cust": {"kind": "csv", "file_role": "c"},
                    "other": {"kind": "csv", "file_role": "o"},
                },
                "ml": {
                    "name": "d",
                    "input": "cust",
                    "target": "y",
                    "features": ["k"],
                    "exclude": ["spend"],
                    "split": {"strategy": "random", "test_size": 0.2, "seed": 1},
                },
                "outputs": {"t": {"input": "d_train"}},
            }
        )
    )
    rep = run(spec, {"c": main, "o": other}, tmp_path / "out")
    assert rep.verify is not None
    assert any(e.startswith("cust.spend") for e in rep.verify.excluded)
    assert not any(e.startswith("other.spend") for e in rep.verify.excluded)
    assert any("other.spend" in lost for lost in rep.verify.lost)


def test_agents_can_find_the_ml_section_from_the_tools(tmp_path):
    from excel_etl.state import Job, new_state
    from excel_etl.tools import JobTools

    t = JobTools(Job(new_state("j")), None, tmp_path)
    detail = t.node_catalog("ml")["text"]
    assert "seed*" in detail and "validation_size" in detail and "example:" in detail
    assert "\nml:" in "\n" + t.node_catalog()["text"]
    assert "ml (optional)" in JobTools.save_spec.__doc__
    assert '"ml"' in JobTools.edit_spec.__doc__


def test_the_documented_ml_example_is_a_valid_declaration():
    from excel_etl.ml import EXAMPLE, MlSpec

    assert MlSpec.model_validate(EXAMPLE).name == "churn"


def test_a_typo_in_the_ml_section_lists_what_it_takes():
    with pytest.raises(SpecError, match="takes name, input, target, features"):
        _spec(ml={**_ml(), "feature": ["age"]}, outputs={})


def test_the_missing_validation_table_is_explained():
    ml = _ml(split={"strategy": "random", "test_size": 0.2, "seed": 1})
    with pytest.raises(SpecError, match="validation table exists only when"):
        _spec(ml=ml, outputs={"v": {"input": "churn_validation"}})


def test_the_run_text_says_what_each_artifact_holds(data, tmp_path):
    text = run(_spec(), {"c": data}, tmp_path / "out").text()
    assert "feature_schema.json|artifact: each column's type and role" in text


def test_the_target_class_balance_is_reported_for_a_random_split_too(data, tmp_path):
    run(_spec(), {"c": data}, tmp_path / "out")
    report = json.loads((tmp_path / "out" / "preparation_report.json").read_text())
    assert set(report["target_classes"]) == {"train", "validation", "test"}
