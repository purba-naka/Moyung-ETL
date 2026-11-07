from __future__ import annotations

import inspect
import json
import typing

import pytest
from conftest import make_xlsx

from excel_etl.state import FileStore, Job, new_state
from excel_etl.tools import JobTools

SALES = [["Item", "Qty"], ["pen", 2], ["ink", 1], ["pen", 5], ["pad", 3]]
SPEC = {
    "sources": {
        "sales": {
            "file_role": "f",
            "sheet": {"name": "Sales"},
            "region": {"header_anchor": ["Qty"]},
        }
    },
    "steps": [
        {
            "id": "by_item",
            "node": "group_by",
            "inputs": ["sales"],
            "config": {"by": ["Item"], "aggs": [{"name": "Total", "fn": "sum", "column": "Qty"}]},
        }
    ],
    "outputs": {"totals": {"input": "by_item"}},
    "checks": {
        "same_total": {
            "kind": "reconcile",
            "left": {"table": "sales", "column": "Qty"},
            "right": {"table": "by_item", "column": "Total"},
        }
    },
}


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


@pytest.fixture
def blobs():
    return {"a": make_xlsx({"Sales": SALES})}


@pytest.fixture
def tools(blobs, tmp_path):
    t = JobTools(Job(new_state("j1")), Provider(blobs), tmp_path / "out")
    assert "registered" in t.add_file("a", "jan.xlsx", "f")["text"]
    return t


def spec_json(**over):
    return json.dumps({**SPEC, **over})


# --- every tool returns a dict and never raises ---------------------------------------------


def test_agent_tools_are_the_documented_fixed_list(tools):
    names = [f.__name__ for f in tools.agent_tools()]
    assert names[:3] == ["job_summary", "list_files", "workbook_map"]
    assert len(names) == len(set(names)) == 21
    for banned in ("add_file", "answer", "approve"):  # host only
        assert banned not in names


def test_tool_schemas_are_flat_and_documented(tools):
    """DESIGN 11: primitives, enums and lists of strings only; the docstring is the description."""
    ok = {str, int, float, bool}
    for fn in tools.agent_tools() + [tools.add_file, tools.answer, tools.approve]:
        assert (fn.__doc__ or "").strip(), fn.__name__
        hints = typing.get_type_hints(fn, include_extras=False)
        for name, hint in hints.items():
            if name == "return":
                assert hint is dict
                continue
            origin = typing.get_origin(hint)
            if origin is typing.Literal:
                assert all(isinstance(a, str) for a in typing.get_args(hint)), (fn.__name__, name)
            elif origin is list:
                assert typing.get_args(hint) == (str,), (fn.__name__, name)
            else:
                assert hint in ok, f"{fn.__name__}.{name}: {hint}"
        for p in inspect.signature(fn).parameters.values():
            assert p.default is not None, f"{fn.__name__}.{p.name} defaults to None"


def test_every_tool_survives_hostile_arguments(tools):
    hostile = [None, 5, "", "zzz", [], {}, -1, "F9", "\x00", "x" * 5000]
    for fn in tools.agent_tools():
        params = list(inspect.signature(fn).parameters)
        for bad in hostile:
            for n in range(len(params) + 1):
                if n == len(params) and params:
                    continue
                result = fn(*[bad] * min(n, len(params)))
                assert isinstance(result, dict), (fn.__name__, bad)
                assert "text" in result or "error" in result, (fn.__name__, bad, result)
                assert "internal error" not in result.get("error", ""), (fn.__name__, bad, result)
                json.dumps(result)


# --- the agent's path through a job ---------------------------------------------------------


def test_full_path_inspect_save_ask_approve_run(tools, tmp_path):
    assert "files: F1 jan.xlsx" in tools.job_summary()["text"]
    assert tools.list_files()["text"] == "F1|jan.xlsx|role f"
    assert "F1.S1|Sales" in tools.workbook_map("F1")["text"]
    assert "F1.S1.R1" in tools.sheet_regions("F1", "Sales")["text"]
    assert "Qty" in tools.render_range("F1", "F1.S1.R1")["text"]
    assert "Qty" in tools.profile_region("F1", "F1.S1.R1")["text"]
    assert "matches" in tools.find_text("F1", "pen")["text"]
    assert "group_by" in tools.node_catalog()["text"]
    assert "aggs*" in tools.node_catalog("group_by")["text"]

    saved = tools.save_spec(spec_json(), "")
    assert saved["saved"] and saved["version"] == 1, saved
    assert json.loads(tools.get_spec()["text"])["outputs"] == {
        "totals": {"input": "by_item", "format": "parquet"}
    }

    refused = tools.run_pipeline()  # after_propose_datasets is required
    assert "needs a person's approval" in refused["error"]

    assert tools.approve("after_propose_datasets", "dina", "v1")["text"].startswith("after_propose")
    done = tools.run_pipeline()
    assert (
        done["ok"] and "wrote" in done["text"] and "check same_total|reconcile|pass" in done["text"]
    )
    assert (tmp_path / "out" / "totals.parquet").exists()
    assert "run: ok" in tools.last_run()["text"]
    assert "last run: ok" in tools.job_summary()["text"]


def test_approval_for_an_older_version_does_not_cover_the_next(tools):
    tools.save_spec(spec_json())
    tools.approve("after_propose_datasets", "dina", "v1")
    other = dict(SPEC, outputs={"t2": {"input": "by_item"}})
    assert tools.save_spec(json.dumps(other), "second output")["version"] == 2
    err = tools.run_pipeline()["error"]
    assert "approved for 'v1', now it is 'v2'" in err


def test_save_spec_refuses_a_spec_that_does_not_validate(tools):
    bad = json.loads(spec_json())
    bad["steps"][0]["config"]["by"] = ["Itm"]
    r = tools.save_spec(json.dumps(bad))
    assert r["saved"] is False and "Itm" in r["text"]
    assert "no spec saved yet" in tools.get_spec()["error"]
    assert tools.job.state.version == 1  # only add_file was saved


def test_save_spec_errors_are_dicts_with_a_reason(tools):
    assert "Invalid JSON" in tools.save_spec("sources: {}")["error"]
    assert "output: is not a setting here" in tools.save_spec('{"output": {}}')["error"]
    tools.save_spec(spec_json())
    assert "same as the current version" in tools.save_spec(spec_json(), "again")["error"]
    other = dict(SPEC, outputs={})
    assert "give a note" in tools.save_spec(json.dumps(other), "")["error"]
    assert tools.save_spec(json.dumps(other), "dropped output")["version"] == 2


def test_get_spec_versions_and_history(tools):
    tools.save_spec(spec_json())
    tools.save_spec(json.dumps(dict(SPEC, outputs={})), "no output")
    assert "totals" in json.loads(tools.get_spec(1)["text"])["outputs"]
    assert tools.get_spec(2)["version"] == 2
    assert "no version 9" in tools.get_spec(9)["error"]
    assert "v2: no output" in tools.spec_history()["text"]


def test_validate_spec_saved_and_inline(tools):
    assert "no spec saved yet" in tools.validate_spec()["error"]
    assert tools.validate_spec(spec_json())["ok"] is True
    tools.save_spec(spec_json())
    assert tools.validate_spec()["ok"] is True
    bad = json.loads(spec_json())
    bad["checks"]["same_total"]["left"]["column"] = "Qy"
    r = tools.validate_spec(json.dumps(bad))
    assert r["ok"] is False and "checks.same_total" in r["text"]


def test_rebind_remembers_places_and_flags_drift(tools, blobs):
    assert "no spec saved yet" in tools.rebind_sources()["error"]
    tools.save_spec(spec_json())
    r = tools.rebind_sources()
    assert r["ok"] and "sales|exact" in r["text"] and "sales" in tools.job.state.binds
    # a new upload with a moved table: same id, new bytes
    blobs["a"] = make_xlsx({"Sales": [[None, None]] + [[None, None]] + SALES})
    tools.add_file("a", "feb.xlsx", "f")
    r = tools.rebind_sources()
    assert r["ok"] and "shifted" in r["text"]
    blobs["a"] = make_xlsx({"Sales": [["Item", "Amount"], ["pen", 2], ["ink", 1], ["pad", 3]]})
    tools.add_file("a", "mar.xlsx", "f")
    r = tools.rebind_sources()
    assert r["ok"] is False and "ask the person before repairing" in r["text"]


def test_run_failed_reports_and_asks_the_person(tools):
    tools.save_spec(spec_json())
    tools.approve("after_propose_datasets", "dina", "v1")
    tools.add_file("a", "bad.xlsx", "f")  # same id, same bytes: fine
    tools.provider.blobs["a"] = make_xlsx({"Sales": SALES[:1] + [["pen", None], ["ink", None]]})
    tools.add_file("a", "empty.xlsx", "f")
    r = tools.run_pipeline()
    assert "run:" in r["text"]
    if not r["ok"]:
        assert "after_run needs a person's approval" in r["text"]


def test_run_needs_out_dir_files_and_a_spec(blobs):
    t = JobTools(Job(new_state("j")), Provider(blobs))  # no out_dir
    assert "no spec saved yet" in t.run_pipeline()["error"]
    t.job.update(lambda s: s)
    t2 = JobTools(Job(new_state("j")), Provider(blobs))
    t2.save_spec(spec_json())  # no files registered: only settings checked
    t2.approve("after_propose_datasets", "dina", "v1")
    assert "out_dir" in t2.run_pipeline()["error"]
    t2.out_dir = "x"
    assert "no files registered" in t2.run_pipeline()["error"]


def test_file_ids_accept_f_number_name_and_host_id(tools):
    for ref in ("F1", "f1", "jan.xlsx", "a"):
        assert tools.workbook_map(ref)["truncated"] is False
    err = tools.workbook_map("F2")["error"]
    assert "no file 'F2'; files: F1 (jan.xlsx)" in err


def test_a_file_that_changed_since_registration_is_refused(tools, blobs):
    blobs["a"] = make_xlsx({"Other": [["x"]]})
    assert "not the one registered" in tools.workbook_map("F1")["error"]


# --- questions to the person ----------------------------------------------------------------


def test_ask_answer_and_pending(tools):
    r = tools.ask_human("choose", "which month?", ["jan", "feb"], "sources.sales")
    assert r["question_id"] == "q1" and "Stop until" in r["text"]
    assert (
        "q1 (choose) about sources.sales: which month? options: jan | feb"
        in (tools.pending_questions()["text"])
    )
    assert (
        tools.ask_human("choose", "which month?", ["jan", "feb"], "sources.sales")["question_id"]
        == "q1"
    )
    assert "one of: jan, feb" in tools.answer("q1", "mar")["error"]
    assert "recorded" in tools.answer("q1", "FEB")["text"]
    assert tools.pending_questions()["text"] == "none waiting"
    assert "feb" in tools.lookup_knowledge("month")["text"]
    assert "waiting questions: 0" in tools.job_summary()["text"]


def test_ask_human_input_errors_are_dicts(tools):
    assert "at least 2 options" in tools.ask_human("choose", "q", ["only"])["error"]
    assert "takes no options" in tools.ask_human("confirm", "q", ["a", "b"])["error"]
    assert "kind must be" in tools.ask_human("shout", "q")["error"]
    assert "unknown checkpoint" in tools.approve("nope", "x")["error"]


def test_pending_question_blocks_a_required_checkpoint(tools):
    tools.save_spec(spec_json())
    tools.approve("after_propose_datasets", "dina", "v1")
    tools.ask_human("clarify", "unit of Qty?")
    assert "1 question(s) still waiting" in tools.run_pipeline()["error"]
    tools.answer("q1", "pieces")
    assert tools.run_pipeline()["ok"]


# --- state, store and conflicts -------------------------------------------------------------


def test_store_backed_tools_are_saved_each_call_and_conflict_is_an_error(blobs, tmp_path):
    store = FileStore(tmp_path / "store")
    a = JobTools(Job.open("j", store), Provider(blobs), tmp_path / "o")
    b = JobTools(Job.open("j", store), Provider(blobs), tmp_path / "o")
    a.add_file("a", "jan.xlsx", "f")
    r = b.add_file("a", "jan.xlsx", "f")  # b is behind
    assert r["conflict"] is True and "Nothing was changed" in r["error"]
    b = JobTools(Job.open("j", store), Provider(blobs), tmp_path / "o")
    assert b.save_spec(spec_json())["saved"]
    again = JobTools(Job.open("j", store), Provider(blobs), tmp_path / "o")
    assert "files: F1 jan.xlsx" in again.job_summary()["text"] and again.get_spec()["version"] == 1


def test_stateless_host_keeps_json_between_calls(blobs, tmp_path):
    t = JobTools(Job(new_state("j")), Provider(blobs), tmp_path)
    t.add_file("a", "jan.xlsx", "f")
    t.save_spec(spec_json())
    text = t.job.state_json()
    t2 = JobTools(Job.from_state(text), Provider(blobs), tmp_path)
    assert t2.get_spec()["version"] == 1 and "F1|jan.xlsx" in t2.list_files()["text"]


def test_add_file_errors(blobs, tmp_path):
    t = JobTools(Job(new_state("j")), Provider(blobs), tmp_path)
    assert "error" in t.add_file("missing", "x.xlsx")
    assert "error" in t.add_file("a", "")
    assert t.job.state.files == []


def test_text_is_capped_with_truncated_flag(tools, monkeypatch):
    import excel_etl.tools as mod

    monkeypatch.setattr(mod, "_MAX_TEXT", 50)
    r = tools.node_catalog()
    assert r["truncated"] is True and len(r["text"]) == 50 and r["text"].endswith("...")


def test_internal_bug_becomes_an_error_dict(tools, monkeypatch):
    import excel_etl.tools as mod

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(mod, "catalog", boom)
    r = tools.node_catalog()
    assert "internal error (RuntimeError): kaput" in r["error"] and "library bug" in r["error"]


def test_job_summary_counts_waiting_questions_and_approvals(tools):
    tools.ask_human("clarify", "a?")
    tools.ask_human("clarify", "b?")
    tools.approve("after_spec", "dina")
    text = tools.job_summary()["text"]
    assert "waiting questions: 2" in text and "approvals: after_spec" in text


def test_missing_version_error_has_no_repr_quotes(tools):
    tools.save_spec(spec_json())
    err = tools.get_spec(9)["error"]
    assert err.startswith("no version 9") and not err.startswith('"')


def test_agent_tools_include_every_documented_tool(tools):
    names = {f.__name__ for f in tools.agent_tools()}
    assert {"lookup_knowledge", "ask_human", "pending_questions", "run_pipeline"} <= names


def test_a_provider_that_raises_anything_is_a_host_fault_not_a_library_bug(tmp_path):
    class Broken:
        def open(self, file_id):
            raise RuntimeError("disk on fire")

    t = JobTools(Job(new_state("j")), Broken(), tmp_path)
    r = t.add_file("a", "x.xlsx", "f")
    assert "disk on fire" in r["error"] and "internal error" not in r["error"]
    t.provider = Provider({"a": make_xlsx({"Sales": SALES})})
    t.add_file("a", "x.xlsx", "f")
    t.provider = Broken()
    assert "internal error" not in t.workbook_map("F1")["error"]
    assert "disk on fire" in t.save_spec(spec_json())["error"]


def test_spec_note_is_capped_with_a_clean_message(tools):
    r = tools.save_spec(spec_json(), "n" * 501)
    assert r["error"] == "the note is 501 characters; keep it under 500"
    assert tools.save_spec(spec_json(), "n" * 500)["saved"]
    assert "validation error" not in tools.save_spec(spec_json(), "")["error"]


def test_a_pydantic_error_is_described_not_dumped(tools, monkeypatch):
    from pydantic import BaseModel

    import excel_etl.tools as mod

    class M(BaseModel):
        n: int

    def refuse(*a, **k):
        M(n="x")  # raises ValidationError

    monkeypatch.setattr(mod, "catalog", refuse)
    err = tools.node_catalog()["error"]
    assert err.startswith("n: ") and "validation error" not in err and "pydantic" not in err


def test_a_refused_checkpoint_tells_the_agent_what_to_do_instead(tools):
    tools.save_spec(spec_json())
    err = tools.run_pipeline()["error"]
    assert "needs a person's approval" in err and "use ask_human" in err
    tools.approve("after_propose_datasets", "dina", "v1")
    assert "ok" in tools.run_pipeline()
    other = dict(SPEC, outputs={})
    tools.save_spec(json.dumps(other), "no output")
    assert "use ask_human" in tools.run_pipeline()["error"]  # stale approval too


def test_other_pipeline_errors_get_no_approval_hint(tools):
    assert "ask_human" not in tools.run_pipeline()["error"]  # no spec yet


def test_pipeline_error_not_about_approval_has_no_hint(blobs, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    t = JobTools(Job(new_state("j")), Provider(blobs), blocker)  # out_dir is a file
    t.add_file("a", "jan.xlsx", "f")
    t.save_spec(spec_json())
    t.approve("after_propose_datasets", "dina", "v1")
    err = t.run_pipeline()["error"]
    assert err.startswith("out_dir: cannot use") and "ask_human" not in err


def test_a_spec_that_reads_one_role_opens_only_that_file(blobs, tmp_path):
    opened: list[str] = []

    class Spy(Provider):
        def open(self, file_id):
            opened.append(file_id)
            return super().open(file_id)

    blobs["b"] = make_xlsx({"Other": [["x"], [1]]})
    t = JobTools(Job(new_state("j")), Spy(blobs), tmp_path)
    t.add_file("a", "jan.xlsx", "f")
    t.add_file("b", "other.xlsx", "g")
    opened.clear()
    assert t.save_spec(spec_json())["saved"]
    assert set(opened) == {"a"}  # role g belongs to no source of the spec
    opened.clear()
    t.validate_spec()
    t.rebind_sources()
    assert set(opened) == {"a"}


def test_per_version_output_keeps_older_files(blobs, tmp_path):
    t = JobTools(Job(new_state("j")), Provider(blobs), tmp_path / "out", per_version=True)
    t.add_file("a", "jan.xlsx", "f")
    t.save_spec(spec_json())
    t.approve("after_propose_datasets", "dina", "v1")
    assert t.run_pipeline()["ok"]
    assert (tmp_path / "out" / "v1" / "totals.parquet").exists()
    other = dict(SPEC, outputs={"t2": {"input": "by_item"}})
    t.save_spec(json.dumps(other), "second output")
    t.approve("after_propose_datasets", "dina", "v2")
    assert t.run_pipeline()["ok"]
    assert (tmp_path / "out" / "v1" / "totals.parquet").exists()  # still there
    assert (tmp_path / "out" / "v2" / "t2.parquet").exists()
    flat = JobTools(Job(new_state("k")), Provider(blobs), tmp_path / "flat")
    flat.add_file("a", "jan.xlsx", "f")
    flat.save_spec(spec_json())
    flat.approve("after_propose_datasets", "dina", "v1")
    flat.run_pipeline()
    assert (tmp_path / "flat" / "totals.parquet").exists()


def test_reload_job_after_a_conflict_lets_the_retry_work(blobs, tmp_path):
    store = FileStore(tmp_path / "store")
    a = JobTools(Job.open("j", store), Provider(blobs), tmp_path)
    b = JobTools(Job.open("j", store), Provider(blobs), tmp_path)
    a.add_file("a", "jan.xlsx", "f")
    assert b.save_spec(spec_json())["conflict"] is True
    assert "reloaded job j at version 1" in b.reload_job()["text"]
    assert b.save_spec(spec_json())["saved"]
    assert "reload_job" not in [f.__name__ for f in b.agent_tools()]  # host only


def test_cut_text_says_truncated_and_has_a_next_offset_key(tools, monkeypatch):
    import excel_etl.tools as mod

    monkeypatch.setattr(mod, "_MAX_TEXT", 50)
    r = tools.node_catalog()
    assert r["truncated"] is True and "next_offset" in r
    ok = tools.pending_questions()
    assert "truncated" not in ok
