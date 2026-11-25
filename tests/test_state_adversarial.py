from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from moyung import state as state_mod
from moyung.state import (
    MAX_RUNS,
    STATE_SCHEMA,
    FileRef,
    FileStore,
    Job,
    JobState,
    LoadRecord,
    StateError,
    add_file,
    new_state,
)

SHA = "a" * 64


def _state(**over):
    return JobState(job_id="job1", **over)


class TestSchemaVersion:
    def test_the_current_version_is_written_by_default(self):
        assert json.loads(_state().model_dump_json())["schema_version"] == STATE_SCHEMA

    @pytest.mark.parametrize("version", [0, -1])
    def test_zero_and_negative_versions_are_refused(self, version):
        with pytest.raises(ValidationError):
            _state(schema_version=version)

    @pytest.mark.parametrize("version", [STATE_SCHEMA + 1, STATE_SCHEMA + 50])
    def test_a_newer_version_is_refused_with_an_upgrade_hint(self, version):
        with pytest.raises(StateError, match="upgrade moyung"):
            Job.from_state(json.dumps({"job_id": "job1", "schema_version": version}))

    @pytest.mark.parametrize("version", [1.5, None, [1], {}])
    def test_non_integer_versions_are_refused(self, version):
        with pytest.raises(StateError):
            Job.from_state(json.dumps({"job_id": "job1", "schema_version": version}))

    @pytest.mark.xfail(strict=True, reason="BUG-009: lax coercion accepts True and '1' as versions")
    @pytest.mark.parametrize("version", ["1", True])
    def test_a_string_or_bool_is_not_a_version(self, version):
        with pytest.raises(StateError):
            Job.from_state(json.dumps({"job_id": "job1", "schema_version": version}))

    def test_a_missing_version_means_the_current_one(self):
        job = Job.from_state(json.dumps({"job_id": "job1"}))
        assert job.state.schema_version == STATE_SCHEMA


class TestFileRef:
    @pytest.mark.parametrize(
        "sha",
        ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, "a" * 63 + "\n", None, 5],
    )
    def test_bad_hashes(self, sha):
        with pytest.raises(ValidationError):
            FileRef(id="x", filename="f.xlsx", sha256=sha)

    @pytest.mark.parametrize("field", ["id", "filename"])
    def test_empty_and_overlong_names(self, field):
        base = {"id": "x", "filename": "f.xlsx", "sha256": SHA}
        for bad in ("", "x" * 301):
            with pytest.raises(ValidationError):
                FileRef(**{**base, field: bad})

    def test_limits_themselves_are_allowed(self):
        FileRef(id="x" * 200, filename="f" * 300, sha256=SHA)

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValidationError):
            FileRef(id="x", filename="f", sha256=SHA, kind="pdf")

    def test_the_default_kind_is_omitted_and_the_others_kept(self):
        base = {"id": "x", "filename": "f", "sha256": SHA}
        assert "kind" not in json.loads(FileRef(**base).model_dump_json())
        assert json.loads(FileRef(**base, kind="csv").model_dump_json())["kind"] == "csv"

    @pytest.mark.parametrize("kind", ["excel", "csv", "parquet"])
    def test_every_kind_round_trips(self, kind):
        ref = FileRef(id="x", filename="f", sha256=SHA, kind=kind)
        assert FileRef.model_validate_json(ref.model_dump_json()) == ref

    def test_unknown_keys_are_refused(self):
        with pytest.raises(ValidationError):
            FileRef(id="x", filename="f", sha256=SHA, extra=1)


class TestLimits:
    def test_runs_are_capped(self):
        from moyung.engine import RunSummary

        fields = RunSummary.model_fields
        assert MAX_RUNS == 20 and "status" in fields

    def test_files_are_capped_at_fifty(self):
        files = [FileRef(id=f"f{i}", filename="a", sha256=SHA) for i in range(51)]
        with pytest.raises(ValidationError):
            _state(files=files)
        _state(files=files[:50])

    def test_loads_are_capped_at_two_hundred(self):
        rec = LoadRecord(
            output="o", platform="p", table="t", status="ok", rows=0, ref="r", at="2026-01-01"
        )
        with pytest.raises(ValidationError):
            _state(loads=[rec] * 201)
        _state(loads=[rec] * 200)

    @pytest.mark.parametrize("rows", [-1])
    def test_a_load_cannot_report_negative_rows(self, rows):
        with pytest.raises(ValidationError):
            LoadRecord(output="o", platform="p", table="t", status="ok", rows=rows, ref="r", at="x")

    def test_a_load_note_is_capped(self):
        kwargs = {
            "output": "o",
            "platform": "p",
            "table": "t",
            "status": "failed",
            "rows": 0,
            "ref": "r",
            "at": "x",
        }
        LoadRecord(**kwargs, note="n" * 500)
        with pytest.raises(ValidationError):
            LoadRecord(**kwargs, note="n" * 501)

    def test_a_load_status_must_be_known(self):
        with pytest.raises(ValidationError):
            LoadRecord(output="o", platform="p", table="t", status="done", rows=0, ref="r", at="x")

    def test_a_negative_version_is_refused(self):
        with pytest.raises(ValidationError):
            _state(version=-1)


class TestConsistency:
    def test_a_role_must_point_at_a_known_file(self):
        with pytest.raises(ValidationError, match="not in files"):
            _state(roles={"f": "ghost"})

    def test_duplicate_file_ids_are_refused(self):
        ref = FileRef(id="x", filename="f", sha256=SHA)
        with pytest.raises(ValidationError, match="same id"):
            _state(files=[ref, ref])

    def test_two_roles_may_share_one_file(self):
        ref = FileRef(id="x", filename="f", sha256=SHA)
        assert _state(files=[ref], roles={"a": "x", "b": "x"}).roles == {"a": "x", "b": "x"}

    @pytest.mark.parametrize(
        "job_id",
        ["", "A", "a b", "a/b", "../x", "a\\b", ".a", "a.", "-a", "a" * 101, "é", "a\x00", "con "],
    )
    def test_bad_job_ids(self, job_id):
        with pytest.raises(StateError):
            new_state(job_id)

    @pytest.mark.parametrize("job_id", ["a", "0", "a-b", "a_b", "a.b", "a" * 100, "9z", "a..b"])
    def test_good_job_ids(self, job_id):
        assert new_state(job_id).job_id == job_id

    @pytest.mark.parametrize("job_id", [None, 5, ["a"]])
    def test_job_ids_of_the_wrong_type(self, job_id):
        with pytest.raises(StateError):
            new_state(job_id)

    @pytest.mark.xfail(strict=True, reason="BUG-009: lax coercion turns bytes into a job_id")
    def test_bytes_are_not_a_job_id(self):
        with pytest.raises(StateError):
            new_state(b"abc")


class TestAddFile:
    def test_replacing_keeps_the_position(self):
        s = add_file(new_state("j"), "a", "a.xlsx", SHA)
        s = add_file(s, "b", "b.xlsx", SHA)
        s = add_file(s, "a", "a2.xlsx", "b" * 64)
        assert [(f.id, f.filename) for f in s.files] == [("a2" and "a", "a2.xlsx"), ("b", "b.xlsx")]
        assert s.files[0].sha256 == "b" * 64

    def test_the_old_state_is_not_changed(self):
        before = new_state("j")
        add_file(before, "a", "a.xlsx", SHA, role="r")
        assert before.files == [] and before.roles == {}

    def test_a_second_role_for_a_file_adds_to_roles(self):
        s = add_file(new_state("j"), "a", "a.xlsx", SHA, role="r1")
        s = add_file(s, "a", "a.xlsx", SHA, role="r2")
        assert s.roles == {"r1": "a", "r2": "a"}

    def test_a_role_can_be_pointed_at_another_file(self):
        s = add_file(new_state("j"), "a", "a.xlsx", SHA, role="r")
        s = add_file(s, "b", "b.xlsx", SHA, role="r")
        assert s.roles == {"r": "b"}

    def test_no_role_leaves_roles_alone(self):
        assert add_file(new_state("j"), "a", "a.xlsx", SHA).roles == {}

    @pytest.mark.parametrize("role", [5, b"r", ["r"]])
    def test_a_role_must_be_text(self, role):
        with pytest.raises(StateError, match="role must be text"):
            add_file(new_state("j"), "a", "a.xlsx", SHA, role=role)

    @pytest.mark.parametrize("sha", ["short", "A" * 64])
    def test_bad_hash_is_a_state_error(self, sha):
        with pytest.raises(StateError):
            add_file(new_state("j"), "a", "a.xlsx", sha)

    def test_the_fifty_first_file_is_a_state_error_through_update(self):
        job = Job(new_state("j"))
        for i in range(50):
            job.update(lambda s, i=i: add_file(s, f"f{i}", "a", SHA))
        with pytest.raises((StateError, ValidationError)):
            job.update(lambda s: add_file(s, "f50", "a", SHA))
        assert len(job.state.files) == 50


class TestJob:
    @pytest.mark.parametrize("text", [None, 5, b"{}", {}, ["x"]])
    def test_from_state_needs_a_string(self, text):
        with pytest.raises(StateError, match="must be a JSON string"):
            Job.from_state(text)

    @pytest.mark.parametrize("text", ["", "null", "[]", "42", '"x"', "{", '{"job_id": 5}'])
    def test_from_state_refuses_text_that_is_not_a_state(self, text):
        with pytest.raises(StateError):
            Job.from_state(text)

    def test_an_update_must_return_a_job_state(self):
        job = Job(new_state("j"))
        with pytest.raises(StateError, match="must return a JobState"):
            job.update(lambda s: None)
        assert job.state.version == 0

    def test_an_update_cannot_change_the_job_id(self):
        job = Job(new_state("j"))
        with pytest.raises(StateError, match="cannot change job_id"):
            job.update(lambda s: s.model_copy(update={"job_id": "other"}))

    def test_an_update_that_raises_leaves_the_job_alone(self):
        job = Job(new_state("j"))

        def boom(s):
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            job.update(boom)
        assert job.state.version == 0

    def test_the_version_counts_every_update(self):
        job = Job(new_state("j"))
        for _ in range(5):
            job.update(lambda s: s)
        assert job.state.version == 5

    def test_state_is_a_deep_copy(self):
        job = Job(add_file(new_state("j"), "a", "a", SHA))
        job.state.files.clear()
        assert len(job.state.files) == 1

    def test_state_json_round_trips(self):
        job = Job(add_file(new_state("j"), "a", "a", SHA, role="r"))
        assert Job.from_state(job.state_json()).state == job.state

    def test_open_with_a_stored_state_for_another_job_is_refused(self, tmp_path):
        store = FileStore(tmp_path)
        Job.open("one", store).update(lambda s: s)
        (tmp_path / "job-one.json").rename(tmp_path / "job-two.json")
        with pytest.raises(StateError, match="not 'two'"):
            Job.open("two", store)


class TestFileStore:
    @pytest.mark.parametrize("job_id", ["../x", "a/b", "", "A", None, 5])
    def test_bad_ids_never_touch_the_disk(self, tmp_path, job_id):
        store = FileStore(tmp_path / "s")
        with pytest.raises(StateError):
            store.load(job_id)
        with pytest.raises(StateError):
            store.save(job_id, "{}", 0)
        assert not (tmp_path / "s").exists()

    def test_loading_a_job_never_saved_is_none(self, tmp_path):
        assert FileStore(tmp_path).load("nope") is None
        assert FileStore(tmp_path / "missing_dir").load("nope") is None

    def test_a_stored_file_that_is_not_a_state_blocks_a_save(self, tmp_path):
        (tmp_path / "job-j.json").write_text("garbage", encoding="utf-8")
        with pytest.raises(StateError, match="not valid"):
            FileStore(tmp_path).save("j", new_state("j").model_dump_json(), 1)

    def test_a_directory_in_place_of_the_state_file_is_a_state_error(self, tmp_path):
        (tmp_path / "job-j.json").mkdir()
        with pytest.raises(StateError):
            FileStore(tmp_path).load("j")

    def test_the_store_folder_being_a_file_is_a_state_error(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x")
        with pytest.raises(StateError, match="cannot write"):
            FileStore(target).save("j", new_state("j").model_dump_json(), 0)

    def test_no_part_or_lock_file_is_left_behind(self, tmp_path):
        store = FileStore(tmp_path)
        job = Job.open("j", store)
        job.update(lambda s: s)
        job.update(lambda s: s)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["job-j.json"]

    def test_a_live_lock_times_out_with_a_state_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state_mod, "_LOCK_WAIT", 0.05)
        store = FileStore(tmp_path)
        lock = next(
            (p for p in [tmp_path / "job-j.json.lock"]),
        )
        lock.write_text("held")
        with pytest.raises(StateError, match="locked"):
            store.save("j", new_state("j").model_dump_json(), 0)
        assert lock.exists()

    def test_save_with_a_wrong_expected_version_changes_nothing(self, tmp_path):
        store = FileStore(tmp_path)
        text = new_state("j").model_dump_json()
        store.save("j", text, 0)
        with pytest.raises(state_mod.StateConflict, match="version 0"):
            store.save("j", text, 7)
        assert store.load("j") == text

    def test_unicode_in_a_state_survives_the_store(self, tmp_path):
        store = FileStore(tmp_path)
        job = Job.open("j", store)
        job.update(lambda s: add_file(s, "a", "laporan_ké_😀.xlsx", SHA))
        assert Job.open("j", store).state.files[0].filename == "laporan_ké_😀.xlsx"
