"""The README snippets that do not need an agent framework must keep working."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import make_xlsx

from excel_etl.state import Job, new_state

README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


def _blocks() -> list[str]:
    return re.findall(r"```python\n(.*?)```", README, re.S)


def _fix(code: str, tmp: Path) -> str:
    # forward slashes: a Windows path with backslashes would be read as escapes in the snippet
    return (
        code.replace("C:/uploads", tmp.as_posix())
        .replace("C:/state", (tmp / "state").as_posix())
        .replace("C:/out/job1", (tmp / "out").as_posix())
    )


def _run(code: str, name: str, ns: dict) -> dict:
    exec(compile(code, name, "exec"), ns)  # noqa: S102 - our own README
    return ns


def test_store_snippet_runs_and_saves_at_once(tmp_path):
    (tmp_path / "{file_id}.xlsx").parent.mkdir(exist_ok=True)
    (tmp_path / "up-17.xlsx").write_bytes(make_xlsx({"S": [["a", "b"], [1, 2]]}))
    ns = _run(_fix(_blocks()[0], tmp_path), "README block 1", {})
    assert "F1|Balance Jan.xlsx|role main" in ns["tools"].list_files()["text"]
    assert (tmp_path / "state" / "job-job1.json").exists()


def test_stateless_snippet_round_trips(tmp_path):
    ns = _run(_fix(_blocks()[0].split("job = Job.open")[0], tmp_path), "README head", {})
    ns["saved_json"] = Job(new_state("job1")).state_json()
    code = _fix(_blocks()[1], tmp_path).replace("...  ", "pass  # ")
    ns = _run(code, "README block 2", ns)
    assert ns["tools"].job.state.job_id == "job1"
    assert Job.from_state(ns["saved_json"]).state.job_id == "job1"


def test_tools_table_matches_the_code():
    from excel_etl.tools import JobTools

    t = JobTools(Job(new_state("j")), object())
    table = README.split("## Tools")[1].split("\n\nA result")[0]
    named = set(re.findall(r"`([a-z_]+)`", table))
    exposed = {f.__name__ for f in t.agent_tools()} | {
        "add_file",
        "answer",
        "approve",
        "reload_job",
    }
    assert named == exposed


def test_the_complete_spec_in_the_readme_parses_and_runs(tmp_path):
    import json

    from excel_etl.engine import run
    from excel_etl.pipeline import parse_spec

    block = re.findall(r"```json\n(.*?)```", README, re.S)[0]
    spec = parse_spec(block)
    book = make_xlsx(
        {
            "Sales": [["Item", "Qty"], ["pen", 2], ["ink", 1], ["pen", 5]],
            "Items": [["Item", "Group"], ["pen", "write"], ["ink", "write"]],
        }
    )
    rep = run(spec, {"main": book}, tmp_path)
    assert rep.status == "ok", rep.text()
    assert [c.name for c in rep.checks] == ["no_lost_qty", "groups_known"]
    assert all(c.passed for c in rep.checks) and json.loads(block)["outputs"]


def test_steps_table_in_the_readme_lists_every_registered_node():
    from excel_etl.nodes import names

    table = README.split("| Group | Steps |")[1].split("`filter` and `derive`")[0]
    named = set(re.findall(r"`([a-z_]+)`", table))
    named.discard("read_region")  # the source reader, not a step
    # a test elsewhere registers a throw-away node; only the nodes that ship count here
    shipped = {n for n in names() if not n.startswith("test_")}
    assert named == shipped


def test_readme_names_where_the_ideas_come_from_with_working_links():
    section = README.split("## Where the ideas come from")[1].split("## Checks before")[0]
    for must in (
        "](https://arxiv.org/abs/2407.09025)",
        "](https://github.com/microsoft/SheetBrain)",
        "](https://github.com/Khushiyant/sheetwise)",
        "](https://github.com/ucbepic/docetl)",
    ):
        assert must in section, must
    assert "Nothing below is a dependency" in section
    import tomllib

    deps = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    names = {d.split(">")[0].split("=")[0].strip() for d in deps["project"]["dependencies"]}
    assert names == {"python-calamine", "openpyxl", "duckdb", "pydantic"}
    for banned in ("sheetwise", "docetl", "SheetBrain"):
        assert banned.lower() not in {n.lower() for n in names}
