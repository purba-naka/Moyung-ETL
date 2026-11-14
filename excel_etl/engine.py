"""Check a spec against real files (`validate`) and run it to parquet/csv files (`run`).

Both go through `_load_sources` and `_run_steps`, one pass, so they cannot disagree: `validate`
stops short of running the steps, nothing more. A problem in one step does not hide problems
in steps that do not depend on it; the report lists them all.

ponytail: no memory or row limit on a run (a many-to-many join can be large); duckdb's own
default (80% of RAM) applies. Upgrade: a `memory_limit` argument passed to `connect`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from excel_etl.hitl import HitlState, gate
from excel_etl.nodes import Column, NodeError, Schema, build, infer, quote, schema_of
from excel_etl.nodes.base import check_arity, connect
from excel_etl.nodes.source import (
    expect_hits,
    expect_label,
    locate_all,
    missing_required,
    read_locations,
    type_fits,
)
from excel_etl.pipeline import PipelineSpec, Source, Step
from excel_etl.quality import CheckResult, check_schema, run_checks
from excel_etl.verify import VerifyReport, verify
from excel_etl.workbook import Workbook, WorkbookError

type FileInput = Workbook | str | os.PathLike[str] | bytes

_MAX_MESSAGE = 400
_MAX_ISSUES = 50


class PipelineError(Exception):
    """A run could not start or finish. `issues` has one entry per problem."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        shown = "\n".join(f"{i.where}: {i.message}" for i in issues[:10])
        more = f"\n(+{len(issues) - 10} more)" if len(issues) > 10 else ""
        super().__init__(shown + more)


class _Report(BaseModel):
    model_config = ConfigDict(frozen=True)


class Issue(_Report):
    where: str  # "sources.sales", "steps.clean", "outputs.final", "files"
    message: str


class NodeStat(_Report):
    id: str
    kind: Literal["source", "step"]
    node: str  # "read_region" for a source
    rows_in: int  # rows of all inputs together; 0 for a source
    rows_out: int
    columns: int
    seconds: float
    # why the row count may differ from the rows read; shown only when it did
    note: str = ""


class OutputStat(_Report):
    name: str
    path: str
    format: Literal["parquet", "csv"]
    rows: int
    columns: int


def _columns(cols: tuple[Column, ...], limit: int = 40) -> str:
    shown = ", ".join(c.name for c in cols[:limit])
    return shown + (f", ... ({len(cols)} columns)" if len(cols) > limit else "")


def _explain(e: Exception, step: Step, schemas: dict[str, Schema]) -> str:
    """The first line of a duckdb error, plus what to try for the two mistakes seen most in
    real sessions. Without this the agent guesses and retries several times."""
    first = str(e).splitlines()[0]
    if isinstance(e, duckdb.ConversionException) and "Unimplemented type for cast" in first:
        return (
            f"{first}. duckdb has no direct cast between these types; cast through VARCHAR "
            "first, or use a function such as make_date or strptime."
        )
    if isinstance(e, duckdb.ConversionException):
        return (
            f"{first}. A column mixes text with numbers or dates (a label row, a footnote). "
            "Add a cast step before this one (a value that does not fit becomes NULL), or "
            "drop_rows with keep_type to remove the label rows."
        )
    if isinstance(e, duckdb.BinderException) and "not found" in first:
        have = "; ".join(
            f"{t}: {', '.join(c.name for c in schemas[t][:15])}"
            for t in step.inputs
            if t in schemas
        )
        return f"{first} Columns this step reads: {have or 'unknown'}."
    return first


class ValidationReport(_Report):
    ok: bool
    issues: tuple[Issue, ...]
    warnings: tuple[str, ...]
    # columns each source and step gives, by name; a name is absent when it could not be
    # worked out (no file given, or an earlier step failed)
    schemas: dict[str, tuple[Column, ...]]

    def text(self, only: set[str] | None = None) -> str:
        """`only` limits the table listing to those names (what a repeat save changed)."""
        head = "ok" if self.ok else f"{len(self.issues)} problem(s)"
        lines = [f"validate: {head}, {len(self.schemas)} tables known"]
        lines += [
            f"  {name}: {_columns(cols)}"
            for name, cols in self.schemas.items()
            if only is None or name in only
        ]
        lines += [f"  {i.where}: {i.message}" for i in self.issues]
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)


class RunReport(_Report):
    status: Literal["ok", "failed"]
    issues: tuple[Issue, ...]
    warnings: tuple[str, ...]
    nodes: tuple[NodeStat, ...]
    outputs: tuple[OutputStat, ...]
    seconds: float
    checks: tuple[CheckResult, ...] = ()
    # what the run read: the spec version and file_role -> sha256, so a load can tell that
    ref: str | None = None
    files: dict[str, str] = {}
    # where the sources' numbers went; None for a failed run (nothing was written)
    verify: VerifyReport | None = None
    files: dict[str, str] = {}

    def text(self, full: bool = True) -> str:
        """`full=False` is for an agent that only needs to act: on success the outputs, on
        failure the steps that went wrong. Steps that ran fine and said nothing are left out
        (they only repeat what the agent just wrote); `full=True` lists every step."""
        lines = [f"run: {self.status}, {self.seconds:.2f}s"]
        failed = {i.where.removeprefix("steps.") for i in self.issues}
        for n in self.nodes:
            if not full and not n.note and n.id not in failed and n.rows_out > 0:
                continue
            rows = f"{n.rows_in}->{n.rows_out} rows"
            lines.append(f"  {n.id}|{n.node}|{rows}|{n.columns} cols|{n.seconds:.2f}s")
            if n.note:
                lines.append(f"    {n.note}")
        if not full and len(lines) < 1 + len(self.nodes):
            lines.insert(1, f"  ({len(self.nodes)} steps ran; only the ones to look at are listed)")
        for c in self.checks:
            mark = "pass" if c.passed else ("FAIL" if c.severity == "error" else "warn")
            lines.append(
                f"  check {c.name}|{c.kind}|{mark}" + ("" if c.passed else f": {c.message}")
            )
        lines += [f"  wrote {o.path}|{o.rows} rows|{o.columns} cols" for o in self.outputs]
        lines += [f"  {i.where}: {i.message}" for i in self.issues]
        lines += [f"  warning: {w}" for w in self.warnings]
        if self.verify is not None:
            # the agent sees the whole verify text only when something was lost
            text = self.verify.text()
            lines.append(text if full or self.verify.lost else text.split("\n")[0])
        return "\n".join(lines)


@dataclass
class _State:
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schemas: dict[str, Schema] = field(default_factory=dict)
    stats: list[NodeStat] = field(default_factory=list)
    cut: int = 0  # issues dropped for lack of room; said once at the end

    def rows(self, name: str) -> int:
        """Rows of a source or step that ran: the one place a count lives is its NodeStat."""
        return next(s.rows_out for s in self.stats if s.id == name)

    def issue(self, where: str, message: str) -> None:
        if len(self.issues) >= _MAX_ISSUES:
            self.cut += 1
            return
        if len(message) > _MAX_MESSAGE:
            message = message[: _MAX_MESSAGE - 3] + "..."
        self.issues.append(Issue(where=where, message=message))

    def done(self) -> tuple[Issue, ...]:
        """The issues, with a last line when some did not fit."""
        if not self.cut:
            return tuple(self.issues)
        more = Issue(where="report", message=f"{self.cut} more problem(s) not shown")
        return (*self.issues, more)


def open_file(ref: FileInput) -> Workbook:
    if isinstance(ref, Workbook):
        return ref
    return Workbook.open(ref)


def _check_columns(source: Source, schema: Schema, st: _State, where: str) -> None:
    """`expect_columns`: a `required` one must match at least one column of the table."""
    names = [c.name for c in schema]
    shown = ", ".join(names[:12]) + (", ..." if len(names) > 12 else "")
    for want in missing_required(source, names):
        st.issue(where, f"required column {expect_label(want)} not found; columns: {shown}")
    for want in source.expect_columns:
        hits = expect_hits(want, names)
        for col in schema:
            if want.type and col.name in hits and not type_fits(want.type, col.type):
                st.issue(
                    where,
                    f"column {col.name!r} is {col.type} but expect_columns "
                    f"{expect_label(want)} says {want.type}",
                )


def _load_sources(
    spec: PipelineSpec,
    files: dict[str, FileInput] | None,
    con: duckdb.DuckDBPyConnection,
    st: _State,
) -> None:
    books: dict[str, Workbook | None] = {}
    for name, source in spec.sources.items():
        where = f"sources.{name}"
        if files is None:
            continue
        role = source.file_role
        if role not in books:
            if role not in files:
                known = ", ".join(sorted(files)) or "none"
                st.issue(where, f"no file for file_role {role!r}; files given: {known}")
                books[role] = None
            else:
                try:
                    books[role] = open_file(files[role])
                except WorkbookError as e:
                    st.issue(where, f"file {role!r}: {e}")
                    books[role] = None
        wb = books[role]
        if wb is None:
            continue
        t0 = time.perf_counter()
        try:
            locs = locate_all(wb, source)
            rel = read_locations(locs, source, con, name)
        except (NodeError, WorkbookError, duckdb.Error) as e:
            st.issue(where, str(e))
            continue
        schema = schema_of(rel)
        st.schemas[name] = schema
        _check_columns(source, schema, st, where)
        errors = sum(loc.errors for loc in locs)
        if errors:
            st.warnings.append(
                f"{where}: {errors} cell(s) hold an Excel error (#DIV/0!, #N/A ...) and are "
                f"read as empty; fix them in the workbook, or use fill_missing after the read"
            )
        for loc in locs:
            if loc.data_in_header is not None:
                st.warnings.append(
                    f"{where}: header_rows {source.region.header_rows} reaches row "
                    f"{loc.data_in_header + 1} of sheet {loc.sheet!r}, which sheet_regions sees "
                    "as data, so data values became column names; check header_rows"
                )
                break
        if (below := sum(loc.below for loc in locs)) > 0:
            st.warnings.append(
                f"{where}: {below} more filled row(s) lie below the table after "
                f"{source.region.blank_gap} empty row(s), such as a grand total; "
                f"they are not read. A larger end (until_blank_rows(N)) reads them."
            )
        n_rows = _count(con, name)
        st.stats.append(
            NodeStat(
                id=name,
                kind="source",
                node="read_region",
                rows_in=0,
                rows_out=n_rows,
                columns=len(schema),
                seconds=round(time.perf_counter() - t0, 4),
                note=(
                    f"{len(locs)} sheets: {locs[0].sheet}..{locs[-1].sheet}"
                    if source.sheet.name_regex is not None
                    else ""
                ),
            )
        )


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    # fetchall, not fetchone: duckdb 2.0 keeps the result of fetchone() open on the connection
    # and the next relation operation on it fails with "unfinished result"
    return con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0]


def _run_steps(
    spec: PipelineSpec, con: duckdb.DuckDBPyConnection, st: _State, run_nodes: bool
) -> None:
    for step in spec.steps:
        where = f"steps.{step.id}"
        try:
            n = build(step.node, step.config)
            check_arity(n, len(step.inputs))
        except NodeError as e:
            st.issue(where, str(e))
            continue
        inputs = [st.schemas.get(i) for i in step.inputs]
        if any(s is None for s in inputs):
            continue  # an input is unknown or failed: that problem is already listed
        t0 = time.perf_counter()
        try:
            inferred = infer(n, inputs)  # type: ignore[arg-type]
        except NodeError as e:
            st.issue(where, str(e))
            continue
        st.schemas[step.id] = inferred
        if not run_nodes:
            continue
        try:
            rels = [con.table(i) for i in step.inputs]
            n.run(rels).create(step.id)
        except (NodeError, duckdb.Error) as e:
            st.issue(where, f"failed while running {step.node}: {_explain(e, step, st.schemas)}")
            del st.schemas[step.id]
            continue
        actual = schema_of(con.table(step.id))
        if actual != inferred:
            st.issue(
                where,
                f"internal: {step.node} promised {[(c.name, c.type) for c in inferred]} but "
                f"produced {[(c.name, c.type) for c in actual]}; this is a bug in the node",
            )
            del st.schemas[step.id]
            continue
        rows = _count(con, step.id)
        st.stats.append(
            NodeStat(
                id=step.id,
                kind="step",
                node=step.node,
                rows_in=(rows_in := sum(st.rows(i) for i in step.inputs)),
                rows_out=rows,
                columns=len(actual),
                seconds=round(time.perf_counter() - t0, 4),
                note=n.why_rows_change() if rows != rows_in else "",
            )
        )


def validate(spec: PipelineSpec, files: dict[str, FileInput] | None = None) -> ValidationReport:
    """Check a spec. With `files` (file_role -> path, bytes or Workbook) the sources are found
    and read, and every step's columns are worked out; no step is run. Without files only the
    steps' own settings can be checked (node names, configs, number of inputs).
    """
    st = _State()
    if files is not None and not isinstance(files, dict):
        st.issue("files", "files must be a dict of file_role -> file")
        files = None
    con = connect()
    try:
        _load_sources(spec, files, con, st)
        _run_steps(spec, con, st, run_nodes=False)
        for name, why in check_schema(spec.checks, st.schemas).items():
            st.issue(f"checks.{name}", why)
    finally:
        con.close()
    if files is None:
        st.warnings.append("no files given: only node names and settings were checked")
    return ValidationReport(
        ok=not st.issues,
        issues=st.done(),
        warnings=tuple(st.warnings),
        schemas={k: tuple(v) for k, v in st.schemas.items()},
    )


def _literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def run(
    spec: PipelineSpec,
    files: dict[str, FileInput],
    out_dir: str | os.PathLike[str],
    hitl: HitlState | None = None,
    ref: str | None = None,
) -> RunReport:
    """Run a spec: read the sources, run the steps in order, write each output to
    `out_dir/<output name>.<format>`. Files are written under a temporary name and moved into
    place only when every step and every output succeeded, so a failed run leaves no
    half-written file. A failed run returns status "failed" with the issues; it raises
    PipelineError only when it cannot start (bad `out_dir`, `files`, or a checkpoint that
    needs a person's approval). With `hitl` the checkpoints `after_propose_datasets` and
    `after_spec` are checked first; `ref` names what is being run (e.g. a spec version) so an
    approval given for another one is refused.
    """
    if hitl is not None and not isinstance(hitl, HitlState):
        raise PipelineError(
            [Issue(where="hitl", message=f"hitl must be a HitlState, got {type(hitl).__name__}")]
        )
    if hitl is not None:
        blocked = [
            Issue(where=f"hitl.{c}", message=why)
            for c in ("after_propose_datasets", "after_spec")
            if (why := gate(hitl, c, ref=ref))
        ]
        if blocked:
            raise PipelineError(blocked)
    if not isinstance(files, dict):
        raise PipelineError(
            [Issue(where="files", message="files must be a dict of file_role -> file")]
        )
    if not isinstance(out_dir, str | os.PathLike):
        raise PipelineError(
            [
                Issue(
                    where="out_dir", message=f"out_dir must be a path, got {type(out_dir).__name__}"
                )
            ]
        )
    target = Path(out_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PipelineError(
            [Issue(where="out_dir", message=f"cannot use {str(out_dir)!r}: {e}")]
        ) from None
    t0 = time.perf_counter()
    st = _State()
    con = connect(target)
    parts: list[tuple[Path, Path]] = []
    outputs: list[OutputStat] = []
    results: list[CheckResult] = []
    checked: VerifyReport | None = None
    try:
        _load_sources(spec, files, con, st)
        _run_steps(spec, con, st, run_nodes=True)
        if not st.issues:
            results, broken = run_checks(spec.checks, con, st.schemas)
            for name, why in broken.items():
                st.issue(f"checks.{name}", why)
            for r in results:
                if r.passed:
                    continue
                if r.severity == "error":
                    st.issue(f"checks.{r.name}", r.message)
                else:
                    st.warnings.append(f"checks.{r.name}: {r.message}")
        if not st.issues:
            _write_outputs(spec, con, target, st, parts, outputs)
        if not st.issues:
            for tmp, final in parts:
                try:
                    os.replace(tmp, final)
                except OSError as e:
                    st.issue(f"outputs.{final.stem}", f"cannot write {final.name!r}: {e}")
                    break
        if not st.issues:
            checked = verify(spec, con)
    finally:
        con.close()
        for tmp, _ in parts:
            if not tmp.is_dir():  # a directory in the way is not ours: leave it
                tmp.unlink(missing_ok=True)
    failed = bool(st.issues)
    return RunReport(
        status="failed" if failed else "ok",
        issues=st.done(),
        warnings=tuple(st.warnings),
        nodes=tuple(st.stats),
        outputs=() if failed else tuple(outputs),
        checks=tuple(results),
        seconds=round(time.perf_counter() - t0, 3),
        ref=ref,
        files={role: f.sha256 for role, f in files.items() if isinstance(f, Workbook)},
        verify=checked,
    )


def _write_outputs(
    spec: PipelineSpec,
    con: duckdb.DuckDBPyConnection,
    target: Path,
    st: _State,
    parts: list[tuple[Path, Path]],
    outputs: list[OutputStat],
) -> None:
    for name, out in spec.outputs.items():
        where = f"outputs.{name}"
        schema = st.schemas.get(out.input)
        final = target / f"{name}.{out.format}"
        tmp = target / f"{name}.{out.format}.part"
        if (
            out.format == "parquet"
            and schema
            and any(c.type in ("HUGEINT", "UHUGEINT") for c in schema)
        ):
            st.warnings.append(
                f"{where}: a HUGEINT column is stored as DOUBLE in parquet (precision is lost "
                "past 2^53); cast it to BIGINT or DECIMAL first"
            )
        fmt = "PARQUET" if out.format == "parquet" else "CSV, HEADER"
        try:
            parts.append((tmp, final))
            con.execute(
                f"COPY (SELECT * FROM {quote(out.input)}) TO {_literal(tmp)} (FORMAT {fmt})"
            )
        except duckdb.Error as e:
            st.issue(where, f"could not write {final.name}: {str(e).splitlines()[0]}")
            return
        outputs.append(
            OutputStat(
                name=name,
                path=str(final),
                format=out.format,
                rows=st.rows(out.input),
                columns=len(schema or ()),
            )
        )
