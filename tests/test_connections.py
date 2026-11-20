from __future__ import annotations

import json

import pytest

from moyung.state import (
    STATE_SCHEMA,
    ConnRef,
    Job,
    JobState,
    StateError,
    add_connection,
    new_state,
    open_connection,
    probe_connection,
)
from moyung.tools import JobTools


class FakeCursor:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.closed = False

    def execute(self, sql: str) -> None:
        if self.fail == "execute":
            raise RuntimeError("permission denied for database")
        self.sql = sql

    def fetchone(self) -> tuple[int]:
        return (1,)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cur = FakeCursor(self.fail)
        self.cursors.append(cur)
        return cur


class Conns:
    """A host that hands over live connections, the way a FileProvider hands over files."""

    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.opened: list[str] = []
        self.made: list[FakeConnection] = []

    def open(self, conn_id: str) -> object:
        self.opened.append(conn_id)
        if self.fail == "open":
            raise RuntimeError("host could not reach the server")
        if self.fail == "not_dbapi":
            return object()
        con = FakeConnection("execute" if self.fail == "execute" else "")
        self.made.append(con)
        return con


class Files:
    def open(self, file_id: str) -> bytes:
        raise AssertionError("this test registers no file")


@pytest.fixture
def tools(tmp_path):
    job = Job(new_state("j1"))
    return JobTools(job, Files(), out_dir=tmp_path, connections=Conns())


def _ref(**kw) -> ConnRef:
    return ConnRef(id=kw.pop("id", "c1"), label=kw.pop("label", "gudang"), **kw)


def test_a_connection_holds_no_credential_and_no_address():
    fields = set(ConnRef.model_fields)
    assert fields == {"id", "label", "dialect", "allow_sql"}


def test_own_sql_is_off_until_the_host_turns_it_on():
    assert _ref(dialect="postgres").allow_sql is False
    assert _ref(dialect="postgres", allow_sql=True).allow_sql is True


def test_an_unknown_dialect_is_refused():
    with pytest.raises(ValueError, match="dialect"):
        _ref(dialect="oracle")


def test_registering_a_database_keeps_its_role():
    s = add_connection(new_state("j1"), "c1", "gudang", "postgres", role="warehouse")
    assert [c.id for c in s.connections] == ["c1"]
    assert s.conn_roles == {"warehouse": "c1"}


def test_registering_the_same_id_replaces_it():
    s = add_connection(new_state("j1"), "c1", "gudang lama", "postgres", role="warehouse")
    s = add_connection(s, "c1", "gudang baru", "postgres", allow_sql=True)
    assert len(s.connections) == 1
    assert s.connections[0].label == "gudang baru" and s.connections[0].allow_sql is True
    assert s.conn_roles == {"warehouse": "c1"}


def test_a_role_pointing_nowhere_is_refused():
    with pytest.raises(ValueError, match="connection_role 'warehouse' points to connection 'c9'"):
        JobState(job_id="j1", conn_roles={"warehouse": "c9"})


def test_two_connections_cannot_share_an_id():
    with pytest.raises(ValueError, match="same id"):
        JobState(
            job_id="j1",
            connections=[
                _ref(id="c1", dialect="postgres"),
                _ref(id="c1", label="lain", dialect="postgres"),
            ],
        )


def test_a_job_without_a_database_is_saved_as_before():
    text = Job(new_state("j1")).state_json()
    assert "connections" not in text and "conn_roles" not in text


def test_whether_own_sql_is_allowed_is_always_written_down():
    """Unlike a file's `kind`, this flag is never omitted: someone auditing the state must see
    that own SQL is off instead of having to know the default."""
    s = add_connection(new_state("j1"), "c1", "gudang", "postgres")
    assert '"allow_sql":false' in Job(s).state_json().replace(" ", "").replace("\n", "")


def test_a_job_with_a_database_saves_it():
    s = add_connection(new_state("j1"), "c1", "gudang", "postgres", role="warehouse")
    data = json.loads(Job(s).state_json())
    assert data["connections"] == [
        {"id": "c1", "label": "gudang", "dialect": "postgres", "allow_sql": False}
    ]
    assert data["conn_roles"] == {"warehouse": "c1"}


def test_a_state_written_by_a_newer_library_is_refused():
    with pytest.raises(ValueError, match=f"reads up to {STATE_SCHEMA}; upgrade moyung"):
        JobState(job_id="j1", schema_version=STATE_SCHEMA + 1)


def test_a_state_from_the_version_before_databases_still_loads():
    old = {"schema_version": 1, "job_id": "j1", "version": 3}
    state = Job.from_state(json.dumps(old)).state
    assert state.connections == [] and state.conn_roles == {}


def test_opening_a_database_reports_the_hosts_failure_as_a_state_error():
    with pytest.raises(StateError, match="cannot be opened: host could not reach the server"):
        open_connection(_ref(dialect="postgres"), Conns("open"))


def test_something_that_is_not_a_dbapi_connection_says_so():
    with pytest.raises(StateError, match="has no cursor"):
        open_connection(_ref(dialect="postgres"), Conns("not_dbapi"))


def test_a_database_that_does_not_answer_is_refused_at_registration():
    with pytest.raises(StateError, match="did not answer 'SELECT 1': permission denied"):
        probe_connection(_ref(dialect="postgres"), Conns("execute"))


def test_the_probe_asks_select_1_and_closes_its_cursor():
    conns = Conns()
    probe_connection(_ref(dialect="postgres"), conns)
    (con,) = conns.made
    (cur,) = con.cursors
    assert cur.sql == "SELECT 1" and cur.closed is True


def test_add_connection_proves_the_database_answers_then_records_it(tools):
    out = tools.add_connection("c1", "gudang", role="warehouse")
    assert (
        "registered 'gudang' as 'c1' (postgres, table and filter only) for role 'warehouse'"
        in (out["text"])
    )
    assert tools.connections.opened == ["c1"]
    assert tools._state.conn_roles == {"warehouse": "c1"}


def test_add_connection_says_when_own_sql_is_allowed(tools):
    assert "own SQL allowed" in tools.add_connection("c1", "gudang", allow_sql=True)["text"]


def test_a_broken_database_is_not_recorded(tmp_path):
    tools = JobTools(Job(new_state("j1")), Files(), out_dir=tmp_path, connections=Conns("open"))
    assert "cannot be opened" in tools.add_connection("c1", "gudang")["error"]
    assert tools._state.connections == []


def test_a_job_with_no_connection_provider_says_what_the_host_must_pass(tmp_path):
    tools = JobTools(Job(new_state("j1")), Files(), out_dir=tmp_path)
    assert "pass connections= to JobTools" in tools.add_connection("c1", "gudang")["error"]


def test_job_summary_stays_quiet_about_databases_until_there_is_one(tools):
    assert "databases:" not in tools.job_summary()["text"]
    tools.add_connection("c1", "gudang", role="warehouse")
    text = tools.job_summary()["text"]
    assert "databases: D1 gudang (postgres); connection_roles: warehouse=c1" in text


def test_the_agent_is_not_given_add_connection(tools):
    assert "add_connection" not in [f.__name__ for f in tools.agent_tools()]
