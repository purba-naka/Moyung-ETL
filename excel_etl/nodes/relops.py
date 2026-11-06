"""Helpers for nodes that read several relations or reshape a table.

`apply` runs a query over relations, `shape` runs the same query over empty tables of the
same schemas to learn the output columns. A node writes its query once as a function of the
view names, so `infer_schema` and `run` cannot drift apart.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

import duckdb

from excel_etl.nodes.base import Column, NodeError, Relation, Schema, connect, quote
from excel_etl.nodes.sqlexpr import _reason

type MakeSql = Callable[[list[str]], str]

_ids = itertools.count()
_RESERVED = "__"


def _fresh() -> str:
    # A view name used twice makes an older relation read the newer data (measured), so every
    # call gets its own.
    return f"__v{next(_ids)}"


def apply(rels: list[Relation], make_sql: MakeSql) -> Relation:
    """Query over `rels`; `make_sql` gets one view name per relation, in order."""
    names = [_fresh() for _ in rels]
    for rel, name in zip(rels[1:], names[1:], strict=True):
        rel.create_view(name, True)
    return rels[0].query(names[0], make_sql(names))


def shape(schemas: list[Schema], make_sql: MakeSql, who: str) -> Schema:
    """Output columns of the query over empty tables shaped like `schemas`; no data read."""
    con = connect()
    names = [_fresh() for _ in schemas]
    try:
        for schema, name in zip(schemas, names, strict=True):
            cols = ", ".join(f"{quote(c.name)} {c.type}" for c in schema)
            con.execute(f"CREATE TEMP TABLE {name} ({cols})")
        rel = con.sql(make_sql(names))
        return tuple(Column(n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True))
    except duckdb.Error as e:
        raise NodeError(f"{who}: {_reason(e)}") from None


def check_reserved(schema: Schema, who: str) -> None:
    """Column names starting with `__` are used for helper columns inside queries."""
    if bad := next((c.name for c in schema if c.name.startswith(_RESERVED)), None):
        raise NodeError(f"{who}: column {bad!r} starts with '__', which is reserved; rename it")


def need(schema: Schema, column: str, who: str) -> Column:
    for c in schema:
        if c.name == column:
            return c
    shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
    raise NodeError(f"{who}: no column {column!r}; columns: {shown}")


def unique(names: list[str], what: str, who: str) -> None:
    if dup := next((n for n in names if names.count(n) > 1), None):
        raise NodeError(f"{who}: {what} {dup!r} appears twice")
