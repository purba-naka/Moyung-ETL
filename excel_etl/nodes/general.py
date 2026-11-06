"""General-purpose nodes (select, rename, filter, ...). Each is a few lines over duckdb SQL."""

from __future__ import annotations

from pydantic import Field

from excel_etl.nodes.base import (
    Node,
    NodeConfig,
    NodeError,
    Relation,
    Schema,
    node,
    quote,
)


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
