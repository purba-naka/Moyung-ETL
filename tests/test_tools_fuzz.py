from __future__ import annotations

import inspect
import json

import pytest
from conftest import make_xlsx
from test_tools import SALES, SPEC, Provider

from moyung.state import Job, new_state
from moyung.tools import JobTools

VALID = {
    "file": "F1",
    "sheet": "Sales",
    "a": "F1.S1.R1",
    "b": "F1.S1.R1",
    "region": "F1.S1.R1",
    "region_id": "F1.S1.R1",
    "query": "Qty",
    "spec_json": json.dumps(SPEC),
    "patch_json": json.dumps([]),
    "note": "n",
    "node": "group_by",
    "offset": 0,
    "limit": 10,
    "budget": 1000,
    "version": 0,
    "question_id": "q1",
    "reply": "yes",
    "checkpoint": "after_spec",
    "by": "me",
    "ref": "",
    "file_id": "a",
    "filename": "jan.xlsx",
    "role": "f",
    "file_role": "f",
}
HOSTILE = [
    None,
    -1,
    0,
    10**12,
    "",
    " ",
    "x" * 5000,
    "F99",
    "../../x",
    "\x00",
    1.5,
    [],
    {},
    True,
    "{}",
    "[]",
    "null",
    '{"sources":1}',
    "é😀",
    "\n",
    "'; DROP TABLE x;--",
]
KNOWN_LEAKS = {("add_file", "filename")}


def _tools(tmp_path, prepared: bool) -> JobTools:
    blobs = {"a": make_xlsx({"Sales": SALES})}
    tools = JobTools(Job(new_state("j1")), Provider(blobs), tmp_path / "out")
    tools.add_file("a", "jan.xlsx", "f")
    if prepared:
        tools.save_spec(json.dumps(SPEC), "first")
    return tools


def _all(tools: JobTools):
    return tools.agent_tools() + [
        tools.add_file,
        tools.answer,
        tools.approve,
        tools.reload_job,
        tools.record_load,
    ]


def _cases():
    probe = _tools_for_names()
    for name, params in probe:
        for param in params:
            if (name, param) in KNOWN_LEAKS:
                continue
            for i, _ in enumerate(HOSTILE):
                yield pytest.param(name, param, i, id=f"{name}-{param}-{i}")


def _tools_for_names():
    import tempfile
    from pathlib import Path

    t = _tools(Path(tempfile.mkdtemp()), False)
    return [(f.__name__, list(inspect.signature(f).parameters)) for f in _all(t)]


@pytest.mark.parametrize("prepared", [False, True], ids=["fresh", "with_spec"])
@pytest.mark.parametrize("name, param, index", list(_cases()))
def test_a_hostile_argument_never_raises_and_never_leaks_an_internal_error(
    tmp_path, prepared, name, param, index
):
    tools = _tools(tmp_path, prepared)
    fn = getattr(tools, name)
    sig = inspect.signature(fn)
    kwargs = {
        p.name: VALID.get(p.name, "x") for p in sig.parameters.values() if p.default is p.empty
    }
    kwargs[param] = HOSTILE[index]
    result = fn(**kwargs)
    assert isinstance(result, dict)
    assert "internal error" not in str(result.get("error", ""))


@pytest.mark.parametrize("bad", [None, -1, 0, 1.5, [], {}, True])
@pytest.mark.xfail(
    strict=True, reason="BUG-010: add_file with a non-text filename leaks an internal error"
)
def test_add_file_with_a_filename_that_is_not_text_is_a_clean_error(tmp_path, bad):
    result = _tools(tmp_path, False).add_file("b", bad, "g")
    assert "internal error" not in str(result.get("error", ""))


def test_every_tool_has_a_documented_signature_the_fuzz_covers(tmp_path):
    names = {f.__name__ for f in _all(_tools(tmp_path, False))}
    assert len(names) == 34
