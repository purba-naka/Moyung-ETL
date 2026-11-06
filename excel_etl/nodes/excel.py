"""Nodes for the shapes Excel tables come in: label rows among data, merged labels."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from excel_etl.nodes.base import Node, NodeConfig, NodeError, Relation, Schema, node, quote


def _need(schema: Schema, column: str, node_name: str) -> None:
    if column not in {c.name for c in schema}:
        shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
        raise NodeError(f"{node_name}: no column {column!r}; columns: {shown}")


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


@node("drop_rows")
class DropRows(Node):
    """Drop rows that are not records: blank rows, and label rows among the data.

    `keep_type` keeps only rows whose `column` can be read as that type (a date column holding
    `JAN` or `FEB - 2025` subtotal labels); `drop_regex` drops rows whose `column` matches
    (a `Total` row). The column keeps its type: use `cast` after this if you want it changed.
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

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        if self.config.column:
            _need(schema, self.config.column, self.name)
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
            hit = f"regexp_matches(CAST({col} AS VARCHAR), {_lit(cfg.drop_regex)})"
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
            _need(schema, c, self.name)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        frame = "OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        fills = ", ".join(
            f"last_value({quote(c)} IGNORE NULLS) {frame} AS {quote(c)}"
            for c in dict.fromkeys(self.config.columns)
        )
        return rel.project(f"* REPLACE ({fills})")
