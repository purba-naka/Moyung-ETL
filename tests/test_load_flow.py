"""The whole path on this machine: run a spec, plan the load, run the plan on a local duckdb.

The "warehouse" here is a second duckdb. It is not Snowflake, Databricks or BigQuery, so this
proves the flow (the right file, steps in the right order, the right rows after replace, append
and merge) and not the syntax of those platforms; `test_load.py` covers the syntax. Three
things are translated for duckdb: the stage is a folder, `COPY INTO ... FROM @stage` is an
INSERT from the parquet file, and `SET t.x = s.x` is written `SET x = s.x` (duckdb refuses the
qualified name that the three platforms require).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest
from conftest import make_xlsx

from excel_etl.load import LoadError, Where, file_columns, plan_load
from excel_etl.state import Job, new_state
from excel_etl.tools import JobTools

ROWS = [["Plant", "Product", "Ton"], ["A", "urea", 10], ["A", "npk", 5], ["B", "urea", 7]]
KEYS = {"table": "sales", "mode": "merge", "keys": ["Plant", "Product"]}


class Provider:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob

    def open(self, file_id: str) -> bytes:
        return self.blob


def _spec(load: dict | None, anchor=("Plant", "Ton")) -> str:
    out: dict = {"input": "t", "format": "parquet"}
    if load:
        out["load"] = load
    return json.dumps(
        {
            "sources": {
                "t": {
                    "file_role": "f",
                    "sheet": {"name": "Data"},
                    "region": {"header_anchor": list(anchor)},
                }
            },
            "steps": [],
            "outputs": {"clean": out},
        }
    )


def _tools(tmp_path: Path, load: dict | None, rows=ROWS, anchor=("Plant", "Ton")) -> JobTools:
    t = JobTools(Job(new_state("j")), Provider(make_xlsx({"Data": rows})), tmp_path / "out")
    assert "registered" in t.add_file("a", "jan.xlsx", "f")["text"]
    assert t.save_spec(_spec(load, anchor), "")["saved"]
    t.approve("after_propose_datasets", "dina")
    assert t.run_pipeline()["ok"]
    return t


# --- the agent's view ------------------------------------------------------


def test_plan_load_needs_a_run_a_load_target_and_the_before_load_approval(tmp_path) -> None:
    t = JobTools(Job(new_state("j")), Provider(make_xlsx({"Data": ROWS})), tmp_path / "out")
    t.add_file("a", "jan.xlsx", "f")
    assert "run the pipeline successfully first" in t.plan_load("snowflake", "clean")["error"]
    t.save_spec(_spec(None), "")
    t.approve("after_propose_datasets", "dina")
    t.run_pipeline()
    assert "no output 'nope'" in t.plan_load("snowflake", "nope")["error"]
    assert "has no `load` in the spec" in t.plan_load("snowflake", "clean")["error"]

    t2 = _tools(tmp_path / "b", {"table": "sales"})
    refused = t2.plan_load("snowflake", "clean")["error"]
    assert "before_load needs a person's approval" in refused and "ask_human" in refused
    t2.approve("before_load", "dina")
    text = t2.plan_load("snowflake", "clean", "DB", "SC", "@DB.SC.STG")["text"]
    assert 'COPY INTO "DB"."SC"."sales"' in text and "record_load" in text


def test_an_approval_for_an_older_spec_version_does_not_cover_a_new_one(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    t.approve("before_load", "dina")
    assert "COPY INTO" in t.plan_load("snowflake", "clean")["text"]  # v1 approved, v1 planned
    t.save_spec(_spec({"table": "sales", "mode": "append"}), "append now")
    t.run_pipeline()
    assert "approved for 'v1', but this is 'v2'" in t.plan_load("snowflake", "clean")["error"]


def test_record_load_answers_only_a_real_plan_and_is_not_an_agent_tool(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    assert "record_load" not in [f.__name__ for f in t.agent_tools()]
    assert "no load plan was made" in t.record_load("clean", "ok", 3)["error"]
    t.approve("before_load", "dina")
    t.plan_load("bigquery", "clean", "p", "d", "gs://b")
    assert "no load plan was made for 'other'" in t.record_load("other", "ok", 3)["error"]
    done = t.record_load("clean", "failed", 0, "permission denied")
    assert done["text"].endswith("failed, 0 rows")
    rec = t.job.state.loads[-1]
    assert (rec.platform, rec.table, rec.status, rec.ref) == ("bigquery", "sales", "failed", "v1")
    assert rec.note == "permission denied"


def test_a_table_that_does_not_fit_the_file_refuses_append_but_not_replace(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales", "mode": "append"})
    t.approve("before_load", "dina")
    bad = ["Plant VARCHAR(16777216)", "Ton VARCHAR(16777216)"]
    err = t.plan_load("snowflake", "clean", "DB", "SC", "@s", bad)["error"]
    assert "does not fit the file" in err and "Product" in err and "'Ton'" in err
    good = ["Plant VARCHAR(16777216)", "Product VARCHAR(16777216)", "Ton NUMBER(38,0)"]
    assert "COPY INTO" in t.plan_load("snowflake", "clean", "DB", "SC", "@s", good)["text"]
    assert "error" in t.plan_load("snowflake", "clean", "DB", "SC", "@s", ["oops"])

    r = _tools(tmp_path / "r", {"table": "sales", "mode": "replace"})
    r.approve("before_load", "dina")
    plan = r.plan_load("snowflake", "clean", "DB", "SC", "@s", bad)["text"]
    assert "CREATE OR REPLACE TABLE" in plan and "the table's shape changes" in plan


def test_a_hostile_stage_or_name_from_the_agent_is_an_error_not_sql(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    t.approve("before_load", "dina")
    assert "stage path" in t.plan_load("snowflake", "clean", stage="@s; DROP TABLE x")["error"]
    q = t.plan_load("databricks", "clean", "cat`; DROP TABLE x; --", "sc", "/Volumes/v")["text"]
    assert "`cat``; DROP TABLE x; --`" in q  # the backtick is doubled, so it stays one name


def test_a_wide_column_name_survives_and_bigquery_says_why_it_cannot(tmp_path) -> None:
    rows = [["Plant", "JAN|Ton", "Net Weight (kg)"], ["A", 1, 2], ["B", 3, 4]]
    t = _tools(tmp_path, {"table": "sales"}, rows, anchor=("Plant", "JAN|Ton"))
    t.approve("before_load", "dina")
    for platform, stage in (("snowflake", "@s"), ("databricks", "/v")):
        text = t.plan_load(platform, "clean", "d", "s", stage)["text"]
        assert "JAN|Ton" in text and "Net Weight (kg)" in text
    assert "does not allow" in t.plan_load("bigquery", "clean", "p", "d", "gs://b")["error"]


# --- the file is the truth ------------------------------------------------------


def test_columns_and_rows_come_from_the_file(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    path = t.job.state.last_run.outputs[0].path
    cols, n = file_columns(path)
    assert [c.name for c in cols] == ["Plant", "Product", "Ton"]
    assert cols[0].type == "VARCHAR" and n == 3


def test_an_unreadable_file_is_a_load_error(tmp_path) -> None:
    (tmp_path / "bad.parquet").write_bytes(b"not parquet")
    with pytest.raises(LoadError, match="cannot read"):
        file_columns(str(tmp_path / "bad.parquet"))
    with pytest.raises(LoadError, match="cannot read"):
        file_columns(str(tmp_path / "missing.parquet"))


# --- run the plan on a local duckdb ------------------------------------------------------


class LocalWarehouse:
    """Runs a Snowflake-shaped plan on duckdb, one statement at a time."""

    def __init__(self, stage: Path) -> None:
        self.con = duckdb.connect()
        self.stage = stage

    def run(self, plan) -> None:
        for s in plan.steps:
            if s.kind == "upload":
                self.stage.mkdir(exist_ok=True)
                (self.stage / Path(s.file).name).write_bytes(Path(s.file).read_bytes())
            else:
                self.con.execute(self._local(s.sql))

    def _local(self, sql: str) -> str:
        sql = re.sub(r'"[^"]*"\."[^"]*"\.', "", sql)  # database.schema. -> nothing
        sql = re.sub(r"\bNUMBER\((\d+),(\d+)\)", r"DECIMAL(\1,\2)", sql)
        sql = re.sub(
            r"COPY INTO (\S+) FROM @\S*?/(\S+\.parquet) .*",
            lambda m: (
                f"INSERT INTO {m[1]} SELECT * FROM read_parquet('{self.stage.as_posix()}/{m[2]}')"
            ),
            sql,
        )
        sql = re.sub(
            r"CREATE OR REPLACE TRANSIENT TABLE (\S+) LIKE (\S+)",
            r"CREATE OR REPLACE TABLE \1 AS SELECT * FROM \2 WHERE false",
            sql,
        )
        if "WHEN MATCHED THEN UPDATE SET" in sql:
            head, rest = sql.split("WHEN MATCHED THEN UPDATE SET", 1)
            sets, tail = rest.split("\nWHEN NOT MATCHED", 1)
            sets = re.sub(r't\.("[^"]+") =', r"\1 =", sets)
            sql = head + "WHEN MATCHED THEN UPDATE SET" + sets + "\nWHEN NOT MATCHED" + tail
        return sql

    def rows(self) -> list[tuple]:
        return sorted(self.con.execute('SELECT * FROM "sales"').fetchall())


def _plan(t: JobTools, mode: str, keys=()):
    stat = t.job.state.last_run.outputs[0]
    cols, _ = file_columns(stat.path)
    where = Where(database="DB", schema_name="SC", stage="@DB.SC.STG")
    return plan_load("snowflake", "clean", stat.path, "sales", mode, list(keys), cols, where)


def test_replace_twice_keeps_one_copy_of_the_rows(tmp_path) -> None:
    plan = _plan(_tools(tmp_path, {"table": "sales"}), "replace")
    w = LocalWarehouse(tmp_path / "stage")
    w.run(plan)
    w.run(plan)
    assert w.rows() == [("A", "npk", 5), ("A", "urea", 10), ("B", "urea", 7)]


def test_append_twice_doubles_the_rows_and_the_plan_warns_about_it(tmp_path) -> None:
    plan = _plan(_tools(tmp_path, {"table": "sales", "mode": "append"}), "append")
    assert "use a new file name" in plan.steps[-1].why
    w = LocalWarehouse(tmp_path / "stage")
    w.run(plan)
    w.run(plan)
    assert len(w.rows()) == 6


def test_merge_updates_matching_keys_adds_new_ones_and_leaves_no_scratch_table(tmp_path) -> None:
    first = _plan(_tools(tmp_path, KEYS), "merge", KEYS["keys"])
    w = LocalWarehouse(tmp_path / "stage")
    w.run(first)
    assert len(w.rows()) == 3  # the table was empty: every row is new
    later = [["Plant", "Product", "Ton"], ["A", "urea", 99], ["C", "npk", 1]]
    second = _plan(_tools(tmp_path / "later", KEYS, later), "merge", KEYS["keys"])
    w.run(second)
    assert w.rows() == [("A", "npk", 5), ("A", "urea", 99), ("B", "urea", 7), ("C", "npk", 1)]
    scratch = w.con.execute("select 1 from duckdb_tables() where table_name = 'sales__load'")
    assert scratch.fetchall() == []
    assert second.steps[-1].kind == "cleanup"


def test_merge_of_the_same_rows_again_changes_nothing(tmp_path) -> None:
    plan = _plan(_tools(tmp_path, KEYS), "merge", KEYS["keys"])
    w = LocalWarehouse(tmp_path / "stage")
    w.run(plan)
    once = w.rows()
    w.run(plan)
    assert w.rows() == once


def test_a_run_that_failed_cannot_be_planned_for_loading(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    t.approve("before_load", "dina")
    bad = json.loads(_spec({"table": "sales"}))
    bad["outputs"]["clean"]["input"] = "t"
    bad["checks"] = {
        "never": {"kind": "range", "table": "t", "column": "Ton", "min": 1000, "severity": "error"}
    }
    t.save_spec(json.dumps(bad), "a check that cannot pass")
    t.approve("after_propose_datasets", "dina")  # an approval is for one version: v2 now
    assert t.run_pipeline()["ok"] is False
    assert "run the pipeline successfully first" in t.plan_load("snowflake", "clean")["error"]


def test_plan_load_refuses_a_run_made_for_another_spec_version_or_file(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    t.save_spec(_spec({"table": "sales", "mode": "append"}), "append now")
    t.approve("before_load", "dina")  # v2 approved, but the parquet on disk is from v1
    assert "made for v1" in t.plan_load("snowflake", "clean")["error"]

    t.approve("after_propose_datasets", "dina")
    t.run_pipeline()
    assert "COPY INTO" in t.plan_load("snowflake", "clean")["text"]
    t.provider.blob = make_xlsx({"Data": [*ROWS, ["C", "urea", 1]]})  # next month's upload
    t.add_file("a", "feb.xlsx", "f")
    assert "file 'f' changed" in t.plan_load("snowflake", "clean")["error"]


def test_record_load_works_from_a_new_tools_object_and_keeps_the_plan_ref(tmp_path) -> None:
    t = _tools(tmp_path, {"table": "sales"})
    t.approve("before_load", "dina")
    t.plan_load("bigquery", "clean", "p", "d", "gs://b")
    t.save_spec(_spec({"table": "sales", "mode": "append"}), "append now")  # moves on to v2
    later = JobTools(Job.from_state(t.job.state_json()), t.provider, tmp_path / "out")
    assert later.record_load("clean", "ok", 3)["text"].endswith("ok, 3 rows")
    rec = later.job.state.loads[-1]
    assert (rec.platform, rec.table, rec.ref) == ("bigquery", "sales", "v1")
