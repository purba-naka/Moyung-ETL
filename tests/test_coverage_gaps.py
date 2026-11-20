from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import duckdb
import pytest
from conftest import make_xlsx
from pydantic import TypeAdapter
from test_ml import _ml
from test_ml import data as data  # noqa: F401

from moyung import state as state_mod
from moyung.auto import auto
from moyung.engine import run
from moyung.ml import ARTIFACT_FILES
from moyung.nodes.base import Column
from moyung.pipeline import parse_spec
from moyung.quality import Check, run_checks
from moyung.state import FileStore, Job, StateError, new_state
from moyung.tools import JobTools

ROWS = [["Plant", "Product", "Ton"], ["A", "urea", 10], ["B", "npk", 5]]


def _excel_spec(sheet="Data", anchor=("Plant", "Ton")):
    return {
        "sources": {
            "t": {"file_role": "f", "sheet": {"name": sheet}, "region": {"header_anchor": anchor}}
        },
        "steps": [],
        "outputs": {"clean": {"input": "t"}},
    }


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "jan.xlsx"
    path.write_bytes(make_xlsx({"Data": ROWS}))
    return path


class TestAuto:
    def test_the_first_spec_that_fits_is_run(self, book, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "a_wrong.json").write_text(json.dumps(_excel_spec(sheet="Other")))
        (specs / "b_right.json").write_text(json.dumps(_excel_spec()))
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "ok" and rep.spec == "b_right"
        assert (tmp_path / "out" / "b_right" / "jan" / "clean.parquet").exists()
        assert "a_wrong" in rep.tried and "0/1 sources found" in rep.tried["a_wrong"]
        assert "auto: ok with spec 'b_right'" in rep.text()

    def test_a_bad_spec_is_skipped_with_the_reason(self, book, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "bad.json").write_text("{not json")
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "no_spec" and rep.tried["bad"].startswith("bad spec:")

    def test_no_spec_at_all_says_where_it_looked(self, book, tmp_path):
        (tmp_path / "empty").mkdir()
        rep = auto(book, tmp_path / "empty", tmp_path / "out")
        assert rep.status == "no_spec" and "no saved spec in" in rep.tried["-"]
        assert not (tmp_path / "out").exists()

    def test_when_nothing_fits_the_closest_spec_is_named(self, book, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "near.json").write_text(json.dumps(_excel_spec(sheet="Other")))
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "no_spec" and rep.spec == "near" and rep.bind
        assert not (tmp_path / "out").exists()

    def test_a_failed_check_makes_the_auto_run_failed(self, book, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        spec = {**_excel_spec(), "checks": {"c": {"kind": "row_count", "table": "t", "min": 9}}}
        (specs / "s.json").write_text(json.dumps(spec))
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "failed" and rep.run is not None

    def _cli(self, *args):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        return subprocess.run(
            [sys.executable, "-m", "moyung.auto", *map(str, args)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_cli_exit_codes(self, book, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "s.json").write_text(json.dumps(_excel_spec()))
        ok = self._cli(book, specs, tmp_path / "out")
        assert ok.returncode == 0 and "auto: ok" in ok.stdout
        (specs / "s.json").write_text(json.dumps(_excel_spec(sheet="Other")))
        miss = self._cli(book, specs, tmp_path / "out2")
        assert miss.returncode == 1 and "auto: no_spec" in miss.stdout

    def test_cli_usage(self):
        r = self._cli("only-one")
        assert r.returncode == 1 and "python -m moyung.auto" in r.stderr


def _ml_spec_with_side_output(check_table):
    return parse_spec(
        json.dumps(
            {
                "sources": {
                    "cust": {"kind": "csv", "file_role": "c"},
                    "side": {"kind": "csv", "file_role": "s"},
                },
                "ml": _ml(),
                "outputs": {
                    "train": {"input": "churn_train"},
                    "validation": {"input": "churn_validation"},
                    "test": {"input": "churn_test"},
                    "side": {"input": "side"},
                },
                "checks": {"c": {"kind": "row_count", "table": check_table, "min": 10**6}},
            }
        )
    )


@pytest.fixture
def side(tmp_path):
    path = tmp_path / "side.csv"
    path.write_text("k,v\n1,2\n")
    return path


class TestPartialWithMl:
    def test_a_failed_ml_check_holds_every_partition_and_the_artifacts(self, data, side, tmp_path):  # noqa: F811
        out = tmp_path / "out"
        files = {"c": data, "s": side}
        clean = _ml_spec_with_side_output("side").model_copy(update={"checks": {}})
        assert run(clean, files, out).status == "ok"
        assert all((out / name).exists() for name in ARTIFACT_FILES)
        rep = run(_ml_spec_with_side_output("churn_train"), files, out)
        assert rep.status == "partial"
        assert set(rep.blocked) == {"train", "validation", "test"}
        assert [o.name for o in rep.outputs] == ["side"] and rep.artifacts == ()
        assert any("ml artifacts were not written" in w for w in rep.warnings)
        assert not any((out / name).exists() for name in ARTIFACT_FILES)
        assert not (out / "train.parquet").exists() and (out / "side.parquet").exists()

    def test_a_failed_check_off_the_ml_lineage_keeps_the_artifacts(self, data, side, tmp_path):  # noqa: F811
        rep = run(_ml_spec_with_side_output("side"), {"c": data, "s": side}, tmp_path / "out")
        assert rep.status == "partial" and set(rep.blocked) == {"side"}
        assert len(rep.artifacts) == len(ARTIFACT_FILES)

    def test_an_old_file_that_cannot_be_removed_is_flagged(
        self,
        data,  # noqa: F811
        side,
        tmp_path,
        monkeypatch,
    ):
        out = tmp_path / "out"
        files = {"c": data, "s": side}
        assert run(_ml_spec_with_side_output("side").model_copy(update={"checks": {}}), files, out)
        real = type(out).unlink

        def refuse(self, *a, **k):
            if self.name == "side.parquet":
                raise PermissionError("in use")
            return real(self, *a, **k)

        monkeypatch.setattr(type(out), "unlink", refuse)
        rep = run(_ml_spec_with_side_output("side"), files, out)
        assert rep.status == "partial"
        assert any("could not remove the old side.parquet" in w for w in rep.warnings)


class _Provider:
    def __init__(self, path):
        self.path = path

    def open(self, file_id):
        return self.path


def _stored_tools(store, book, tmp_path):
    t = JobTools(Job.open("j", store), _Provider(book), tmp_path / "out")
    t.add_file("a", "jan.xlsx", "f")
    assert t.save_spec(json.dumps(_excel_spec()), "")["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    return t


class TestRunPipelineRecordRetry:
    def test_a_concurrent_unrelated_write_is_retried(self, book, tmp_path, monkeypatch):
        store = FileStore(tmp_path / "store")
        t = _stored_tools(store, book, tmp_path)
        real = run

        def racing(*a, **k):
            rep = real(*a, **k)
            Job.open("j", store).update(lambda s: s)
            return rep

        monkeypatch.setattr("moyung.tools.run", racing)
        assert t.run_pipeline()["ok"] is True
        assert Job.open("j", store).state.last_run is not None

    def test_a_concurrent_spec_change_is_refused(self, book, tmp_path, monkeypatch):
        store = FileStore(tmp_path / "store")
        t = _stored_tools(store, book, tmp_path)
        real = run

        def racing(*a, **k):
            rep = real(*a, **k)
            other = JobTools(Job.open("j", store), _Provider(book), tmp_path / "out")
            spec = _excel_spec()
            spec["outputs"] = {"other": {"input": "t"}}
            assert other.save_spec(json.dumps(spec), "rename the output")["saved"]
            return rep

        monkeypatch.setattr("moyung.tools.run", racing)
        res = t.run_pipeline()
        assert "the spec changed while this run was being recorded" in res["error"]
        assert Job.open("j", store).state.last_run is None


class TestFileStoreErrors:
    def test_a_read_error_is_a_state_error(self, tmp_path, monkeypatch):
        store = FileStore(tmp_path)
        Job(new_state("j"), store).update(lambda s: s)

        def boom(self, *a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(type(tmp_path), "read_text", boom)
        with pytest.raises(StateError, match="cannot read job-j.json: disk gone"):
            store.load("j")

    def test_a_file_that_stays_locked_for_reading_gives_up(self, tmp_path, monkeypatch):
        store = FileStore(tmp_path)

        def locked(self, *a, **k):
            raise PermissionError("locked")

        monkeypatch.setattr(type(tmp_path), "read_text", locked)
        monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
        with pytest.raises(StateError, match="cannot read job-j.json"):
            store.load("j")

    def test_a_write_error_leaves_no_part_file(self, tmp_path, monkeypatch):
        store = FileStore(tmp_path)

        def fail(tmp, path):
            raise OSError("full")

        monkeypatch.setattr(state_mod, "_replace", fail)
        with pytest.raises(StateError, match="cannot write job-j.json: full"):
            Job(new_state("j"), store).update(lambda s: s)
        assert not list(tmp_path.glob("*.part")) and store.load("j") is None

    def test_a_directory_that_cannot_be_made_is_a_state_error(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(StateError, match="cannot write"):
            Job(new_state("j"), FileStore(blocker / "sub")).update(lambda s: s)

    def test_a_fresh_lock_held_by_another_writer_times_out(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state_mod, "_LOCK_WAIT", 0.05)
        (tmp_path / "job-j.json.lock").write_text("")
        with pytest.raises(StateError, match="locked by another writer"):
            Job(new_state("j"), FileStore(tmp_path)).update(lambda s: s)

    def test_a_stale_lock_is_taken_over(self, tmp_path):
        lock = tmp_path / "job-j.json.lock"
        lock.write_text("")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        Job(new_state("j"), FileStore(tmp_path)).update(lambda s: s)
        assert FileStore(tmp_path).load("j") is not None and not lock.exists()

    def test_a_lock_that_cannot_be_made_is_a_state_error(self, tmp_path, monkeypatch):
        def no(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(state_mod.os, "open", no)
        with pytest.raises(StateError, match="cannot lock job-j.json: read-only"):
            Job(new_state("j"), FileStore(tmp_path)).update(lambda s: s)


def test_a_check_whose_query_fails_at_run_time_is_broken_not_passed():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 1 v")
    schemas = {"t": (Column("v", "INTEGER"),)}
    con.execute("DROP TABLE t")
    check = TypeAdapter(Check).validate_python({"kind": "not_null", "table": "t", "columns": ["v"]})
    results, broken = run_checks({"c": check}, con, schemas)
    assert results == [] and broken["c"].startswith("could not run:")
