from __future__ import annotations

import json

import duckdb
import pytest
from conftest import make_xlsx

from moyung.engine import (
    Issue,
    PipelineError,
    RunReport,
    _explain,
    _upstream,
    run,
    validate,
)
from moyung.pipeline import SpecError, parse_spec
from moyung.workbook import Workbook

ROWS = [["A", "B"], [1, "x"], [2, 'y,"q"\nz'], [3, "é😀"], [4, None]]


def _spec(**parts):
    base = {"sources": {}, "steps": [], "outputs": {}}
    base.update(parts)
    return parse_spec(json.dumps(base))


def _src(sheet="S", anchor="A", role="f"):
    return {
        "file_role": role,
        "sheet": {"name": sheet},
        "region": {"header_anchor": [anchor], "header_rows": 1},
    }


def _simple(**extra):
    parts = {"sources": {"a": _src()}, "outputs": {"o": {"input": "a"}}}
    parts.update(extra)
    return _spec(**parts)


@pytest.fixture
def wb():
    return Workbook.open(make_xlsx({"S": ROWS}))


class TestArguments:
    @pytest.mark.parametrize("hitl", ["x", 5, {}, []])
    def test_hitl_of_the_wrong_type_cannot_start(self, wb, tmp_path, hitl):
        with pytest.raises(PipelineError) as e:
            run(_simple(), {"f": wb}, tmp_path / "o", hitl=hitl)
        assert e.value.issues[0].where == "hitl"

    @pytest.mark.parametrize("files", [None, [], "f", 5, ("f", 1)])
    def test_files_must_be_a_dict(self, wb, tmp_path, files):
        with pytest.raises(PipelineError) as e:
            run(_simple(), files, tmp_path / "o")
        assert e.value.issues[0].where == "files"

    @pytest.mark.parametrize("out_dir", [None, 5, 1.5, ["a"], {}])
    def test_out_dir_must_be_a_path(self, wb, out_dir):
        with pytest.raises(PipelineError) as e:
            run(_simple(), {"f": wb}, out_dir)
        assert e.value.issues[0].where == "out_dir"

    def test_out_dir_that_is_a_file_cannot_start(self, wb, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x")
        with pytest.raises(PipelineError, match="cannot use"):
            run(_simple(), {"f": wb}, target)
        assert target.read_text() == "x"

    def test_out_dir_is_created_with_parents(self, wb, tmp_path):
        rep = run(_simple(), {"f": wb}, tmp_path / "a" / "b" / "c")
        assert rep.status == "ok" and (tmp_path / "a" / "b" / "c" / "o.parquet").is_file()

    def test_out_dir_may_be_a_string(self, wb, tmp_path):
        assert run(_simple(), {"f": wb}, str(tmp_path / "o")).status == "ok"

    def test_an_existing_out_dir_is_reused(self, wb, tmp_path):
        (tmp_path / "o").mkdir()
        (tmp_path / "o" / "keep.txt").write_text("k")
        assert run(_simple(), {"f": wb}, tmp_path / "o").status == "ok"
        assert (tmp_path / "o" / "keep.txt").read_text() == "k"


class TestInputs:
    def test_missing_file_role_is_an_issue_on_the_source(self, tmp_path):
        rep = run(_simple(), {}, tmp_path / "o")
        assert rep.status == "failed"
        assert rep.issues[0].where == "sources.a"
        assert "no file for file_role 'f'" in rep.issues[0].message

    def test_a_path_to_a_missing_file_is_an_issue_not_an_exception(self, tmp_path):
        rep = run(_simple(), {"f": str(tmp_path / "nope.xlsx")}, tmp_path / "o")
        assert rep.status == "failed" and "cannot read file" in rep.issues[0].message

    def test_extra_roles_are_ignored(self, wb, tmp_path):
        rep = run(_simple(), {"f": wb, "unused": wb}, tmp_path / "o")
        assert rep.status == "ok" and set(rep.files) == {"f", "unused"}

    def test_a_header_only_sheet_is_a_failed_source(self, tmp_path):
        empty = Workbook.open(make_xlsx({"S": [["A", "B"]]}))
        rep = run(_simple(), {"f": empty}, tmp_path / "o")
        assert rep.status == "failed" and "not inside a table" in rep.issues[0].message

    def test_wrong_sheet_name_is_an_issue(self, wb, tmp_path):
        spec = _spec(sources={"a": _src(sheet="Nope")}, outputs={"o": {"input": "a"}})
        rep = run(spec, {"f": wb}, tmp_path / "o")
        assert rep.status == "failed" and not list((tmp_path / "o").glob("*"))

    def test_no_outputs_is_ok_with_a_warning(self, wb, tmp_path):
        rep = run(_spec(sources={"a": _src()}), {"f": wb}, tmp_path / "o")
        assert rep.status == "ok" and "no outputs" in rep.warnings[0]
        assert list((tmp_path / "o").glob("*")) == []

    def test_a_spec_with_nothing_at_all_runs(self, tmp_path):
        rep = run(_spec(), {}, tmp_path / "o")
        assert rep.status == "ok" and rep.outputs == ()


class TestOutputs:
    def test_csv_quotes_commas_quotes_and_newlines(self, wb, tmp_path):
        spec = _simple(outputs={"o": {"input": "a", "format": "csv"}})
        run(spec, {"f": wb}, tmp_path / "o")
        text = (tmp_path / "o" / "o.csv").read_text(encoding="utf-8")
        assert text == 'A,B\n1,x\n2,"y,""q""\nz"\n3,é😀\n4,\n'

    def test_csv_round_trips_through_duckdb(self, wb, tmp_path):
        run(_simple(outputs={"o": {"input": "a", "format": "csv"}}), {"f": wb}, tmp_path / "o")
        got = duckdb.sql(f"SELECT * FROM read_csv('{(tmp_path / 'o' / 'o.csv').as_posix()}')")
        assert got.fetchall() == [(1, "x"), (2, 'y,"q"\nz'), (3, "é😀"), (4, None)]

    def test_parquet_round_trips_with_nulls(self, wb, tmp_path):
        run(_simple(), {"f": wb}, tmp_path / "o")
        path = (tmp_path / "o" / "o.parquet").as_posix()
        assert duckdb.sql(f"SELECT B FROM '{path}' ORDER BY A").fetchall()[-1] == (None,)

    def test_two_outputs_may_share_one_input(self, wb, tmp_path):
        spec = _simple(outputs={"o": {"input": "a"}, "p": {"input": "a", "format": "csv"}})
        rep = run(spec, {"f": wb}, tmp_path / "o")
        assert [o.name for o in rep.outputs] == ["o", "p"]
        assert {o.rows for o in rep.outputs} == {4}

    def test_output_stat_matches_the_file_on_disk(self, wb, tmp_path):
        import hashlib

        rep = run(_simple(), {"f": wb}, tmp_path / "o")
        stat = rep.outputs[0]
        assert (
            stat.sha256 == hashlib.sha256((tmp_path / "o" / "o.parquet").read_bytes()).hexdigest()
        )
        assert (stat.rows, stat.columns, stat.format) == (4, 2, "parquet")

    def test_a_run_is_deterministic(self, wb, tmp_path):
        first = run(_simple(), {"f": wb}, tmp_path / "a")
        second = run(_simple(), {"f": wb}, tmp_path / "b")
        assert first.outputs[0].sha256 == second.outputs[0].sha256
        assert first.spec_sha256 == second.spec_sha256

    def test_no_part_files_are_left_after_success(self, wb, tmp_path):
        run(_simple(), {"f": wb}, tmp_path / "o")
        assert not list((tmp_path / "o").glob("*.part"))

    def test_an_empty_result_is_still_written(self, wb, tmp_path):
        spec = _simple(
            steps=[
                {"id": "none", "node": "filter", "inputs": ["a"], "config": {"where": "A > 99"}}
            ],
            outputs={"o": {"input": "none"}},
        )
        rep = run(spec, {"f": wb}, tmp_path / "o")
        assert rep.status == "ok" and rep.outputs[0].rows == 0
        assert (tmp_path / "o" / "o.parquet").is_file()


class TestFailures:
    def _bad_step(self):
        return _simple(
            steps=[{"id": "s", "node": "select", "inputs": ["a"], "config": {"columns": ["Z"]}}],
            outputs={"o": {"input": "s"}},
        )

    def test_a_bad_step_writes_nothing(self, wb, tmp_path):
        rep = run(self._bad_step(), {"f": wb}, tmp_path / "o")
        assert rep.status == "failed" and rep.outputs == ()
        assert rep.issues[0].where == "steps.s"
        assert list((tmp_path / "o").glob("*")) == []

    def test_a_failed_run_names_files_from_an_earlier_run(self, wb, tmp_path):
        run(_simple(), {"f": wb}, tmp_path / "o")
        rep = run(self._bad_step(), {"f": wb}, tmp_path / "o")
        assert rep.status == "failed"
        assert (tmp_path / "o" / "o.parquet").is_file()
        assert any("o.parquet" in w and "earlier run" in w for w in rep.warnings)

    def test_a_failed_run_with_two_old_files_uses_the_plural(self, wb, tmp_path):
        both = _simple(outputs={"o": {"input": "a"}, "p": {"input": "a", "format": "csv"}})
        run(both, {"f": wb}, tmp_path / "o")
        bad = _simple(
            steps=[{"id": "s", "node": "select", "inputs": ["a"], "config": {"columns": ["Z"]}}],
            outputs={"o": {"input": "s"}, "p": {"input": "s", "format": "csv"}},
        )
        rep = run(bad, {"f": wb}, tmp_path / "o")
        assert any("are from an earlier run" in w and "them" in w for w in rep.warnings)

    def test_a_step_reading_an_unknown_table_is_refused_when_the_spec_is_parsed(self):
        with pytest.raises(SpecError, match="'ghost'"):
            _simple(
                steps=[
                    {"id": "s", "node": "select", "inputs": ["ghost"], "config": {"columns": ["A"]}}
                ],
                outputs={"o": {"input": "s"}},
            )

    def test_a_step_cannot_read_a_later_step(self):
        steps = [
            {"id": "one", "node": "select", "inputs": ["two"], "config": {"columns": ["A"]}},
            {"id": "two", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}},
        ]
        with pytest.raises(SpecError, match="'two'"):
            _simple(steps=steps, outputs={"o": {"input": "two"}})

    def test_steps_after_a_failed_step_are_skipped_with_a_warning(self, wb, tmp_path):
        steps = [
            {"id": "bad", "node": "select", "inputs": ["a"], "config": {"columns": ["Z"]}},
            {"id": "next", "node": "select", "inputs": ["bad"], "config": {"columns": ["A"]}},
        ]
        rep = run(_simple(steps=steps, outputs={"o": {"input": "next"}}), {"f": wb}, tmp_path / "o")
        assert [i.where for i in rep.issues] == ["steps.bad"]
        assert any("skipped because 'bad'" in w for w in rep.warnings)

    def test_a_runtime_conversion_failure_is_a_failed_step(self, wb, tmp_path):
        steps = [
            {
                "id": "c",
                "node": "cast",
                "inputs": ["a"],
                "config": {"types": {"B": "BIGINT"}, "strict": True},
            }
        ]
        rep = run(_simple(steps=steps, outputs={"o": {"input": "c"}}), {"f": wb}, tmp_path / "o")
        assert rep.status == "failed" and "failed while running cast" in rep.issues[0].message

    def test_publish_failure_names_what_was_replaced_and_what_is_old(self, wb, tmp_path):
        out = tmp_path / "o"
        out.mkdir()
        (out / "p.csv").mkdir()
        spec = _simple(outputs={"o": {"input": "a"}, "p": {"input": "a", "format": "csv"}})
        rep = run(spec, {"f": wb}, out)
        assert rep.status == "failed"
        message = rep.issues[0].message
        assert "already replaced: o.parquet" in message and "still old: p.csv" in message
        assert not list(out.glob("*.part"))

    def test_a_failed_publish_keeps_status_failed_and_no_outputs(self, wb, tmp_path):
        out = tmp_path / "o"
        out.mkdir()
        (out / "o.parquet").mkdir()
        rep = run(_simple(), {"f": wb}, out)
        assert rep.status == "failed" and rep.outputs == () and rep.verify is None


class TestExplain:
    def _step(self):
        from moyung.pipeline import Step

        return Step(id="s", node="select", inputs=["a"], config={"columns": ["A"]})

    def test_a_binder_error_lists_the_columns_the_step_reads(self):
        from moyung.nodes.base import Column

        err = duckdb.BinderException('Referenced column "Z" not found in FROM clause!')
        text = _explain(err, self._step(), {"a": (Column("A", "BIGINT"), Column("B", "VARCHAR"))})
        assert "Columns this step reads: a: A, B." in text

    def test_a_binder_error_without_schemas_says_unknown(self):
        err = duckdb.BinderException('Referenced column "Z" not found in FROM clause!')
        assert "Columns this step reads: unknown." in _explain(err, self._step(), {})

    def test_a_cast_mix_gets_a_hint(self):
        err = duckdb.ConversionException("Could not convert string 'x' to INT64")
        assert "mixes text with numbers" in _explain(err, self._step(), {})

    def test_an_unimplemented_cast_gets_another_hint(self):
        err = duckdb.ConversionException("Unimplemented type for cast (DATE -> DOUBLE)")
        assert "cast through VARCHAR" in _explain(err, self._step(), {})

    def test_other_errors_keep_only_the_first_line(self):
        assert _explain(RuntimeError("first\nsecond"), self._step(), {}) == "first"


class TestUpstream:
    def test_a_source_is_its_own_upstream(self):
        assert _upstream(_simple(), "a") == {"a"}

    def test_a_chain_is_followed(self):
        steps = [
            {"id": "s1", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}},
            {"id": "s2", "node": "select", "inputs": ["s1"], "config": {"columns": ["A"]}},
        ]
        assert _upstream(_simple(steps=steps), "s2") == {"s2", "s1", "a"}

    def test_a_diamond_visits_each_table_once(self):
        steps = [
            {"id": "l", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}},
            {"id": "r", "node": "select", "inputs": ["a"], "config": {"columns": ["B"]}},
            {"id": "j", "node": "join", "inputs": ["l", "r"], "config": {"on": ["A"]}},
        ]
        assert _upstream(_simple(steps=steps), "j") == {"j", "l", "r", "a"}

    def test_an_unknown_name_is_just_itself(self):
        assert _upstream(_simple(), "ghost") == {"ghost"}


class TestReportShape:
    def test_report_survives_a_json_round_trip(self, wb, tmp_path):
        rep = run(_simple(), {"f": wb}, tmp_path / "o")
        assert RunReport.model_validate_json(rep.model_dump_json()) == rep

    def test_a_report_is_frozen_and_ignores_unknown_fields(self):
        issue = Issue(where="x", message="y", surprise=1)
        assert not hasattr(issue, "surprise")
        with pytest.raises(ValueError):
            issue.where = "z"

    def test_text_of_a_failed_run_names_the_issue(self, wb, tmp_path):
        rep = run(
            _simple(
                steps=[
                    {"id": "s", "node": "select", "inputs": ["a"], "config": {"columns": ["Z"]}}
                ],
                outputs={"o": {"input": "s"}},
            ),
            {"f": wb},
            tmp_path / "o",
        )
        assert "failed" in rep.text() and "steps.s" in rep.text()

    def test_validate_without_outputs_is_ok(self, wb):
        assert validate(_spec(sources={"a": _src()}), {"f": wb}).ok

    def test_validate_reports_a_missing_role(self):
        rep = validate(_simple(), {})
        assert not rep.ok and "file_role" in rep.issues[0].message
