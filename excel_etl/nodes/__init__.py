"""Pipeline nodes. Importing this package registers every node that ships with the library."""

from excel_etl.nodes.base import (
    Column,
    Node,
    NodeError,
    Schema,
    build,
    catalog,
    infer,
    names,
    node,
    quote,
    schema_of,
)

__all__ = [
    "catalog",
    "Column",
    "Node",
    "NodeError",
    "Schema",
    "build",
    "infer",
    "names",
    "node",
    "quote",
    "schema_of",
]

from excel_etl.nodes import combine as _combine  # noqa: E402,F401  (registers its nodes)
from excel_etl.nodes import excel as _excel  # noqa: E402,F401
from excel_etl.nodes import general as _general  # noqa: E402,F401
from excel_etl.nodes import labels as _labels  # noqa: E402,F401
