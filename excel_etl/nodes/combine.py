"""Nodes with more than one input (join, union) or that change the shape of a table
(group_by, pivot). Nothing here takes SQL text: every choice is a named option, so a spec
cannot smuggle an expression in, and `infer_schema` can say what comes out without data.
"""

from __future__ import annotations

import re
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from excel_etl.nodes.base import (
    Column,
    Node,
    NodeConfig,
    NodeError,
    Relation,
    Schema,
    connect,
    node,
    quote,
    schema_of,
)
from excel_etl.nodes.excel import _common_type, _lit
from excel_etl.nodes.relops import apply, check_reserved, need, shape, unique


def _value_lit(v: str | int | float | bool) -> str:
    """A config value as a SQL literal: bools and numbers bare, text quoted."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        return repr(v)
    return _lit(v)


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- join ------------------------------------------------------------------------------------


class JoinKey(_Part):
    left: str
    right: str


@node("join")
class Join(Node):
    """Combine two tables on matching columns.

    `inputs` are the left and the right table, in that order. `on` lists the column pairs that
    must be equal; give a bare name when both sides call it the same (`"id"`). A key with
    NULL in it matches nothing unless `null_equal` is true (NULL = NULL), as in SQL. Columns
    that are not keys and share a name on both sides are an error until you name them with
    `suffixes` (default none): `["_l", "_r"]` turns `name` into `name_l` and `name_r`.
    `how`: inner, left, right or full. A key pair with equal names appears once; with
    different names both columns stay. In a full join the one key column holds the key from
    whichever side has the row.
    """

    min_inputs = 2
    max_inputs = 2

    class Config(NodeConfig):
        on: list[str | JoinKey] = Field(min_length=1)
        how: Literal["inner", "left", "right", "full"] = "inner"
        suffixes: tuple[str, str] | None = None
        null_equal: bool = False

        @model_validator(mode="after")
        def _suffix_rules(self) -> Join.Config:
            if self.suffixes is not None and self.suffixes[0] == self.suffixes[1]:
                raise ValueError("the two suffixes must differ")
            return self

    def _pairs(self) -> list[JoinKey]:
        return [JoinKey(left=k, right=k) if isinstance(k, str) else k for k in self.config.on]

    def _plan(self, schemas: list[Schema]) -> tuple[list[tuple[str, str]], list[Column]]:
        """((view-side, source column name, output name) per output column, schema)."""
        left, right = schemas
        pairs = self._pairs()
        for p in pairs:
            lc, rc = need(left, p.left, self.name), need(right, p.right, self.name)
            if lc.type != rc.type:
                raise NodeError(
                    f"{self.name}: key {p.left!r} is {lc.type} but {p.right!r} is {rc.type}; "
                    "cast one side first so they compare as the same thing"
                )
        merged = {p.left for p in pairs if p.left == p.right}  # a key named alike is one column
        sfx = self.config.suffixes
        left_names = {c.name for c in left}
        right_names = {c.name for c in right}
        out: list[tuple[str, str]] = []
        cols: list[Column] = []
        for side, schema, other in (("l", left, right_names), ("r", right, left_names)):
            for c in schema:
                if side == "r" and c.name in merged:
                    continue
                name = c.name
                if c.name in other and c.name not in merged:
                    if sfx is None:
                        raise NodeError(
                            f"{self.name}: column {c.name!r} is in both tables and is not a "
                            'key; rename one or give suffixes, e.g. ["_l", "_r"]'
                        )
                    name = c.name + sfx[0 if side == "l" else 1]
                out.append((side, c.name))
                cols.append(Column(name, c.type))
        unique([c.name for c in cols], "output column", self.name)
        return out, cols

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        for s in inputs:
            check_reserved(s, self.name)
        _, cols = self._plan(inputs)
        return tuple(cols)

    def _sql(self, names: list[str], schemas: list[Schema]) -> str:
        out, cols = self._plan(schemas)
        pairs = self._pairs()
        merged = {p.left for p in pairs if p.left == p.right}
        op = "IS NOT DISTINCT FROM" if self.config.null_equal else "="
        cond = " AND ".join(f"l.{quote(p.left)} {op} r.{quote(p.right)}" for p in pairs)
        parts = []
        for (side, src), col in zip(out, cols, strict=True):
            if side == "l" and src in merged and self.config.how in ("right", "full"):
                ref = f"coalesce(l.{quote(src)}, r.{quote(src)})"
            else:
                ref = f"{side}.{quote(src)}"
            parts.append(f"{ref} AS {quote(col.name)}")
        how = {"inner": "INNER", "left": "LEFT", "right": "RIGHT", "full": "FULL"}[self.config.how]
        # A join returns rows in whatever order the plan finds them (measured on 300k rows:
        # many-to-many output was not in left order). Positions make it: left rows in table
        # order, each one's matches in right table order, unmatched right rows last.
        left = f"(SELECT *, row_number() OVER () AS __lp FROM {names[0]})"
        right = f"(SELECT *, row_number() OVER () AS __rp FROM {names[1]})"
        return (
            f"SELECT {', '.join(parts)} FROM {left} AS l {how} JOIN {right} AS r ON {cond} "
            f"ORDER BY l.__lp NULLS LAST, r.__rp"
        )

    def run(self, inputs: list[Relation]) -> Relation:
        schemas = [schema_of(r) for r in inputs]
        self.infer_schema(schemas)
        return apply(inputs, lambda names: self._sql(names, schemas))


# --- union -----------------------------------------------------------------------------------


@node("union")
class Union(Node):
    """Stack tables on top of each other.

    `by` "name" (default) lines columns up by name: a column one table lacks is NULL there.
    `by` "position" lines them up by order and needs the same number of columns; the names
    come from the first table. A column with different types in different tables becomes one
    type: all numbers -> DOUBLE, otherwise text. `source_column` adds a column holding the
    0-based number of the input each row came from. `distinct` drops repeated rows.
    """

    min_inputs = 2
    max_inputs = None

    class Config(NodeConfig):
        by: Literal["name", "position"] = "name"
        distinct: bool = False
        source_column: str | None = Field(default=None, min_length=1)

    def _plan(self, schemas: list[Schema]) -> list[tuple[str, list[str | None]]]:
        """Per output column: (name, source column name in each input or None)."""
        cfg = self.config
        if cfg.by == "position":
            width = {len(s) for s in schemas}
            if len(width) != 1:
                raise NodeError(
                    f"{self.name}: inputs have {sorted(width)} columns; by position they must "
                    "have the same number"
                )
            first = schemas[0]
            return [(c.name, [s[i].name for s in schemas]) for i, c in enumerate(first)]
        order: list[str] = []
        for s in schemas:
            order += [c.name for c in s if c.name not in order]
        for s in schemas:
            unique([c.name for c in s], "column", self.name)
        have = [{c.name for c in s} for s in schemas]
        return [(n, [n if n in h else None for h in have]) for n in order]

    def _types(self, schemas: list[Schema]) -> list[tuple[str, str, list[str | None]]]:
        out = []
        for name, srcs in self._plan(schemas):
            types = [
                next(c.type for c in s if c.name == src)
                for s, src in zip(schemas, srcs, strict=True)
                if src is not None
            ]
            out.append((name, _common_type(types, None), srcs))
        if self.config.source_column and self.config.source_column in {n for n, _, _ in out}:
            raise NodeError(
                f"{self.name}: source_column {self.config.source_column!r} is already a column"
            )
        return out

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        for s in inputs:
            check_reserved(s, self.name)
        cols = [Column(n, t) for n, t, _ in self._types(inputs)]
        if self.config.source_column:
            cols.append(Column(self.config.source_column, "BIGINT"))
        return tuple(cols)

    def _sql(self, names: list[str], schemas: list[Schema]) -> str:
        plan = self._types(schemas)
        cfg = self.config
        selects = []
        for i, name in enumerate(names):
            items = []
            for col, kind, srcs in plan:
                src = srcs[i]
                expr = (
                    f"CAST({quote(src)} AS {kind})" if src is not None else f"CAST(NULL AS {kind})"
                )
                items.append(f"{expr} AS {quote(col)}")
            if cfg.source_column:
                items.append(f"CAST({i} AS BIGINT) AS {quote(cfg.source_column)}")
            if cfg.distinct:
                items.append(f"{i} AS __in, row_number() OVER () AS __row")
            selects.append(f"SELECT {', '.join(items)} FROM {name}")
        stacked = " UNION ALL ".join(selects)
        if not cfg.distinct:
            # UNION ALL keeps the inputs stacked in order (measured on 600k rows; this holds
            # while preserve_insertion_order is on, which `connect()` leaves at its default)
            return stacked
        # distinct = keep the first copy of each row in stacked order; plain UNION would
        # return the survivors in hash order
        cols = ", ".join(quote(c) for c, _, _ in plan)
        if cfg.source_column:
            cols += f", {quote(cfg.source_column)}"
        return (
            f"SELECT {cols} FROM (SELECT *, row_number() OVER (PARTITION BY {cols} "
            f"ORDER BY __in, __row) AS __rn FROM ({stacked})) WHERE __rn = 1 ORDER BY __in, __row"
        )

    def run(self, inputs: list[Relation]) -> Relation:
        schemas = [schema_of(r) for r in inputs]
        self.infer_schema(schemas)
        return apply(inputs, lambda names: self._sql(names, schemas))


# --- group_by --------------------------------------------------------------------------------

_FUNCS = {
    "sum": "sum",
    "count": "count",  # non-empty values of the column
    "count_rows": "count",  # rows in the group; `column` not used
    "count_distinct": "count_distinct",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "first": "first",  # first in table order
    "last": "last",
    "join_text": "string_agg",
}
_NUMERIC = re.compile(r"^(U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?|FLOAT|DOUBLE|DECIMAL\(.*\))$")


class Agg(_Part):
    name: str = Field(min_length=1)
    fn: Literal[
        "sum", "count", "count_rows", "count_distinct", "avg", "min", "max", "first", "last",
        "join_text",
    ]  # fmt: skip
    column: str | None = None
    separator: str = ", "  # join_text only

    @model_validator(mode="after")
    def _column_rules(self) -> Agg:
        if self.fn == "count_rows" and self.column is not None:
            raise ValueError("count_rows counts rows and takes no column")
        if self.fn != "count_rows" and not self.column:
            raise ValueError(f"{self.fn} needs a column")
        return self


@node("group_by")
class GroupBy(Node):
    """Summarise: one row per combination of the `by` columns, with the `aggs` computed.

    Each agg is `{"name": "Total", "fn": "sum", "column": "Ton"}`. `fn`: sum, avg (numbers
    only); min, max; count (rows where the column is not empty); count_rows (all rows, no
    column); count_distinct; first, last (by table order); join_text (values joined with
    `separator`, in table order). Empty values are skipped by every function; a sum of nothing
    is NULL. The result is in order of first appearance of each group. `by` may be empty for
    one row for the whole table. Sums of whole numbers come back as BIGINT (overflow = error).
    """

    class Config(NodeConfig):
        by: list[str] = []
        aggs: list[Agg] = Field(min_length=1)

    def _plan(self, schema: Schema) -> list[tuple[Agg, str, str]]:
        """(agg, sql expression over the column, output type)."""
        check_reserved(schema, self.name)
        for b in self.config.by:
            need(schema, b, self.name)
        unique(self.config.by, "group column", self.name)
        names = [a.name for a in self.config.aggs]
        unique([*self.config.by, *names], "output column", self.name)
        plan = []
        for a in self.config.aggs:
            kind = need(schema, a.column, self.name).type if a.column else "BIGINT"
            if a.fn in ("sum", "avg") and not _NUMERIC.match(kind):
                raise NodeError(
                    f"{self.name}: {a.fn} of {a.name!r} needs a number column, but "
                    f"{a.column!r} is {kind}; cast it first"
                )
            col = quote(a.column) if a.column else "*"
            if a.fn == "count_rows":
                expr, out = "count(*)", "BIGINT"
            elif a.fn == "count":
                expr, out = f"count({col})", "BIGINT"
            elif a.fn == "count_distinct":
                expr, out = f"count(DISTINCT {col})", "BIGINT"
            elif a.fn == "sum":
                whole = bool(re.match(r"^U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?$", kind))
                if whole:
                    expr, out = f"CAST(sum({col}) AS BIGINT)", "BIGINT"
                else:
                    expr, out = f"sum({col})", ""
            elif a.fn == "avg":
                expr, out = f"avg({col})", "DOUBLE"
            elif a.fn in ("min", "max"):
                expr, out = f"{a.fn}({col})", kind
            elif a.fn in ("first", "last"):
                pick = "arg_min" if a.fn == "first" else "arg_max"
                expr = f"{pick}({col}, __pos) FILTER (WHERE {col} IS NOT NULL)"
                out = kind
            else:  # join_text
                expr = f"string_agg(CAST({col} AS VARCHAR), {_lit(a.separator)} ORDER BY __pos)"
                out = "VARCHAR"
            plan.append((a, expr, out))
        return plan

    def _sql(self, name: str, schema: Schema) -> str:
        by = [quote(b) for b in self.config.by]
        aggs = [f"{expr} AS {quote(a.name)}" for a, expr, _ in self._plan(schema)]
        src = f"(SELECT *, row_number() OVER () AS __pos FROM {name})"
        sql = f"SELECT {', '.join([*by, *aggs])} FROM {src}"
        if by:
            sql += f" GROUP BY {', '.join(by)} ORDER BY min(__pos)"
        return sql

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._plan(schema)
        return shape([schema], lambda n: self._sql(n[0], schema), self.name)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        schema = schema_of(rel)
        self.infer_schema([schema])
        return apply([rel], lambda names: self._sql(names[0], schema))


# --- pivot -----------------------------------------------------------------------------------


@node("pivot")
class Pivot(Node):
    """Long to wide: the values of `column` become columns, holding an aggregate of `value`.

    `values` is the list of column values to turn into columns, and it is required: the
    columns of the result must be known before any data is read. Find them with
    `profile_region` or a `group_by` first. A row whose `column` is not in `values` is left
    out. `fn`: sum, avg, min, max, count, first, last. Rows are grouped by `by` (the other
    columns you want to keep); order of first appearance. A new column is named by the value,
    or `prefix + value` if `prefix` is given.
    """

    class Config(NodeConfig):
        by: list[str] = []
        column: str
        value: str
        values: list[str | int | float | bool] = Field(min_length=1)
        fn: Literal["sum", "avg", "min", "max", "count", "first", "last"] = "sum"
        prefix: str = ""

        @model_validator(mode="after")
        def _distinct_values(self) -> Pivot.Config:
            names = [self._name(v) for v in self.values]
            if len(set(names)) != len(names):
                raise ValueError("values must give different column names")
            return self

        def _name(self, v: object) -> str:
            return self.prefix + ("true" if v is True else "false" if v is False else str(v))

    def _plan(self, schema: Schema) -> tuple[str, str]:
        check_reserved(schema, self.name)
        cfg = self.config
        for b in cfg.by:
            need(schema, b, self.name)
        unique(cfg.by, "group column", self.name)
        pc = need(schema, cfg.column, self.name)
        vc = need(schema, cfg.value, self.name)
        if cfg.column in cfg.by or cfg.value in cfg.by or cfg.column == cfg.value:
            raise NodeError(f"{self.name}: by, column and value must be different columns")
        if cfg.fn in ("sum", "avg") and not _NUMERIC.match(vc.type):
            raise NodeError(
                f"{self.name}: {cfg.fn} needs a number column, but {cfg.value!r} is {vc.type}"
            )
        clash = {cfg._name(v) for v in cfg.values} & set(cfg.by)
        if clash:
            raise NodeError(f"{self.name}: new column {sorted(clash)[0]!r} is also a `by` column")
        con = connect()
        for v in cfg.values:
            try:
                # a constant is only evaluated here, not when binding an empty table
                con.execute(f"SELECT CAST({_value_lit(v)} AS {pc.type})")
            except duckdb.Error:
                raise NodeError(
                    f"{self.name}: value {v!r} does not fit column {cfg.column!r} ({pc.type})"
                ) from None
        return pc.type, vc.type

    def _sql(self, name: str, schema: Schema) -> str:
        cfg = self.config
        ptype, vtype = self._plan(schema)
        by = [quote(b) for b in cfg.by]
        col, val = quote(cfg.column), quote(cfg.value)
        items = []
        for v in cfg.values:
            match = f"{col} = CAST({_value_lit(v)} AS {ptype})"
            if cfg.fn == "first":
                agg = f"arg_min({val}, __pos) FILTER (WHERE {match} AND {val} IS NOT NULL)"
            elif cfg.fn == "last":
                agg = f"arg_max({val}, __pos) FILTER (WHERE {match} AND {val} IS NOT NULL)"
            elif cfg.fn == "count":
                agg = f"count({val}) FILTER (WHERE {match})"
            else:
                agg = f"{cfg.fn}({val}) FILTER (WHERE {match})"
            items.append(f"{agg} AS {quote(cfg._name(v))}")
        src = f"(SELECT *, row_number() OVER () AS __pos FROM {name})"
        sql = f"SELECT {', '.join([*by, *items])} FROM {src}"
        if by:
            sql += f" GROUP BY {', '.join(by)} ORDER BY min(__pos)"
        return sql

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._plan(schema)
        return shape([schema], lambda n: self._sql(n[0], schema), self.name)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        schema = schema_of(rel)
        self.infer_schema([schema])
        return apply([rel], lambda names: self._sql(names[0], schema))
