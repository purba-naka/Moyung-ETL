# ruff: noqa: F811, F401
from __future__ import annotations

import json
import re

import duckdb
import pytest
from conftest import make_xlsx
from pydantic import TypeAdapter
from test_phase2_drift import Provider, _ref, _swap, blobs, tools

from moyung.nodes.base import NodeError, connect, schema_of
from moyung.nodes.sqlexpr import check_expression
from moyung.pipeline import parse_spec
from moyung.quality import Check, run_checks
from moyung.rebind import rebind
from moyung.state import Job, new_state
from moyung.tools import JobTools

_ADAPTER = TypeAdapter(Check)


def _bind_first(tools):
    assert tools.rebind_sources()["ok"] and tools.run_pipeline()["ok"]


def _policy_off(tools):
    def off(st):
        policy = st.hitl.policy
        checkpoints = {**policy.checkpoints, "on_rebind_drift": "off"}
        hitl = st.hitl.model_copy(
            update={"policy": policy.model_copy(update={"checkpoints": checkpoints})}
        )
        return st.model_copy(update={"hitl": hitl})

    tools.job.update(off)


@pytest.fixture
def regex_tools(blobs, tmp_path):
    spec = {
        "sources": {
            "sales": {
                "file_role": "f",
                "sheet": {"name_regex": "^S[a-z]+$", "sheet_column": "sheet"},
                "region": {"header_anchor": ["Item", "Qty"]},
            }
        },
        "outputs": {"o": {"input": "sales"}},
    }
    t = JobTools(Job(new_state("j2")), Provider(blobs), tmp_path / "out")
    t.add_file("a", "jan.xlsx", "f")
    assert t.save_spec(json.dumps(spec), "")["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    return t


def test_a_failed_run_on_an_approved_drift_does_not_claim_a_new_normal(regex_tools, blobs):
    _bind_first(regex_tools)
    before = regex_tools.job.state.binds["sales"]
    _swap(regex_tools, blobs, [["Item", "Quantity"], ["pen", 1]])
    refused = regex_tools.run_pipeline()["error"]
    assert "new normal" in refused and "once a run on it succeeds" in refused
    regex_tools.approve("on_rebind_drift", "dina", _ref(refused))
    res = regex_tools.run_pipeline()
    assert not res["ok"] and "new normal" not in res["text"]
    assert "the sources drifted since the last run" in res["text"]
    assert regex_tools.job.state.binds["sales"] == before


def test_an_emptied_sheet_is_a_different_drift_from_a_renamed_header(regex_tools, blobs):
    _bind_first(regex_tools)
    _swap(regex_tools, blobs, [["Item", "Quantity"], ["pen", 1]])
    renamed = _ref(regex_tools.run_pipeline()["error"])
    regex_tools.approve("on_rebind_drift", "dina", renamed)
    _swap(regex_tools, blobs, [[None]])
    refused = regex_tools.run_pipeline()["error"]
    assert _ref(refused) != renamed and "is empty" in refused


def test_the_same_drift_in_another_file_needs_its_own_approval(tools, blobs):
    _bind_first(tools)
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    first = _ref(tools.run_pipeline()["error"])
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "lots"]])
    assert _ref(tools.run_pipeline()["error"]) != first


def test_a_drift_nobody_approved_is_not_remembered_when_the_policy_lets_it_run(tools, blobs):
    _bind_first(tools)
    before = tools.job.state.binds["sales"]
    _policy_off(tools)
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    res = tools.run_pipeline()
    assert res["ok"] and "nobody approved" in res["text"]
    assert tools.job.state.binds["sales"] == before
    assert tools.job.state.runs[-1].drift == ""


def test_a_run_on_an_approved_drift_records_it_and_the_input_names(tools, blobs):
    _bind_first(tools)
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    ref = _ref(tools.run_pipeline()["error"])
    tools.approve("on_rebind_drift", "dina", ref)
    assert tools.run_pipeline()["ok"]
    last = tools.job.state.runs[-1]
    assert last.drift == f"{ref} approved by dina" and last.names == {"f": "feb.xlsx"}
    assert tools.job.state.runs[0].names == {"f": "jan.xlsx"}
    text = tools.last_run()["text"]
    assert re.search(r"\|f=jan\.xlsx@[0-9a-f]{8}\|-\s*$", text)


def test_the_refusal_names_the_first_differences_not_the_whole_report(tools, blobs):
    _bind_first(tools)
    _swap(tools, blobs, [["Item", "Qty"], ["pen", "many"]])
    refused = tools.run_pipeline()["error"]
    assert len(refused) < 900 and refused.count("sales:") == 1


def test_sheet_failures_state_the_advice_once():
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "s": {
                        "file_role": "f",
                        "sheet": {"name_regex": "^20[0-9][0-9]$", "sheet_column": "yr"},
                        "region": {"header_anchor": ["Item", "Qty"]},
                    }
                },
                "outputs": {"o": {"input": "s"}},
            }
        )
    )
    book = {"f": make_xlsx({"2023": [["A"], [1]], "2024": [["B"], [1]], "2025": [["C"], [1]]})}
    message = rebind(spec, book).sources["s"].diff[0]
    assert "3 sheet(s) failed" in message and message.count("find_text") == 1
    assert "2024" in message and "2025" in message


def test_plan_load_needs_a_recorded_output_hash(tmp_path):
    rows = [["Plant", "Ton"], ["A", 1]]
    t = _load_tools(tmp_path, rows)
    last = t.job.state.last_run
    cleared = tuple(o.model_copy(update={"sha256": ""}) for o in last.outputs)
    t.job.update(
        lambda st: st.model_copy(update={"last_run": last.model_copy(update={"outputs": cleared})})
    )
    assert "did not record a hash" in t.plan_load("snowflake", "clean", "DB", "SC", "@s")["error"]


def _load_tools(tmp_path, rows):
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
        "outputs": {"clean": {"input": "t", "format": "parquet", "load": {"table": "x"}}},
    }
    assert t.save_spec(json.dumps(spec), "")["saved"]
    t.approve("after_propose_datasets", "dina")
    assert t.run_pipeline()["ok"]
    t.approve("before_load", "dina")
    return t


@pytest.mark.parametrize(
    "expr",
    [
        "age(DATE '2020-01-01')",
        "ago(1)",
        "pg_conf_load_time()",
        "pg_postmaster_start_time()",
        "bitstring('1', 2000000000)",
        "regexp_split_to_table('a b', ' ')",
        "replace(replace(replace(replace(x,'a','bbbbbbbbbb'),'b','cccccccccc'),'c','dddddddddd'),'d','eeeeeeeeee')",
    ],
)
def test_more_expressions_that_explode_or_follow_the_clock_are_refused(expr):
    with pytest.raises(NodeError):
        check_expression(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "age(DATE '2020-01-01', DATE '2019-01-01')",
        "replace(replace(replace(x, '.', ''), ',', ''), ' ', '')",
        "replace(x, 'N/A', 'not available')",
    ],
)
def test_ordinary_text_cleaning_still_passes(expr):
    assert check_expression(expr) == expr


def _verdict(setup, check):
    con = duckdb.connect()
    for sql in setup:
        con.execute(sql)
    names = [
        r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    ]
    schemas = {t: schema_of(con.table(t)) for t in names}
    results, broken = run_checks({"c": _ADAPTER.validate_python(check)}, con, schemas)
    return f"broken: {broken}" if broken else ("pass" if results[0].passed else "fail")


def test_bounds_outside_the_integer_range_never_hit_and_never_break_the_check():
    setup = ["CREATE TABLE a AS SELECT 5::BIGINT v"]
    base = {"kind": "range", "table": "a", "column": "v"}
    assert _verdict(setup, {**base, "max": 10**40}) == "pass"
    assert _verdict(setup, {**base, "min": -(10**40)}) == "pass"
    accepted = {"kind": "accepted_values", "table": "a", "column": "v"}
    assert _verdict(setup, {**accepted, "values": [5, 10**40]}) == "pass"
    assert _verdict(setup, {**accepted, "values": [10**40]}) == "fail"


def test_accepted_values_is_exact_for_wide_decimals_and_float_columns():
    wide = ["CREATE TABLE a AS SELECT 99999999999999999999999999999999999999::DECIMAL(38,0) v"]
    accepted = {"kind": "accepted_values", "table": "a", "column": "v"}
    assert (
        _verdict(wide, {**accepted, "values": [99999999999999999999999999999999999998]}) == "fail"
    )
    assert (
        _verdict(wide, {**accepted, "values": [99999999999999999999999999999999999999]}) == "pass"
    )
    single = ["CREATE TABLE a AS SELECT 0.1::FLOAT v"]
    assert _verdict(single, {"kind": "range", "table": "a", "column": "v", "max": "0.1"}) == "pass"
    assert _verdict(single, {**accepted, "values": [0.1]}) == "pass"


def test_the_memory_limit_is_enforced_on_a_query_that_needs_more():
    con = connect(memory_limit="64MB")
    with pytest.raises(duckdb.Error):
        con.execute("SELECT list(range(30000000))").fetchall()
