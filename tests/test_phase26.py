from __future__ import annotations

import json

import pytest

from moyung.engine import run
from moyung.pipeline import parse_spec
from moyung.state import STATE_SCHEMA, Job, StateError, new_state


@pytest.fixture
def files(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("id,v\n1,10\n2,20\n3,30\n")
    b = tmp_path / "b.csv"
    b.write_text("id,w\n1,5\n2,6\n")
    return {"a": a, "b": b}


def _spec(checks):
    return parse_spec(
        json.dumps(
            {
                "sources": {
                    "sa": {"kind": "csv", "file_role": "a"},
                    "sb": {"kind": "csv", "file_role": "b"},
                },
                "outputs": {"out_a": {"input": "sa"}, "out_b": {"input": "sb"}},
                "checks": checks,
            }
        )
    )


def test_a_failed_check_blocks_only_the_output_built_from_that_table(files, tmp_path):
    spec = _spec({"a_big": {"kind": "row_count", "table": "sa", "min": 100}})
    rep = run(spec, files, tmp_path / "out")
    assert rep.status == "partial"
    assert rep.blocked == {"out_a": ("a_big",)}
    assert [o.name for o in rep.outputs] == ["out_b"]
    assert (tmp_path / "out" / "out_b.parquet").exists()
    assert not (tmp_path / "out" / "out_a.parquet").exists()
    assert "NOT written: output 'out_a'" in rep.text()


def test_a_check_on_a_step_blocks_the_outputs_downstream_of_it(files, tmp_path):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "sa": {"kind": "csv", "file_role": "a"},
                    "sb": {"kind": "csv", "file_role": "b"},
                },
                "steps": [
                    {"id": "s1", "node": "filter", "inputs": ["sa"], "config": {"where": "v > 0"}}
                ],
                "outputs": {"o1": {"input": "s1"}, "ob": {"input": "sb"}},
                "checks": {"c": {"kind": "row_count", "table": "sa", "min": 100}},
            }
        )
    )
    rep = run(spec, files, tmp_path / "out")
    assert rep.status == "partial" and set(rep.blocked) == {"o1"}


def test_every_output_blocked_is_a_failed_run_with_no_files(files, tmp_path):
    spec = _spec(
        {
            "a_big": {"kind": "row_count", "table": "sa", "min": 100},
            "b_big": {"kind": "row_count", "table": "sb", "min": 100},
        }
    )
    rep = run(spec, files, tmp_path / "out")
    assert rep.status == "failed" and rep.outputs == () and rep.blocked == {}
    assert {i.where for i in rep.issues} == {"checks.a_big", "checks.b_big"}
    assert not list((tmp_path / "out").glob("*.parquet"))


def test_a_warn_check_blocks_nothing(files, tmp_path):
    spec = _spec({"w": {"kind": "row_count", "table": "sa", "min": 100, "severity": "warn"}})
    assert run(spec, files, tmp_path / "out").status == "ok"


def test_a_partial_run_is_not_ok_for_the_agent_and_is_recorded(files, tmp_path):
    spec = _spec({"a_big": {"kind": "row_count", "table": "sa", "min": 100}})
    rep = run(spec, files, tmp_path / "out")
    summary = rep.summary("2026-01-01T00:00:00+00:00")
    assert summary.status == "partial" and summary.checks_failed == ("a_big",)
    assert list(summary.rows) == ["out_b"]


def test_state_carries_its_schema_version_and_refuses_a_newer_one():
    job = Job(new_state("j"))
    data = json.loads(job.state_json())
    assert data["schema_version"] == STATE_SCHEMA
    data["schema_version"] = STATE_SCHEMA + 1
    with pytest.raises(StateError, match="upgrade moyung"):
        Job.from_state(json.dumps(data))


def test_a_state_saved_before_schema_version_still_loads():
    data = json.loads(Job(new_state("j")).state_json())
    del data["schema_version"]
    assert Job.from_state(json.dumps(data)).state.schema_version == STATE_SCHEMA


class _Paths:
    def __init__(self, files):
        self.files = files

    def open(self, file_id):
        return self.files[file_id]


def _tool_spec(checks):
    return json.dumps(
        {
            "sources": {
                "sa": {"kind": "csv", "file_role": "a"},
                "sb": {"kind": "csv", "file_role": "b"},
            },
            "outputs": {
                "out_a": {"input": "sa", "load": {"table": "ta", "mode": "append"}},
                "out_b": {"input": "sb", "load": {"table": "tb", "mode": "append"}},
            },
            "checks": checks,
        }
    )


@pytest.fixture
def jt(files, tmp_path):
    from moyung.tools import JobTools

    t = JobTools(Job(new_state("j")), _Paths(files), tmp_path / "out")
    t.add_file("a", "a.csv", "a")
    t.add_file("b", "b.csv", "b")
    return t


def _save_and_run(t, checks):
    assert t.save_spec(_tool_spec(checks), "")["saved"]
    t.approve("after_propose_datasets", "dina", f"v{t._version()}")
    return t.run_pipeline()


def test_a_partial_run_through_the_tools(jt):
    res = _save_and_run(jt, {"a_big": {"kind": "row_count", "table": "sa", "min": 100}})
    assert res["ok"] is False
    assert "partial: 1 output(s) written, 1 held back" in res["text"]
    assert "You cannot approve this yourself" in res["text"]
    s = Job.from_state(jt.job.state_json()).state
    assert s.last_run.status == "partial" and s.runs[-1].blocked == ("out_a",)
    assert "partial" in jt.last_run()["text"]


def test_plan_load_names_the_check_for_a_held_back_output(jt):
    _save_and_run(jt, {"a_big": {"kind": "row_count", "table": "sa", "min": 100}})
    err = jt.plan_load("snowflake", "out_a", "db", "sc", "@st")["error"]
    assert "failed check(s) a_big" in err


def test_plan_load_of_a_written_output_of_a_partial_run_needs_the_person(jt):
    _save_and_run(jt, {"a_big": {"kind": "row_count", "table": "sa", "min": 100}})
    err = jt.plan_load("snowflake", "out_b", "db", "sc", "@st")["error"]
    assert "partial" in err and "ask_human" in err
    jt.approve("after_run", "dina", f"v{jt._version()}")
    jt.approve("before_load", "dina", f"v{jt._version()}")
    assert "out_b" in jt.plan_load("snowflake", "out_b", "db", "sc", "@st")["text"]


def test_a_held_back_output_from_an_earlier_run_is_removed(files, tmp_path):
    out = tmp_path / "out"
    assert run(_spec({}), files, out).status == "ok"
    assert (out / "out_a.parquet").exists()
    rep = run(_spec({"a_big": {"kind": "row_count", "table": "sa", "min": 100}}), files, out)
    assert rep.status == "partial"
    assert not (out / "out_a.parquet").exists() and (out / "out_b.parquet").exists()
    assert any("removed the files of an earlier run: out_a.parquet" in w for w in rep.warnings)


def test_verify_lists_columns_only_a_held_back_output_keeps(files, tmp_path):
    rep = run(_spec({"a_big": {"kind": "row_count", "table": "sa", "min": 100}}), files, tmp_path)
    assert rep.verify is not None and rep.verify.status == "explained"
    assert any("held back" in e for e in rep.verify.excluded)


def test_a_check_no_output_reads_fails_the_run(files, tmp_path):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "sa": {"kind": "csv", "file_role": "a"},
                    "sb": {"kind": "csv", "file_role": "b"},
                },
                "outputs": {"out_b": {"input": "sb"}},
                "checks": {"c": {"kind": "row_count", "table": "sa", "min": 100}},
            }
        )
    )
    assert run(spec, files, tmp_path / "out").status == "failed"


def test_a_failed_run_names_the_files_an_earlier_run_left(files, tmp_path):
    out = tmp_path / "out"
    assert run(_spec({}), files, out).status == "ok"
    files["a"].unlink()
    rep = run(_spec({}), files, out)
    assert rep.status == "failed" and rep.outputs == ()
    assert (out / "out_a.parquet").exists()
    assert any(
        "out_a.parquet, out_b.parquet in the out_dir are from an earlier run" in w
        for w in rep.warnings
    )


def test_a_first_failed_run_has_nothing_to_warn_about(files, tmp_path):
    files["a"].unlink()
    rep = run(_spec({}), files, tmp_path / "out")
    assert rep.status == "failed"
    assert not any("earlier run" in w for w in rep.warnings)
