"""Nodes for the shapes Excel tables come in: label rows among data, merged labels."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from excel_etl.nodes.base import (
    NUMERIC_TYPE,
    Column,
    Node,
    NodeConfig,
    NodeError,
    Relation,
    Schema,
    node,
    quote,
    schema_of,
)


def check_column(schema: Schema, column: str, node_name: str) -> None:
    if column not in {c.name for c in schema}:
        shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
        raise NodeError(f"{node_name}: no column {column!r}; columns: {shown}")


def sql_text(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


@node("drop_rows")
class DropRows(Node):
    """Drop rows that are not records: blank rows, and label rows among the data.

    `keep_type` keeps only rows whose `column` can be read as that type (a date column holding
    `JAN` or `FEB - 2025` subtotal labels); `drop_regex` drops rows whose `column` matches
    (a `Total` row; case matters, `(?i)` ignores it). A row whose `column` is empty is kept by
    both: an empty label is not a `Total`, and `blank` is the setting for empty rows. The
    column keeps its type: use `cast` after this if you want it changed.
    """

    class Config(NodeConfig):
        blank: bool = True
        column: str | None = None
        keep_type: Literal["date", "timestamp", "bigint", "double", "boolean"] | None = None
        drop_regex: str | None = None

        @model_validator(mode="after")
        def _column_rules(self) -> DropRows.Config:
            if (self.keep_type or self.drop_regex) and not self.column:
                raise ValueError("keep_type and drop_regex need column")
            if self.column and not (self.keep_type or self.drop_regex):
                raise ValueError("column is set but neither keep_type nor drop_regex is")
            if self.drop_regex is not None:
                try:
                    re.compile(self.drop_regex)
                except re.error as e:
                    raise ValueError(f"drop_regex {self.drop_regex!r} is not a regex: {e}") from e
            return self

    def why_rows_change(self) -> str:
        parts = ["blank rows"] if self.config.blank else []
        if self.config.keep_type:
            parts.append(f"{self.config.column} not a {self.config.keep_type}")
        if self.config.drop_regex:
            parts.append(f"{self.config.column} matching /{self.config.drop_regex}/")
        return "dropped: " + ", ".join(parts)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        if self.config.column:
            check_column(schema, self.config.column, self.name)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        cfg, keep = self.config, []
        if cfg.blank:
            keep.append("NOT (" + " AND ".join(f"{quote(c)} IS NULL" for c in rel.columns) + ")")
        if cfg.column and cfg.keep_type:
            keep.append(f"try_cast({quote(cfg.column)} AS {cfg.keep_type.upper()}) IS NOT NULL")
        if cfg.column and cfg.drop_regex is not None:
            col = quote(cfg.column)
            # a NULL cell does not match, so it is kept (keep_type is the rule that drops it)
            hit = f"regexp_matches(CAST({col} AS VARCHAR), {sql_text(cfg.drop_regex)})"
            keep.append(f"NOT coalesce({hit}, false)")
        return rel.filter(" AND ".join(keep)) if keep else rel


@node("fill_down")
class FillDown(Node):
    """Fill empty cells with the last value above, for labels that a merged cell left blank.

    Relies on rows keeping their order, which holds for tables read by `read_region`.
    """

    class Config(NodeConfig):
        columns: list[str] = Field(min_length=1)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        for c in self.config.columns:
            check_column(schema, c, self.name)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        frame = "OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        fills = ", ".join(
            f"last_value({quote(c)} IGNORE NULLS) {frame} AS {quote(c)}"
            for c in dict.fromkeys(self.config.columns)
        )
        return rel.project(f"* REPLACE ({fills})")


type ValueType = Literal["VARCHAR", "DOUBLE", "BIGINT", "DATE", "TIMESTAMP", "BOOLEAN"]


def common_type(types: list[str], forced: str | None) -> str:
    """One type for columns that become one: same stays, all numbers become DOUBLE, else text."""
    if forced:
        return forced
    kinds = set(types)
    if len(kinds) == 1:
        return types[0]
    return "DOUBLE" if all(NUMERIC_TYPE.match(t) for t in kinds) else "VARCHAR"


def _pick(
    schema: Schema, keep: list[str], columns: list[str] | None, pattern: str | None, who: str
):
    """The value columns: listed ones, or every other column whose name matches `pattern`."""
    for c in keep:
        check_column(schema, c, who)
    if columns is not None:
        for c in columns:
            check_column(schema, c, who)
        picked = list(dict.fromkeys(columns))
    else:
        assert pattern is not None
        picked = [c.name for c in schema if c.name not in keep and re.search(pattern, c.name)]
    if clash := [c for c in picked if c in keep]:
        raise NodeError(f"{who}: column {clash[0]!r} is both kept and unpivoted")
    if not picked:
        shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
        raise NodeError(f"{who}: no column to unpivot; columns: {shown}")
    return picked


def _regex(value: str, names: tuple[str, ...] = ()) -> str:
    try:
        rx = re.compile(value)
    except re.error as e:
        raise ValueError(f"{value!r} is not a regex: {e}") from e
    if missing := [n for n in names if n not in rx.groupindex]:
        raise ValueError(f"regex needs named groups (?P<{missing[0]}>...); got {value!r}")
    return value


@node("unpivot")
class Unpivot(Node):
    """Turn columns into rows: wide (one column per month, product, ...) to long.

    `keep` columns repeat on every row; every other column not listed in `columns` or matched by
    `pattern` is dropped. Each value column gives one row per record: its name goes to
    `name_column`, its cell to `value_column`. Columns of different types become one type: all
    numbers -> DOUBLE, otherwise text (or the `value_type` you give). Empty cells give no row
    unless `drop_nulls` is false.
    """

    class Config(NodeConfig):
        keep: list[str] = []
        columns: list[str] | None = Field(default=None, min_length=1)
        pattern: str | None = None  # regex searched in the column name
        name_column: str = Field(default="variable", min_length=1)
        value_column: str = Field(default="value", min_length=1)
        value_type: ValueType | None = None
        drop_nulls: bool = True

        @model_validator(mode="after")
        def _rules(self) -> Unpivot.Config:
            if (self.columns is None) == (self.pattern is None):
                raise ValueError("give exactly one of columns or pattern")
            if self.pattern is not None:
                _regex(self.pattern)
            if self.name_column == self.value_column:
                raise ValueError("name_column and value_column must differ")
            if {self.name_column, self.value_column} & set(self.keep):
                raise ValueError("name_column and value_column must not be kept columns")
            return self

    def _plan(self, schema: Schema):
        cfg = self.config
        picked = _pick(schema, cfg.keep, cfg.columns, cfg.pattern, self.name)
        by_name = {c.name: c.type for c in schema}
        return picked, common_type([by_name[c] for c in picked], cfg.value_type), by_name

    def why_rows_change(self) -> str:
        return "one row per value column" + (
            "; empty values give no row" if self.config.drop_nulls else ""
        )

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _, vtype, by_name = self._plan(schema)
        cfg = self.config
        return (
            *(Column(c, by_name[c]) for c in cfg.keep),
            Column(cfg.name_column, "VARCHAR"),
            Column(cfg.value_column, vtype),
        )

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        cfg = self.config
        picked, vtype, _ = self._plan(schema_of(rel))
        cols = [quote(c) for c in cfg.keep] + [
            f"CAST({quote(c)} AS {vtype}) AS {quote(c)}" for c in picked
        ]
        nulls = "" if cfg.drop_nulls else " INCLUDE NULLS"
        listed = ", ".join(quote(c) for c in picked)
        return rel.query(
            "__in",
            f"SELECT * FROM (SELECT {', '.join(cols)} FROM __in) UNPIVOT{nulls} "
            f"({quote(cfg.value_column)} FOR {quote(cfg.name_column)} IN ({listed}))",
        )


@node("unpivot_groups")
class UnpivotGroups(Node):
    """Turn repeated blocks of columns into rows, one row per block.

    A sheet with `A|Ton, A|Rate, B|Ton, B|Rate` (the same measures repeated for several
    groups: months, plants, products) becomes rows `(group, Ton, Rate)`. `pattern` must name two
    regex groups: `(?P<group>...)` is the block, `(?P<measure>...)` the column inside it, e.g.
    `^(?P<group>.+)[|](?P<measure>[^|]+)$`. A block that lacks a measure gets NULL there.
    Columns that are neither kept nor matched are dropped. A measure with different types in
    different blocks follows the `unpivot` rule (numbers -> DOUBLE, else text).
    """

    class Config(NodeConfig):
        pattern: str
        keep: list[str] = []
        group_column: str = Field(default="group", min_length=1)
        drop_empty: bool = True  # skip a block that has no value at all in a row

        @model_validator(mode="after")
        def _rules(self) -> UnpivotGroups.Config:
            _regex(self.pattern, ("group", "measure"))
            if self.group_column in self.keep:
                raise ValueError("group_column must not be a kept column")
            return self

    def _plan(self, schema: Schema):
        cfg, rx = self.config, re.compile(self.config.pattern)
        for c in cfg.keep:
            check_column(schema, c, self.name)
        found: dict[str, dict[str, str]] = {}  # group -> measure -> column
        types: dict[str, list[str]] = {}
        by_name = {c.name: c.type for c in schema}
        for col in schema:
            m = rx.search(col.name) if col.name not in cfg.keep else None
            if not m:
                continue
            g, ms = m["group"], m["measure"]
            if ms in found.setdefault(g, {}):
                raise NodeError(
                    f"{self.name}: columns {found[g][ms]!r} and {col.name!r} are both "
                    f"group {g!r}, measure {ms!r}"
                )
            found[g][ms] = col.name
            types.setdefault(ms, []).append(col.type)
        if not found:
            shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
            raise NodeError(f"{self.name}: pattern matches no column; columns: {shown}")
        measures = {m: common_type(t, None) for m, t in types.items()}
        if clash := [m for m in measures if m in cfg.keep or m == cfg.group_column]:
            raise NodeError(f"{self.name}: measure {clash[0]!r} clashes with another output column")
        return found, measures, by_name

    def why_rows_change(self) -> str:
        return "one row per column block; blocks with no value give no row"

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _, measures, by_name = self._plan(schema)
        cfg = self.config
        return (
            *(Column(c, by_name[c]) for c in cfg.keep),
            Column(cfg.group_column, "VARCHAR"),
            *(Column(m, t) for m, t in measures.items()),
        )

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        cfg = self.config
        found, measures, _ = self._plan(schema_of(rel))
        keep = [quote(c) for c in cfg.keep]
        selects = []
        for n, (g, cols) in enumerate(found.items()):
            vals = [
                f"CAST({quote(cols[m])} AS {t}) AS {quote(m)}"
                if m in cols
                else f"CAST(NULL AS {t}) AS {quote(m)}"
                for m, t in measures.items()
            ]
            where = ""
            if cfg.drop_empty:
                have = [quote(c) for c in cols.values()]
                where = " WHERE " + " OR ".join(f"{c} IS NOT NULL" for c in have)
            text = sql_text(g)
            head = ", ".join([*keep, f"{text} AS {quote(cfg.group_column)}", *vals])
            # each input row comes out as its blocks in column order: UNION ALL alone gives
            # whatever order the plan finds, so the row and block numbers are kept to sort on
            selects.append(f"SELECT {head}, __row, {n} AS __blk FROM __numbered{where}")
        out = ", ".join([*keep, quote(cfg.group_column), *(quote(m) for m in measures)])
        sql = (
            "WITH __numbered AS (SELECT *, row_number() OVER () AS __row FROM __in) "
            f"SELECT {out} FROM ({' UNION ALL '.join(selects)}) ORDER BY __row, __blk"
        )
        return rel.query("__in", sql)
