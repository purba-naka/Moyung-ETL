"""Nodes that read structure out of label text: outline codes (`1.2.1`) and joined headers."""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from excel_etl.nodes.base import (
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
from excel_etl.nodes.excel import _lit, _need

_INTEGER = re.compile(r"^U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?$")


def _clash(schema: Schema, new: list[str], dropped: str | None, who: str) -> None:
    have = {c.name for c in schema if c.name != dropped}
    if bad := [n for n in new if n in have]:
        raise NodeError(f"{who}: output column {bad[0]!r} already exists")


@node("parse_hierarchy")
class ParseHierarchy(Node):
    """Read an outline code (`1.2.1`, `A-3-b`, `I > II`) into its depth and its parent's code.

    Adds `level_column` (number of segments, 1 = top) and `parent_column` (the code without
    its last segment, NULL at the top). A code with an empty segment (`1..2`, `1.2.`) or no
    text at all gets NULL in both, so a stray label is visible instead of guessed at.
    The column must be text or whole numbers: Excel stores `1.10` as the number 1.1, so a
    column of decimals cannot hold codes (keep them as text in the sheet).
    """

    class Config(NodeConfig):
        column: str
        separator: str = Field(default=".", min_length=1)
        level_column: str = Field(default="level", min_length=1)
        parent_column: str = Field(default="parent", min_length=1)

        @model_validator(mode="after")
        def _distinct(self) -> ParseHierarchy.Config:
            if self.level_column == self.parent_column:
                raise ValueError("level_column and parent_column must differ")
            return self

    def _check(self, schema: Schema) -> None:
        cfg = self.config
        _need(schema, cfg.column, self.name)
        kind = next(c.type for c in schema if c.name == cfg.column)
        if kind != "VARCHAR" and not _INTEGER.match(kind):
            raise NodeError(
                f"{self.name}: column {cfg.column!r} is {kind}; codes like 1.10 are lost as "
                "numbers (1.10 becomes 1.1). Keep them as text"
            )
        _clash(schema, [cfg.level_column, cfg.parent_column], None, self.name)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._check(schema)
        return (
            *schema,
            Column(self.config.level_column, "BIGINT"),
            Column(self.config.parent_column, "VARCHAR"),
        )

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        self._check(schema_of(rel))
        cfg = self.config
        code = f"trim(CAST({quote(cfg.column)} AS VARCHAR))"
        sep = _lit(cfg.separator)
        parts = f"string_split({code}, {sep})"
        ok = f"({code} IS NOT NULL AND NOT list_contains({parts}, ''))"
        level = f"CASE WHEN {ok} THEN len({parts}) END AS {quote(cfg.level_column)}"
        parent = (
            f"CASE WHEN {ok} AND len({parts}) > 1 "
            f"THEN array_to_string(list_slice({parts}, 1, -2), {sep}) END "
            f"AS {quote(cfg.parent_column)}"
        )
        return rel.project(f"*, {level}, {parent}")


@node("split_header_token")
class SplitHeaderToken(Node):
    """Split one text column into several: a header like `JAN|Ton` into `month` and `unit`.

    Mostly used after `unpivot`, whose `name_column` holds the old column names. Give `into`
    (the new column names, at least two) and either a literal `separator` (split at the first
    separators; the last new column keeps the rest) or a `pattern` regex with exactly one
    capture group per new column. A value that has fewer parts, or that the pattern does not
    match, gets NULL in the columns it cannot fill. Parts are trimmed and empty parts are NULL.
    """

    class Config(NodeConfig):
        column: str
        into: list[str] = Field(min_length=2)
        separator: str | None = Field(default=None, min_length=1)
        pattern: str | None = None
        drop_source: bool = False

        @model_validator(mode="after")
        def _rules(self) -> SplitHeaderToken.Config:
            if (self.separator is None) == (self.pattern is None):
                raise ValueError("give exactly one of separator or pattern")
            if len(set(self.into)) != len(self.into) or "" in self.into:
                raise ValueError("into must be distinct, non-empty names")
            if self.pattern is not None:
                try:
                    groups = re.compile(self.pattern).groups
                except re.error as e:
                    raise ValueError(f"pattern {self.pattern!r} is not a regex: {e}") from e
                if groups != len(self.into):
                    raise ValueError(
                        f"pattern has {groups} capture groups; into names {len(self.into)} columns"
                    )
            return self

    def _check(self, schema: Schema) -> None:
        cfg = self.config
        _need(schema, cfg.column, self.name)
        _clash(schema, cfg.into, cfg.column if cfg.drop_source else None, self.name)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        self._check(schema)
        cfg = self.config
        kept = [c for c in schema if not (cfg.drop_source and c.name == cfg.column)]
        return (*kept, *(Column(n, "VARCHAR") for n in cfg.into))

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        self._check(schema_of(rel))
        cfg = self.config
        text = f"CAST({quote(cfg.column)} AS VARCHAR)"
        n = len(cfg.into)
        exprs = []
        for i, name in enumerate(cfg.into):
            if cfg.separator is not None:
                sep = _lit(cfg.separator)
                parts = f"string_split({text}, {sep})"
                if i < n - 1:
                    raw = f"{parts}[{i + 1}]"
                else:  # the last column takes everything that is left
                    raw = f"array_to_string(list_slice({parts}, {n}, len({parts})), {sep})"
            else:
                pat = _lit(cfg.pattern or "")
                # no match gives '' from regexp_extract, which nullif below turns into NULL
                raw = f"regexp_extract({text}, {pat}, {i + 1})"
            exprs.append(f"nullif(trim({raw}), '') AS {quote(name)}")
        star = f"* EXCLUDE ({quote(cfg.column)})" if cfg.drop_source else "*"
        return rel.project(f"{star}, {', '.join(exprs)}")
