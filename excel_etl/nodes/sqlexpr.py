"""Check that a text from a spec is one scalar SQL expression, before it goes into a query.

Specs come from an LLM and may be edited by people, so an expression is never pasted into
SQL unchecked. The text is wrapped as `SELECT (<expr>) AS __e` and parsed by duckdb's own
parser, so quotes, comments and nesting are read the way duckdb reads them; no regex guesses.
Three layers, each enough on its own for the cases it names:

1. Shape: the parse must be exactly one SELECT with one column and no FROM, WHERE, WITH,
   ORDER BY... around it. This is what stops `1) FROM t --`, `1) UNION SELECT ...`, `1; DROP ...`.
2. Tree: only expression node classes in `_ALLOWED`; a subquery, `*`, `COLUMNS(...)`, a `?`
   parameter or `#1` fails. A function must not be in `_DENIED_FUNCTIONS` (file and network
   readers, session state, sequences, and ones that build huge values).
3. The connection from `connect()` has external access off, so a function missing from the
   deny list still cannot read a file.
"""

from __future__ import annotations

import json
import re
from typing import Any

import duckdb

from excel_etl.nodes.base import Column, NodeError, Schema, connect, quote

_MAX_LEN = 4000

_ALLOWED = frozenset(
    {
        "BETWEEN",
        "CASE",
        "CAST",
        "COLUMN_REF",
        "COMPARISON",
        "CONJUNCTION",
        "CONSTANT",
        "FUNCTION",
        "OPERATOR",
        "TYPE",
    }
)

_DENIED_FUNCTIONS = frozenset(
    {
        "read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_ndjson",
        "read_parquet", "read_text", "read_blob", "parquet_scan", "parquet_metadata",
        "glob", "sniff_csv", "iceberg_scan", "delta_scan", "sqlite_scan", "postgres_scan",
        "mysql_scan", "excel_scan", "read_xlsx", "http_get", "http_post",
        "query", "query_table", "json_execute_serialized_sql", "json_serialize_sql",
        "json_deserialize_sql", "parse_query", "from_json_sql",
        "getvariable", "current_setting", "set_variable", "nextval", "currval",
        "setseed", "checkpoint", "force_checkpoint", "load_extension", "install_extension",
        "which_secret", "duckdb_secrets", "pragma_database_size", "current_database",
        "current_schema", "current_schemas", "current_catalog",
        "error", "sleep_ms", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "repeat",
        "range", "generate_series", "list_resize", "array_resize", "lpad", "rpad",
        "random_bytes",
        "version", "duckdb_version", "pg_backend_pid", "txid_current", "current_query",
        "random", "uuid", "gen_random_uuid", "uuidv4", "uuidv7", "now", "current_timestamp",
        "current_date", "current_time", "today", "get_current_timestamp",
        "get_current_time", "localtime", "localtimestamp", "transaction_timestamp",
        "statement_timestamp", "current_localtime", "current_localtimestamp",
        "unnest", "unlist", "generate_subscripts", "format", "printf", "bar", "bitstring",
        "regexp_split_to_table", "ago", "pg_conf_load_time", "pg_postmaster_start_time",
    }
)  # fmt: skip

_MAX_GROWTH = 4096
_GROWERS = frozenset({"replace", "regexp_replace"})
_MACRO_RISK = (
    "unnest|generate_series|generate_subscripts|range\\(|current_timestamp|current_date|"
    "now\\(\\)|get_current|today\\(\\)|current_time|localtime"
)
_MACROS: frozenset[str] | None = None


def _macro_denies() -> frozenset[str]:
    """Names of duckdb macros built on row-multiplying or clock functions, read from duckdb
    itself so a macro added by an upgrade is covered without a list to keep."""
    global _MACROS
    if _MACROS is None:
        rows = duckdb.connect().execute(
            "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type IN ('macro', 'table_macro') "
            "AND regexp_matches(lower(macro_definition), ?)",
            [_MACRO_RISK],
        )
        _MACROS = frozenset(r[0] for r in rows.fetchall())
    return _MACROS


_CLOCK_NAMES = frozenset(
    {"current_date", "current_time", "current_timestamp", "localtime", "localtimestamp"}
)


def _fail(expr: str, why: str) -> NodeError:
    shown = expr if len(expr) <= 80 else expr[:79] + "…"
    return NodeError(f"expression {shown!r}: {why}")


def _args(node: dict) -> list[dict]:
    """The argument expressions of a serialized function call (`arguments` in duckdb 2.x,
    `children` before it)."""
    raw = node.get("arguments") or node.get("children") or []
    return [a.get("expression", a) if isinstance(a, dict) else {} for a in raw]


def _literal_text(arg: dict) -> str | None:
    """The text of a string constant argument, or None when it is not one."""
    if arg.get("class") != "CONSTANT":
        return None
    literal = arg.get("literal")
    if isinstance(literal, dict) and isinstance(literal.get("text"), str):
        return literal["text"]
    value = arg.get("value")
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        return value["value"]
    return None


def _walk(node: Any, expr: str, growth: int = 1) -> None:
    """Visit every dict that looks like an expression node, depth first. `growth` is how many
    times longer nested replace calls can make a text."""
    if isinstance(node, dict):
        cls = node.get("class")
        if cls is not None and cls not in _ALLOWED:
            what = {
                "SUBQUERY": "a subquery",
                "STAR": "* or COLUMNS(...)",
                "PARAMETER": "a ? parameter",
                "POSITIONAL_REFERENCE": "a #1 reference",
                "WINDOW": "a window function (OVER)",
                "LAMBDA": "a lambda",
            }.get(cls, f"a {cls.lower()} expression")
            use = (
                "use the window node" if cls == "WINDOW" else "use plain column and function calls"
            )
            raise _fail(expr, f"{what} is not allowed; {use}")
        if cls == "COLUMN_REF":
            names = [str(n).lower() for n in node.get("column_names") or []]
            if len(names) == 1 and names[0] in _CLOCK_NAMES:
                raise _fail(
                    expr,
                    f"{names[0]} reads the clock, so the output would change from run to run; "
                    "give the date as a constant or a column",
                )
        if cls == "FUNCTION":
            name = str(node.get("function_name", "")).lower()
            if name in _DENIED_FUNCTIONS or name in _macro_denies():
                raise _fail(expr, f"function {name!r} is not allowed")
            args = _args(node)
            if name == "age" and len(args) == 1:
                raise _fail(expr, "age(x) with one argument reads the clock; give both dates")
            if name in _GROWERS and len(args) >= 3:
                text = _literal_text(args[2])
                growth *= max(1, len(text)) if text is not None else 1
                if growth > _MAX_GROWTH:
                    raise _fail(
                        expr,
                        "nested replace calls with long replacement texts can build a huge "
                        "text; use shorter replacements or fewer levels",
                    )
            if name in _aggregates():
                raise _fail(expr, f"aggregate {name!r} is not allowed here; use the group_by node")
            if node.get("order_bys", {}).get("orders") or node.get("filter"):
                raise _fail(expr, "ORDER BY or FILTER inside a function call is not allowed")
            if node.get("distinct"):
                raise _fail(expr, "DISTINCT inside a function call is not allowed")
        for v in node.values():
            _walk(v, expr, growth)
    elif isinstance(node, list):
        for v in node:
            _walk(v, expr, growth)


_AGGREGATES: frozenset[str] | None = None


def _aggregates() -> frozenset[str]:
    """Names duckdb lists as aggregate functions (sum, count, string_agg, ...); none is also scalar.

    Read from duckdb itself, so a function added by an upgrade is covered. In a plain SELECT
    an aggregate collapses the whole table to one row instead of computing per row.
    """
    global _AGGREGATES
    if _AGGREGATES is None:
        rows = duckdb.connect().execute(
            "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type = 'aggregate'"
        )
        _AGGREGATES = frozenset(r[0] for r in rows.fetchall()) | {"count_star"}
    return _AGGREGATES


def _parse(expr: str, wrap: bool = True) -> dict:
    con = duckdb.connect()
    sql = f"SELECT ({expr}) AS __e" if wrap else f"SELECT {expr} AS __e"
    raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    return json.loads(raw)


def check_expression(expr: str) -> str:
    """Return `expr` (stripped) if it is one allowed scalar expression, else raise NodeError.

    Column names that do not exist are not found here: that needs the input schema, and
    happens when the node's `infer_schema` binds the expression.
    """
    try:
        return _check_expression(expr)
    except RecursionError:
        raise NodeError("expression is nested too deeply to read") from None


def _check_expression(expr: str) -> str:
    """The same as `check_expression`, before `RecursionError` is turned into a NodeError."""
    if not isinstance(expr, str):
        raise NodeError(f"expression must be a string, got {type(expr).__name__}")
    text = expr.strip()
    if not text:
        raise NodeError("expression is empty")
    if len(text) > _MAX_LEN:
        raise _fail(text, f"longer than {_MAX_LEN} characters")
    if "\x00" in text:
        raise _fail(text, "contains a NUL character")
    doc = _parse(text)
    if doc.get("error"):
        reason = str(doc.get("error_message", "")).replace("\n", " ")
        if "Only SELECT" in reason:
            reason = "must be one expression, not a statement"
        raise _fail(text, reason[:160])
    statements = doc.get("statements", [])
    if len(statements) != 1:
        raise _fail(text, "must be one expression")
    node = statements[0]["node"]
    items = node.get("select_list") or []
    bare = (
        node.get("type") == "SELECT_NODE"
        and len(items) == 1
        and (node.get("from_table") or {}).get("type") == "EMPTY"
        and node.get("where_clause") is None
        and not node.get("modifiers")
        and not node.get("group_expressions")
        and not node.get("group_sets")
        and node.get("having") is None
        and node.get("qualify") is None
        and node.get("sample") is None
        and not (node.get("cte_map") or {}).get("map")
    )
    if not bare:
        raise _fail(text, "must be one expression, nothing around it (no FROM, WHERE, UNION, ...)")
    _walk(items[0], text)
    if _parse(text, wrap=False).get("error"):
        raise _fail(text, "brackets do not match, or this is not one expression")
    return text


_TYPE = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}(\(\s*\d{1,2}\s*(,\s*\d{1,2}\s*)?\))?(\[\])?$")


def check_type(name: str) -> str:
    """Return duckdb's own spelling of a plain type name (`varchar(5)` -> `VARCHAR`).

    A type name goes into `CAST(x AS <type>)`, so it is as much an injection point as an
    expression: only letters, spaces, `(10,2)` and `[]` are accepted, then duckdb must know it.
    STRUCT, ENUM and other types that carry names or values are not plain and are refused.
    """
    if not isinstance(name, str):
        raise NodeError(f"type must be a string, got {type(name).__name__}")
    text = " ".join(name.split())
    if not _TYPE.match(text):
        raise NodeError(
            f"type {name!r}: use a plain type name such as VARCHAR, DOUBLE, DECIMAL(10,2), "
            "DATE, TIMESTAMP, BOOLEAN"
        )
    try:
        return duckdb.connect().execute(f"SELECT typeof(try_cast(NULL AS {text}))").fetchone()[0]
    except duckdb.Error:
        raise NodeError(f"type {name!r} is not a duckdb type") from None


def reason(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if "GROUP BY" in msg:
        return "aggregate functions are not allowed here; use the group_by node"
    return msg.split(": ", 1)[-1][:200] if ": " in msg[:30] else msg[:200]


def bind(schema: Schema, select_list: str, who: str) -> Schema:
    """Output columns of `SELECT <select_list> FROM <table shaped like schema>`; no data read.

    The select list holds only text that `check_expression` and `check_type` passed and names
    that went through `quote`. A missing column, an aggregate or a type mismatch comes back
    as a NodeError that names `who`.
    """
    cols = ", ".join(f"{quote(c.name)} {c.type}" for c in schema)
    con = connect()
    try:
        con.execute(f"CREATE TEMP TABLE __s ({cols})")
        rel = con.sql(f"SELECT {select_list} FROM __s")
        return tuple(Column(n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True))
    except duckdb.Error as e:
        raise NodeError(f"{who}: {reason(e)}") from None
