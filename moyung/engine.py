"""Check a spec against real files (`validate`) and run it to parquet/csv files (`run`).

Both go through `_load_sources` and `_run_steps`, one pass, so they cannot disagree: `validate`
stops short of running the steps, nothing more. A problem in one step does not hide problems
in steps that do not depend on it; the report lists them all.

ponytail: a run has a memory limit (`connect`, 4GB) that bounds tables and joins but not the
vectors of one scalar expression (the deny list in `sqlexpr` covers those), and no row limit.
Upgrade: run in a process with an OS memory cap, a row cap per step.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from moyung.hitl import HitlState, gate
from moyung.inspect.encode import few
from moyung.inspect.regions import TOTAL_LABEL
from moyung.ml import ARTIFACT_FILES, ARTIFACT_NOTES, MlResult, artifacts, check_columns
from moyung.ml import split as ml_split
from moyung.nodes import Column, NodeError, Schema, build, infer, quote, schema_of
from moyung.nodes.base import check_arity, connect, sql_text
from moyung.nodes.source import (
    Location,
    accepted_types,
    duplicate_headers,
    expect_hits,
    expect_label,
    locate_all,
    missing_required,
    read_locations,
    read_table_file,
    type_fits,
)
from moyung.pipeline import PipelineSpec, Source, Step, dump_spec
from moyung.prep import planned_columns
from moyung.quality import CheckResult, check_schema, run_checks, tables_of
from moyung.tablefile import TableFile, TableFileError, as_table_file
from moyung.verify import VerifyReport, verify
from moyung.workbook import Workbook, WorkbookError

type FileInput = Workbook | TableFile | str | os.PathLike[str] | bytes

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
    where: str
    message: str


class NodeStat(_Report):
    id: str
    kind: Literal["source", "step", "ml"]
    node: str
    rows_in: int
    rows_out: int
    columns: int
    seconds: float
    note: str = ""
    schema_out: tuple[str, ...] = ()


class OutputStat(_Report):
    name: str
    path: str
    format: Literal["parquet", "csv"]
    rows: int
    columns: int
    sha256: str = ""


class NodeRows(_Report):
    id: str
    rows_in: int
    rows_out: int


class RunSummary(_Report):
    """One line of a job's run history: when it ran (`at`, the end), on which spec version and
    content (`spec_sha256`), from which input files (`files`: role -> sha256), through which
    steps, and which output files it made (`outputs` with their sha256)."""

    at: str
    ref: str | None = None
    status: Literal["ok", "partial", "failed"]
    seconds: float
    rows: dict[str, int] = {}
    checks_failed: tuple[str, ...] = ()
    spec_sha256: str = ""
    engine_version: str = ""
    files: dict[str, str] = {}
    names: dict[str, str] = {}
    nodes: tuple[NodeRows, ...] = ()
    outputs: tuple[OutputStat, ...] = ()
    drift: str = ""
    blocked: tuple[str, ...] = ()


_SUMMARY_NODES = 100
_CHUNK = 1 << 20


def _engine_version() -> str:
    try:
        return metadata.version("moyung")
    except metadata.PackageNotFoundError:
        return "unknown"


ENGINE_VERSION = _engine_version()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """The sha256 of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _input_sha256(ref: FileInput) -> str:
    """The sha256 of an input file whatever form the host gave it; empty when unreadable."""
    if isinstance(ref, Workbook | TableFile):
        return ref.sha256
    if isinstance(ref, bytes):
        return hashlib.sha256(ref).hexdigest()
    try:
        return sha256_file(ref)
    except OSError:
        return ""


def _columns(cols: tuple[Column, ...], limit: int = 40) -> str:
    shown = ", ".join(c.name for c in cols[:limit])
    return shown + (f", ... ({len(cols)} columns)" if len(cols) > limit else "")


def _schema_tuple(cols: tuple[Column, ...], limit: int = 40) -> tuple[str, ...]:
    """A node's output schema as `name TYPE` lines (first 40): what a run actually produced."""
    return tuple(f"{c.name} {c.type}" for c in cols[:limit])


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
    status: Literal["ok", "partial", "failed"]
    issues: tuple[Issue, ...]
    warnings: tuple[str, ...]
    nodes: tuple[NodeStat, ...]
    outputs: tuple[OutputStat, ...]
    seconds: float
    checks: tuple[CheckResult, ...] = ()
    ref: str | None = None
    files: dict[str, str] = {}
    verify: VerifyReport | None = None
    artifacts: tuple[str, ...] = ()
    spec_sha256: str = ""
    engine_version: str = ""
    blocked: dict[str, tuple[str, ...]] = {}

    def summary(self, at: str) -> RunSummary:
        """The run as one history line (`at` is now, UTC, given by the caller)."""
        return RunSummary(
            at=at,
            ref=self.ref,
            status=self.status,
            seconds=self.seconds,
            rows={o.name: o.rows for o in self.outputs},
            checks_failed=tuple(
                c.name for c in self.checks if not c.passed and c.severity == "error"
            ),
            spec_sha256=self.spec_sha256,
            engine_version=self.engine_version,
            files=dict(self.files),
            nodes=tuple(
                NodeRows(id=n.id, rows_in=n.rows_in, rows_out=n.rows_out)
                for n in self.nodes[:_SUMMARY_NODES]
            ),
            outputs=self.outputs,
            blocked=tuple(self.blocked),
        )

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
        lines += [
            f"  NOT written: output {name!r}, blocked by failed check(s) {', '.join(why)}"
            for name, why in self.blocked.items()
        ]
        if self.blocked:
            lines.append(
                f"  partial: {len(self.outputs)} output(s) written, {len(self.blocked)} held "
                "back; fix the data or the check and run again, or ask the person whether "
                "the written outputs may be used"
            )
        lines += [
            f"  wrote {a}|artifact: {ARTIFACT_NOTES.get(Path(a).name, '')}" for a in self.artifacts
        ]
        lines += [f"  {i.where}: {i.message}" for i in self.issues]
        lines += [f"  warning: {w}" for w in self.warnings]
        if self.verify is not None:
            text = self.verify.text()
            lines.append(text if full or self.verify.lost else text.split("\n")[0])
        return "\n".join(lines)


@dataclass
class _State:
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schemas: dict[str, Schema] = field(default_factory=dict)
    stats: list[NodeStat] = field(default_factory=list)
    cut: int = 0
    failed_checks: dict[str, str] = field(default_factory=dict)

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
    if isinstance(ref, TableFile):
        raise WorkbookError(f"the file is a {ref.format} file, but this source reads Excel")
    return Workbook.open(ref)


def _open_tables(
    spec: PipelineSpec, files: dict[str, FileInput] | None, st: _State
) -> dict[str, TableFile]:
    """The CSV and Parquet files the spec reads, one per `file_role`. A missing, unreadable or
    mismatched file is an issue on its source, not an exception."""
    tables: dict[str, TableFile] = {}
    if files is None:
        return tables
    for name, source in spec.sources.items():
        if source.kind == "excel":
            continue
        role, where = source.file_role, f"sources.{name}"
        if role in tables:
            if tables[role].format != source.kind:
                st.issue(
                    where,
                    f"file {role!r} is {tables[role].format}, but this source is {source.kind}",
                )
            continue
        if role not in files:
            known = ", ".join(sorted(files)) or "none"
            st.issue(where, f"no file for file_role {role!r}; files given: {known}")
            continue
        try:
            tables[role] = as_table_file(files[role], source.kind)
        except TableFileError as e:
            st.issue(where, f"file {role!r}: {e}")
    return tables


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
                    f"{expect_label(want)} says {want.type} "
                    f"(accepts {'/'.join(accepted_types(want.type))})",
                )


def _open_role(role: str, files: dict[str, FileInput], st: _State, where: str) -> Workbook | None:
    """Open the file for a file_role; a missing or unreadable one is an issue, not an exception."""
    if role not in files:
        known = ", ".join(sorted(files)) or "none"
        st.issue(where, f"no file for file_role {role!r}; files given: {known}")
        return None
    try:
        return open_file(files[role])
    except WorkbookError as e:
        st.issue(where, f"file {role!r}: {e}")
        return None


def _read_warnings(source: Source, locs: list[Location], where: str) -> list[str]:
    """Things a read got through but the author should know about."""
    found: list[str] = []
    if errors := sum(loc.errors for loc in locs):
        found.append(
            f"{where}: {errors} cell(s) hold an Excel error (#DIV/0!, #N/A ...) and are "
            f"read as empty; fix them in the workbook, or use fill_missing after the read"
        )
    for loc in locs:
        if loc.data_in_header is not None:
            found.append(
                f"{where}: header_rows {source.region.header_rows} reaches row "
                f"{loc.data_in_header + 1} of sheet {loc.sheet!r}, which sheet_regions sees "
                "as data, so data values became column names; check header_rows"
            )
            break
    if (below := sum(loc.below for loc in locs)) > 0:
        found.append(
            f"{where}: {below} more filled row(s) lie below the table after "
            f"{source.region.blank_gap} empty row(s), such as a grand total; "
            f"they are not read. A larger end (until_blank_rows(N)) reads them."
        )
    return found


def _load_sources(
    spec: PipelineSpec,
    files: dict[str, FileInput] | None,
    tables: dict[str, TableFile],
    con: duckdb.DuckDBPyConnection,
    st: _State,
) -> None:
    if files is None:
        return
    books: dict[str, Workbook | None] = {}
    for name, source in spec.sources.items():
        where = f"sources.{name}"
        role = source.file_role
        if source.kind != "excel":
            if role in tables:
                _load_table(name, source, tables[role], con, st)
            continue
        if role not in books:
            books[role] = _open_role(role, files, st, where)
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
        st.warnings.extend(_read_warnings(source, locs, where))
        st.stats.append(
            NodeStat(
                id=name,
                kind="source",
                node="read_region",
                rows_in=0,
                rows_out=_count(con, name),
                columns=len(schema),
                seconds=round(time.perf_counter() - t0, 4),
                schema_out=_schema_tuple(schema),
                note=(
                    f"{len(locs)} sheets: {locs[0].sheet}..{locs[-1].sheet}"
                    if source.sheet.name_regex is not None
                    else ""
                ),
            )
        )


def _load_table(
    name: str, source: Source, tf: TableFile, con: duckdb.DuckDBPyConnection, st: _State
) -> None:
    t0 = time.perf_counter()
    try:
        rel = read_table_file(tf, source, con, name)
    except (NodeError, duckdb.Error) as e:
        st.issue(f"sources.{name}", str(e))
        return
    schema = schema_of(rel)
    st.schemas[name] = schema
    _check_columns(source, schema, st, f"sources.{name}")
    if twice := duplicate_headers(tf, source, con):
        st.warnings.append(
            f"sources.{name}: duplicate header(s) {few([repr(n) for n in twice], 6)}; the "
            "reader renamed the copies with a suffix (a_1, A_1), so check which column each "
            "step reads"
        )
    one = schema[0].name if len(schema) == 1 else ""
    if tf.format == "csv" and source.delimiter is None and any(d in one for d in ",;\t|"):
        st.warnings.append(
            f"sources.{name}: the file reads as one column {one!r}; the delimiter was not "
            "detected, set `delimiter` on the source"
        )
    st.stats.append(
        NodeStat(
            id=name,
            kind="source",
            node=f"read_{tf.format}",
            rows_in=0,
            rows_out=_count(con, name),
            columns=len(schema),
            seconds=round(time.perf_counter() - t0, 4),
            schema_out=_schema_tuple(schema),
        )
    )


def _encoded_hint(spec: PipelineSpec, why: str) -> str:
    """Say why a check cannot name a one_hot column: it exists only after the run."""
    ml = spec.ml
    if ml is None or not any(r.strategy == "one_hot" for r in ml.categorical.values()):
        return ""
    if "has no column" not in why or not any(t in why for t in ml.tables().values()):
        return ""
    return (
        " (a one_hot column is replaced by <column>_<value> columns that exist only after the "
        "run: check the other columns, or the row counts)"
    )


def _plan_ml(spec: PipelineSpec, st: _State) -> bool:
    """Check the `ml` declaration against its input's columns and register its tables."""
    ml = spec.ml
    if ml is None or ml.input not in st.schemas:
        return False
    problems = check_columns(ml, st.schemas[ml.input])
    for problem in problems:
        st.issue("ml", problem)
    if problems:
        return False
    columns = planned_columns(ml, st.schemas[ml.input])
    for table in ml.tables().values():
        st.schemas[table] = columns
    return True


def _run_ml(spec: PipelineSpec, con: duckdb.DuckDBPyConnection, st: _State) -> MlResult | None:
    """Split the `ml` input into its partition tables."""
    ml = spec.ml
    if ml is None or not _plan_ml(spec, st):
        return None
    t0 = time.perf_counter()
    try:
        result, problems = ml_split(con, ml, st.schemas[ml.input])
    except (NodeError, duckdb.Error) as e:
        st.issue("ml", str(e).splitlines()[0])
        return None
    for problem in problems:
        st.issue("ml", problem)
    if result is None:
        return None
    seconds = round(time.perf_counter() - t0, 4)
    st.warnings.extend(f"ml: {w}" for w in result.warnings)
    if result.cut:
        cut = ", ".join(f"{part} {n}" for part, n in result.cut.items())
        st.warnings.append(
            f"ml: rows with an empty value were dropped ({cut}); the partitions are smaller "
            "than the sizes asked for"
        )
    for table in ml.tables().values():
        st.schemas[table] = result.columns
    for part, table in ml.tables().items():
        st.stats.append(
            NodeStat(
                id=table,
                kind="ml",
                node="ml_split",
                rows_in=result.rows_in,
                rows_out=result.rows[part],
                columns=len(result.columns),
                seconds=seconds,
                schema_out=_schema_tuple(result.columns),
            )
        )
    return result


def _write_artifacts(
    spec: PipelineSpec,
    result: MlResult | None,
    st: _State,
    out_path: Path,
    parts: list[tuple[Path, Path]],
) -> tuple[str, ...]:
    """The JSON files that describe the prepared dataset, queued like the outputs."""
    if spec.ml is None or result is None:
        return ()
    names = []
    for name, text in artifacts(spec.ml, st.schemas[spec.ml.input], result).items():
        tmp, final = out_path / f"{name}.part", out_path / name
        try:
            tmp.write_text(text, encoding="utf-8", newline="\n")
        except OSError as e:
            st.issue("ml", f"could not write {name}: {e}")
            return ()
        parts.append((tmp, final))
        names.append(str(final))
    return tuple(names)


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0]


def _run_steps(
    spec: PipelineSpec, con: duckdb.DuckDBPyConnection, st: _State, run_nodes: bool
) -> None:
    earlier = set(spec.sources)
    for step in spec.steps:
        where = f"steps.{step.id}"
        known, earlier = earlier, earlier | {step.id}
        try:
            n = build(step.node, step.config)
            check_arity(n, len(step.inputs))
        except NodeError as e:
            st.issue(where, str(e))
            continue
        if unknown := [i for i in step.inputs if i not in known]:
            st.issue(
                where,
                f"reads {unknown[0]!r}, which is not a source or an earlier step",
            )
            continue
        inputs = [st.schemas.get(i) for i in step.inputs]
        if any(s is None for s in inputs):
            lost = next(i for i, s in zip(step.inputs, inputs, strict=True) if s is None)
            if st.issues:
                st.warnings.append(f"{where}: skipped because {lost!r} did not come out")
            continue
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
                schema_out=_schema_tuple(actual),
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
    tables = _open_tables(spec, files, st)
    con = connect(read_paths=[t.path for t in tables.values()])
    try:
        _load_sources(spec, files, tables, con, st)
        _run_steps(spec, con, st, run_nodes=False)
        _plan_ml(spec, st)
        for name, why in check_schema(spec.checks, st.schemas).items():
            st.issue(f"checks.{name}", why + _encoded_hint(spec, why))
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


def _run_checks(
    spec: PipelineSpec, con: duckdb.DuckDBPyConnection, st: _State
) -> list[CheckResult]:
    """Run the data checks: broken ones and failed `error` ones are issues, the rest warnings."""
    results, broken = run_checks(spec.checks, con, st.schemas)
    for name, why in broken.items():
        st.issue(f"checks.{name}", why)
    for r in results:
        if r.passed:
            continue
        if r.severity == "error":
            st.failed_checks[r.name] = r.message
        else:
            st.warnings.append(f"checks.{r.name}: {r.message}")
    return results


def _upstream(spec: PipelineSpec, name: str) -> set[str]:
    """`name` and every source or step it reads, directly or not."""
    steps = {s.id: s for s in spec.steps}
    ml_tables = set(spec.ml.tables().values()) if spec.ml is not None else set()
    seen: set[str] = set()
    todo = [name]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        if n in steps:
            todo.extend(steps[n].inputs)
        elif n in ml_tables and spec.ml is not None:
            todo.append(spec.ml.input)
            seen |= ml_tables
    return seen


def _blocked_outputs(spec: PipelineSpec, failed: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Output -> the failed `error` checks that read a table it is built from. A check that
    reads no output's lineage (an ml table, a dead branch) blocks every output."""
    ups = {oname: _upstream(spec, o.input) for oname, o in spec.outputs.items()}
    out: dict[str, list[str]] = {}
    for cname in failed:
        tables = set(tables_of(spec.checks[cname]))
        hit = [o for o, up in ups.items() if up & tables] or list(ups)
        for oname in hit:
            out.setdefault(oname, []).append(cname)
    return {k: tuple(v) for k, v in out.items()}


def _ml_held(spec: PipelineSpec, failed: dict[str, str]) -> bool:
    """A failed check reads a table the ml partitions are built from."""
    if spec.ml is None:
        return False
    up = _upstream(spec, next(iter(spec.ml.tables().values())))
    return any(up.intersection(tables_of(spec.checks[c])) for c in failed)


def _remove_stale(
    spec: PipelineSpec,
    out_path: Path,
    blocked: dict[str, tuple[str, ...]],
    ml_held: bool,
    st: _State,
) -> None:
    """Delete what an earlier run wrote for an output held back now, so no old file is read
    as this run's result."""
    names = [f"{n}.{spec.outputs[n].format}" for n in blocked]
    if ml_held:
        names += list(ARTIFACT_FILES)
    gone = []
    for name in names:
        path = out_path / name
        try:
            if path.is_file():
                path.unlink()
                gone.append(name)
        except OSError as e:
            st.warnings.append(f"could not remove the old {name}: {e}; do not use it")
    if gone:
        st.warnings.append(f"removed the files of an earlier run: {', '.join(gone)}")


def _warn_old_files(spec: PipelineSpec, out_path: Path, st: _State) -> None:
    """A failed run writes nothing, so whatever stands in the out_dir belongs to an earlier run.
    Name those files: without this the out_dir of a failed run reads like a fresh result."""
    names = [
        name
        for n, out in spec.outputs.items()
        if (out_path / (name := f"{n}.{out.format}")).is_file()
    ]
    names += [a for a in ARTIFACT_FILES if (out_path / a).is_file()]
    if names:
        one = len(names) == 1
        st.warnings.append(
            f"nothing was written, so {', '.join(names)} in the out_dir "
            f"{'is' if one else 'are'} from an earlier run, not this one; do not read "
            f"{'it' if one else 'them'} as this run's result"
        )


def _publish(parts: list[tuple[Path, Path]], st: _State) -> None:
    """Move each temporary file to its final name; on a failure, say which files are new."""
    for n, (tmp, final) in enumerate(parts):
        try:
            os.replace(tmp, final)
        except OSError as e:
            new = ", ".join(f.name for _, f in parts[:n]) or "none"
            old = ", ".join(f.name for _, f in parts[n:])
            st.issue(
                f"outputs.{final.stem}",
                f"cannot write {final.name!r}: {e}; already replaced: {new}; "
                f"still old: {old}; fix the cause and run again",
            )
            return


def _data_warnings(
    con: duckdb.DuckDBPyConnection, table: str, schema: Schema, where: str
) -> list[str]:
    """A total row left in, or NaN in a number column: one query per output."""
    label = next((c.name for c in schema if c.type == "VARCHAR"), None)
    floats = [c.name for c in schema if c.type in ("DOUBLE", "FLOAT")]
    parts = [f"count(*) FILTER (isnan({quote(c)}))" for c in floats]
    if label:
        parts.append(f"count(*) FILTER (regexp_matches({quote(label)}, ?))")
    if not parts:
        return []
    row = con.execute(
        f"SELECT {', '.join(parts)} FROM {quote(table)}", [TOTAL_LABEL] if label else []
    ).fetchall()[0]
    out = [
        f"{where}: column {c!r} has {n} NaN values (text like 'NaN' cast to DOUBLE); "
        "filter or fix them"
        for c, n in zip(floats, row, strict=False)
        if n
    ]
    if label and row[-1]:
        out.append(
            f"{where}: {row[-1]} row(s) with a total label in {label!r}; add a drop_rows step "
            "with drop_regex to remove them"
        )
    return out


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
    half-written file. A failed `error` check holds back only the outputs built from a table
    it reads: the others are written and the status is "partial", and the held back output's
    file from an earlier run is removed. When a check holds back every output, or reads a
    table no output is built from, the run is "failed". A failed run returns status "failed"
    with the issues; it raises
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
    out_path = Path(out_dir)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PipelineError(
            [Issue(where="out_dir", message=f"cannot use {str(out_dir)!r}: {e}")]
        ) from None
    t0 = time.perf_counter()
    st = _State()
    if not spec.outputs:
        st.warnings.append("the spec has no outputs: nothing is written")
    tables = _open_tables(spec, files, st)
    con = connect(out_path, [t.path for t in tables.values()])
    parts: list[tuple[Path, Path]] = []
    outputs: list[OutputStat] = []
    results: list[CheckResult] = []
    checked: VerifyReport | None = None
    ml_result: MlResult | None = None
    written: tuple[str, ...] = ()
    blocked: dict[str, tuple[str, ...]] = {}
    try:
        _load_sources(spec, files, tables, con, st)
        _run_steps(spec, con, st, run_nodes=True)
        if not st.issues:
            ml_result = _run_ml(spec, con, st)
        if not st.issues:
            results = _run_checks(spec, con, st)
        if not st.issues and st.failed_checks:
            blocked = _blocked_outputs(spec, st.failed_checks)
            if len(blocked) >= len(spec.outputs):
                for cname, why in st.failed_checks.items():
                    st.issue(f"checks.{cname}", why)
                blocked = {}
        ml_held = blocked and _ml_held(spec, st.failed_checks)
        if not st.issues:
            _write_outputs(spec, con, out_path, st, parts, outputs, frozenset(blocked))
        if not st.issues and ml_held:
            st.warnings.append("the ml artifacts were not written: a check on the ml data failed")
        elif not st.issues:
            written = _write_artifacts(spec, ml_result, st, out_path, parts)
        if not st.issues:
            _publish(parts, st)
        if not st.issues and blocked:
            _remove_stale(spec, out_path, blocked, bool(ml_held), st)
        if not st.issues:
            checked = verify(spec, con, skip=set(blocked))
    finally:
        con.close()
        for tmp, _ in parts:
            if not tmp.is_dir():
                tmp.unlink(missing_ok=True)
    failed = bool(st.issues)
    if failed:
        _warn_old_files(spec, out_path, st)
    return RunReport(
        status="failed" if failed else ("partial" if blocked else "ok"),
        blocked={} if failed else blocked,
        issues=st.done(),
        warnings=tuple(st.warnings),
        nodes=tuple(st.stats),
        outputs=() if failed else tuple(outputs),
        checks=tuple(results),
        seconds=round(time.perf_counter() - t0, 3),
        ref=ref,
        files={role: sha for role, f in files.items() if (sha := _input_sha256(f))},
        spec_sha256=hashlib.sha256(dump_spec(spec).encode()).hexdigest(),
        engine_version=ENGINE_VERSION,
        verify=checked,
        artifacts=() if failed else written,
    )


def _write_outputs(
    spec: PipelineSpec,
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
    st: _State,
    parts: list[tuple[Path, Path]],
    outputs: list[OutputStat],
    skip: frozenset[str] = frozenset(),
) -> None:
    for name, out in spec.outputs.items():
        if name in skip:
            continue
        where = f"outputs.{name}"
        schema = st.schemas.get(out.input)
        final = out_path / f"{name}.{out.format}"
        tmp = out_path / f"{name}.{out.format}.part"
        if (
            out.format == "parquet"
            and schema
            and any(c.type in ("HUGEINT", "UHUGEINT") for c in schema)
        ):
            st.warnings.append(
                f"{where}: a HUGEINT column is stored as DOUBLE in parquet (precision is lost "
                "past 2^53); cast it to BIGINT or DECIMAL first"
            )
        if schema:
            st.warnings.extend(_data_warnings(con, out.input, schema, where))
        fmt = "PARQUET" if out.format == "parquet" else "CSV, HEADER"
        try:
            parts.append((tmp, final))
            dest = sql_text(tmp.as_posix())
            con.execute(f"COPY (SELECT * FROM {quote(out.input)}) TO {dest} (FORMAT {fmt})")
        except (duckdb.Error, NodeError) as e:
            st.issue(where, f"could not write {final.name}: {str(e).splitlines()[0]}")
            return
        outputs.append(
            OutputStat(
                name=name,
                path=str(final),
                format=out.format,
                rows=st.rows(out.input),
                columns=len(schema or ()),
                sha256=sha256_file(tmp),
            )
        )
