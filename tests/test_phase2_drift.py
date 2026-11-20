from __future__ import annotations

import json
import re

import pytest
from conftest import make_xlsx

from moyung.engine import run, validate
from moyung.load import LoadError, Where, _stage_path
from moyung.pipeline import parse_spec
from moyung.rebind import rebind
from moyung.state import Job, new_state
from moyung.tools import JobTools

GOOD = [["Item", "Qty"], ["pen", 1], ["ink", 2]]
SPEC = {
    "sources": {
        "sales": {
            "file_role": "f",
            "sheet": {"name": "Sales"},
            "region": {"header_anchor": ["Item", "Qty"]},
        }
    },
    "outputs": {"o": {"input": "sales"}},
}


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


@pytest.fixture
def blobs():
    return {"a": make_xlsx({"Sales": GOOD})}


@pytest.fixture
def tools(blobs, tmp_path):
    t = JobTools(Job(new_state("j1")), Provider(blobs), tmp_path / "out")
    t.add_file("a", "jan.xlsx", "f")
    assert t.save_spec(json.dumps(SPEC), "")["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    return t


def _swap(tools, blobs, rows):
    blobs["a"] = make_xlsx({"Sales": rows})
    tools.add_file("a", "feb.xlsx", "f")


def _ref(error: str) -> str:
    return re.search(r"ref='(drift:[0-9a-f]+)'", error)[1]


def test_a_drifted_source_refuses_the_run_until_that_drift_is_approved(tools, blobs):
    assert tools.rebind_sources()["ok"] and tools.run_pipeline()["ok"]
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"], ["ink", 1]])
    refused = tools.run_pipeline()["error"]
    assert "so nothing was run" in refused and "Qty" in refused
    ref = _ref(refused)
    tools.approve("on_rebind_drift", "dina", ref)
    done = tools.run_pipeline()
    assert done["ok"] and "ran on the approved drift" in done["text"]
    assert tools.rebind_sources()["ok"]


def test_an_approval_for_the_spec_version_does_not_cover_a_drift(tools, blobs):
    tools.rebind_sources()
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    tools.approve("on_rebind_drift", "dina")
    assert "approved for 'v1'" in tools.run_pipeline()["error"]


def test_an_approved_drift_does_not_cover_a_different_one(tools, blobs):
    tools.rebind_sources()
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    first = _ref(tools.run_pipeline()["error"])
    tools.approve("on_rebind_drift", "dina", first)
    assert tools.run_pipeline()["ok"]
    _swap(tools, blobs, [["Item", "Qty"], ["pen", 5]])
    second = _ref(tools.run_pipeline()["error"])
    assert second != first


def test_without_a_baseline_a_broken_contract_still_fails_the_run(tools, blobs):
    assert not tools.job.state.binds
    _swap(tools, blobs, [["Item", "Price"], ["pen", 1]])
    res = tools.run_pipeline()
    assert not res["ok"] and "header ['Item', 'Qty'] not found" in res["text"]


def test_a_clean_run_needs_no_drift_approval_and_records_the_baseline(tools):
    assert tools.run_pipeline()["ok"]
    assert "sales" in tools.job.state.binds
    assert tools.job.state.binds["sales"].rows == 2


def _multi(sheets: dict[str, list]):
    spec = {
        "sources": {
            "s": {
                "file_role": "f",
                "sheet": {"name_regex": "^20[0-9][0-9]$", "sheet_column": "yr"},
                "region": {"header_anchor": ["Item", "Qty"]},
            }
        },
        "outputs": {"o": {"input": "s"}},
    }
    return parse_spec(json.dumps(spec)), {"f": make_xlsx(sheets)}


def test_a_sheet_that_goes_is_drift_not_exact():
    spec, files = _multi({"2023": GOOD, "2024": GOOD})
    first = rebind(spec, files)
    assert first.sources["s"].status == "exact"
    gone = rebind(spec, _multi({"2023": GOOD})[1], first.records())
    assert gone.sources["s"].status == "drift" and "2024" in gone.sources["s"].diff[0]


def test_a_sheet_that_loses_a_column_the_other_sheets_keep_is_drift():
    wide = [["Item", "Qty", "Z"], ["a", 1, 2]]
    spec, files = _multi({"2023": wide, "2024": wide})
    first = rebind(spec, files)
    lost = rebind(spec, _multi({"2023": wide, "2024": GOOD})[1], first.records())
    assert lost.sources["s"].status == "drift"
    assert any("sheet '2024' lost column" in line for line in lost.sources["s"].diff)


def test_an_empty_table_after_a_good_bind_is_drift_and_a_big_drop_is_reported():
    spec, files = _multi({"2023": [["Item", "Qty"], *[[f"i{n}", n] for n in range(40)]]})
    first = rebind(spec, files)
    small = [["Item", "Qty"], *[[f"i{n}", n] for n in range(5)]]
    dropped = rebind(spec, _multi({"2023": small})[1], first.records())
    assert (
        dropped.sources["s"].status == "shifted"
        and "row count fell from 40 to 5" in dropped.sources["s"].diff[0]
    )
    empty = rebind(spec, _multi({"2023": [["Item", "Qty"]]})[1], first.records())
    assert empty.sources["s"].status == "drift"


def test_a_spec_changed_after_parsing_cannot_hide_a_cycle_or_a_missing_input(tmp_path):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {"s": {"kind": "csv", "file_role": "f"}},
                "steps": [{"id": "a", "node": "sort", "inputs": ["s"], "config": {"by": ["x"]}}],
                "outputs": {"o": {"input": "a"}},
            }
        )
    )
    spec.steps[0].inputs = ["b"]
    spec.steps.append(spec.steps[0].model_copy(update={"id": "b", "inputs": ["a"]}))
    rep = validate(spec, {"f": b"x\n1\n"})
    assert not rep.ok and any("not a source or an earlier step" in i.message for i in rep.issues)
    assert run(spec, {"f": b"x\n1\n"}, tmp_path / "out").status == "failed"


def test_a_step_that_fails_says_which_later_steps_were_skipped(tmp_path):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {"s": {"kind": "csv", "file_role": "f"}},
                "steps": [
                    {"id": "a", "node": "sort", "inputs": ["s"], "config": {"by": ["nope"]}},
                    {"id": "b", "node": "sort", "inputs": ["a"], "config": {"by": ["x"]}},
                ],
                "outputs": {"o": {"input": "b"}},
            }
        )
    )
    rep = run(spec, {"f": b"x\n1\n"}, tmp_path / "out")
    assert rep.status == "failed"
    assert any("steps.b: skipped because 'a' did not come out" in w for w in rep.warnings)


def test_a_spec_with_no_outputs_says_so(tmp_path):
    spec = parse_spec(json.dumps({"sources": {"s": {"kind": "csv", "file_role": "f"}}}))
    rep = run(spec, {"f": b"x\n1\n"}, tmp_path / "out")
    assert rep.status == "ok" and any("no outputs" in w for w in rep.warnings)


@pytest.mark.parametrize("stage", ["@s/--x", "@s//x", "@s/../x", "@s/a--b"])
def test_a_stage_with_comment_or_parent_segments_is_refused(stage):
    where = Where(database="D", schema_name="S", stage=stage)
    with pytest.raises(LoadError, match="may not hold"):
        _stage_path(where, "x.parquet")
