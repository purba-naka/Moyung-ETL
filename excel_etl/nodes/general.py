"""General-purpose nodes (select, rename, filter, ...). Each is a few lines over duckdb SQL."""

from __future__ import annotations

import duckdb
from pydantic import Field, model_validator

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
from excel_etl.nodes.sqlexpr import bind, check_expression, check_type


def _missing(wanted: list[str], schema: Schema, node_name: str) -> None:
    have = {c.name for c in schema}
    if gone := [w for w in wanted if w not in have]:
        shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
        raise NodeError(f"{node_name}: no column {gone[0]!r}; columns: {shown}")


@node("select")
class Select(Node):
    """Keep the listed columns, in the listed order."""

    class Config(NodeConfig):
        columns: list[str] = Field(min_length=1)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _missing(self.config.columns, schema, self.name)
        by_name = {c.name: c for c in schema}
        return tuple(by_name[n] for n in self.config.columns)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        return rel.project(", ".join(quote(c) for c in self.config.columns))


def _unique(names: list[str], what: str, who: str) -> None:
    if dup := next((n for n in names if names.count(n) > 1), None):
        raise NodeError(f"{who}: {what} {dup!r} is listed twice")


@node("rename")
class Rename(Node):
    """Rename columns: `{"old name": "new name"}`. Other columns and the order stay."""

    class Config(NodeConfig):
        mapping: dict[str, str] = Field(min_length=1)

        @model_validator(mode="after")
        def _rules(self) -> Rename.Config:
            if "" in self.mapping.values():
                raise ValueError("a new name cannot be empty")
            _unique(list(self.mapping.values()), "new name", "rename")
            return self

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _missing(list(self.config.mapping), schema, self.name)
        out = tuple(Column(self.config.mapping.get(c.name, c.name), c.type) for c in schema)
        _unique([c.name for c in out], "output column", self.name)
        return out

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        self.infer_schema([schema_of(rel)])
        new = self.config.mapping
        return rel.project(", ".join(f"{quote(c)} AS {quote(new.get(c, c))}" for c in rel.columns))


@node("cast")
class Cast(Node):
    """Change column types: `{"Ton": "DOUBLE", "Tanggal": "DATE"}`.

    `strict` false (default) turns a value that does not fit into NULL, so a stray label does
    not stop the run; true fails on the first one. Text to DATE/TIMESTAMP reads ISO text
    (`2026-01-31`); another date format needs `derive` with `strptime`.
    """

    class Config(NodeConfig):
        types: dict[str, str] = Field(min_length=1)
        strict: bool = False

        @model_validator(mode="after")
        def _known_types(self) -> Cast.Config:
            self.types = {c: check_type(t) for c, t in self.types.items()}
            return self

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _missing(list(self.config.types), schema, self.name)
        return tuple(Column(c.name, self.config.types.get(c.name, c.type)) for c in schema)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        self.infer_schema([schema_of(rel)])
        fn = "CAST" if self.config.strict else "TRY_CAST"
        sets = ", ".join(
            f"{fn}({quote(c)} AS {t}) AS {quote(c)}" for c, t in self.config.types.items()
        )
        return rel.project(f"* REPLACE ({sets})")


@node("filter")
class Filter(Node):
    """Keep rows where a scalar SQL expression is true: `"Ton" > 0 AND "Unit" = 'KG'`.

    Quote column names that are not plain words with double quotes. A row where the
    expression is NULL is dropped, as in SQL.
    """

    class Config(NodeConfig):
        where: str

        @model_validator(mode="after")
        def _safe(self) -> Filter.Config:
            self.where = check_expression(self.where)
            return self

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        (kind,) = bind(schema, f"({self.config.where}) AS __e", self.name)
        if kind.type not in ("BOOLEAN", "NULL"):
            raise NodeError(
                f"filter: {self.config.where!r} gives {kind.type}, not true/false; "
                'write a comparison such as "col" > 0'
            )
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        self.infer_schema([schema_of(rel)])
        return rel.filter(f"({self.config.where})")


@node("derive")
class Derive(Node):
    """Add or replace a column with a scalar SQL expression: `{"name": "Total", "expr": "a + b"}`.

    An existing column of that name is replaced in place; a new one goes last. `type` casts the
    result. Several columns: put one `derive` after another (later ones can use earlier ones).
    """

    class Config(NodeConfig):
        name: str = Field(min_length=1)
        expr: str
        type: str | None = None

        @model_validator(mode="after")
        def _safe(self) -> Derive.Config:
            self.expr = check_expression(self.expr)
            if self.type is not None:
                self.type = check_type(self.type)
            return self

    def _select(self) -> str:
        cfg = self.config
        value = f"({cfg.expr})" if cfg.type is None else f"CAST(({cfg.expr}) AS {cfg.type})"
        return f"{value} AS {quote(cfg.name)}"

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        (made,) = bind(schema, self._select(), self.name)
        if made.name in {c.name for c in schema}:
            return tuple(made if c.name == made.name else c for c in schema)
        return (*schema, made)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        schema = schema_of(rel)
        self.infer_schema([schema])
        if self.config.name in {c.name for c in schema}:
            return rel.project(f"* REPLACE ({self._select()})")
        return rel.project(f"*, {self._select()}")


@node("fill_missing")
class FillMissing(Node):
    """Replace NULL with a fixed value: `{"values": {"Ton": 0, "Unit": "KG"}}`.

    The value must fit the column (a number for a number column, text for text); a mismatch
    is an error when the spec is checked. For "same as above" use `fill_down`.
    """

    class Config(NodeConfig):
        values: dict[str, int | float | str | bool] = Field(min_length=1)

    def _plan(self, schema: Schema) -> list[str]:
        _missing(list(self.config.values), schema, self.name)
        by_name = {c.name: c.type for c in schema}
        plan = []
        for col, value in self.config.values.items():
            lit = _sql_value(value)
            kind = by_name[col]
            try:
                # a constant is only evaluated here, not when binding an empty table
                connect().execute(f"SELECT CAST({lit} AS {kind})")
            except duckdb.Error:
                raise NodeError(
                    f"{self.name}: {value!r} does not fit column {col!r} ({kind})"
                ) from None
            plan.append(f"coalesce({quote(col)}, CAST({lit} AS {kind})) AS {quote(col)}")
        return plan

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._plan(schema)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        plan = self._plan(schema_of(rel))
        return rel.project(f"* REPLACE ({', '.join(plan)})")


def _sql_value(v: int | float | str | bool) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, float) and v != v or v in (float("inf"), float("-inf")):
        raise NodeError("fill_missing: nan and infinity are not valid values")
    return repr(v)


@node("sort")
class Sort(Node):
    """Order rows: `{"by": ["Date", "-Ton"]}`; a leading `-` is descending. NULLs go last.

    Ties keep no promised order, so list enough columns to make the order what you mean.
    """

    class Config(NodeConfig):
        by: list[str] = Field(min_length=1)

    def _keys(self, schema: Schema) -> list[str]:
        have = {c.name for c in schema}
        keys = []
        for item in self.config.by:
            # a column that really starts with `-` wins over the descending marker
            desc = item.startswith("-") and item not in have
            col = item[1:] if desc else item
            _missing([col], schema, self.name)
            keys.append(f"{quote(col)} {'DESC' if desc else 'ASC'} NULLS LAST")
        return keys

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._keys(schema)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        return rel.order(", ".join(self._keys(schema_of(rel))))


@node("dedupe")
class Dedupe(Node):
    """Drop repeated rows. `keys` = the columns that identify a row (default: all columns).

    Of rows with the same keys the first in the table's current order is kept, so run it after
    `sort` if "first" must mean something. Empty (NULL) keys count as equal to each other.
    """

    class Config(NodeConfig):
        keys: list[str] | None = Field(default=None, min_length=1)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        if self.config.keys:
            _missing(self.config.keys, schema, self.name)
            _unique(self.config.keys, "key", self.name)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        schema = schema_of(rel)
        self.infer_schema([schema])
        if not self.config.keys:
            return rel.distinct()
        part = ", ".join(quote(k) for k in self.config.keys)
        cols = ", ".join(quote(c.name) for c in schema)
        # row_number() over () numbers rows in table order. Without it the "first" row of a key
        # is whichever a parallel scan reaches first (measured on 200k rows: not the first).
        return rel.query(
            "__in",
            f"SELECT {cols} FROM (SELECT *, row_number() OVER (PARTITION BY {part} "
            f"ORDER BY __pos) AS __rn FROM (SELECT *, row_number() OVER () AS __pos "
            f"FROM __in)) WHERE __rn = 1 ORDER BY __pos",
        )
