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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from excel_etl.engine import PipelineError, run, validate
from excel_etl.hitl import HitlError, answer, approve, ask_human, gate
from excel_etl.inspect import encode
from excel_etl.knowledge import lookup_knowledge
from excel_etl.load import LoadError, Where, file_columns, plan_load
from excel_etl.nodes import Column, NodeError, catalog
from excel_etl.nodes.base import describe
from excel_etl.pipeline import PipelineSpec, SpecError, dump_spec, parse_spec
from excel_etl.rebind import rebind
from excel_etl.state import (
    FileProvider,
    Job,
    JobState,
    LoadRecord,
    StateConflict,
    StateError,
    add_file,
    open_files,
    open_ref,
    read_file,
)
from excel_etl.versions import MAX_NOTE, SpecHistory
from excel_etl.workbook import Workbook, WorkbookError

_MAX_TEXT = 24_000
_ERRORS = (
    SpecError, NodeError, HitlError, StateError, WorkbookError, LoadError, ValueError, KeyError,
)  # fmt: skip


def _cap(result: dict) -> dict:
    """Cut a text result at the budget. `truncated` says it, and `next_offset` is the first
    cut character, so a tool that has an `offset` argument can be called again from there."""
    text = result.get("text")
    if isinstance(text, str) and len(text) > _MAX_TEXT:
        cut = _MAX_TEXT - 3
        result = {**result, "text": text[:cut] + "...", "truncated": True}
        result.setdefault("next_offset", None)  # paged tools set their own; others cannot resume
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
            if any(i.where.startswith("hitl.") for i in e.issues):
                shown += (
                    "\nYou cannot approve this yourself: use ask_human (confirm, or choose for "
                    "which tables) and stop. The host approves after the person answers."
                )
            return {"error": shown}
        except StateConflict as e:
            return {
                "error": f"{e}. Nothing was changed; reload the job and repeat.",
                "conflict": True,
            }
        except ValidationError as e:  # a model refused its input; keep pydantic's noise out
            return {"error": describe(e, "input")}
        except _ERRORS as e:
            return {"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)}
        except Exception as e:  # a bug must not end the agent's turn
            return {"error": f"internal error ({type(e).__name__}): {e}; this is a library bug"}

    return wrapper


def _split_column(text: str) -> Column:
    """`name type` (the last word or `type(p,s)` is the type) -> Column. The type is kept as the
    platform wrote it; `compare_target` only looks at its family."""
    name, _, kind = text.strip().rpartition(" ")
    if not name or not kind:
        raise StateError(f"existing_columns item {text[:40]!r} must look like 'name TYPE'")
    return Column(name.strip().strip('"`'), kind)


class JobTools:
    def __init__(
        self,
        job: Job,
        provider: FileProvider,
        out_dir: str | os.PathLike[str] | None = None,
        per_version: bool = False,
    ) -> None:
        self.job = job
        self.provider = provider
        self.out_dir = out_dir
        self.per_version = per_version  # write into out_dir/v<N> so a rerun keeps older files
        # (output, platform, table) of the last plan_load, so record_load answers a real plan
        self._last_plan: tuple[str, str, str] | None = None

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
        wb = read_file(file_id, self.provider, f"file {file_id!r}")
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
        `ref` names what was approved, such as `v2` for a spec version. Left empty it is the
        version of the spec saved now, because `run_pipeline` only accepts an approval given
        for the version it runs (empty only when no spec is saved yet)."""
        what = ref or (f"v{self._version()}" if self._state.history else "")
        self.job.update(
            lambda s: s.model_copy(update={"hitl": approve(s.hitl, checkpoint, by, what or None)})
        )
        return {"text": f"{checkpoint} approved by {by}" + (f" for {what}" if what else "")}

    @_tool
    def reload_job(self) -> dict:
        """Host: read the saved state again. Call it after a conflict error, then repeat the
        call that failed. Without a store there is nothing to read."""
        s = self.job.reload()
        return {"text": f"reloaded job {s.job_id} at version {s.version}"}

    @_tool
    def record_load(
        self, output: str, status: Literal["ok", "failed"], rows: int, note: str = ""
    ) -> dict:
        """Host: record the result of a load plan it ran for `output` (rows loaded, ok or
        failed, and a short note such as the error). The library loads nothing itself."""
        last = self._state.last_run
        plan = self._last_plan
        if last is None or plan is None or plan[0] != output:
            raise StateError(f"no load plan was made for {output!r}; call plan_load first")
        rec = LoadRecord(
            output=output,
            platform=plan[1],
            table=plan[2],
            status=status,
            rows=rows,
            ref=f"v{self._version()}",
            note=" ".join(note.split())[:500],
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.job.update(lambda s: s.model_copy(update={"loads": [*s.loads, rec][-200:]}))
        return {"text": f"recorded load of {output} to {plan[1]} {plan[2]}: {status}, {rows} rows"}

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
            "plan_load",
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
        if isinstance(note, str) and len(note) > MAX_NOTE:
            raise ValueError(f"the note is {len(note)} characters; keep it under {MAX_NOTE}")
        spec = parse_spec(spec_json)
        files = self._open_all(spec)
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

    def _open_all(self, spec: PipelineSpec) -> dict[str, Workbook] | None:
        """The files the spec reads (its `file_role`s), or None when no file is registered."""
        s = self._state
        if not s.roles:
            return None
        return open_files(s, self.provider, {src.file_role for src in spec.sources.values()})

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
        report = validate(spec, self._open_all(spec))
        return {"ok": report.ok, "text": report.text()}

    @_tool
    def rebind_sources(self) -> dict:
        """After a new file arrives: find each source of the saved spec again by its anchors.
        exact/shifted = the pipeline still runs; drift/missing = it needs repair."""
        s = self._state
        if s.history is None:
            raise StateError("no spec saved yet; use save_spec")
        spec = s.history.current.spec
        files = self._open_all(spec)
        if files is None:
            raise StateError("no files registered with a role; the host must add_file first")
        report = rebind(spec, files, s.binds)
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
        spec = s.history.current.spec
        files = self._open_all(spec)
        if files is None:
            raise StateError("no files registered with a role; the host must add_file first")
        target = Path(self.out_dir)
        if self.per_version:
            target = target / f"v{self._version()}"
        rep = run(spec, files, target, hitl=s.hitl, ref=f"v{self._version()}")
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
    def plan_load(
        self,
        platform: Literal["snowflake", "databricks", "bigquery"],
        output: str,
        database: str = "",
        schema: str = "",
        stage: str = "",
        existing_columns: list[str] = [],  # noqa: B006 - never mutated
    ) -> dict:
        """Plan the load of one output of the last run into a warehouse table. It runs
        nothing: it returns the steps and SQL for a person to approve and the host to run.
        `database`, `schema` and `stage` come from the person or `lookup_knowledge`, never
        guess them. `existing_columns` is the target table's current columns as `name type`
        strings (from DESCRIBE); with it, a table that does not fit the file is refused.
        The output must have `load` in the spec (table, mode, keys)."""
        s = self._state
        rep = s.last_run
        if rep is None or rep.status != "ok" or s.history is None:
            raise StateError("run the pipeline successfully first (run_pipeline)")
        spec = s.history.current.spec
        out = spec.outputs.get(output)
        if out is None:
            raise StateError(f"no output {output!r}; outputs: {', '.join(spec.outputs) or 'none'}")
        if out.load is None:
            raise StateError(f"output {output!r} has no `load` in the spec; add table and mode")
        stat = next((o for o in rep.outputs if o.name == output), None)
        if stat is None:
            raise StateError(f"the last run did not write {output!r}; run again")
        if why := gate(s.hitl, "before_load", ref=f"v{self._version()}"):
            raise StateError(f"{why}. Use ask_human (confirm) and stop; the host approves.")
        cols, _ = file_columns(stat.path)
        found = tuple(_split_column(c) for c in existing_columns) or None
        plan = plan_load(
            platform, output, stat.path, out.load.table, out.load.mode, out.load.keys, cols,
            Where(database=database, schema_name=schema, stage=stage), found,
        )  # fmt: skip
        self._last_plan = (output, plan.platform, out.load.table)
        return {"text": plan.text() + "\nThe host runs these steps, then calls record_load."}

    @_tool
    def spec_history(self) -> dict:
        """Versions of the spec and what changed in each."""
        h = self._state.history
        return {"text": h.text() if h else "no spec saved yet"}
