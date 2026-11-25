from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from conftest import make_xlsx
from test_coverage_gaps import ROWS, _excel_spec

from moyung.auto import AutoReport, auto
from moyung.workbook import WorkbookError


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "jan.xlsx"
    path.write_bytes(make_xlsx({"Data": ROWS}))
    return path


@pytest.fixture
def specs(tmp_path):
    folder = tmp_path / "specs"
    folder.mkdir()
    (folder / "s.json").write_text(json.dumps(_excel_spec()))
    return folder


def _cli(*args):
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    return subprocess.run(
        [sys.executable, "-m", "moyung.auto", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestSpecFolder:
    def test_files_that_are_not_json_are_ignored(self, book, specs, tmp_path):
        (specs / "notes.txt").write_text("hi")
        (specs / "s.json.bak").write_text("{}")
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "ok" and rep.tried == {}

    def test_a_missing_specs_folder_is_no_spec_not_a_crash(self, book, tmp_path):
        rep = auto(book, tmp_path / "nodir", tmp_path / "out")
        assert rep.status == "no_spec" and "no saved spec" in rep.tried["-"]

    def test_specs_are_tried_in_name_order(self, book, specs, tmp_path):
        (specs / "a.json").write_text(json.dumps(_excel_spec()))
        rep = auto(book, specs, tmp_path / "out")
        assert rep.spec == "a"

    def test_a_spec_that_is_not_utf8_is_skipped_with_a_reason(self, book, specs, tmp_path):
        (specs / "a_bin.json").write_bytes(b"\xff\xfe\x00")
        rep = auto(book, specs, tmp_path / "out")
        assert rep.status == "ok" and rep.spec == "s"
        assert rep.tried["a_bin"].startswith("bad spec:")

    def test_an_empty_json_file_is_a_bad_spec(self, book, specs, tmp_path):
        (specs / "a_empty.json").write_text("")
        rep = auto(book, specs, tmp_path / "out")
        assert rep.tried["a_empty"].startswith("bad spec:") and rep.spec == "s"

    @pytest.mark.parametrize("body", ["[]", "null", "42", '"text"'])
    def test_valid_json_that_is_not_a_spec_is_skipped(self, book, specs, tmp_path, body):
        (specs / "a_odd.json").write_text(body)
        rep = auto(book, specs, tmp_path / "out")
        assert rep.tried["a_odd"].startswith("bad spec:") and rep.spec == "s"

    @pytest.mark.xfail(strict=True, reason="BUG-007: an empty spec fits every workbook vacuously")
    def test_an_empty_spec_does_not_hijack_the_run(self, book, specs, tmp_path):
        (specs / "a_empty.json").write_text("{}")
        rep = auto(book, specs, tmp_path / "out")
        assert rep.spec == "s"

    def test_only_bad_specs_means_no_spec_with_every_reason(self, book, tmp_path):
        folder = tmp_path / "bad"
        folder.mkdir()
        for name in ("x", "y"):
            (folder / f"{name}.json").write_text("{nope")
        rep = auto(book, folder, tmp_path / "out")
        assert rep.status == "no_spec" and set(rep.tried) == {"x", "y"}

    def test_the_closest_spec_wins_when_none_fit(self, book, tmp_path):
        folder = tmp_path / "near"
        folder.mkdir()
        two = {
            "sources": {
                "t": _excel_spec()["sources"]["t"],
                "u": _excel_spec(sheet="Missing")["sources"]["t"],
            },
            "steps": [],
            "outputs": {"clean": {"input": "t"}},
        }
        (folder / "a_far.json").write_text(json.dumps(_excel_spec(sheet="Missing")))
        (folder / "b_near.json").write_text(json.dumps(two))
        rep = auto(book, folder, tmp_path / "out")
        assert rep.status == "no_spec" and rep.spec == "b_near"
        assert rep.tried == {"a_far": "0/1 sources found", "b_near": "1/2 sources found"}


class TestPaths:
    def test_string_paths_work_like_path_objects(self, book, specs, tmp_path):
        rep = auto(str(book), str(specs), str(tmp_path / "out"))
        assert rep.status == "ok"

    def test_a_dotted_file_stem_is_kept_whole(self, tmp_path, specs):
        path = tmp_path / "jan.v2.xlsx"
        path.write_bytes(make_xlsx({"Data": ROWS}))
        rep = auto(path, specs, tmp_path / "out")
        assert rep.out_dir.endswith("jan.v2")
        assert (tmp_path / "out" / "s" / "jan.v2" / "clean.parquet").exists()

    def test_two_files_with_the_same_stem_share_a_folder(self, tmp_path, specs):
        one = tmp_path / "a"
        two = tmp_path / "b"
        one.mkdir()
        two.mkdir()
        for folder in (one, two):
            (folder / "jan.xlsx").write_bytes(make_xlsx({"Data": ROWS}))
        first = auto(one / "jan.xlsx", specs, tmp_path / "out")
        second = auto(two / "jan.xlsx", specs, tmp_path / "out")
        assert first.out_dir == second.out_dir

    def test_running_twice_into_the_same_folder_is_stable(self, book, specs, tmp_path):
        first = auto(book, specs, tmp_path / "out")
        second = auto(book, specs, tmp_path / "out")
        assert first.run.outputs[0].sha256 == second.run.outputs[0].sha256
        assert second.status == "ok"

    def test_a_unicode_file_name_is_fine(self, tmp_path, specs):
        path = tmp_path / "laporan_bulan_ké.xlsx"
        path.write_bytes(make_xlsx({"Data": ROWS}))
        rep = auto(path, specs, tmp_path / "out")
        assert rep.status == "ok" and rep.out_dir.endswith("laporan_bulan_ké")


class TestReportText:
    def test_no_spec_text_names_what_was_skipped(self):
        rep = AutoReport(status="no_spec", tried={"a": "0/1 sources found"})
        assert rep.text() == "auto: no_spec\n  skipped a: 0/1 sources found"

    def test_text_with_a_spec_and_no_run(self):
        rep = AutoReport(status="no_spec", spec="near", bind="rebind: failed")
        assert rep.text() == "auto: no_spec with spec 'near'\nrebind: failed"

    def test_empty_report_is_one_line(self):
        assert AutoReport(status="no_spec").text() == "auto: no_spec"

    def test_report_survives_a_json_round_trip(self, book, specs, tmp_path):
        rep = auto(book, specs, tmp_path / "out")
        assert AutoReport.model_validate_json(rep.model_dump_json()) == rep


class TestUnreadableInput:
    def test_missing_file_raises_a_workbook_error_today(self, specs, tmp_path):
        with pytest.raises(WorkbookError):
            auto(tmp_path / "nope.xlsx", specs, tmp_path / "out")

    def test_nothing_is_written_for_an_unreadable_file(self, specs, tmp_path):
        bad = tmp_path / "bad.xlsx"
        bad.write_bytes(b"not a zip")
        with pytest.raises(WorkbookError):
            auto(bad, specs, tmp_path / "out")
        assert not (tmp_path / "out").exists()

    @pytest.mark.xfail(
        strict=True, raises=WorkbookError, reason="BUG-003: unreadable input raises, no report"
    )
    @pytest.mark.parametrize(
        "name, payload",
        [
            ("x.csv", b"Plant,Product,Ton\nA,u,1\n"),
            ("bad.xlsx", b"not a zip"),
            ("empty.xlsx", b""),
            ("missing.xlsx", None),
        ],
    )
    def test_unreadable_input_gives_a_report_not_an_exception(self, specs, tmp_path, name, payload):
        path = tmp_path / name
        if payload is not None:
            path.write_bytes(payload)
        rep = auto(path, specs, tmp_path / "out")
        assert rep.status != "ok"

    @pytest.mark.xfail(strict=True, reason="BUG-003: CLI prints a traceback on unreadable input")
    def test_cli_on_a_corrupt_file_exits_cleanly(self, specs, tmp_path):
        bad = tmp_path / "bad.xlsx"
        bad.write_bytes(b"not a zip")
        r = _cli(bad, specs, tmp_path / "out")
        assert r.returncode == 1 and "Traceback" not in r.stderr


class TestCli:
    def test_cli_with_too_many_arguments_prints_usage(self):
        r = _cli("a", "b", "c", "d")
        assert r.returncode == 1 and "python -m moyung.auto" in r.stderr

    def test_cli_with_no_arguments_prints_usage(self):
        r = _cli()
        assert r.returncode == 1 and "python -m moyung.auto" in r.stderr

    def test_cli_prints_the_skipped_specs(self, book, specs, tmp_path):
        (specs / "a_bad.json").write_text("{nope")
        r = _cli(book, specs, tmp_path / "out")
        assert r.returncode == 0 and "skipped a_bad" in r.stdout

    def test_cli_failed_check_exits_one(self, book, specs, tmp_path):
        spec = {**_excel_spec(), "checks": {"c": {"kind": "row_count", "table": "t", "min": 9}}}
        (specs / "s.json").write_text(json.dumps(spec))
        r = _cli(book, specs, tmp_path / "out")
        assert r.returncode == 1 and "auto: failed" in r.stdout
