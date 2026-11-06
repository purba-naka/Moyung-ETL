"""Everything about one job in a single JSON-able object, and where it is kept.

`JobState` holds the facts: which files (by id and hash, never their bytes), what the human
said, the pending questions and approvals, the spec and its versions, where each source was
found, the last run. The library keeps nothing itself; the host keeps the JSON.

    stateless   job = Job.from_state(json_text) ... job.state_json()   (host stores the text)
    store       job = Job.open("job1", store)                          (saved on every update)

Every change goes through `Job.update(fn)` with `fn(state) -> new_state`. The result is
turned into JSON and read back before it is saved, so a state that cannot be loaded again is
refused instead of stored. A store refuses a save when the stored version is not the one this
job started from (two tabs, two workers), so one writer cannot overwrite the other's answer.

ponytail: `FileStore` checks the version and then replaces the file; two processes saving in
the same instant can both pass. Upgrade: a store of the host's database with a real
compare-and-set (the `StateStore` protocol is two methods).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from excel_etl.engine import RunReport
from excel_etl.hitl import HitlState
from excel_etl.knowledge import Knowledge
from excel_etl.nodes.base import describe
from excel_etl.rebind import BindRecord
from excel_etl.versions import SpecHistory
from excel_etl.workbook import Workbook, WorkbookError

# lower case only: on Windows `Job1` and `job1` would be the same file
_JOB_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9_-])?$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILES = 50


class StateError(ValueError):
    """The state or a call on it is not valid; the message says what and where."""


class StateConflict(StateError):
    """The stored state moved on since this job read it. Reload and redo the change."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileRef(_Model):
    id: str = Field(min_length=1, max_length=200)  # the host's handle for the file
    filename: str = Field(min_length=1, max_length=300)
    sha256: str  # of the bytes; a different hash means a different file

    @model_validator(mode="after")
    def _hash(self) -> FileRef:
        if not _SHA.match(self.sha256):
            raise ValueError("sha256 must be 64 lower-case hex characters")
        return self


class JobState(_Model):
    job_id: str
    version: int = Field(default=0, ge=0)  # how many times it was saved; the optimistic lock
    files: list[FileRef] = Field(default=[], max_length=_MAX_FILES)
    roles: dict[str, str] = {}  # file_role used in the spec -> FileRef.id
    knowledge: Knowledge = Field(default_factory=Knowledge)
    hitl: HitlState = Field(default_factory=HitlState)
    history: SpecHistory | None = None
    binds: dict[str, BindRecord] = {}  # source name -> where it was found last time
    last_run: RunReport | None = None

    @model_validator(mode="after")
    def _consistent(self) -> JobState:
        if not _JOB_ID.match(self.job_id):
            raise ValueError(
                "job_id must be 1-100 characters: a-z, 0-9, _ . - (lower case, "
                "starting with a letter or digit, not ending with a dot)"
            )
        ids = [f.id for f in self.files]
        if len(set(ids)) != len(ids):
            raise ValueError("two files have the same id")
        for role, fid in self.roles.items():
            if fid not in ids:
                raise ValueError(f"role {role!r} points to file {fid!r}, which is not in files")
        return self


def new_state(job_id: str) -> JobState:
    try:
        return JobState(job_id=job_id)
    except ValidationError as e:
        raise StateError(describe(e, "state")) from None


def add_file(
    state: JobState, file_id: str, filename: str, sha256: str, role: str | None = None
) -> JobState:
    """Register a file, or replace the one with this id (a new month's upload keeps its id
    and gets a new hash). `role` maps the spec's `file_role` to it.
    """
    try:
        ref = FileRef(id=file_id, filename=filename, sha256=sha256)
    except ValidationError as e:
        raise StateError(describe(e, "file")) from None
    files = [ref if f.id == file_id else f for f in state.files]
    if all(f.id != file_id for f in state.files):
        files.append(ref)
    roles = {**state.roles, **({role: file_id} if role else {})}
    return state.model_copy(update={"files": files, "roles": roles})


# --- stores ----------------------------------------------------------------------------------


class StateStore(Protocol):
    def load(self, job_id: str) -> str | None:
        """The stored JSON, or None when the job was never saved."""

    def save(self, job_id: str, state_json: str, expected_version: int) -> None:
        """Store `state_json`. Raise `StateConflict` when the stored version (0 when none)
        is not `expected_version`."""


class FileStore:
    """One JSON file per job in a directory, for development and tests."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.match(job_id):
            raise StateError(f"bad job_id {job_id!r}")
        return self.dir / f"job-{job_id}.json"  # the prefix keeps `con` and friends legal

    def load(self, job_id: str) -> str | None:
        path = self._path(job_id)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StateError(f"cannot read {path.name}: {e}") from None

    def save(self, job_id: str, state_json: str, expected_version: int) -> None:
        path = self._path(job_id)
        stored = self.load(job_id)
        have = 0 if stored is None else _version_of(stored, path.name)
        if have != expected_version:
            raise StateConflict(
                f"job {job_id!r} is at version {have} in the store, not {expected_version}; "
                "someone else changed it"
            )
        tmp = path.with_name(path.name + ".part")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(state_json, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise StateError(f"cannot write {path.name}: {e}") from None


def _version_of(text: str, where: str) -> int:
    try:
        return JobState.model_validate_json(text).version
    except ValidationError as e:
        raise StateError(f"stored state {where} is not valid: {describe(e, 'state')}") from None


# --- the job ---------------------------------------------------------------------------------


class Job:
    """A `JobState` plus, optionally, the store that keeps it."""

    def __init__(self, state: JobState, store: StateStore | None = None) -> None:
        self._state = state
        self._store = store

    @classmethod
    def from_state(cls, state_json: str) -> Job:
        """A stateless job from JSON the host kept. Raises StateError when it is not valid."""
        if not isinstance(state_json, str):
            raise StateError(f"state must be a JSON string, got {type(state_json).__name__}")
        try:
            return cls(JobState.model_validate_json(state_json))
        except ValidationError as e:
            raise StateError(describe(e, "state")) from None

    @classmethod
    def open(cls, job_id: str, store: StateStore) -> Job:
        """Load a job from `store`, or start an empty one when it was never saved."""
        text = store.load(job_id)
        if text is None:
            return cls(new_state(job_id), store)
        job = cls.from_state(text)
        job._store = store
        if job.state.job_id != job_id:
            raise StateError(f"stored state is for job {job.state.job_id!r}, not {job_id!r}")
        return job

    @property
    def state(self) -> JobState:
        """A copy: changing it does not change the job; use `update`."""
        return self._state.model_copy(deep=True)

    def state_json(self) -> str:
        return self._state.model_dump_json()

    def update(self, fn: Callable[[JobState], JobState]) -> JobState:
        """Apply `fn(state) -> new_state`, bump the version, save when there is a store.

        On any error (fn raises, the result is not a valid state, the store refuses) the job
        keeps its old state. After a `StateConflict` reload with `Job.open` and try again.
        """
        old = self._state
        new = fn(old)
        if not isinstance(new, JobState):
            raise StateError(f"the update must return a JobState, got {type(new).__name__}")
        if new.job_id != old.job_id:
            raise StateError("an update cannot change job_id")
        text = new.model_copy(update={"version": old.version + 1}).model_dump_json()
        try:
            checked = JobState.model_validate_json(text)  # what we store must load again
        except ValidationError as e:
            raise StateError(describe(e, "state")) from None
        if self._store is not None:
            self._store.save(old.job_id, text, expected_version=old.version)
        self._state = checked
        return checked.model_copy(deep=True)  # the caller's copy must not reach into the job


# --- files -----------------------------------------------------------------------------------


class FileProvider(Protocol):
    def open(self, file_id: str) -> str | os.PathLike[str] | bytes:
        """The file as a path or its bytes."""


def open_files(state: JobState, provider: FileProvider) -> dict[str, Workbook]:
    """`file_role -> Workbook` for `run` / `validate` / `rebind`. A file whose hash differs
    from the one registered is refused: the spec and the checks were made for the other one.
    """
    by_id = {f.id: f for f in state.files}
    out: dict[str, Workbook] = {}
    for role, fid in state.roles.items():
        ref = by_id[fid]
        try:
            wb = Workbook.open(provider.open(fid))
        except (WorkbookError, OSError, KeyError, ValueError) as e:
            raise StateError(
                f"file {ref.filename!r} (role {role!r}) cannot be opened: {e}"
            ) from None
        if wb.sha256 != ref.sha256:
            raise StateError(
                f"file {ref.filename!r} (role {role!r}) is not the one registered: "
                f"hash {wb.sha256[:12]} instead of {ref.sha256[:12]}; register it again"
            )
        out[role] = wb
    return out
