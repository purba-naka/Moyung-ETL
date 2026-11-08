"""The skills in `skills/` follow the Agent Skills format and cannot go stale quietly.

A skill that names a tool or a setting that no longer exists would send an agent to a
dead end, so every name and every JSON snippet in them is checked against the code.
"""

from __future__ import annotations

import inspect
import json
import re
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from excel_etl.hitl import CHECKPOINTS
from excel_etl.nodes import build, names
from excel_etl.nodes.base import _REGISTRY
from excel_etl.pipeline import Output, PipelineSpec, RegionRef, Source, Step
from excel_etl.quality import Check
from excel_etl.state import Job, new_state
from excel_etl.tools import JobTools

ROOT = Path(__file__).resolve().parents[1] / "skills"
SKILLS = sorted(p for p in ROOT.iterdir() if p.is_dir())
FILES = [p / "SKILL.md" for p in SKILLS] + sorted(ROOT.glob("*/references/*.md"))
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Top-level `key: value` pairs of the YAML header, and the body. Enough for this format."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    head, body = text[4:].split("\n---\n", 1)
    pairs = {}
    for line in head.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            pairs[key] = value.strip()
    return pairs, body


def _fields(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    """Every setting name of a pydantic model and of the models nested in it."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    out = set(model.model_fields)
    for f in model.model_fields.values():
        for arg in (f.annotation, *getattr(f.annotation, "__args__", ())):
            for inner in (arg, *getattr(arg, "__args__", ())):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    out |= _fields(inner, seen)
    return out


def _choices(model: type[BaseModel]) -> set[str]:
    """The fixed values a setting accepts (`fn`: `last_value`, `how`: `left`, ...)."""
    text = json.dumps(model.model_json_schema())
    return {
        v for m in re.findall(r'"enum": \[([^\]]*)\]', text) for v in re.findall(r'"([^"]+)"', m)
    }


def _check_words() -> set[str]:
    """The `kind` names of the checks (`reconcile`, `not_null`, ...) and their settings."""
    out: set[str] = set()
    for model in typing.get_args(typing.get_args(Check.__value__)[0]):
        out |= _fields(model) | set(typing.get_args(model.model_fields["kind"].annotation))
    return out


def _known_words() -> set[str]:
    tools = JobTools(Job(new_state("j")), None, None)
    words = {f.__name__ for f in tools.agent_tools()} | set(names()) | set(CHECKPOINTS)
    for fn in tools.agent_tools():  # an argument an agent passes, such as `existing_columns`
        words |= {p for p in inspect.signature(fn).parameters if "_" in p}
    for cls in _REGISTRY.values():
        words |= _fields(cls.Config) | _choices(cls.Config)
    for model in (PipelineSpec, Source, Step, Output, RegionRef):
        words |= _fields(model)
    words |= _check_words()
    # SQL functions and wording that are not settings of ours
    host = {"record_load", "add_file", "approve", "answer", "reload_job"}  # named, never given
    return words | host | {"try_strptime", "until_blank_rows", "file_id", "next_offset"}


def _agent_tools() -> list[str]:
    return [f.__name__ for f in JobTools(Job(new_state("j")), None, None).agent_tools()]


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_frontmatter_follows_the_agent_skills_format(skill: Path) -> None:
    meta, body = _frontmatter(skill / "SKILL.md")
    name, desc = meta["name"], meta["description"]
    assert name == skill.name  # the spec: the name is the folder name
    assert len(name) <= 64 and _NAME.match(name)  # no leading, trailing or double hyphen
    assert 40 <= len(desc) <= 1024
    # a plain YAML value breaks at ": " and " #", so a loader would not read it as one string
    assert ": " not in desc and " #" not in desc and not desc.startswith(('"', "'", "|", ">"))
    assert re.search(r"\buse\b", desc.lower()) and "not for" in desc.lower()
    assert len(body.splitlines()) <= 150  # the whole file is loaded into the agent's context


def test_each_skill_sets_a_stop_rule_so_the_agent_asks_instead_of_guessing() -> None:
    for skill in SKILLS:
        body = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert "## Do not" in body and "## Stop and ask when" in body, skill.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_text_has_no_house_style_slips(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    prose = re.sub(r"`[^`\n]*`", "", text)  # a quoted tool output may hold `5->5 rows`
    assert "—" not in text and "->" not in prose and "→" not in text
    assert "BALANSITAS" not in text.upper()  # internal data stays out of anything we ship


def test_every_tool_and_node_a_skill_names_exists() -> None:
    known = _known_words()
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```", "", text, flags=re.S)  # snippets are checked below
        for token in set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", text)):
            assert token in known, (
                f"{path.parent.name}/{path.name}: `{token}` is not a tool, node or setting"
            )


def test_the_skills_together_name_every_agent_tool() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in FILES)
    missing = [t for t in _agent_tools() if f"`{t}" not in text]
    assert missing == [], f"no skill tells the agent about: {missing}"


def _kind(snippet: dict) -> str:
    if "node" in snippet:
        return "step"
    if "file_role" in snippet:
        return "source"
    return "output" if "input" in snippet else "check"


def _snippets() -> list[tuple[str, dict]]:
    out = []
    for path in FILES:
        for block in re.findall(r"```json\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
            out.append((path.name, json.loads(block)))
    return out


def test_the_patterns_file_has_snippets_of_every_kind() -> None:
    assert {_kind(s) for _, s in _snippets()} == {"step", "check", "source", "output"}
    used = {s["node"] for _, s in _snippets() if "node" in s}
    assert {"unpivot", "unpivot_groups", "fill_down", "drop_rows", "window", "group_by"} <= used
    checks = {s["kind"] for _, s in _snippets() if "kind" in s}
    assert checks == {"reconcile", "not_null", "unique", "range", "accepted_values"}


@pytest.mark.parametrize(
    "snippet",
    [s for _, s in _snippets()],
    ids=lambda s: s.get("id") or s.get("kind") or _kind(s),
)
def test_every_json_snippet_is_a_valid_step_check_source_or_output(snippet: dict) -> None:
    if "node" in snippet:
        Step.model_validate(snippet)  # ids, inputs list
        build(snippet["node"], snippet["config"])  # the node's own rules
    elif "file_role" in snippet:
        Source.model_validate(snippet)
    elif "input" in snippet:
        Output.model_validate(snippet)
        assert snippet["load"]["table"]  # the point of an output snippet
    else:
        TypeAdapter(Check).validate_python(snippet)


def test_the_steps_in_the_patterns_file_run_end_to_end() -> None:
    """A snippet that parses can still be wrong. Run the wide-to-long ones on a real table."""
    from excel_etl.nodes.base import connect

    steps = {s["id"]: s for _, s in _snippets() if "node" in s}
    con = connect()
    raw = con.sql(
        "SELECT * FROM (VALUES ('P1', 1.0, 2.0, 3.0, 4.0), ('P2', 5.0, 6.0, 7.0, 8.0)) "
        'v("Plant", "JAN|Ton", "JAN|Rate", "FEB|Ton", "FEB|Rate")'
    )
    long = build("unpivot", steps["long"]["config"]).run([raw])
    out = build("split_header_token", steps["split"]["config"]).run([long])
    assert out.columns == ["Plant", "val", "month", "measure"]
    assert len(out.fetchall()) == 8
    blocks = build("unpivot_groups", steps["blocks"]["config"]).run([raw])
    assert blocks.columns == ["Plant", "month", "Ton", "Rate"]
    assert len(blocks.fetchall()) == 4
    sales = con.sql(
        "SELECT * FROM (VALUES (NULL, 'Total', 9), ('north', 'pen', 4), (NULL, 'ink', 5), "
        '(\'south\', \'Total row\', 1)) v("Area", "Item", "Qty")'
    )
    filled = build("fill_down", steps["filled"]["config"]).run([sales])
    assert [r[0] for r in filled.fetchall()] == [None, "north", "north", "south"]
    kept = build("drop_rows", steps["rows"]["config"]).run([filled])
    assert [r[1] for r in kept.fetchall()] == ["pen", "ink"]
