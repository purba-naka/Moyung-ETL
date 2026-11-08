"""Plan how an output file is loaded into a warehouse table. Nothing here connects to anything.

The library has no credentials and no network. `plan_load` turns a finished run into an
ordered list of steps (send the file, make the table, copy, merge) with the SQL of each, and
the host runs them with its own connection. Platforms differ in a few facts only (how a name
is quoted, how long it may be, which type a duckdb type becomes, how the file reaches the
table), so each platform is a `Profile` of plain data plus one small builder.

The facts come from each vendor's documentation (read 2026-11-08); the page is named next to
each one. They are not proven on a live account: `tests/test_load.py` checks the text of the
SQL and the checks, not a run on the platform.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from excel_etl.nodes.base import Column

type Mode = Literal["replace", "append", "merge"]
type StepKind = Literal["upload", "create", "copy", "merge", "cleanup"]
type Typed = tuple[tuple[str, str, str], ...]  # name, duckdb type, platform type

_DECIMAL = re.compile(r"^DECIMAL\((\d+),(\d+)\)$")
_MAX_SQL = 20_000
_MAX_SHOWN = 10


class LoadError(ValueError):
    """The output cannot be loaded as asked; the message says what to change."""


@dataclass(frozen=True, slots=True)
class Profile:
    """The facts of one platform. A new platform is a new Profile and a builder."""

    name: str
    quote_char: str
    max_name: int
    bad_name: Callable[[str], str | None]  # why a name cannot be used, whatever the quoting
    fold: Callable[[str], str]  # two names with the same folded text are one column
    to_type: Callable[[str], str | None]  # duckdb type name -> platform type, None = none fits
    notes: tuple[str, ...] = ()

    def quote(self, name: str) -> str:
        """A name as one quoted identifier. The only place a name enters SQL text."""
        if "\x00" in name:
            raise LoadError(f"name {name[:40]!r} holds a NUL character")
        q = self.quote_char
        return q + name.replace(q, q * 2) + q

    def path(self, *parts: str) -> str:
        return ".".join(self.quote(p) for p in parts if p)


def _decimal(t: str, max_p: int, max_s: int, name: str) -> str | None:
    m = _DECIMAL.match(t)
    if not m or int(m[1]) > max_p or int(m[2]) > max_s:
        return None
    return f"{name}({m[1]},{m[2]})"


# Snowflake: identifiers (double quotes keep case and any character, at most 255 characters),
# data-types-numeric (NUMBER up to 38 digits, scale up to 37), copy-into-table, merge, put.
_SNOWFLAKE_TYPES = {
    "TINYINT": "NUMBER(3,0)", "SMALLINT": "NUMBER(5,0)", "INTEGER": "NUMBER(10,0)",
    "BIGINT": "NUMBER(19,0)", "UTINYINT": "NUMBER(3,0)", "USMALLINT": "NUMBER(5,0)",
    "UINTEGER": "NUMBER(10,0)", "UBIGINT": "NUMBER(20,0)", "HUGEINT": "NUMBER(38,0)",
    "FLOAT": "FLOAT", "DOUBLE": "FLOAT", "VARCHAR": "VARCHAR", "BLOB": "BINARY",
    "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIME": "TIME", "TIMESTAMP": "TIMESTAMP_NTZ",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP_TZ",
}  # fmt: skip

SNOWFLAKE = Profile(
    name="snowflake",
    quote_char='"',
    max_name=255,
    bad_name=lambda n: None,
    fold=lambda n: n,  # a quoted name keeps its case: "Ton" and "TON" are two columns
    to_type=lambda t: _SNOWFLAKE_TYPES.get(t) or _decimal(t, 38, 37, "NUMBER"),
    notes=(
        "a HUGEINT above 38 digits fails the load; cast it to DECIMAL(38,0) if it may be that big",
    ),
)

# Databricks: delta-copy-into (the target Delta table must exist; a file already loaded is
# skipped), delta-merge-into, sql-ref-names (backticks), column-mapping (a space or one of
# `,;{}()\n\t=` in a column name needs delta.columnMapping.mode), sql-ref-datatypes.
_DATABRICKS_TYPES = {
    "TINYINT": "TINYINT", "SMALLINT": "SMALLINT", "INTEGER": "INT", "BIGINT": "BIGINT",
    "FLOAT": "FLOAT", "DOUBLE": "DOUBLE", "VARCHAR": "STRING", "BLOB": "BINARY",
    "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP_NTZ",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
}  # fmt: skip
_NEEDS_MAPPING = re.compile(r"[ ,;{}()\n\t=]")

DATABRICKS = Profile(
    name="databricks",
    quote_char="`",
    # ponytail: the docs give no length limit for a column name; 255 is a guess at a safe ceiling
    max_name=255,
    bad_name=lambda n: None,
    fold=str.casefold,  # names are kept as written but resolved without letter case
    to_type=lambda t: _DATABRICKS_TYPES.get(t) or _decimal(t, 38, 38, "DECIMAL"),
    notes=("unsigned integers and HUGEINT have no Databricks type: cast them first",),
)

# BigQuery: schemas (column names up to 300 characters, no duplicates that differ only in
# case, reserved prefixes, flexible names allow spaces and `|`), load-statements (LOAD DATA
# INTO appends, OVERWRITE replaces and leaves the table unchanged if the load fails),
# data-types (NUMERIC: 38 digits, scale 9; BIGNUMERIC: 76 digits, scale 38).
_BQ_PREFIXES = (
    "_TABLE_", "_FILE_", "_PARTITION", "_ROW_TIMESTAMP", "__ROOT__", "_COLIDENTIFIER",
    "_CHANGE_SEQUENCE_NUMBER", "_CHANGE_TYPE", "_CHANGE_TIMESTAMP",
)  # fmt: skip
_BQ_BAD_CHARS = re.compile(r'[!"$()*,./:;?@\[\\\]^`{}~]')
_BIGQUERY_TYPES = {
    "TINYINT": "INT64", "SMALLINT": "INT64", "INTEGER": "INT64", "BIGINT": "INT64",
    "UTINYINT": "INT64", "USMALLINT": "INT64", "UINTEGER": "INT64", "FLOAT": "FLOAT64",
    "DOUBLE": "FLOAT64", "VARCHAR": "STRING", "BLOB": "BYTES", "BOOLEAN": "BOOL",
    "DATE": "DATE", "TIME": "TIME", "TIMESTAMP": "DATETIME",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
}  # fmt: skip


def _bigquery_name(n: str) -> str | None:
    if n.upper().startswith(_BQ_PREFIXES):
        return "starts with a prefix BigQuery keeps for itself"
    if _BQ_BAD_CHARS.search(n):
        return "holds a character BigQuery does not allow in a column name"
    return None


def _bigquery_type(t: str) -> str | None:
    return (
        _BIGQUERY_TYPES.get(t) or _decimal(t, 38, 9, "NUMERIC") or _decimal(t, 76, 38, "BIGNUMERIC")
    )


BIGQUERY = Profile(
    name="bigquery",
    quote_char="`",
    max_name=300,
    bad_name=_bigquery_name,
    fold=str.casefold,
    to_type=_bigquery_type,
    notes=(
        "UBIGINT and HUGEINT have no BigQuery integer type: cast them first",
        "a TIMESTAMP without a zone is created as DATETIME here; the docs did not confirm that "
        "a parquet TIMESTAMP loads into it, so check this on the first run",
    ),
)

PROFILES: dict[str, Profile] = {p.name: p for p in (SNOWFLAKE, DATABRICKS, BIGQUERY)}


def profile(name: str) -> Profile:
    p = PROFILES.get(name.lower()) if isinstance(name, str) else None
    if p is None:
        raise LoadError(f"unknown platform {name!r}; use one of: {', '.join(sorted(PROFILES))}")
    return p


class Where(BaseModel):
    """Where the table lives. The host gives it; none of it is in the spec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database: str = ""  # Snowflake database, BigQuery project, Databricks catalog
    schema_name: str = ""  # Snowflake or Databricks schema, BigQuery dataset
    stage: str = ""  # Snowflake stage (@db.schema.stage/dir), Databricks volume path, gs:// folder


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int
    kind: StepKind
    sql: str  # empty for an upload: the host sends the file itself
    why: str
    file: str = ""  # upload: the local file to send


class LoadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str
    output: str
    table: str
    mode: Mode
    columns: Typed
    steps: tuple[Step, ...]
    warnings: tuple[str, ...] = ()

    def text(self) -> str:
        lines = [f"load {self.output} to {self.platform} table {self.table} ({self.mode})"]
        for s in self.steps:
            lines.append(f"  {s.n}. {s.kind}: {s.why}")
            lines += [f"       {ln}" for ln in s.sql.splitlines()]
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def check_columns(p: Profile, columns: tuple[Column, ...]) -> Typed:
    """Each column with its platform type, or LoadError listing every problem at once."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    out = []
    for c in columns:
        shown = c.name[:40]
        if not c.name:
            problems.append("a column has an empty name")
        if len(c.name) > p.max_name:
            problems.append(f"column {shown!r} is longer than {p.max_name} characters")
        if c.name and (why := p.bad_name(c.name)) is not None:
            problems.append(f"column {shown!r} {why}")
        key = p.fold(c.name)
        if key in seen:
            problems.append(f"columns {seen[key]!r} and {shown!r} are one column on {p.name}")
        seen.setdefault(key, shown)
        kind = p.to_type(c.type)
        if kind is None:
            problems.append(f"column {shown!r} is {c.type}, which {p.name} cannot hold; cast it")
        out.append((c.name, c.type, kind or ""))
    if problems:
        more = f"\n(+{len(problems) - _MAX_SHOWN} more)" if len(problems) > _MAX_SHOWN else ""
        raise LoadError(f"cannot load to {p.name}:\n" + "\n".join(problems[:_MAX_SHOWN]) + more)
    return tuple(out)


def _literal(text: str) -> str:
    """A SQL string literal. With `quote`, the only way a value enters SQL text."""
    if "\x00" in text:
        raise LoadError("a value holds a NUL character")
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


_STAGE = re.compile(r"@?[A-Za-z0-9_$./~%-]*")


def _stage_path(where: Where, name: str) -> str:
    """A Snowflake stage reference such as @db.schema.stage/dir/file.parquet (not a literal)."""
    ref = f"{where.stage.rstrip('/')}/{name}" if where.stage else f"@~/{name}"
    if not _STAGE.fullmatch(ref):
        raise LoadError(f"stage path {ref!r} may hold only letters, digits and _ $ . / ~ % -")
    return ref


def _location(where: Where, name: str) -> str:
    """A volume path or gs:// URI for the file, as a SQL string literal."""
    return _literal(f"{where.stage.rstrip('/')}/{name}" if where.stage else name)


def _step(kind: StepKind, sql: str, why: str, file: str = "") -> Step:
    return Step(n=0, kind=kind, sql=sql, why=why, file=file)


def _ddl(p: Profile, fq: str, cols: Typed, tail: str = "") -> str:
    body = ",\n  ".join(f"{p.quote(n)} {t}" for n, _, t in cols)
    return f"{fq} (\n  {body}\n){tail}"


def _merge(p: Profile, target: str, source: str, keys: list[str], cols: Typed) -> str:
    names = [n for n, _, _ in cols]
    on = " AND ".join(f"t.{p.quote(k)} = s.{p.quote(k)}" for k in keys)
    sets = ", ".join(f"t.{p.quote(n)} = s.{p.quote(n)}" for n in names if n not in keys)
    update = f"WHEN MATCHED THEN UPDATE SET {sets}\n" if sets else ""
    into = ", ".join(p.quote(n) for n in names)
    values = ", ".join(f"s.{p.quote(n)}" for n in names)
    return (
        f"MERGE INTO {target} t USING {source} s ON {on}\n{update}"
        f"WHEN NOT MATCHED THEN INSERT ({into}) VALUES ({values})"
    )


type Build = Callable[[Profile, Where, str, str, Mode, list[str], Typed, bool], list[Step]]


def _snowflake(
    p: Profile,
    w: Where,
    file: str,
    table: str,
    mode: Mode,
    keys: list[str],
    cols: Typed,
    reshape: bool,
) -> list[Step]:
    fq = p.path(w.database, w.schema_name, table)
    name = file.replace("\\", "/").rsplit("/", 1)[-1]
    stage = _stage_path(w, "").rstrip("/")
    src = _stage_path(w, name)
    fmt = "FILE_FORMAT = (TYPE = PARQUET) MATCH_BY_COLUMN_NAME = CASE_SENSITIVE"
    local = _literal("file://" + file.replace("\\", "/"))
    put = f"PUT {local} {stage} AUTO_COMPRESS = FALSE OVERWRITE = TRUE"
    steps = [
        _step("upload", put, "send the parquet file to the stage", file),
        _step(
            "create",
            f"CREATE TABLE IF NOT EXISTS {_ddl(p, fq, cols)}",
            "make the table if it is not there yet",
        ),
    ]
    if mode == "replace" and reshape:
        steps[1] = _step(
            "create",
            f"CREATE OR REPLACE TABLE {_ddl(p, fq, cols)}",
            "the table's shape changed: make it again with the file's columns",
        )
        steps.append(
            _step("copy", f"COPY INTO {fq} FROM {src} {fmt} FORCE = TRUE", "load the file")
        )
    elif mode == "replace":
        steps += [
            _step("copy", "BEGIN", "start one transaction, so a failed load keeps the old rows"),
            _step("copy", f"TRUNCATE TABLE {fq}", "empty the table"),
            _step("copy", f"COPY INTO {fq} FROM {src} {fmt} FORCE = TRUE", "load the file"),
            _step("copy", "COMMIT", "keep the new rows"),
        ]
    elif mode == "append":
        why = "add the rows (a file loaded in the last 64 days is skipped: use a new file name)"
        steps.append(_step("copy", f"COPY INTO {fq} FROM {src} {fmt}", why))
    else:
        tmp = p.path(w.database, w.schema_name, f"{table}__load")
        steps += [
            _step(
                "create",
                f"CREATE OR REPLACE TRANSIENT TABLE {tmp} LIKE {fq}",
                "a scratch table for the new rows",
            ),
            _step(
                "copy",
                f"COPY INTO {tmp} FROM {src} {fmt} FORCE = TRUE",
                "load the file into the scratch table",
            ),
            _step(
                "merge",
                _merge(p, fq, tmp, keys, cols),
                "update rows with the same keys, add the others",
            ),
            _step("cleanup", f"DROP TABLE IF EXISTS {tmp}", "remove the scratch table"),
        ]
    return steps


def _databricks(
    p: Profile,
    w: Where,
    file: str,
    table: str,
    mode: Mode,
    keys: list[str],
    cols: Typed,
    reshape: bool,
) -> list[Step]:
    fq = p.path(w.database, w.schema_name, table)
    name = file.replace("\\", "/").rsplit("/", 1)[-1]
    src = _location(w, name)
    mapping = any(_NEEDS_MAPPING.search(n) for n, _, _ in cols)
    props = " USING DELTA" + (
        " TBLPROPERTIES ('delta.columnMapping.mode' = 'name')" if mapping else ""
    )
    copy = f"COPY INTO {fq} FROM {src} FILEFORMAT = PARQUET"
    steps = [
        _step(
            "upload",
            "",
            "put the parquet file where the table can read it (a volume or cloud path)",
            file,
        )
    ]
    if mode == "replace":
        steps += [
            _step(
                "create",
                f"CREATE OR REPLACE TABLE {_ddl(p, fq, cols, props)}",
                "replace the table with an empty one of this shape",
            ),
            _step("copy", copy, "load the file"),
        ]
    elif mode == "append":
        steps += [
            _step(
                "create",
                f"CREATE TABLE IF NOT EXISTS {_ddl(p, fq, cols, props)}",
                "make the table if it is not there yet",
            ),
            _step(
                "copy",
                copy,
                "add the rows (COPY INTO skips a file it already loaded: use a new file name)",
            ),
        ]
    else:
        tmp = p.path(w.database, w.schema_name, f"{table}__load")
        steps += [
            _step(
                "create",
                f"CREATE TABLE IF NOT EXISTS {_ddl(p, fq, cols, props)}",
                "make the table if it is not there yet",
            ),
            _step(
                "create",
                f"CREATE OR REPLACE TABLE {_ddl(p, tmp, cols, props)}",
                "a scratch table for the new rows",
            ),
            _step(
                "copy",
                f"COPY INTO {tmp} FROM {src} FILEFORMAT = PARQUET",
                "load the file into the scratch table",
            ),
            _step(
                "merge",
                _merge(p, fq, tmp, keys, cols),
                "update rows with the same keys, add the others",
            ),
            _step("cleanup", f"DROP TABLE IF EXISTS {tmp}", "remove the scratch table"),
        ]
    return steps


def _bigquery(
    p: Profile,
    w: Where,
    file: str,
    table: str,
    mode: Mode,
    keys: list[str],
    cols: Typed,
    reshape: bool,
) -> list[Step]:
    fq = p.path(w.database, w.schema_name, table)
    name = file.replace("\\", "/").rsplit("/", 1)[-1]
    files = f"FROM FILES (format = 'PARQUET', uris = [{_location(w, name)}])"
    steps = [
        _step("upload", "", "put the parquet file in the Cloud Storage folder the load reads", file)
    ]
    if mode == "replace":
        why = "replace the table; if the load fails the table is unchanged"
        steps.append(_step("copy", f"LOAD DATA OVERWRITE {fq}\n{files}", why))
    elif mode == "append":
        steps.append(
            _step(
                "copy",
                f"LOAD DATA INTO {fq}\n{files}",
                "create the table if needed, then add the rows",
            )
        )
    else:
        tmp = p.path(w.database, w.schema_name, f"{table}__load")
        steps += [
            _step(
                "copy", f"LOAD DATA OVERWRITE {tmp}\n{files}", "load the file into a scratch table"
            ),
            _step(
                "merge",
                _merge(p, fq, tmp, keys, cols),
                "update rows with the same keys, add the others",
            ),
            _step("cleanup", f"DROP TABLE IF EXISTS {tmp}", "remove the scratch table"),
        ]
    return steps


_BUILDERS: dict[str, Build] = {
    "snowflake": _snowflake,
    "databricks": _databricks,
    "bigquery": _bigquery,
}


def plan_load(
    platform: str,
    output: str,
    file: str,
    table: str,
    mode: Mode,
    keys: list[str],
    columns: tuple[Column, ...],
    where: Where,
    existing: tuple[Column, ...] | None = None,
) -> LoadPlan:
    """The steps that load `file` (parquet with `columns`) into `table` on `platform`.

    `existing` is the host's `DESCRIBE` of the target table (None = not looked at, or no such
    table). Raises LoadError when the columns do not fit the platform, the mode lacks its keys,
    or an append or merge would meet a table of another shape.
    """
    p = profile(platform)
    cols = check_columns(p, columns)
    have = {c.name for c in columns}
    if mode == "merge":
        if missing := [k for k in keys if k not in have]:
            shown = ", ".join(c.name for c in columns[:12])
            raise LoadError(f"merge key {missing[0]!r} is not a column; columns: {shown}")
        if not keys:
            raise LoadError("merge needs keys: the columns that identify one row")
    elif keys:
        raise LoadError(f"keys are only for merge, not {mode}")
    drift = compare_target(p, cols, existing) if existing else []
    notes = list(p.notes)
    if drift and mode != "replace":
        more = f"\n(+{len(drift) - _MAX_SHOWN} more)" if len(drift) > _MAX_SHOWN else ""
        raise LoadError(
            f"the {p.name} table {table!r} does not fit the file, so {mode} would fail or "
            "lose data:\n" + "\n".join(drift[:_MAX_SHOWN]) + more
        )
    if drift:
        notes.append("the table's shape changes: " + "; ".join(drift[:3]))
    elif existing is None:
        notes.append("the table was not compared with the file: pass its DESCRIBE to check")
    steps = tuple(
        s.model_copy(update={"n": i})
        for i, s in enumerate(
            _BUILDERS[p.name](p, where, file, table, mode, keys, cols, bool(drift)), 1
        )
    )
    if sum(len(s.sql) for s in steps) > _MAX_SQL:
        raise LoadError(f"the plan is over {_MAX_SQL} characters; the table has too many columns")
    if p.name == "databricks" and any(_NEEDS_MAPPING.search(c.name) for c in columns):
        notes.append(
            "a column name with a space or ,;{}()= needs column mapping: the table is made with it"
        )
    return LoadPlan(
        platform=p.name,
        output=output,
        table=table,
        mode=mode,
        columns=cols,
        steps=steps,
        warnings=tuple(notes),
    )


# --- the file that is loaded, and the table it meets ---------------------------------------------


def file_columns(path: str) -> tuple[tuple[Column, ...], int]:
    """Columns (duckdb type names) and row count of a parquet file, read from its footer.

    The file is what gets loaded, so its columns are the truth, not the spec: a HUGEINT
    column was already stored as DOUBLE when the file was written.
    """
    import duckdb

    con = duckdb.connect()
    try:
        lit = _literal(path)
        rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit})").fetchall()
        n = con.execute(f"SELECT count(*) FROM read_parquet({lit})").fetchone()
    except duckdb.Error as e:
        raise LoadError(f"cannot read {path!r}: {str(e).splitlines()[0][:150]}") from None
    finally:
        con.close()
    return tuple(Column(r[0], str(r[1])) for r in rows), int(n[0] if n else 0)


def _family(t: str) -> str:
    """`NUMBER(38,0)` -> `NUMBER`, `varchar(16777216)` -> `VARCHAR`: the part that must agree."""
    return re.split(r"[(\s<]", t.strip().upper(), maxsplit=1)[0]


# Different spellings of one family, as each platform's own DESCRIBE reports them.
_SAME = {
    "INT": "INT", "INTEGER": "INT", "BIGINT": "INT", "SMALLINT": "INT", "TINYINT": "INT",
    "INT64": "INT", "FLOAT": "FLOAT", "DOUBLE": "FLOAT", "FLOAT64": "FLOAT", "REAL": "FLOAT",
    "STRING": "TEXT", "VARCHAR": "TEXT", "TEXT": "TEXT", "BOOL": "BOOL", "BOOLEAN": "BOOL",
    "NUMBER": "NUM", "NUMERIC": "NUM", "DECIMAL": "NUM", "BIGNUMERIC": "NUM",
    "TIMESTAMP_NTZ": "TS", "DATETIME": "TS", "TIMESTAMP": "TS", "TIMESTAMP_TZ": "TSZ",
    "BYTES": "BIN", "BINARY": "BIN", "BLOB": "BIN", "DATE": "DATE", "TIME": "TIME",
}  # fmt: skip


def _kind(t: str) -> str:
    fam = _family(t)
    return _SAME.get(fam, fam)


def compare_target(p: Profile, planned: Typed, existing: tuple[Column, ...]) -> list[str]:
    """What differs between the table the file would create and the table that exists.

    `existing` is the host's `DESCRIBE` of the target: (name, type) with the platform's own type
    names. A number column of any width is one family: `NUMBER(38,0)` and `NUMBER(19,0)` are
    not reported, because a wider column holds a narrower one. Returns lines; empty = they fit.
    """
    have = {p.fold(c.name): c for c in existing}
    want = {p.fold(n): (n, kind) for n, _, kind in planned}
    out = []
    for key, (name, kind) in want.items():
        if key not in have:
            out.append(f"new column {name!r} ({kind}) is not in the table")
        elif _kind(have[key].type) != _kind(kind):
            out.append(f"column {name!r} is {have[key].type} in the table but {kind} in the file")
    out += [f"table column {c.name!r} is not in the file" for k, c in have.items() if k not in want]
    return out
