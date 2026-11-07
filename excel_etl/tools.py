"""The surface an agent sees: one method per tool, every result a dict, never an exception.

    tools = JobTools(Job.open("job1", store), provider, out_dir="C:/out/job1")
    agent_functions = tools.agent_tools()      # hand these to the agent framework
    tools.answer("q1", "ton")                   # host-only: the human's reply
    tools.job.state_json()                      # stateless hosts: keep this after each call

Results are `{"text": ...}` (plus `truncated`, `next_offset` when paged) or `{"error": ...}`;
an error says what to change. Arguments are plain strings, numbers, booleans, enums and lists
of strings, so a provider's tool schema never needs `anyOf` or `$ref` (DESIGN section 11). A
spec travels as a JSON string. Files are named `F1`, `F2`, ... in the order of `state.files`.

Tools that change the job (`save_spec`, `rebind`, `run_pipeline`, `ask_human`) go through
`Job.update`, so with a store they are saved at once and a second writer gets a conflict error.

ponytail: a text result is cut at 24000 characters (about 6k tokens) with `truncated: true`;
the inspect tools page themselves, the others are short. Upgrade: `offset` on every tool.
"""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable
from typing import Any, Literal

from excel_etl.engine import PipelineError, run, validate
from excel_etl.hitl import HitlError, answer, approve, ask_human, gate
from excel_etl.inspect import encode
from excel_etl.knowledge import lookup_knowledge
from excel_etl.nodes import NodeError, catalog
from excel_etl.pipeline import SpecError, dump_spec, parse_spec
from excel_etl.rebind import rebind
from excel_etl.state import (
    FileProvider,
    Job,
    JobState,
    StateConflict,
    StateError,
    add_file,
    open_files,
    open_ref,
)
from excel_etl.versions import SpecHistory
from excel_etl.workbook import Workbook, WorkbookError

_MAX_TEXT = 24_000
_ERRORS = (SpecError, NodeError, HitlError, StateError, WorkbookError, ValueError, KeyError)


def _cap(result: dict) -> dict:
    text = result.get("text")
    if isinstance(text, str) and len(text) > _MAX_TEXT:
        result = {**result, "text": text[: _MAX_TEXT - 3] + "...", "truncated": True}
    return result


def _tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn every failure of a tool into `{"error": ...}`; cap the text."""

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self: JobTools, *args: Any, **kwargs: Any) -> dict:
        try:
            sig.bind(self, *args, **kwargs)
        except TypeError as e:  # the agent called it wrongly: say how it is called
            params = ", ".join(list(sig.parameters)[1:])
            return {"error": f"{fn.__name__}({params}): {e}".replace(f"{fn.__name__}() ", "")}
        try:
            return _cap(fn(self, *args, **kwargs))
        except PipelineError as e:
            shown = "\n".join(f"{i.where}: {i.message}" for i in e.issues[:10])
            return {"error": shown}
        except StateConflict as e:
            return {
                "error": f"{e}. Nothing was changed; reload the job and repeat.",
                "conflict": True,
            }
        except _ERRORS as e:
            return {"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)}
        except Exception as e:  # a bug must not end the agent's turn
            return {"error": f"internal error ({type(e).__name__}): {e}; this is a library bug"}

    return wrapper


class JobTools:
    def __init__(
        self,
        job: Job,
        provider: FileProvider,
        out_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.job = job
        self.provider = provider
        self.out_dir = out_dir

    # --- helpers -----------------------------------------------------------------------------

    @property
    def _state(self) -> JobState:
        return self.job.state

    def _file(self, file: str) -> tuple[str, Workbook]:
        """`F1` (or the file name, or the host's id) -> (`F1`, workbook)."""
        files = self._state.files
        for i, f in enumerate(files, start=1):
            if file in (f"F{i}", f.id, f.filename) or (
                isinstance(file, str) and file.upper() == f"F{i}"
            ):
                return f"F{i}", open_ref(f, self.provider)
        known = ", ".join(f"F{i} ({f.filename})" for i, f in enumerate(files, start=1)) or "none"
        raise StateError(f"no file {file!r}; files: {known}")

    def _version(self) -> int:
        h = self._state.history
        return h.current.number if h else 0

    # --- host only: not given to the agent ---------------------------------------------------

    @_tool
    def add_file(self, file_id: str, filename: str, role: str = "") -> dict:
        """Host: register a file the provider can open under `file_id`; `role` is the
        `file_role` the spec uses for it. Re-registering an id replaces it (a new upload)."""
        wb = Workbook.open(self.provider.open(file_id))
        self.job.update(lambda s: add_file(s, file_id, filename, wb.sha256, role or None))
        return {
            "text": f"registered {filename!r} as {file_id!r} sha {wb.sha256[:12]}"
            + (f" for role {role!r}" if role else "")
        }

    @_tool
    def answer(self, question_id: str, reply: str) -> dict:
        """Host: pass on the human's reply to a pending question."""

        def apply(s: JobState) -> JobState:
            hitl, knowledge = answer(s.hitl, s.knowledge, question_id, reply)
            return s.model_copy(update={"hitl": hitl, "knowledge": knowledge})

        self.job.update(apply)
        return {"text": f"recorded the answer to {question_id}"}

    @_tool
    def approve(self, checkpoint: str, by: str, ref: str = "") -> dict:
        """Host: record that a person approved `checkpoint` (e.g. `after_propose_datasets`).
        `ref` names what was approved, such as `v2` for a spec version."""
        self.job.update(
            lambda s: s.model_copy(update={"hitl": approve(s.hitl, checkpoint, by, ref or None)})
        )
        return {"text": f"{checkpoint} approved by {by}" + (f" for {ref}" if ref else "")}

    # --- the agent's tools -------------------------------------------------------------------

    def agent_tools(self) -> list[Callable[..., dict]]:
        """The functions to give the agent, in a fixed order."""
        names = [
            "job_summary",
            "list_files",
            "workbook_map",
            "sheet_regions",
            "render_range",
            "profile_region",
            "find_text",
            "compare_sheets",
            "formula_lineage",
            "propose_datasets",
            "node_catalog",
            "lookup_knowledge",
            "ask_human",
            "pending_questions",
            "save_spec",
            "get_spec",
            "validate_spec",
            "rebind_sources",
            "run_pipeline",
            "last_run",
            "spec_history",
        ]
        return [getattr(self, n) for n in names]

    @_tool
    def job_summary(self) -> dict:
        """Where the job stands: files, spec version, waiting questions, approvals, last run.
        Call this first in a new conversation."""
        s = self._state
        files = [f"F{i} {f.filename}" for i, f in enumerate(s.files, start=1)]
        roles = ", ".join(f"{r}={f}" for r, f in s.roles.items()) or "none"
        run_ = s.last_run
        lines = [
            f"job {s.job_id} (saved {s.version} times)",
            f"files: {', '.join(files) or 'none'}; roles: {roles}",
            f"spec: {'v' + str(self._version()) if s.history else 'none yet'}",
            f"waiting questions: {len(s.hitl.pending)}",
            f"approvals: {', '.join(s.hitl.approvals) or 'none'}",
            f"last run: {run_.status if run_ else 'never'}",
            f"known source places: {', '.join(s.binds) or 'none'}",
        ]
        return {"text": "\n".join(lines)}

    @_tool
    def list_files(self) -> dict:
        """The files of this job: id (F1, F2, ...), name, and the role each plays in the spec."""
        s = self._state
        by_id = {fid: role for role, fid in s.roles.items()}
        rows = [
            f"F{i}|{f.filename}|role {by_id.get(f.id, '-')}" for i, f in enumerate(s.files, start=1)
        ]
        return {"text": "\n".join(rows) or "no files yet"}

    @_tool
    def workbook_map(self, file: str, offset: int = 0, limit: int = 200) -> dict:
        """Every sheet of a file: id, size, how many tables, share of formulas, guessed role."""
        fid, wb = self._file(file)
        return encode.workbook_map(wb, fid, offset, limit)

    @_tool
    def sheet_regions(self, file: str, sheet: str, offset: int = 0, limit: int = 50) -> dict:
        """The tables on one sheet (name or id like F1.S2): range, header rows, total rows."""
        fid, wb = self._file(file)
        return encode.sheet_regions(wb, fid, sheet, offset, limit)

    @_tool
    def render_range(
        self,
        file: str,
        ref: str,
        range: str = "",  # noqa: A002 - the name the agent sees
        mode: Literal["compressed", "raw"] = "compressed",
        offset: int = 0,
        budget: int = 24_000,
    ) -> dict:
        """Show cells as text. ref = sheet name, sheet id (F1.S2) or region id (F1.S2.R1);
        range = A1 range such as 'B4:N40' (not with a region id)."""
        fid, wb = self._file(file)
        return encode.render_range(wb, fid, ref, range or None, mode, offset, budget)

    @_tool
    def profile_region(self, file: str, ref: str, offset: int = 0, budget: int = 24_000) -> dict:
        """Describe each column of a table (region id from sheet_regions): type, range, nulls."""
        fid, wb = self._file(file)
        return encode.profile_region(wb, fid, ref, offset, budget)

    @_tool
    def find_text(self, file: str, query: str, offset: int = 0, budget: int = 24_000) -> dict:
        """Find text cells containing `query`, to locate a label or term."""
        fid, wb = self._file(file)
        return encode.find_text(wb, fid, query, offset, budget)

    @_tool
    def compare_sheets(self, file: str, a: str, b: str, budget: int = 24_000) -> dict:
        """How alike two sheets are in structure and values (copies, versions)."""
        fid, wb = self._file(file)
        return encode.compare_sheets(wb, fid, a, b, budget)

    @_tool
    def formula_lineage(self, file: str, offset: int = 0, limit: int = 100) -> dict:
        """Which sheets read which: tells typed source sheets from derived ones."""
        fid, wb = self._file(file)
        return encode.formula_lineage(wb, fid, offset, limit)

    @_tool
    def propose_datasets(self, file: str, offset: int = 0, limit: int = 50) -> dict:
        """Tables worth loading as datasets (typed sources), for a person to choose from."""
        fid, wb = self._file(file)
        return encode.propose_datasets(wb, fid, offset, limit)

    @_tool
    def node_catalog(self, node: str = "") -> dict:
        """The step types for a spec. Without `node`: one line each. With a name: its
        description and config fields (`*` = required)."""
        return {"text": catalog(node or None)}

    @_tool
    def lookup_knowledge(self, query: str = "") -> dict:
        """What the human told us (brief, terms, rules, earlier answers) that matches `query`.
        Empty query: what is stored."""
        return {"text": lookup_knowledge(self._state.knowledge, query)}

    @_tool
    def ask_human(
        self,
        kind: Literal["clarify", "confirm", "choose"],
        question: str,
        options: list[str] = [],  # noqa: B006 - never mutated
        ref: str = "",
    ) -> dict:
        """Ask the person something you cannot decide. clarify = free text, confirm = yes/no,
        choose = pick one of 2+ `options`. Then stop and wait: the answer arrives later."""
        asked: list[str] = []

        def apply(s: JobState) -> JobState:
            q, hitl = ask_human(s.hitl, kind, question, options, ref or None)
            asked.append(q.id)
            return s.model_copy(update={"hitl": hitl})

        self.job.update(apply)
        return {
            "question_id": asked[0],
            "text": f"question {asked[0]} is waiting for the person. Stop until it is answered.",
        }

    @_tool
    def pending_questions(self) -> dict:
        """The questions still waiting for the person."""
        rows = []
        for q in self._state.hitl.pending:
            opts = f" options: {' | '.join(q.options)}" if q.options else ""
            rows.append(
                f"{q.id} ({q.kind}){f' about {q.ref}' if q.ref else ''}: {q.question}{opts}"
            )
        return {"text": "\n".join(rows) or "none waiting"}

    @_tool
    def save_spec(self, spec_json: str, note: str = "") -> dict:
        """Save a pipeline spec (JSON text) as the next version. It is checked against the
        files first; a spec with problems is not saved. `note` says why (needed from v2)."""
        spec = parse_spec(spec_json)
        files = self._open_all()
        report = validate(spec, files)
        if not report.ok:
            return {"saved": False, "text": "not saved\n" + report.text()}

        def apply(s: JobState) -> JobState:
            if s.history is None:
                hist = SpecHistory.start(spec, note.strip() or "first version")
            else:
                hist = s.history.add(spec, note)
            return s.model_copy(update={"history": hist})

        self.job.update(apply)
        warn = "" if files else "\nno files registered: only node names and settings were checked"
        return {
            "saved": True,
            "version": self._version(),
            "text": f"saved v{self._version()}\n" + report.text() + warn,
        }

    def _open_all(self) -> dict[str, Workbook] | None:
        s = self._state
        return open_files(s, self.provider) if s.roles else None

    @_tool
    def get_spec(self, version: int = 0) -> dict:
        """The spec as JSON text. `version` 0 = the current one."""
        h = self._state.history
        if h is None:
            raise StateError("no spec saved yet; use save_spec")
        v = h.current if version == 0 else h.get(version)
        return {"version": v.number, "text": dump_spec(v.spec)}

    @_tool
    def validate_spec(self, spec_json: str = "") -> dict:
        """Check a spec against the files without running it. Without `spec_json` the
        saved current spec is checked."""
        if not spec_json:
            h = self._state.history
            if h is None:
                raise StateError("no spec saved yet; pass spec_json or use save_spec")
            spec = h.current.spec
        else:
            spec = parse_spec(spec_json)
        report = validate(spec, self._open_all())
        return {"ok": report.ok, "text": report.text()}

    @_tool
    def rebind_sources(self) -> dict:
        """After a new file arrives: find each source of the saved spec again by its anchors.
        exact/shifted = the pipeline still runs; drift/missing = it needs repair."""
        s = self._state
        if s.history is None:
            raise StateError("no spec saved yet; use save_spec")
        files = self._open_all()
        if files is None:
            raise StateError("no files registered with a role; the host must add_file first")
        report = rebind(s.history.current.spec, files, s.binds)
        self.job.update(
            lambda st: st.model_copy(update={"binds": {**st.binds, **report.records()}})
        )
        text = report.text()
        if not report.ok and (why := gate(self._state.hitl, "on_rebind_drift")):
            text += f"\nask the person before repairing: {why}"
        return {"ok": report.ok, "text": text}

    @_tool
    def run_pipeline(self) -> dict:
        """Run the saved spec and write its outputs. Refused until the checkpoints that
        need a person's approval are approved. The result is kept as the last run."""
        s = self._state
        if s.history is None:
            raise StateError("no spec saved yet; use save_spec")
        if self.out_dir is None:
            raise StateError("the host gave no out_dir; it cannot write outputs")
        files = self._open_all()
        if files is None:
            raise StateError("no files registered with a role; the host must add_file first")
        rep = run(
            s.history.current.spec, files, self.out_dir, hitl=s.hitl, ref=f"v{self._version()}"
        )
        self.job.update(lambda st: st.model_copy(update={"last_run": rep}))
        text = rep.text()
        if why := gate(self._state.hitl, "after_run", failed=rep.status != "ok"):
            text += f"\nask the person before going on: {why}"
        return {"ok": rep.status == "ok", "text": text}

    @_tool
    def last_run(self) -> dict:
        """The report of the last run: rows per step, checks, files written, problems."""
        rep = self._state.last_run
        return {"text": rep.text() if rep else "no run yet"}

    @_tool
    def spec_history(self) -> dict:
        """Versions of the spec and what changed in each."""
        h = self._state.history
        return {"text": h.text() if h else "no spec saved yet"}
