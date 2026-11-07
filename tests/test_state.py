from __future__ import annotations

import hashlib
import json

import pytest
from conftest import make_xlsx

from excel_etl.engine import run
from excel_etl.hitl import HitlState, answer, approve, ask_human
from excel_etl.pipeline import parse_spec
from excel_etl.rebind import BindRecord
from excel_etl.state import (
    FileStore,
    Job,
    JobState,
    StateConflict,
    StateError,
    add_file,
    new_state,
    open_files,
)
from excel_etl.versions import SpecHistory

SHA = "a" * 64


def _spec():
    return parse_spec(
        json.dumps(
            {
                "sources": {
                    "s": {
                        "file_role": "f",
                        "sheet": {"name": "D"},
                        "region": {"header_anchor": ["Ton"]},
                    }
                },
                "outputs": {"o": {"input": "s"}},
            }
        )
    )


def _full() -> JobState:
    s = add_file(new_state("job1"), "f1", "jan.xlsx", SHA, role="f")
    q, h = ask_human(s.hitl, "clarify", "unit?", ref="sources.s")
    h, k = answer(h, s.knowledge, q.id, "ton")
    h = approve(h, "after_spec", "dina", ref="v1")
    return s.model_copy(
        update={
            "hitl": h,
            "knowledge": k,
            "history": SpecHistory.start(_spec()),
            "binds": {"s": BindRecord(sheet="D", header_row=0, first_col=0, columns=("Ton",))},
        }
    )


# --- the state itself ------------------------------------------------------------------------


def test_full_state_round_trips_through_json():
    s = _full()
    assert JobState.model_validate_json(s.model_dump_json()) == s
    assert Job.from_state(Job(s).state_json()).state == s


def test_last_run_round_trips(tmp_path):
    wb = make_xlsx({"D": [["Ton"], [1], [2]]})
    rep = run(_spec(), {"f": wb}, tmp_path)
    s = new_state("j").model_copy(update={"last_run": rep})
    assert JobState.model_validate_json(s.model_dump_json()).last_run == rep


@pytest.mark.parametrize(
    "bad", ["", "Job1", "a b", "a/b", "a\\b", "..", ".x", "x.", "é", "x" * 101, "a\nb", "-"]
)
def test_bad_job_ids_refused(bad):
    # "-" alone is fine as a char class member but must start with a letter or digit
    with pytest.raises(StateError):
        new_state(bad)


@pytest.mark.parametrize("good", ["a", "job-1", "j_2.v3", "0", "x" * 100, "con"])
def test_good_job_ids(good):
    assert new_state(good).job_id == good


def test_state_rejects_unknown_keys_and_inconsistency():
    with pytest.raises(StateError):
        Job.from_state('{"job_id": "j", "extra": 1}')
    with pytest.raises(StateError, match="not in files"):
        Job.from_state('{"job_id": "j", "roles": {"f": "nope"}}')
    two = '{"job_id":"j","files":[{"id":"a","filename":"x","sha256":"%s"},{"id":"a","filename":"y","sha256":"%s"}]}'
    with pytest.raises(StateError, match="same id"):
        Job.from_state(two % (SHA, SHA))
    with pytest.raises(StateError, match="64 lower-case"):
        add_file(new_state("j"), "a", "x", "ABC")


@pytest.mark.parametrize("bad", ["", "not json", "[]", "null", 5, None, b"{}"])
def test_from_state_bad_text_is_a_state_error(bad):
    with pytest.raises(StateError):
        Job.from_state(bad)


def test_add_file_replaces_same_id_and_maps_role():
    s = add_file(new_state("j"), "f1", "jan.xlsx", SHA, role="f")
    s = add_file(s, "f1", "feb.xlsx", "b" * 64)
    assert [(f.filename, f.sha256[0]) for f in s.files] == [("feb.xlsx", "b")]
    assert s.roles == {"f": "f1"}  # role kept when none given
    s = add_file(s, "f2", "other.xlsx", SHA, role="g")
    assert [f.id for f in s.files] == ["f1", "f2"] and s.roles == {"f": "f1", "g": "f2"}


def test_file_cap():
    s = new_state("j")
    for i in range(50):
        s = add_file(s, f"f{i}", "x.xlsx", SHA)
    over = add_file(s, "f50", "x.xlsx", SHA)  # model_copy does not validate...
    with pytest.raises(StateError, match="at most 50"):
        Job(s).update(lambda _: over)  # ...so the update refuses it


# --- job.update ------------------------------------------------------------------------------


def test_update_bumps_version_and_keeps_old_state_on_error():
    job = Job(new_state("j"))
    job.update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    assert job.state.version == 1 and len(job.state.files) == 1
    before = job.state

    def boom(s):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        job.update(boom)
    with pytest.raises(StateError, match="must return a JobState"):
        job.update(lambda s: None)
    with pytest.raises(StateError, match="cannot change job_id"):
        job.update(lambda s: s.model_copy(update={"job_id": "other"}))
    assert job.state == before


def test_update_refuses_a_state_that_would_not_load_again():
    job = Job(new_state("j"))
    # model_copy skips validation: this role points nowhere
    with pytest.raises(StateError, match="not in files"):
        job.update(lambda s: s.model_copy(update={"roles": {"f": "ghost"}}))
    assert job.state.version == 0 and job.state.roles == {}


def test_update_result_is_independent_of_the_callers_object():
    job = Job(new_state("j"))
    mine = job.update(lambda s: s)
    mine.files.append(None)  # type: ignore[arg-type]
    job.state.files.append(None)  # type: ignore[arg-type]
    assert job.state.files == []


# --- store mode ------------------------------------------------------------------------------


def test_store_job_saves_on_every_update_and_reopens(tmp_path):
    store = FileStore(tmp_path)
    job = Job.open("j", store)
    assert job.state.version == 0 and store.load("j") is None
    job.update(lambda s: add_file(s, "f1", "a.xlsx", SHA, role="f"))
    job.update(lambda s: s.model_copy(update={"hitl": approve(s.hitl, "after_spec", "dina")}))
    again = Job.open("j", store)
    assert again.state == job.state and again.state.version == 2
    assert again.state.hitl.approvals["after_spec"].by == "dina"


def test_two_writers_conflict_and_nothing_is_overwritten(tmp_path):
    store = FileStore(tmp_path)
    a, b = Job.open("j", store), Job.open("j", store)
    a.update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    with pytest.raises(StateConflict, match="version 1 in the store, not 0"):
        b.update(lambda s: add_file(s, "f2", "b.xlsx", SHA))
    assert b.state.version == 0 and not b.state.files  # b kept its state
    assert [f.id for f in Job.open("j", store).state.files] == ["f1"]
    b = Job.open("j", store)  # reload, redo
    b.update(lambda s: add_file(s, "f2", "b.xlsx", SHA))
    assert [f.id for f in Job.open("j", store).state.files] == ["f1", "f2"]


def test_a_hitl_answer_is_not_lost_to_a_stale_writer(tmp_path):
    store = FileStore(tmp_path)
    j = Job.open("j", store)
    q, h = ask_human(j.state.hitl, "clarify", "unit?")
    j.update(lambda s: s.model_copy(update={"hitl": h}))
    tab1, tab2 = Job.open("j", store), Job.open("j", store)

    def reply(s):
        hh, k = answer(s.hitl, s.knowledge, q.id, "ton")
        return s.model_copy(update={"hitl": hh, "knowledge": k})

    tab1.update(reply)
    with pytest.raises(StateConflict):
        tab2.update(reply)
    assert Job.open("j", store).state.knowledge.qa_log[0].answer == "ton"


def test_store_edge_cases(tmp_path):
    store = FileStore(tmp_path / "deep" / "dir")
    assert store.load("x") is None
    Job.open("x", store).update(lambda s: s)
    assert (tmp_path / "deep" / "dir" / "job-x.json").exists()
    assert not list((tmp_path / "deep" / "dir").glob("*.part"))
    with pytest.raises(StateError, match="bad job_id"):
        store.load("../evil")
    with pytest.raises(StateError, match="bad job_id"):
        store.save("A", "{}", 0)
    (tmp_path / "deep" / "dir" / "job-y.json").write_text("garbage", encoding="utf-8")
    with pytest.raises(StateError):
        Job.open("y", store)
    with pytest.raises(StateError):
        store.save("y", "{}", 0)  # cannot tell the stored version: refuse, do not overwrite
    assert (tmp_path / "deep" / "dir" / "job-y.json").read_text(encoding="utf-8") == "garbage"


def test_store_with_other_jobs_state_is_refused(tmp_path):
    store = FileStore(tmp_path)
    Job.open("a", store).update(lambda s: s)
    (tmp_path / "job-b.json").write_text((tmp_path / "job-a.json").read_text(), encoding="utf-8")
    with pytest.raises(StateError, match="stored state is for job 'a', not 'b'"):
        Job.open("b", store)


def test_device_like_job_id_is_a_normal_file(tmp_path):
    store = FileStore(tmp_path)
    Job.open("con", store).update(lambda s: s)
    assert Job.open("con", store).state.version == 1


def test_store_save_failure_keeps_job_state(tmp_path, monkeypatch):
    """A save that cannot write must leave the job as it was. The failure is made the same
    way on every OS: mkdir is refused, instead of relying on how an OS reports a path under
    a file (Windows says FileNotFoundError, Linux NotADirectoryError)."""
    import pathlib

    job = Job.open("j", FileStore(tmp_path / "deep"))

    def refuse(self, *a, **k):
        raise PermissionError("read-only disk")

    monkeypatch.setattr(pathlib.Path, "mkdir", refuse)
    with pytest.raises(StateError, match="cannot write"):
        job.update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    assert job.state.version == 0 and not job.state.files


@pytest.mark.parametrize("error", [FileNotFoundError, NotADirectoryError])
def test_a_path_under_a_file_reads_as_never_saved_on_every_os(tmp_path, monkeypatch, error):
    import pathlib

    def fail(self, *a, **k):
        raise error(2, "no such path", str(self))

    monkeypatch.setattr(pathlib.Path, "read_text", fail)
    store = FileStore(tmp_path / "file" / "sub")
    assert store.load("j") is None
    assert Job.open("j", store).state.version == 0


# --- files -----------------------------------------------------------------------------------


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


def test_open_files_returns_workbooks_by_role(tmp_path):
    data = make_xlsx({"D": [["Ton", "Unit"], [1, "kg"], [2, "kg"], [3, "kg"]]})
    p = tmp_path / "a.xlsx"
    p.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    s = add_file(new_state("j"), "f1", "a.xlsx", sha, role="f")
    books = open_files(s, Provider({"f1": str(p)}))
    assert list(books) == ["f"] and books["f"].sha256 == sha
    rep = run(_spec(), books, tmp_path / "out")
    assert rep.status == "ok", rep.text()
    assert open_files(s, Provider({"f1": data}))["f"].sha256 == sha  # bytes too


def test_open_files_refuses_changed_file_missing_file_and_garbage(tmp_path):
    p = tmp_path / "a.xlsx"
    p.write_bytes(make_xlsx({"D": [["Ton"], [1]]}))
    s = add_file(new_state("j"), "f1", "a.xlsx", SHA, role="f")  # SHA is not this file's hash
    with pytest.raises(StateError, match="not the one registered"):
        open_files(s, Provider({"f1": str(p)}))
    with pytest.raises(StateError, match="cannot be opened"):
        open_files(s, Provider({}))  # provider has no such id
    with pytest.raises(StateError, match="cannot be opened"):
        open_files(s, Provider({"f1": b"not a workbook"}))
    with pytest.raises(StateError, match="cannot be opened"):
        open_files(s, Provider({"f1": str(tmp_path / "missing.xlsx")}))


def test_open_files_with_no_roles_is_empty():
    assert open_files(new_state("j"), Provider({})) == {}


def test_state_json_has_no_file_bytes():
    s = _full()
    assert len(s.model_dump_json()) < 5000
    assert "xlsx" in s.model_dump_json() and HitlState().policy is not None


def test_update_ignores_a_version_set_by_the_callback(tmp_path):
    store = FileStore(tmp_path)
    job = Job.open("j", store)
    job.update(lambda s: s.model_copy(update={"version": 99}))
    assert job.state.version == 1  # the job counts, not the callback
    job.update(lambda s: s.model_copy(update={"version": 0}))
    assert Job.open("j", store).state.version == 2


def test_update_callback_editing_its_argument_in_place_cannot_corrupt_the_job():
    job = Job(new_state("j"))

    def bad(s):
        s.roles["r"] = "nope"  # in place, and the result is invalid
        return s

    with pytest.raises(StateError, match="not in files"):
        job.update(bad)
    assert job.state.roles == {} and job.state.version == 0


@pytest.mark.parametrize("bad", ["abc\n", "a\n", "ab.\n"])
def test_job_id_with_trailing_newline_refused(bad):
    with pytest.raises(StateError):
        new_state(bad)
    with pytest.raises(StateError, match="bad job_id"):
        FileStore("x").load(bad)


def test_sha_with_trailing_newline_refused():
    with pytest.raises(StateError, match="64 lower-case"):
        add_file(new_state("j"), "a", "x", SHA + "\n")


def test_a_state_file_saved_with_a_bom_loads(tmp_path):
    store = FileStore(tmp_path)
    Job.open("j", store).update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    path = tmp_path / "job-j.json"
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")  # an editor's BOM
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert [f.id for f in Job.open("j", store).state.files] == ["f1"]


def test_reload_picks_up_what_another_writer_saved_and_keeps_state_without_a_store(tmp_path):
    store = FileStore(tmp_path)
    a, b = Job.open("j", store), Job.open("j", store)
    a.update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    with pytest.raises(StateConflict):
        b.update(lambda s: add_file(s, "f2", "b.xlsx", SHA))
    assert b.state.version == 0
    assert b.reload().version == 1 and [f.id for f in b.state.files] == ["f1"]
    b.update(lambda s: add_file(s, "f2", "b.xlsx", SHA))  # the retry now works
    assert Job.open("j", store).state.version == 2
    plain = Job(new_state("p"))
    plain.update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    assert plain.reload().version == 1  # nothing to read: left as it is


def test_reload_of_a_job_never_saved_keeps_the_empty_state(tmp_path):
    job = Job.open("fresh", FileStore(tmp_path))
    assert job.reload().version == 0


def test_two_threads_saving_at_once_one_wins_and_none_is_lost(tmp_path):
    import threading

    store = FileStore(tmp_path)
    Job.open("j", store).update(lambda s: s)
    results: list[str] = []

    def writer(name):
        job = Job.open("j", store)
        try:
            job.update(lambda s: add_file(s, name, f"{name}.xlsx", SHA))
            results.append("ok")
        except StateConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(f"f{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = Job.open("j", store).state
    # every save that said ok is in the file, and the version counts exactly those saves
    assert results.count("ok") == len(final.files) == final.version - 1
    assert results.count("ok") >= 1 and not list(tmp_path.glob("*.lock"))


def test_a_lock_left_by_a_dead_writer_is_taken_over_and_a_live_one_is_waited_for(
    tmp_path, monkeypatch
):
    import os
    import time

    import excel_etl.state as mod

    store = FileStore(tmp_path)
    lock = tmp_path / "job-j.json.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    Job.open("j", store).update(lambda s: s)  # stale: taken over
    assert not lock.exists()
    monkeypatch.setattr(mod, "_LOCK_WAIT", 0.05)
    lock.write_text("", encoding="utf-8")  # fresh: somebody is writing
    with pytest.raises(StateError, match="locked by another writer"):
        Job.open("j", store).update(lambda s: add_file(s, "f1", "a.xlsx", SHA))
    assert lock.exists()  # not ours, not removed


def test_open_files_opens_only_the_roles_asked_for():
    opened: list[str] = []
    data = make_xlsx({"D": [["a"], [1]]})  # built once: the zip holds a timestamp

    class Spy:
        def open(self, file_id):
            opened.append(file_id)
            return data

    from excel_etl.workbook import Workbook

    sha = Workbook.open(data).sha256
    s = new_state("j")
    for i, role in enumerate(("x", "y", "z")):
        s = add_file(s, f"f{i}", f"{role}.xlsx", sha, role=role)
    assert set(open_files(s, Spy(), ["y"])) == {"y"} and opened == ["f1"]
    assert set(open_files(s, Spy())) == {"x", "y", "z"}
    assert open_files(s, Spy(), ["nope"]) == {}  # a role with no file is the engine's to report


def test_readers_during_writes_never_see_a_torn_file(tmp_path):
    import threading

    store = FileStore(tmp_path)
    Job.open("j", store).update(lambda s: s)
    stop = threading.Event()
    seen: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                text = store.load("j")
                if text is not None:
                    JobState.model_validate_json(text)
            except PermissionError:
                continue  # Windows may refuse a read while the file is swapped
            except Exception as e:
                seen.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=reader)
    t.start()
    job = Job.open("j", store)
    for i in range(30):
        job.update(lambda s, i=i: add_file(s, f"f{i % 5}", f"{i}.xlsx", SHA))
    stop.set()
    t.join()
    assert seen == []
