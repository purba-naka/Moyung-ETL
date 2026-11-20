from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import make_xlsx

from excel_etl.engine import run, sha256_file
from excel_etl.pipeline import parse_spec
from excel_etl.state import Job, new_state
from excel_etl.tools import JobTools

CSV_A = b"item,qty\npen,1\nink,2\n"
CSV_B = b"item,qty\npen,1\nink,2\ncap,3\n"


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


def _spec(extra_step: bool = False) -> str:
    spec = {
        "sources": {"s": {"kind": "csv", "file_role": "f"}},
        "outputs": {"o": {"input": "s", "format": "parquet"}},
    }
    if extra_step:
        spec["steps"] = [{"id": "t", "node": "sort", "inputs": ["s"], "config": {"by": ["qty"]}}]
        spec["outputs"] = {"o": {"input": "t", "format": "parquet"}}
    return json.dumps(spec)


@pytest.fixture
def tools(tmp_path):
    t = JobTools(Job(new_state("j1")), Provider({"a": CSV_A, "b": CSV_B}), tmp_path / "out")
    assert "registered" in t.add_file("a", "m.csv", "f")["text"]
    return t


def _approve_and_run(t):
    version = t._version()
    t.approve("after_propose_datasets", "dina", f"v{version}")
    return t.run_pipeline()


def test_each_run_keeps_its_inputs_spec_steps_and_output_hashes(tools, tmp_path):
    assert tools.save_spec(_spec(), "")["saved"]
    assert _approve_and_run(tools)["ok"]
    first = tools.job.state.runs[-1]
    assert first.files == {"f": hashlib.sha256(CSV_A).hexdigest()}
    assert len(first.spec_sha256) == 64 and first.engine_version
    assert [n.id for n in first.nodes] == ["s"] and first.nodes[0].rows_out == 2
    (out,) = first.outputs
    assert out.path == "o.parquet" and out.sha256 == sha256_file(tmp_path / "out" / "o.parquet")

    tools.provider.blobs["a"] = CSV_B
    tools.add_file("a", "m.csv", "f")
    assert tools.run_pipeline()["ok"]
    second = tools.job.state.runs[-1]
    assert second.files != first.files and second.spec_sha256 == first.spec_sha256
    assert second.outputs[0].sha256 != first.outputs[0].sha256
    assert tools.job.state.runs[0].files == first.files


def test_a_changed_spec_changes_the_spec_hash(tools):
    tools.save_spec(_spec(), "")
    _approve_and_run(tools)
    before = tools.job.state.runs[-1].spec_sha256
    tools.save_spec(_spec(extra_step=True), "sorted")
    assert _approve_and_run(tools)["ok"]
    after = tools.job.state.runs[-1]
    assert after.spec_sha256 != before and [n.id for n in after.nodes] == ["s", "t"]


def test_the_state_names_no_folder_of_the_host(tools, tmp_path):
    tools.save_spec(_spec(), "")
    _approve_and_run(tools)
    dumped = tools.job.state.model_dump_json()
    assert str(tmp_path) not in dumped and str(tmp_path).replace("\\", "/") not in dumped
    assert tools.job.state.last_run.outputs[0].path == "o.parquet"


@pytest.mark.parametrize("form", ["path", "str", "bytes"])
def test_a_direct_run_hashes_inputs_given_as_paths_or_bytes(tmp_path, form):
    path = tmp_path / "m.csv"
    path.write_bytes(CSV_A)
    given = {"path": path, "str": str(path), "bytes": CSV_A}[form]
    rep = run(parse_spec(_spec()), {"f": given}, tmp_path / "out")
    assert rep.status == "ok"
    assert rep.files == {"f": hashlib.sha256(CSV_A).hexdigest()}
    assert len(rep.spec_sha256) == 64 and len(rep.outputs[0].sha256) == 64


def test_run_report_round_trips_with_the_new_fields(tmp_path):
    rep = run(parse_spec(_spec()), {"f": CSV_A}, tmp_path / "out")
    assert type(rep).model_validate_json(rep.model_dump_json()) == rep


def test_the_load_plan_refuses_an_output_changed_after_the_run(tmp_path):
    rows = [["Plant", "Ton"], ["A", 1], ["B", 2]]
    t = JobTools(Job(new_state("j")), Provider({"a": make_xlsx({"Data": rows})}), tmp_path / "o")
    t.add_file("a", "jan.xlsx", "f")
    spec = {
        "sources": {
            "t": {
                "file_role": "f",
                "sheet": {"name": "Data"},
                "region": {"header_anchor": ["Plant", "Ton"]},
            }
        },
        "steps": [],
        "outputs": {"clean": {"input": "t", "format": "parquet", "load": {"table": "x"}}},
    }
    assert t.save_spec(json.dumps(spec), "")["saved"]
    t.approve("after_propose_datasets", "dina")
    assert t.run_pipeline()["ok"]
    t.approve("before_load", "dina")
    assert "COPY INTO" in t.plan_load("snowflake", "clean", "DB", "SC", "@s")["text"]
    Path(tmp_path / "o" / "clean.parquet").write_bytes(b"tampered")
    assert "changed on disk" in t.plan_load("snowflake", "clean", "DB", "SC", "@s")["error"]
