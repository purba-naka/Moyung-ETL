from __future__ import annotations

import json

import pytest

from excel_etl.engine import run
from excel_etl.pipeline import parse_spec
from excel_etl.state import STATE_SCHEMA, Job, StateError, new_state


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
    with pytest.raises(StateError, match="upgrade excel-etl"):
        Job.from_state(json.dumps(data))


def test_a_state_saved_before_schema_version_still_loads():
    data = json.loads(Job(new_state("j")).state_json())
    del data["schema_version"]
    assert Job.from_state(json.dumps(data)).state.schema_version == STATE_SCHEMA
