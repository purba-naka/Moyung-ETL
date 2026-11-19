from __future__ import annotations

import json

import pytest
from conftest import make_xlsx

from excel_etl.state import Job, add_file, new_state
from excel_etl.tools import JobTools
from excel_etl.workbook import Workbook

FILES = {
    "a": make_xlsx(
        {
            "Orders": [["order_id", "cust", "amt"]]
            + [[i, f"C{i % 5}", i * 1.5] for i in range(30)],
            "Cust": [["code", "name"]] + [[f"C{i}", f"N{i}"] for i in range(5)],
        }
    )
}


class _Provider:
    def open(self, file_id: str) -> bytes:
        return FILES[file_id]


@pytest.fixture
def tools() -> JobTools:
    st = add_file(new_state("j"), "a", "a.xlsx", Workbook.open(FILES["a"]).sha256, "main")
    return JobTools(Job(st), _Provider())


def _sections(text: str) -> list[str]:
    return [line[3:] for line in text.splitlines() if line.startswith("## ")]


def test_discovery_has_job_files_and_workbook_map(tools):
    out = tools.build_context("discovery")
    assert _sections(out["text"]) == ["facts: job", "facts: files", "facts: workbook_map F1"]
    assert "F1.S1|Orders" in out["text"]
    assert out["sections"] == ["job", "files", "workbook_map F1"]
    json.dumps(out)


def test_cleaning_profiles_and_samples_each_table(tools):
    out = tools.build_context("cleaning", refs=["F1.S1.R1"])
    titles = _sections(out["text"])
    assert titles == [
        "facts: job",
        "facts: profile F1.S1.R1",
        "facts: sample F1.S1.R1 head_tail",
    ]
    assert "key candidates: order_id" in out["text"]


def test_join_relates_every_pair_of_tables(tools):
    out = tools.build_context("join", refs=["F1.S1.R1", "F1.S2.R1"])
    assert "facts: relate F1.S1.R1 F1.S2.R1" in _sections(out["text"])
    assert "cust -> code: N:1" in out["text"]


def test_validation_has_spec_and_last_run(tools):
    titles = _sections(tools.build_context("validation")["text"])
    assert titles == ["facts: job", "facts: spec", "facts: last run"]


def test_facts_and_reading_guide_are_separate(tools):
    text = tools.build_context("cleaning", refs=["F1.S1.R1"])["text"]
    first, _, rest = text.partition("## facts:")
    assert first.startswith("# context for cleaning")
    assert "evidence, not decisions" in first
    assert "interpret" not in rest


def test_budget_keeps_higher_priority_sections_and_lists_what_was_left_out(tools):
    full = tools.build_context("join", refs=["F1.S1.R1", "F1.S2.R1"])
    small = tools.build_context("join", refs=["F1.S1.R1", "F1.S2.R1"], budget=900)
    assert len(small["text"]) <= 900
    assert small["sections"] == full["sections"][: len(small["sections"])]
    assert small["left_out"]
    assert "left out (call the tool for them): " in small["text"]
    assert full["left_out"] == []


def test_a_failing_part_is_reported_not_raised(tools):
    out = tools.build_context("cleaning", refs=["F1.S1.R9"])
    assert "facts: profile F1.S1.R9" in _sections(out["text"])
    assert "error: " in out["text"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"task": "train"}, "task"),
        ({"task": "cleaning"}, "refs"),
        ({"task": "join", "refs": ["F1.S1.R1"]}, "two"),
        ({"task": "discovery", "budget": 10}, "budget"),
        ({"task": "cleaning", "refs": "F1.S1.R1"}, "list"),
        ({"task": "cleaning", "refs": [f"F1.S1.R{i}" for i in range(1, 12)]}, "at most"),
    ],
)
def test_bad_arguments_are_errors(tools, kwargs, match):
    out = tools.build_context(**kwargs)
    assert match in out["error"]


def test_build_context_is_host_only(tools):
    assert "build_context" not in [f.__name__ for f in tools.agent_tools()]
