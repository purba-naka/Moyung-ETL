"""HANDOVER.md makes promises about checkpoints and about the snippet it shows. Keep them true."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import make_xlsx

from excel_etl.hitl import HitlPolicy
from excel_etl.state import Job, new_state
from excel_etl.tools import JobTools

TEXT = (Path(__file__).parent.parent / "HANDOVER.md").read_text(encoding="utf-8")
SPEC = {
    "sources": {
        "s": {"file_role": "f", "sheet": {"name": "D"}, "region": {"header_anchor": ["Ton"]}}
    },
    "outputs": {"o": {"input": "s"}},
}


class Files:
    data = make_xlsx({"D": [["Ton", "Unit"], [1, "kg"], [2, "kg"], [3, "t"]]})

    def open(self, file_id):
        return self.data


def job_with(tmp_path, checkpoints=None):
    t = JobTools(Job(new_state("j")), Files(), tmp_path / "out")
    t.add_file("a", "x.xlsx", "f")
    if checkpoints is not None:
        policy = HitlPolicy(checkpoints=checkpoints)
        t.job.update(
            lambda s: s.model_copy(update={"hitl": s.hitl.model_copy(update={"policy": policy})})
        )
    assert t.save_spec(json.dumps(SPEC))["saved"]
    return t


def table_row(checkpoint: str) -> list[str]:
    for line in TEXT.splitlines():
        if line.startswith(f"| `{checkpoint}`"):
            return [c.strip() for c in line.strip("|").split("|")]
    raise AssertionError(f"{checkpoint} is not in the HANDOVER table")


def test_the_default_policy_matches_the_handover_table():
    defaults = HitlPolicy().checkpoints
    wanted = {
        "after_workbook_map": "off",
        "after_propose_datasets": "required",
        "after_spec": "optional",
        "after_run": "required_if_fail",
        "on_rebind_drift": "required",
    }
    for name, words in wanted.items():
        assert table_row(name)[1] == words
        assert defaults[name] == words  # type: ignore[index]


def test_required_propose_datasets_blocks_the_run_until_approved(tmp_path):
    t = job_with(tmp_path)
    assert "after_propose_datasets needs" in t.run_pipeline()["error"]
    t.approve("after_propose_datasets", "dina", "v1")
    assert t.run_pipeline()["ok"]


def test_after_spec_blocks_only_when_the_host_sets_it_to_required(tmp_path):
    t = job_with(tmp_path)  # default: optional
    t.approve("after_propose_datasets", "dina", "v1")
    assert t.run_pipeline()["ok"]
    strict = job_with(
        tmp_path / "x", {"after_propose_datasets": "required", "after_spec": "required"}
    )
    strict.approve("after_propose_datasets", "dina", "v1")
    assert "after_spec needs" in strict.run_pipeline()["error"]
    strict.approve("after_spec", "dina", "v1")
    assert strict.run_pipeline()["ok"]


def test_after_run_only_reminds_and_does_not_block(tmp_path):
    t = job_with(tmp_path, {"after_propose_datasets": "off", "after_run": "required"})
    r = t.run_pipeline()
    assert r["ok"] is True and "after_run needs a person's approval" in r["text"]


def test_workbook_map_checkpoint_is_reserved_and_blocks_nothing(tmp_path):
    t = job_with(tmp_path, {"after_propose_datasets": "off", "after_workbook_map": "required"})
    assert t.run_pipeline()["ok"] is True
    assert "reserved" in " ".join(table_row("after_workbook_map")[2:])


def test_rebind_reminds_about_drift_only_when_there_is_drift(tmp_path):
    t = job_with(tmp_path)
    assert "ask the person" not in t.rebind_sources()["text"]
    Files.data, kept = make_xlsx({"D": [["Other", "Unit"], [1, "kg"], [2, "kg"]]}), Files.data
    try:
        t.add_file("a", "feb.xlsx", "f")
        r = t.rebind_sources()
        assert r["ok"] is False and "ask the person before repairing" in r["text"]
    finally:
        Files.data = kept


def test_the_snippet_that_sets_knowledge_and_policy_runs(tmp_path):
    block = [b for b in re.findall(r"```python\n(.*?)```", TEXT, re.S) if "HitlPolicy" in b][0]
    t = job_with(tmp_path)
    ns = {"tools": t}
    exec(compile(block, "HANDOVER snippet", "exec"), ns)  # noqa: S102 - our own document
    state = t.job.state
    assert "Grafik" in t.lookup_knowledge("grafik")["text"]
    assert state.hitl.policy.checkpoints["after_spec"] == "required"


def test_every_tool_the_handover_names_exists():
    t = JobTools(Job(new_state("j")), Files())
    names = {f.__name__ for f in t.agent_tools()}
    for host in ("add_file", "answer", "approve", "reload_job"):
        assert callable(getattr(t, host)) and host not in names
    assert len(names) == 21 and "21 plain methods" in TEXT


def test_numbers_quoted_in_the_handover_match_the_code():
    import excel_etl.tools as tools
    import excel_etl.workbook as wb

    assert "25 million cells" in TEXT and wb._MAX_CELLS == 25_000_000
    assert "24000 characters" in TEXT and tools._MAX_TEXT == 24_000
