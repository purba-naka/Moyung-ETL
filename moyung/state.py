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

import contextlib
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from moyung.engine import RunReport, RunSummary
from moyung.hitl import HitlState
from moyung.knowledge import Knowledge
from moyung.nodes.base import describe
from moyung.rebind import BindRecord
from moyung.tablefile import FileKind, TableFile
from moyung.versions import SpecHistory
from moyung.workbook import Workbook

_JOB_ID = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9_-])?")
_SHA = re.compile(r"[0-9a-f]{64}")
_MAX_FILES = 50
_MAX_CONNECTIONS = 20
MAX_RUNS = 20
STATE_SCHEMA = 2
_LOCK_WAIT = 5.0
_STALE = 30.0


class StateError(ValueError):
    """The state or a call on it is not valid; the message says what and where."""


class StateConflict(StateError):
    """The stored state moved on since this job read it. Reload and redo the change."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileRef(_Model):
    id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=300)
    sha256: str
    kind: FileKind = "excel"

    @model_serializer(mode="wrap")
    def _omit_default_kind(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("kind") == "excel":
            data.pop("kind", None)
        return data

    @model_validator(mode="after")
    def _hash(self) -> FileRef:
        if not _SHA.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lower-case hex characters")
        return self


type Dialect = Literal["postgres"]


class ConnRef(_Model):
    """A database the host lets this job read. It holds no credential and no address: the host
    keeps those and hands over a live connection for `id`, the way it hands over a file.

    There is no hash to pin a database to, so a source that reads one is written to a parquet
    snapshot whose hash is recorded instead (see `moyung/pipeline.py`).

    `allow_sql` is the host's decision, never the spec's, because the agent writes the spec and
    would otherwise grant itself the right to send its own SQL.
    """

    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    dialect: Dialect
    allow_sql: bool = False


class LoadRecord(_Model):
    """What the host reports after it ran a load plan. The library never loads anything."""

    output: str
    platform: str
    table: str
    status: Literal["ok", "failed"]
    rows: int = Field(ge=0)
    ref: str
    note: str = Field(default="", max_length=500)
    at: str


class PlannedLoad(_Model):
    """The last load plan, so `record_load` in a later turn knows what it answers."""

    output: str
    platform: str
    table: str
    ref: str


class JobState(_Model):
    """A job's saved state. A state with a newer `schema_version` is refused."""

    schema_version: int = Field(default=STATE_SCHEMA, ge=1)
    job_id: str
    version: int = Field(default=0, ge=0)
    files: list[FileRef] = Field(default=[], max_length=_MAX_FILES)
    roles: dict[str, str] = {}
    connections: list[ConnRef] = Field(default=[], max_length=_MAX_CONNECTIONS)
    conn_roles: dict[str, str] = {}
    knowledge: Knowledge = Field(default_factory=Knowledge)
    hitl: HitlState = Field(default_factory=HitlState)
    history: SpecHistory | None = None
    binds: dict[str, BindRecord] = {}
    last_run: RunReport | None = None
    runs: list[RunSummary] = Field(default=[], max_length=MAX_RUNS)
    loads: list[LoadRecord] = Field(default=[], max_length=200)
    planned: PlannedLoad | None = None

    @model_validator(mode="after")
    def _consistent(self) -> JobState:
        if self.schema_version > STATE_SCHEMA:
            raise ValueError(
                f"this state has schema_version {self.schema_version}, but this library reads "
                f"up to {STATE_SCHEMA}; upgrade moyung"
            )
        if not _JOB_ID.fullmatch(self.job_id):
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
        conn_ids = [c.id for c in self.connections]
        if len(set(conn_ids)) != len(conn_ids):
            raise ValueError("two connections have the same id")
        for role, cid in self.conn_roles.items():
            if cid not in conn_ids:
                raise ValueError(
                    f"connection_role {role!r} points to connection {cid!r}, "
                    "which is not in connections"
                )
        return self

    @model_serializer(mode="wrap")
    def _omit_empty_connections(self, handler: Any) -> dict[str, Any]:
        """A job with no database reads like a job saved before databases existed, so its JSON
        stays as small and as easy to diff as it was."""
        data = handler(self)
        for key in ("connections", "conn_roles"):
            if not data.get(key):
                data.pop(key, None)
        return data


def new_state(job_id: str) -> JobState:
    try:
        return JobState(job_id=job_id)
    except ValidationError as e:
        raise StateError(describe(e, "state")) from None


def add_file(
    state: JobState,
    file_id: str,
    filename: str,
    sha256: str,
    role: str | None = None,
    kind: FileKind = "excel",
) -> JobState:
    """Register a file, or replace the one with this id (a new month's upload keeps its id
    and gets a new hash). `role` maps the spec's `file_role` to it.
    """
    if role is not None and not isinstance(role, str):
        raise StateError(f"role must be text, got {type(role).__name__}")
    try:
        ref = FileRef(id=file_id, filename=filename, sha256=sha256, kind=kind)
    except ValidationError as e:
        raise StateError(describe(e, "file")) from None
    files = [ref if f.id == file_id else f for f in state.files]
    if all(f.id != file_id for f in state.files):
        files.append(ref)
    roles = {**state.roles, **({role: file_id} if role else {})}
    return state.model_copy(update={"files": files, "roles": roles})


def add_connection(
    state: JobState,
    conn_id: str,
    label: str,
    dialect: Dialect = "postgres",
    role: str | None = None,
    allow_sql: bool = False,
) -> JobState:
    """Register a database, or replace the one with this id. `role` maps the spec's
    `connection_role` to it.
    """
    if role is not None and not isinstance(role, str):
        raise StateError(f"role must be text, got {type(role).__name__}")
    try:
        ref = ConnRef(id=conn_id, label=label, dialect=dialect, allow_sql=allow_sql)
    except ValidationError as e:
        raise StateError(describe(e, "connection")) from None
    conns = [ref if c.id == conn_id else c for c in state.connections]
    if all(c.id != conn_id for c in state.connections):
        conns.append(ref)
    roles = {**state.conn_roles, **({role: conn_id} if role else {})}
    return state.model_copy(update={"connections": conns, "conn_roles": roles})


class StateStore(Protocol):
    def load(self, job_id: str) -> str | None:
        """The stored JSON, or None when the job was never saved."""

    def save(self, job_id: str, state_json: str, expected_version: int) -> None:
        """Store `state_json`. Raise `StateConflict` when the stored version (0 when none)
        is not `expected_version`."""


def _replace(tmp: Path, path: Path) -> None:
    """`os.replace`, tried again for a moment: on Windows it fails with 'access denied' while
    a reader has the target open, which is over in milliseconds."""
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)


class FileStore:
    """One JSON file per job in a directory, for development and tests."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise StateError(f"bad job_id {job_id!r}")
        return self.dir / f"job-{job_id}.json"

    def load(self, job_id: str) -> str | None:
        path = self._path(job_id)
        for attempt in range(50):
            try:
                return path.read_text(encoding="utf-8-sig")
            except (FileNotFoundError, NotADirectoryError):
                return None
            except PermissionError as e:
                if attempt == 49:
                    raise StateError(f"cannot read {path.name}: {e}") from None
                time.sleep(0.01)
            except OSError as e:
                raise StateError(f"cannot read {path.name}: {e}") from None
        return None

    def save(self, job_id: str, state_json: str, expected_version: int) -> None:
        path = self._path(job_id)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StateError(f"cannot write {path.name}: {e}") from None
        with self._locked(path):
            stored = self.load(job_id)
            have = 0 if stored is None else _version_of(stored, path.name)
            if have != expected_version:
                raise StateConflict(
                    f"job {job_id!r} is at version {have} in the store, not {expected_version}; "
                    "someone else changed it"
                )
            tmp = path.with_name(path.name + ".part")
            try:
                tmp.write_text(state_json, encoding="utf-8")
                _replace(tmp, path)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                raise StateError(f"cannot write {path.name}: {e}") from None

    @contextlib.contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        """A lock file made with O_EXCL (atomic on every OS). A lock older than `_STALE`
        seconds belongs to a process that died and is taken over."""
        lock = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _STALE:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise StateError(
                        f"{path.name} is locked by another writer; try again in a moment"
                    ) from None
                time.sleep(0.01)
            except PermissionError:
                if time.monotonic() > deadline:
                    raise StateError(f"{path.name} is locked by another writer") from None
                time.sleep(0.01)
            except OSError as e:
                raise StateError(f"cannot lock {path.name}: {e}") from None
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)


def _version_of(text: str, where: str) -> int:
    try:
        return JobState.model_validate_json(text).version
    except ValidationError as e:
        raise StateError(f"stored state {where} is not valid: {describe(e, 'state')}") from None


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

    def reload(self) -> JobState:
        """Read the stored state again (after a `StateConflict`). Without a store there is
        nothing to read and the job is left as it is."""
        if self._store is None:
            return self.state
        text = self._store.load(self._state.job_id)
        if text is not None:
            self._state = Job.from_state(text)._state
        return self.state

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
        new = fn(old.model_copy(deep=True))
        if not isinstance(new, JobState):
            raise StateError(f"the update must return a JobState, got {type(new).__name__}")
        if new.job_id != old.job_id:
            raise StateError("an update cannot change job_id")
        text = new.model_copy(update={"version": old.version + 1}).model_dump_json()
        try:
            checked = JobState.model_validate_json(text)
        except ValidationError as e:
            raise StateError(describe(e, "state")) from None
        if self._store is not None:
            self._store.save(old.job_id, text, expected_version=old.version)
        self._state = checked
        return checked.model_copy(deep=True)


class FileProvider(Protocol):
    def open(self, file_id: str) -> str | os.PathLike[str] | bytes:
        """The file as a path or its bytes."""


class ConnectionProvider(Protocol):
    def open(self, conn_id: str) -> Any:
        """A live DB-API 2.0 connection the library may read from. The host owns it: it holds
        the credential, decides whether the account is read-only, and closes it."""


def open_connection(ref: ConnRef, provider: ConnectionProvider, role: str | None = None) -> Any:
    """Ask the host's provider for one registered database. Whatever the provider raises is the
    host's problem and comes back as a StateError, not a bug."""
    label = f"connection {ref.label!r}" + (f" (role {role!r})" if role else "")
    try:
        con = provider.open(ref.id)
    except Exception as e:
        raise StateError(f"{label} cannot be opened: {e}") from None
    if not hasattr(con, "cursor"):
        raise StateError(
            f"{label} is not a DB-API connection: it has no cursor(). "
            f"Give the connection object of a driver such as psycopg, not {type(con).__name__}"
        )
    return con


def probe_connection(ref: ConnRef, provider: ConnectionProvider) -> None:
    """Prove a database answers before it is registered, so a broken connection is found now
    and not in the middle of a run. Mirrors `add_file`, which opens the file to hash it."""
    con = open_connection(ref, provider)
    try:
        cur = con.cursor()
        try:
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            with contextlib.suppress(Exception):
                cur.close()
    except StateError:
        raise
    except Exception as e:
        raise StateError(
            f"connection {ref.label!r} opened but did not answer 'SELECT 1': {e}"
        ) from None


def read_file(
    file_id: str, provider: FileProvider, label: str = "", kind: FileKind = "excel"
) -> Workbook | TableFile:
    """Ask the host's provider for a file and open it as `kind`. Whatever the provider raises
    (a disk error, a missing id) is the host's problem and comes back as a StateError, not a
    bug."""
    try:
        raw = provider.open(file_id)
        return Workbook.open(raw) if kind == "excel" else TableFile.open(raw, kind)
    except Exception as e:
        raise StateError(f"{label or repr(file_id)} cannot be opened: {e}") from None


def open_ref(ref: FileRef, provider: FileProvider, role: str | None = None) -> Workbook | TableFile:
    """Open one registered file. Refused when its hash differs from the registered one: the
    spec and the checks were made for the other file."""
    label = f"file {ref.filename!r}" + (f" (role {role!r})" if role else "")
    wb = read_file(ref.id, provider, label, ref.kind)
    if wb.sha256 != ref.sha256:
        raise StateError(
            f"{label} is not the one registered: "
            f"hash {wb.sha256[:12]} instead of {ref.sha256[:12]}; register it again"
        )
    return wb


def open_files(
    state: JobState, provider: FileProvider, roles: Iterable[str] | None = None
) -> dict[str, Workbook | TableFile]:
    """`file_role -> Workbook or TableFile` for `run` / `validate` / `rebind`. With `roles` only
    those are opened (a spec that uses one role of a job with many files does not pay for the
    rest)."""
    by_id = {f.id: f for f in state.files}
    want = state.roles if roles is None else {r: state.roles[r] for r in roles if r in state.roles}
    return {role: open_ref(by_id[fid], provider, role) for role, fid in want.items()}
