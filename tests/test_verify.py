"""verify: a run that drops numbers without saying why must say so, and only then."""

from __future__ import annotations

import json

import pytest
from conftest import make_xlsx

from excel_etl.engine import run
from excel_etl.pipeline import SpecError, parse_spec, patch_spec
from excel_etl.state import Job, new_state
from excel_etl.tools import JobTools

ROWS = [
    ["Region", "Jan", "Feb", "Note", "Empty"],
    ["North", 10, 1, "a", 0],
    ["South", 20, 2, "b", 0],
    ["East", 30, 0, "c", None],
]
DATA = make_xlsx({"D": ROWS})
SOURCE = {"file_role": "f", "sheet": {"name": "D"}, "region": {"header_anchor": ["Region"]}}


def go(tmp_path, steps=(), excluded=None, out="s"):
    spec = {
        "sources": {"s": SOURCE},
        "steps": list(steps),
        "outputs": {"o": {"input": out}},
        "excluded": excluded or {},
    }
    rep = run(parse_spec(json.dumps(spec)), {"f": DATA}, tmp_path)
    assert rep.status == "ok", rep.text()
    return rep.verify


def step(id_, node, inp, **config):
    return {"id": id_, "node": node, "inputs": [inp], "config": config}


@pytest.mark.parametrize(
    ("steps", "out"),
    [
        ((), "s"),  # everything kept
        ([step("r", "rename", "s", mapping={"Jan": "January", "Feb": "February"})], "r"),
        ([step("u", "unpivot", "s", keep=["Region"], columns=["Jan", "Feb"])], "u"),
        ([step("f", "filter", "s", where="Jan > 0")], "f"),
    ],
)
def test_runs_that_keep_every_number_are_ok(tmp_path, steps, out):
    v = go(tmp_path, steps, out=out)
    # Empty holds only zeros and blanks: nothing there to lose
    assert v.status == "ok" and v.checked == 2 and not v.lost and not v.values


def test_a_dropped_number_column_is_lost_and_named(tmp_path):
    v = go(tmp_path, [step("k", "select", "s", columns=["Region", "Jan"])], out="k")
    assert v.status == "unexplained"
    assert v.lost == ("s.Feb (2 non-zero)",)
    assert "LOST" in v.text() and "s.Feb" in v.text()


def test_a_declared_exclusion_explains_the_loss(tmp_path):
    v = go(
        tmp_path,
        [step("k", "select", "s", columns=["Region", "Jan"])],
        excluded={"s": {"feb": "a forecast, not actuals"}},  # letter case ignored
        out="k",
    )
    assert v.status == "explained" and not v.lost
    assert v.excluded == ("s.Feb: a forecast, not actuals",)


def test_changed_values_under_the_same_name_are_reported(tmp_path):
    v = go(tmp_path, [step("x", "derive", "s", name="Jan", expr="Jan * 1000")], out="x")
    assert v.status == "ok"  # the column is there: a unit change is not a loss
    assert v.values == (
        "x.Jan: 3 of up to 20 sampled values are not in s.Jan "
        "(fine after a unit conversion, otherwise check the steps)",
    )


class Files:
    def open(self, file_id):
        return DATA


def test_plan_load_refuses_lost_data_until_explained_and_accepted(tmp_path):
    t = JobTools(Job(new_state("j")), Files(), tmp_path / "out")
    t.add_file("a", "x.xlsx", "f")
    spec = {
        "sources": {"s": SOURCE},
        "steps": [step("k", "select", "s", columns=["Region", "Jan"])],
        "outputs": {"o": {"input": "k", "load": {"table": "t"}}},
    }
    assert t.save_spec(json.dumps(spec))["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    r = t.run_pipeline()
    assert r["ok"] and "LOST" in r["text"]  # the run is written; the loss is shown
    assert t.verify_run()["ok"] is False
    t.approve("before_load", "dina", "v1")
    assert "lost 1 number column" in t.plan_load("snowflake", "o")["error"]

    assert t.edit_spec(json.dumps({"excluded": {"s": {"Feb": "forecast"}}}), "drop Feb")["saved"]
    for cp in ("after_propose_datasets", "before_load"):
        t.approve(cp, "dina", "v2")
    assert t.run_pipeline()["ok"]
    assert t.verify_run()["ok"] is True
    assert "accept_exclusions needs" in t.plan_load("snowflake", "o")["error"]
    t.approve("accept_exclusions", "dina", "v2")
    assert "COPY INTO" in t.plan_load("snowflake", "o")["text"]


def test_excluded_must_name_a_source_and_give_a_reason():
    base = {"sources": {"s": SOURCE}, "outputs": {"o": {"input": "s"}}}
    with pytest.raises(SpecError, match="not a source"):
        parse_spec(json.dumps({**base, "excluded": {"nope": {"Feb": "why not"}}}))
    with pytest.raises(SpecError):
        parse_spec(json.dumps({**base, "excluded": {"s": {"Feb": ""}}}))
    spec = patch_spec(parse_spec(json.dumps(base)), json.dumps({"excluded": {"s": {"Feb": "old"}}}))
    assert spec.excluded == {"s": {"Feb": "old"}}
