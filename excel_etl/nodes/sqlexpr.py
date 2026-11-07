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
        "TYPE",  # the target of a CAST
    }
)

# Functions that read files or the network, read or change session state, run SQL from text,
# or build a value whose size the caller sets. Matched case-insensitively on the bare name.
_DENIED_FUNCTIONS = frozenset(
    {
        # files, network, other databases
        "read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_ndjson",
        "read_parquet", "read_text", "read_blob", "parquet_scan", "parquet_metadata",
        "glob", "sniff_csv", "iceberg_scan", "delta_scan", "sqlite_scan", "postgres_scan",
        "mysql_scan", "excel_scan", "read_xlsx", "http_get", "http_post",
        # running SQL from a string
        "query", "query_table", "json_execute_serialized_sql", "json_serialize_sql",
        "json_deserialize_sql", "parse_query", "from_json_sql",
        # session and database state
        "getvariable", "current_setting", "set_variable", "nextval", "currval",
        "setseed", "checkpoint", "force_checkpoint", "load_extension", "install_extension",
        "which_secret", "duckdb_secrets", "pragma_database_size", "current_database",
        "current_schema", "current_schemas", "current_catalog",
        # raise an error on demand, or take unbounded time or memory
        "error", "sleep_ms", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "repeat",
        "range", "generate_series", "list_resize", "array_resize", "lpad", "rpad",
        "random_bytes",
        # tell the caller about the machine or the server version
        "version", "duckdb_version", "pg_backend_pid", "txid_current", "current_query",
        # not reproducible: a rerun of the same spec would give other data
        "random", "uuid", "gen_random_uuid", "uuidv4", "uuidv7", "now", "current_timestamp",
        "current_date", "current_time", "today", "get_current_timestamp",
        "get_current_time", "localtime", "localtimestamp", "transaction_timestamp",
        "statement_timestamp",
    }
)  # fmt: skip


def _fail(expr: str, why: str) -> NodeError:
    shown = expr if len(expr) <= 80 else expr[:79] + "…"
    return NodeError(f"expression {shown!r}: {why}")


def _walk(node: Any, expr: str) -> None:
    """Visit every dict that looks like an expression node, depth first."""
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
            raise _fail(expr, f"{what} is not allowed; use plain column and function calls")
        if cls == "FUNCTION":
            name = str(node.get("function_name", "")).lower()  # duckdb already lowercases
            if name in _DENIED_FUNCTIONS:
                raise _fail(expr, f"function {name!r} is not allowed")
            if name in _aggregates():
                raise _fail(expr, f"aggregate {name!r} is not allowed here; use the group_by node")
            if node.get("order_bys", {}).get("orders") or node.get("filter"):
                raise _fail(expr, "ORDER BY or FILTER inside a function call is not allowed")
            if node.get("distinct"):
                raise _fail(expr, "DISTINCT inside a function call is not allowed")
        for v in node.values():
            _walk(v, expr)
    elif isinstance(node, list):
        for v in node:
            _walk(v, expr)


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


def _parse(expr: str) -> dict:
    con = duckdb.connect()
    raw = con.execute("SELECT json_serialize_sql(?)", [f"SELECT ({expr}) AS __e"]).fetchone()[0]
    return json.loads(raw)


def check_expression(expr: str) -> str:
    """Return `expr` (stripped) if it is one allowed scalar expression, else raise NodeError.

    Column names that do not exist are not found here: that needs the input schema, and
    happens when the node's `infer_schema` binds the expression.
    """
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
    # Defence in depth: with the parser of duckdb 2.0 the wrapping `( ... )` already makes
    # `1) WHERE (1` or `1) UNION ...` a syntax error or a node without a select list, so
    # several of these checks never fire today. They stay because that grammar is duckdb's,
    # not ours, and an upgrade could loosen it; tests/test_sqlexpr.py pins today's behaviour.
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
