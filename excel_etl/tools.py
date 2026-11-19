"""The surface an agent sees: one method per tool, every result a dict, never an exception.

    tools = JobTools(Job.open("job1", store), provider, out_dir="C:/out/job1")
    agent_functions = tools.agent_tools()      # hand these to the agent framework
    tools.answer("q1", "ton")                   # host-only: the human's reply
    tools.job.state_json()                      # stateless hosts: keep this after each call

Results are `{"text": ...}` (plus `truncated`, `next_offset` when paged) or `{"error": ...}`;
an error says what to change. Arguments are plain strings, numbers, booleans, enums and lists
of strings, so a provider's tool schema never needs `anyOf` or `$ref`. A
spec travels as a JSON string. Files are named `F1`, `F2`, ... in the order of `state.files`.

`provider` is any object with `open(file_id) -> path | bytes` (`state.FileProvider`); `store` is
a host `StateStore`, or `state.FileStore(dir)` for dev and tests.

Tools that change the job (`save_spec`, `rebind`, `run_pipeline`, `ask_human`, `plan_load`)
go through `Job.update`, so with a store they are saved at once and a second writer gets a
conflict error.

ponytail: a text result is cut at 24000 characters (about 6k tokens) with `truncated: true`;
the inspect tools page themselves, the others are short. Upgrade: `offset` on every tool.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from excel_etl.engine import PipelineError, run, validate
from excel_etl.hitl import HitlError, answer, approve, ask_human, gate
from excel_etl.inspect import encode, tables
from excel_etl.knowledge import lookup_knowledge
from excel_etl.load import LoadError, Where, file_columns, plan_load
from excel_etl.nodes import Column, NodeError, catalog
from excel_etl.nodes.base import describe
from excel_etl.pipeline import (
    SOURCE_KINDS,
    PipelineSpec,
    SpecError,
    dump_spec,
    parse_spec,
    patch_spec,
)
from excel_etl.rebind import rebind
from excel_etl.state import (
    FileProvider,
    Job,
    JobState,
    LoadRecord,
    PlannedLoad,
    StateConflict,
    StateError,
    add_file,
    open_files,
    open_ref,
    read_file,
)
from excel_etl.synth import source_from_region
from excel_etl.versions import MAX_NOTE, SpecHistory
from excel_etl.workbook import Workbook, WorkbookError

_MAX_TEXT = 24_000
_CONTEXT_TASKS = ("discovery", "cleaning", "join", "validation")
_CONTEXT_REFS = 10
_REGION_REF = re.compile(r"F[0-9]+\.S[0-9]+\.R[0-9]+")
_ERRORS = (
    SpecError, NodeError, HitlError, StateError, WorkbookError, LoadError, ValueError, KeyError,
)  # fmt: skip


def _left_out(titles: list[str], room: int | None = None) -> str:
    """The last line naming what was cut; with `room` too small for the names, only the count."""
    if not titles:
        return ""
    line = "\nleft out (call the tool for them): " + ", ".join(titles)
    if room is not None and len(line) > room:
        return f"\nleft out: {len(titles)} section(s), see left_out"
    return line


def _cap(result: dict) -> dict:
    """Cut a text result at the budget. `truncated` says it, and `next_offset` is the first
    cut character, so a tool that has an `offset` argument can be called again from there."""
    text = result.get("text")
    if isinstance(text, str) and len(text) > _MAX_TEXT:
        cut = _MAX_TEXT - 3
        result = {**result, "text": text[:cut] + "...", "truncated": True}
        result.setdefault("next_offset", None)
    return result


def _tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn every failure of a tool into `{"error": ...}`; cap the text."""

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self: JobTools, *args: Any, **kwargs: Any) -> dict:
        try:
            sig.bind(self, *args, **kwargs)
        except TypeError as e:
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
        except ValidationError as e:
            return {"error": describe(e, "input")}
        except _ERRORS as e:
            return {"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)}
        except Exception as e:
            return {"error": f"internal error ({type(e).__name__}): {e}; this is a library bug"}

    return wrapper


def _changed_tables(old: PipelineSpec | None, new: PipelineSpec) -> set[str] | None:
    """Tables whose columns may differ from the last saved version: new or edited sources and
    steps, and everything downstream of them. None (list all) for a first version. A repeat
    save then shows only what the agent just touched; the rest is already in its context."""
    if old is None:
        return None
    before = {k: v.model_dump() for k, v in old.sources.items()}
    before.update({s.id: s.model_dump() for s in old.steps})
    changed = {k for k, v in new.sources.items() if before.get(k) != v.model_dump()}
    for step in new.steps:
        if before.get(step.id) != step.model_dump() or changed & set(step.inputs):
            changed.add(step.id)
    return changed


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
        self.per_version = per_version

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
        plan = self._state.planned
        if plan is None or plan.output != output:
            raise StateError(f"no load plan was made for {output!r}; call plan_load first")
        rec = LoadRecord(
            output=output,
            platform=plan.platform,
            table=plan.table,
            status=status,
            rows=rows,
            ref=plan.ref,
            note=" ".join(note.split())[:500],
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.job.update(lambda s: s.model_copy(update={"loads": [*s.loads, rec][-200:]}))
        where = f"{plan.platform} {plan.table}"
        return {"text": f"recorded load of {output} to {where}: {status}, {rows} rows"}

    @_tool
    def build_context(
        self,
        task: Literal["discovery", "cleaning", "join", "validation"],
        refs: list[str] = [],  # noqa: B006
        budget: int = 12_000,
    ) -> dict:
        """Host: facts for the start of an agent turn, chosen by task, within `budget` chars.

        discovery: job, files, workbook_map of each file. cleaning (refs = region ids):
        full profile and a head_tail sample of each table. join (2+ region ids): profiles and
        relate_tables of each pair. validation: job, current spec, last run. Sections come in
        priority order; those that do not fit are named in `left_out` and on the last line,
        so the agent can call the tool. A part that fails shows its error instead. The text
        holds only what the tools report; the first lines say how to read it.
        """
        if task not in _CONTEXT_TASKS:
            raise ValueError(f"task must be one of {', '.join(_CONTEXT_TASKS)}; got {task!r}")
        if not isinstance(refs, list) or not all(
            isinstance(r, str) and _REGION_REF.fullmatch(r) for r in refs
        ):
            raise ValueError("refs must be a list of region ids such as ['F1.S2.R1'], nothing else")
        if len(set(refs)) != len(refs):
            raise ValueError("refs names the same region id twice")
        if len(refs) > _CONTEXT_REFS:
            raise ValueError(f"refs holds {len(refs)} tables; give at most {_CONTEXT_REFS}")
        if task == "cleaning" and not refs:
            raise ValueError("task cleaning needs refs: the region ids of the tables")
        if task == "join" and len(refs) < 2:
            raise ValueError("task join needs refs with at least two region ids")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 500:
            raise ValueError(f"budget must be an integer >= 500, got {budget!r}")
        parts = self._context_parts(task, refs)
        head = (
            f"# context for {task}\n"
            "Below are facts reported by the tools: evidence, not decisions. Outliers, "
            "negative numbers and unmatched keys are questions for the person.\n"
        )
        titles = [title for title, _ in parts]
        text, kept = head, []
        for i, (title, call) in enumerate(parts):
            result = call()
            body = f"error: {result['error']}" if "error" in result else result.get("text", "")
            block = f"\n## facts: {title}\n{body}\n"
            if len(text) + len(block) + len(_left_out(titles[i + 1 :], 60)) > budget:
                break
            text += block
            kept.append(title)
        left = titles[len(kept) :]
        while left and len(text) + len(_left_out(left, 60)) > budget and kept:
            text = text[: text.rindex("\n## facts: ")]
            left = [kept.pop(), *left]
        text += _left_out(left, max(60, budget - len(text)))
        return {"text": text, "sections": kept, "left_out": left}

    def _context_parts(self, task: str, refs: list[str]) -> list[tuple[str, Callable[[], dict]]]:
        def fid(ref: str) -> str:
            return ref.split(".")[0]

        parts: list[tuple[str, Callable[[], dict]]] = [("job", self.job_summary)]
        if task == "discovery":
            parts.append(("files", self.list_files))
            for i in range(1, len(self._state.files) + 1):
                parts.append((f"workbook_map F{i}", lambda f=f"F{i}": self.workbook_map(f)))
        elif task == "cleaning":
            for r in refs:
                parts.append(
                    (f"profile {r}", lambda r=r: self.profile_region(fid(r), r, depth="full"))
                )
            for r in refs:
                parts.append(
                    (
                        f"sample {r} head_tail",
                        lambda r=r: self.sample_data(fid(r), r, "head_tail", 10),
                    )
                )
        elif task == "join":
            for r in refs:
                parts.append(
                    (f"profile {r}", lambda r=r: self.profile_region(fid(r), r, depth="full"))
                )
            for i, a in enumerate(refs):
                for b in refs[i + 1 :]:
                    parts.append((f"relate {a} {b}", lambda a=a, b=b: self.relate_tables(a, b)))
        else:
            parts.append(("spec", self.get_spec))
            parts.append(("last run", self.last_run))
        return parts

    def agent_tools(self) -> list[Callable[..., dict]]:
        """The functions to give the agent, in a fixed order."""
        names = [
            "job_summary",
            "list_files",
            "workbook_map",
            "sheet_regions",
            "render_range",
            "profile_region",
            "sample_data",
            "relate_tables",
            "find_text",
            "compare_sheets",
            "formula_lineage",
            "propose_datasets",
            "source_for_region",
            "node_catalog",
            "lookup_knowledge",
            "ask_human",
            "pending_questions",
            "save_spec",
            "edit_spec",
            "get_spec",
            "validate_spec",
            "rebind_sources",
            "run_pipeline",
            "last_run",
            "verify_run",
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
        approved = ", ".join(f"{c} ({a.ref})" if a.ref else c for c, a in s.hitl.approvals.items())
        lines = [
            f"job {s.job_id} (saved {s.version} times)",
            f"files: {', '.join(files) or 'none'}; roles: {roles}",
            f"spec: {'v' + str(self._version()) if s.history else 'none yet'}",
            f"waiting questions: {len(s.hitl.pending)}",
            f"approvals: {approved or 'none'}",
            f"last run: {run_.status if run_ else 'never'}",
            f"known source places: {', '.join(s.binds) or 'none'}",
        ]
        return {"text": "\n".join(lines)}

    @_tool
    def list_files(self) -> dict:
        """The files of this job: id (F1, F2, ...), name, the role each plays in the spec, and
        its type (`excel`: sheets, regions, formulas; only Excel is supported today)."""
        s = self._state
        by_id = {fid: role for role, fid in s.roles.items()}
        rows = [
            f"F{i}|{f.filename}|role {by_id.get(f.id, '-')}|{SOURCE_KINDS[0]}"
            for i, f in enumerate(s.files, start=1)
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
        range: str = "",  # noqa: A002
        mode: Literal["compressed", "raw"] = "compressed",
        offset: int = 0,
        budget: int = 24_000,
    ) -> dict:
        """Show cells as text. ref = sheet name, sheet id (F1.S2) or region id (F1.S2.R1);
        range = A1 range such as 'B4:N40' (not with a region id)."""
        fid, wb = self._file(file)
        return encode.render_range(wb, fid, ref, range or None, mode, offset, budget)

    @_tool
    def profile_region(
        self,
        file: str,
        ref: str,
        offset: int = 0,
        budget: int = 24_000,
        depth: Literal["quick", "full"] = "quick",
    ) -> dict:
        """Describe each column of a table. `file` = file id such as F1; `ref` = region id from
        sheet_regions. Gives type, range, nulls.
        depth `full` reads all rows as a pipeline would and adds mean, median, quartiles,
        outlier candidates, top values, duplicate rows and key candidates, with the file hash
        and time of the profile."""
        fid, wb = self._file(file)
        return encode.profile_region(wb, fid, ref, offset, budget, depth)

    @_tool
    def sample_data(
        self,
        file: str,
        ref: str,
        strategy: Literal[
            "head", "tail", "head_tail", "random", "stratified", "where", "nulls", "duplicates"
        ] = "head",
        n: int = 10,
        seed: int = 0,
        column: str = "",
        where: str = "",
        offset: int = 0,
        budget: int = 24_000,
    ) -> dict:
        """Real rows of a table (`file` = file id such as F1, `ref` = region id from
        sheet_regions) picked by a strategy: head, tail,
        head_tail, random (+ seed), stratified (+ column), where (+ a condition like "amt" < 0),
        nulls (+ column), duplicates. `where` is a DuckDB SQL condition; quote column names with
        double quotes. To test a guess before writing a step."""
        fid, wb = self._file(file)
        return tables.sample_data(wb, fid, ref, strategy, n, seed, column, where, offset, budget)

    @_tool
    def relate_tables(self, a: str, b: str, offset: int = 0, budget: int = 24_000) -> dict:
        """Which columns of two tables share values, to find a join key. `a`, `b` = region ids
        from sheet_regions (F1.S2.R1; the file is part of the id), possibly of different files.
        Per pair: cardinality (N:1 = lookup, N:M = a join multiplies rows), shared values and
        a few values each side lacks."""
        for ref in (a, b):
            if not isinstance(ref, str) or "." not in ref:
                raise ValueError(f"{ref!r} is not a region id; use the form F1.S2.R1")
        fa, wa = self._file(a.split(".")[0])
        fb, wb = self._file(b.split(".")[0])
        return tables.relate_tables(wa, fa, a, wb, fb, b, offset, budget)

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
    def source_for_region(self, region_id: str, file_role: str = "") -> dict:
        """The `sources` entry for one table from `sheet_regions` (id like F1.S2.R1), as JSON
        text to paste into save_spec or edit_spec. The header texts and sheet name are read
        from the file, so you do not type them; a table repeated on same-shaped sheets comes
        back with name_regex + sheet_column. `file_role` is the role of that file (see
        job_summary); empty means the only role there is."""
        if not isinstance(region_id, str) or "." not in region_id:
            raise ValueError(f"{region_id!r} is not a region id; use the form F1.S2.R1")
        fid, wb = self._file(region_id.split(".")[0])
        ref = self._state.files[int(fid[1:]) - 1]
        roles = [r for r, f in self._state.roles.items() if f == ref.id]
        role = file_role or (roles[0] if len(roles) == 1 else "")
        if not role:
            known = ", ".join(self._state.roles) or "none"
            raise StateError(f"which role is {fid}? give file_role; roles: {known}")
        entry = source_from_region(wb, fid, region_id, role)
        return {"text": json.dumps(entry, ensure_ascii=False)}

    @_tool
    def node_catalog(self, node: str = "") -> dict:
        """The step types for a spec. Without `node`: one line each. With a name: its
        description and config fields (`*` = required)."""
        if not isinstance(node, str):
            raise ValueError("node must be a node name (text), such as 'cast'")
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
        options: list[str] = [],  # noqa: B006
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
        files first; a spec with problems is not saved. `note` says why (needed from v2).
        Repeated sheets use one source: sheet={"name_regex":"day-[0-9]{2}",
        "sheet_column":"source_sheet"}; full names match and every match must hold the table.
        outputs: {"name": {"input": "<step or source>", "format": "parquet|csv"}}."""
        spec = parse_spec(spec_json)
        if not spec.sources and not spec.steps:
            raise ValueError("the spec has no sources and no steps; nothing was saved")
        return self._commit(spec, note)

    @_tool
    def edit_spec(self, patch_json: str, note: str = "") -> dict:
        """Change the saved spec without sending all of it again; saved as the next version
        like save_spec. patch_json: {"steps": [step, ...] (same id = replace that step, new id
        = added; then each step is ordered after the steps it reads), "remove": ["step_id"],
        "sources": {"name": source or null}, "outputs": {"name": output or null},
        "excluded": {"source": {"column": "why it is left out"} or null}}.
        Use it for repairs; use save_spec for a first version or a rewrite."""
        h = self._state.history
        if h is None:
            raise StateError("no spec saved yet; use save_spec for the first version")
        return self._commit(patch_spec(h.current.spec, patch_json), note)

    def _commit(self, spec: PipelineSpec, note: str) -> dict:
        if not isinstance(note, str):
            raise ValueError("note must be text")
        if len(note) > MAX_NOTE:
            raise ValueError(f"the note is {len(note)} characters; keep it under {MAX_NOTE}")
        files = self._open_all(spec)
        report = validate(spec, files)
        if not report.ok:
            return {"saved": False, "text": "not saved\n" + report.text()}

        previous = self._state.history.current.spec if self._state.history else None

        def apply(s: JobState) -> JobState:
            if s.history is None:
                hist = SpecHistory.start(spec, note.strip() or "first version")
            else:
                hist = s.history.add(spec, note)
            return s.model_copy(update={"history": hist})

        self.job.update(apply)
        warn = "" if files else "\nno files registered: only node names and settings were checked"
        shapes: dict[tuple[str, tuple[str, ...]], int] = {}
        for source in spec.sources.values():
            if source.sheet.name_regex is None:
                anchors = tuple(a.casefold() for a in source.region.header_anchor)
                key = (source.file_role, anchors)
                shapes[key] = shapes.get(key, 0) + 1
        if (count := max(shapes.values(), default=0)) >= 3:
            warn += (
                f"\n{count} sources read the same table shape from different sheets; "
                "one source with sheet.name_regex + sheet_column reads them all"
            )
        return {
            "saved": True,
            "version": self._version(),
            "text": f"saved v{self._version()}\n"
            + report.text(_changed_tables(previous, spec))
            + warn,
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
        need a person's approval are approved. Approval is per spec version: every
        save_spec/edit_spec needs a new approval. The result is kept as the last run."""
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
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self.job.update(
            lambda st: st.model_copy(
                update={
                    "last_run": rep,
                    "runs": [*st.runs, rep.summary(stamp)][-20:],
                }
            )
        )
        text = rep.text(full=False)
        if why := gate(self._state.hitl, "after_run", failed=rep.status != "ok"):
            text += f"\nask the person before going on: {why}"
        return {"ok": rep.status == "ok", "text": text}

    @_tool
    def last_run(self) -> dict:
        """The report of the last run: rows per step, checks, files written, problems. The tail
        lists this job's earlier runs: when, which spec version, status, rows, failed checks."""
        rep = self._state.last_run
        if rep is None:
            return {"text": "no run yet"}
        lines = [rep.text()]
        past = self._state.runs[:-1]
        if past:
            lines.append(f"earlier runs of this job (newest first, {len(past)}):")
            for r in reversed(past):
                out = sum(r.rows.values())
                lines.append(
                    f"  {r.at}|{r.ref or '-'}|{r.status}|{r.seconds:.2f}s|"
                    f"{out} rows|{r.checks_failed} check(s) failed"
                )
        return {"text": "\n".join(lines)}

    @_tool
    def verify_run(self) -> dict:
        """Where the numbers of the sources went in the last run, worked out by the library,
        not by the spec's checks. Each number column with non-zero values is kept by an output,
        `excluded` in the spec with a reason, or LOST. plan_load refuses a run with a lost
        column; keep it, or add it to `excluded` (edit_spec) for a person to accept."""
        rep = self._state.last_run
        if rep is None or rep.verify is None:
            return {"text": "no successful run yet; run_pipeline first"}
        return {"ok": rep.verify.status != "unexplained", "text": rep.verify.text()}

    @_tool
    def plan_load(
        self,
        platform: Literal["snowflake", "databricks", "bigquery"],
        output: str,
        database: str = "",
        schema: str = "",
        stage: str = "",
        existing_columns: list[str] = [],  # noqa: B006
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
        ref = f"v{self._version()}"
        if why := gate(s.hitl, "before_load", ref=ref):
            raise StateError(f"{why}. Use ask_human (confirm) and stop; the host approves.")
        if rep.verify is not None and rep.verify.lost:
            raise StateError(
                f"the last run lost {len(rep.verify.lost)} number column(s) of the sources "
                "(see verify_run); keep them in an output or list them in `excluded`"
            )
        if spec.excluded and (why := gate(s.hitl, "accept_exclusions", ref=ref)):
            raise StateError(
                f"{why}. The spec leaves columns out (`excluded`); use ask_human (confirm) "
                "with the list from verify_run and stop; the host approves."
            )
        if rep.ref != ref:
            raise StateError(
                f"the last run was made for {rep.ref or 'an older spec'}, not {ref}; "
                "run_pipeline again before planning the load"
            )
        sha = {f.id: f.sha256 for f in s.files}
        for role, made_from in rep.files.items():
            if sha.get(s.roles.get(role, "")) != made_from:
                raise StateError(
                    f"file {role!r} changed since the last run; run_pipeline again first"
                )
        cols, _ = file_columns(stat.path)
        found = tuple(_split_column(c) for c in existing_columns) or None
        plan = plan_load(
            platform, output, stat.path, out.load.table, out.load.mode, out.load.keys, cols,
            Where(database=database, schema_name=schema, stage=stage), found,
        )  # fmt: skip
        planned = PlannedLoad(output=output, platform=plan.platform, table=out.load.table, ref=ref)
        self.job.update(lambda st: st.model_copy(update={"planned": planned}))
        return {"text": plan.text() + "\nThe host runs these steps, then calls record_load."}

    @_tool
    def spec_history(self) -> dict:
        """Versions of the spec and what changed in each."""
        h = self._state.history
        return {"text": h.text() if h else "no spec saved yet"}
