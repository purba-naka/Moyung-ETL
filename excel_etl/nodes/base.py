"""What a node is, and the registry that finds one by the name used in a spec.

A node has a pydantic `Config`, `infer_schema` (columns out, from columns in, no data read)
and `run` (a duckdb relation out, from relations in). `infer_schema` is what lets a spec be
checked before any file is opened, so it must agree with what `run` produces.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

type Relation = duckdb.DuckDBPyRelation

_MAX_ERRORS = 10


class NodeError(Exception):
    """A node is unknown, misconfigured, or cannot take the inputs it was given."""


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str  # duckdb type name: VARCHAR, DOUBLE, DATE, ...


type Schema = tuple[Column, ...]


def schema_of(rel: Relation) -> Schema:
    return tuple(Column(n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True))


def quote(name: str) -> str:
    """A column or relation name as a SQL identifier. Excel headers hold `|`, spaces, quotes."""
    return '"' + name.replace('"', '""') + '"'


def describe(e: ValidationError, root: str = "config") -> str:
    """Pydantic errors as `path.to.field: message` lines, at most 10."""
    items = e.errors(include_url=False, include_input=False, include_context=False)
    lines = [
        f"{'.'.join(str(x) for x in i['loc']) or root}: {i['msg'].removeprefix('Value error, ')}"
        for i in items
    ]
    more = f"\n(+{len(lines) - _MAX_ERRORS} more)" if len(lines) > _MAX_ERRORS else ""
    return "\n".join(lines[:_MAX_ERRORS]) + more


class NodeConfig(BaseModel):
    """Base for node configs: a misspelled key is an error, not a silently ignored setting."""

    model_config = ConfigDict(extra="forbid")


class Node:
    """Subclass, set `Config`, implement `infer_schema` and `run`, register with `@node`."""

    Config: ClassVar[type[NodeConfig]] = NodeConfig
    min_inputs: ClassVar[int] = 1
    max_inputs: ClassVar[int | None] = 1  # None = any number
    name: ClassVar[str] = ""

    def __init__(self, config: Any) -> None:
        self.config = config

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        raise NotImplementedError

    def run(self, inputs: list[Relation]) -> Relation:
        raise NotImplementedError


_REGISTRY: dict[str, type[Node]] = {}


def node[T: Node](name: str) -> Callable[[type[T]], type[T]]:
    """Register a node class under the name a spec step uses in `node:`."""

    def register(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"node {name!r} is already registered")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return register


def names() -> list[str]:
    return sorted(_REGISTRY)


def build(name: str, config: dict[str, Any] | None = None) -> Node:
    """Make a node from a spec step (`node` + `config`). Raises NodeError with what to fix."""
    cls = _REGISTRY.get(name)
    if cls is None:
        known = names()
        shown = ", ".join(known[:15]) + (f", +{len(known) - 15}" if len(known) > 15 else "")
        raise NodeError(f"unknown node {name!r}; nodes: {shown}")
    try:
        return cls(cls.Config.model_validate(config or {}))
    except ValidationError as e:
        raise NodeError(f"node {name!r} config:\n{describe(e)}") from None


def check_arity(n: Node, count: int) -> None:
    lo, hi = n.min_inputs, n.max_inputs
    if count < lo or (hi is not None and count > hi):
        want = f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}"
        raise NodeError(f"node {n.name!r} takes {want} input(s), got {count}")


def infer(n: Node, inputs: list[Schema]) -> Schema:
    """Columns a node produces from the columns it reads. Raises NodeError, never a stack trace."""
    check_arity(n, len(inputs))
    return n.infer_schema(inputs)


def connect() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory duckdb that cannot touch files or the network.

    Every relation of one run must come from the same connection.
    """
    con = duckdb.connect()
    con.execute("SET enable_external_access=false")
    return con
