"""What a node is, and the registry that finds one by the name used in a spec.

A node has a pydantic `Config`, `infer_schema` (columns out, from columns in, no data read)
and `run` (a duckdb relation out, from relations in). `infer_schema` is what lets a spec be
checked before any file is opened, so it must agree with what `run` produces.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

type Relation = duckdb.DuckDBPyRelation

_MAX_ERRORS = 10
# the pattern of a spec name (pipeline.py); describe() recognises it to explain it in words
_NAME_PATTERN = r"^(?:[A-Za-z]|_[A-Za-z0-9])[A-Za-z0-9_]{0,62}$"
INTEGER_TYPE = re.compile(r"^U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?$")  # duckdb whole-number types
NUMERIC_TYPE = re.compile(r"^(U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?|FLOAT|DOUBLE|DECIMAL\(.*\))$")


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


def _say(item: Any) -> str:
    """One pydantic error as a sentence an agent can act on."""
    kind, msg, pattern = item["type"], item["msg"], (item.get("ctx") or {}).get("pattern")
    if kind == "string_pattern_mismatch" and pattern == _NAME_PATTERN:
        return (
            "a name is 1-64 characters: letters, digits and _ only, starting with a letter "
            "(no spaces, dashes or dots), such as 'sales_2026'"
        )
    if kind == "list_type":
        return 'must be a list, such as ["a"] (even for one item)'
    if kind == "string_type":
        return "must be one piece of text (a single name), not a list or a number"
    if kind == "extra_forbidden":
        return "is not a setting here (check the spelling; node_catalog lists the valid ones)"
    return msg.removeprefix("Value error, ")


def describe(e: ValidationError, root: str = "config") -> str:
    """Pydantic errors as `path.to.field: what to do` lines, at most 10."""
    items = e.errors(include_url=False, include_input=False, include_context=False)
    ctx = e.errors(include_url=False, include_input=False, include_context=True)
    for plain, rich in zip(items, ctx, strict=True):
        plain["ctx"] = rich.get("ctx")
    lines = [f"{'.'.join(str(x) for x in i['loc']) or root}: {_say(i)}" for i in items]
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
    # (config, what it does) pairs shown by `catalog`. tests/test_node_examples.py runs each
    # one, so an example that stops working fails a test instead of misleading an agent.
    examples: ClassVar[tuple[tuple[dict[str, Any], str], ...]] = ()

    def __init__(self, config: Any) -> None:
        self.config = config

    def why_rows_change(self) -> str:
        """In a few words, why this node may return fewer (or more) rows than it read, for
        the run report. Empty when it never changes the number of rows."""
        return ""

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


def _fields(cls: type[Node]) -> str:
    """`name*: type = default` per config field of a node; `*` marks a required one."""
    schema = cls.Config.model_json_schema()
    defs, need = schema.get("$defs", {}), set(schema.get("required", []))

    def kind(p: dict[str, Any]) -> str:
        if "$ref" in p:
            ref = defs[p["$ref"].rsplit("/", 1)[-1]]
            if "enum" in ref:  # a named choice list is shown as its choices
                return kind(ref)
            inner = ", ".join(
                f"{k}{'*' if k in ref.get('required', []) else ''}"
                for k in ref.get("properties", {})
            )
            return "{" + inner + "}"
        if "enum" in p:
            return "|".join(map(str, p["enum"]))
        if "anyOf" in p:
            return " or ".join(kind(x) for x in p["anyOf"] if x.get("type") != "null")
        if p.get("type") == "array":
            if "prefixItems" in p:  # a fixed-size list, such as a pair
                return "[" + ", ".join(kind(x) for x in p["prefixItems"]) + "]"
            return f"[{kind(p.get('items', {})) or 'any'}]"
        if p.get("type") == "object":
            return "object"
        return str(p.get("type", "any"))

    out = []
    for key, p in schema.get("properties", {}).items():
        default = "" if key in need or "default" not in p else f" = {p['default']!r}"
        out.append(f"{key}{'*' if key in need else ''}: {kind(p)}{default}")
    return "; ".join(out)


def catalog(name: str | None = None) -> str:
    """What an agent needs to write a step: each node's purpose and config fields.

    Without `name`, one line per node with its config fields (`*` = required), enough to
    write most steps without asking again. With a name (or several, comma separated), the
    full docstring, the fields and examples. An unknown name raises NodeError listing the
    known ones.
    """
    if name is None:
        rows = []
        for n in names():
            first = (_REGISTRY[n].__doc__ or "").strip().split("\n\n")[0]
            about = " ".join(first.split())[:110] or "(no description)"
            rows.append(f"{n}: {about} | {_field_names(_REGISTRY[n])}")
        return "\n".join(rows)
    wanted = [w.strip() for w in name.split(",") if w.strip()]
    if not wanted:
        return catalog()
    return "\n\n".join(_describe(w) for w in wanted)


def _field_names(cls: type[Node]) -> str:
    """Config field names only, `*` on the required ones: the short form of `_fields`."""
    schema = cls.Config.model_json_schema()
    need = set(schema.get("required", []))
    return ", ".join(f"{k}{'*' if k in need else ''}" for k in schema.get("properties", {})) or "-"


def _describe(name: str) -> str:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise NodeError(f"unknown node {name!r}; nodes: {', '.join(names())}")
    lo, hi = cls.min_inputs, cls.max_inputs
    n_in = f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}"
    doc = "\n".join(line.strip() for line in (cls.__doc__ or "").strip().splitlines())
    shown = "".join(
        f"\n  {json.dumps(cfg, ensure_ascii=False)}  # {note}" for cfg, note in cls.examples
    )
    return f"{name} (inputs: {n_in})\n{doc}\nconfig: {_fields(cls)}" + (
        f"\nexamples:{shown}" if shown else ""
    )


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
        raise NodeError(
            f"node {name!r} config:\n{describe(e)}\nfields (* required): {_fields(cls)}"
        ) from None


def check_arity(n: Node, count: int) -> None:
    lo, hi = n.min_inputs, n.max_inputs
    if count < lo or (hi is not None and count > hi):
        want = f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}"
        raise NodeError(f"node {n.name!r} takes {want} input(s), got {count}")


def infer(n: Node, inputs: list[Schema]) -> Schema:
    """Columns a node produces from the columns it reads. Raises NodeError, never a stack trace."""
    check_arity(n, len(inputs))
    out = n.infer_schema(inputs)
    # duckdb treats `Item` and `ITEM` as one name and renames the second to `ITEM_1`, so the
    # run would not give what was promised. Checked once here instead of in every node.
    seen: dict[str, str] = {}
    for c in out:
        if (other := seen.setdefault(c.name.casefold(), c.name)) != c.name:
            raise NodeError(
                f"{n.name}: columns {other!r} and {c.name!r} differ only in letter case, "
                "which duckdb treats as one name; use the same spelling or another name"
            )
    return out


def connect(write_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory duckdb that cannot touch files or the network.

    `write_dir` is the one directory it may read and write in (outputs of a run); the
    permission is set before access is locked, after which it cannot be widened (measured:
    `..` paths, siblings and a later SET are refused). Every relation of one run must come
    from the same connection.
    """
    con = duckdb.connect()
    if write_dir is not None:
        path = write_dir.resolve().as_posix().replace("'", "''")
        con.execute(f"SET allowed_directories=['{path}']")
    con.execute("SET enable_external_access=false")
    return con
