"""General-purpose nodes (select, rename, filter, ...). Each is a few lines over duckdb SQL."""

from __future__ import annotations

from typing import Literal

import duckdb
from pydantic import Field, model_validator

from excel_etl.nodes.base import (
    INTEGER_TYPE,
    NUMERIC_TYPE,
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
from excel_etl.nodes.relops import apply, check_reserved, shape
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
    keys = [n.casefold() for n in names]  # duckdb names ignore letter case
    if dup := next((n for n, k in zip(names, keys, strict=True) if keys.count(k) > 1), None):
        raise NodeError(f"{who}: {what} {dup!r} is listed twice (letter case is ignored)")


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
    expression is NULL is dropped, as in SQL. Text, number and date functions of duckdb work
    (see the examples). Not allowed: subqueries, aggregates (use `group_by`) and window
    functions (use `window`).
    """

    examples = (
        ({"where": "qty > 0 AND price IS NOT NULL"}, "numbers; a NULL comparison drops the row"),
        ({"where": "code LIKE 'A-%'"}, "text starts with (ILIKE ignores letter case)"),
        ({"where": "code IN ('A-12', 'B-7')"}, "one of several values"),
        ({"where": "regexp_matches(text, '^PT ')"}, "text matches a regular expression"),
        (
            {"where": "day >= DATE '2026-01-01' AND day < DATE '2027-01-01'"},
            "a date range (the end day itself is left out)",
        ),
        ({"where": "year(day) = 2026 AND month(day) IN (1, 2, 3)"}, "by part of a date"),
    )

    class Config(NodeConfig):
        where: str

        @model_validator(mode="after")
        def _safe(self) -> Filter.Config:
            self.where = check_expression(self.where)
            return self

    def why_rows_change(self) -> str:
        return f"kept rows where {self.config.where}"

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
    Text, number and date functions of duckdb work (see the examples). Division by 0 gives
    `inf`, so divide by `nullif(x, 0)`. Not allowed: subqueries, aggregates (use `group_by`)
    and window functions such as running totals or ranks (use `window`).
    """

    examples = (
        ({"name": "revenue", "expr": "coalesce(qty, 0) * price"}, "NULL counted as 0"),
        (
            {"name": "rate", "expr": "round(qty / nullif(price, 0), 2)"},
            "nullif turns a 0 divisor into NULL, not inf",
        ),
        ({"name": "area_code", "expr": "split_part(code, '-', 1)"}, "text before the first '-'"),
        ({"name": "clean_code", "expr": "upper(trim(code))"}, "trim spaces, capital letters"),
        (
            {"name": "number", "expr": r"try_cast(regexp_extract(note, '(\d+)', 1) AS INTEGER)"},
            "first number inside a text; NULL when there is none",
        ),
        (
            {"name": "order_day", "expr": "try_strptime(date_text, '%d/%m/%Y')", "type": "DATE"},
            "text in another date format to a date (strptime fails on a bad text, try_ gives NULL)",
        ),
        (
            {"name": "month", "expr": "date_trunc('month', day)", "type": "DATE"},
            "first day of month",
        ),
        ({"name": "month_text", "expr": "strftime(day, '%Y-%m')"}, "date as text, such as 2026-03"),
        (
            {"name": "age_days", "expr": "date_diff('day', day, DATE '2026-12-31')"},
            "days between two dates",
        ),
        (
            {
                "name": "size",
                "expr": "CASE WHEN qty >= 100 THEN 'big' WHEN qty > 0 THEN 'small' ELSE 'none' END",
            },
            "pick a label by condition",
        ),
    )

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


def _order_keys(by: list[str], schema: Schema, who: str) -> list[str]:
    """`ORDER BY` terms for names like `["Date", "-Ton"]`; a leading `-` is descending."""
    have = {c.name for c in schema}
    keys = []
    for item in by:
        # a column that really starts with `-` wins over the descending marker
        desc = item.startswith("-") and item not in have
        col = item[1:] if desc else item
        _missing([col], schema, who)
        keys.append(f"{quote(col)} {'DESC' if desc else 'ASC'} NULLS LAST")
    return keys


@node("sort")
class Sort(Node):
    """Order rows: `{"by": ["Date", "-Ton"]}`; a leading `-` is descending. NULLs go last.

    Ties keep no promised order, so list enough columns to make the order what you mean.
    """

    class Config(NodeConfig):
        by: list[str] = Field(min_length=1)

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        _order_keys(self.config.by, schema, self.name)
        return schema

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        return rel.order(", ".join(_order_keys(self.config.by, schema_of(rel), self.name)))


@node("dedupe")
class Dedupe(Node):
    """Drop repeated rows. `keys` = the columns that identify a row (default: all columns).

    Of rows with the same keys the first in the table's current order is kept, and the rest
    keep their order too; run `sort` first if "first" must mean something. Empty (NULL) keys
    count as equal to each other.
    """

    class Config(NodeConfig):
        keys: list[str] | None = Field(default=None, min_length=1)

    def why_rows_change(self) -> str:
        keys = ", ".join(self.config.keys) if self.config.keys else "all columns"
        return f"repeated rows dropped (same {keys})"

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
        cols = ", ".join(quote(c.name) for c in schema)
        # no keys = the whole row is the key; rel.distinct() would return survivors in hash order
        part = ", ".join(quote(k) for k in self.config.keys) if self.config.keys else cols
        # row_number() over () numbers rows in table order. Without it the "first" row of a key
        # is whichever a parallel scan reaches first (measured on 200k rows: not the first).
        return rel.query(
            "__in",
            f"SELECT {cols} FROM (SELECT *, row_number() OVER (PARTITION BY {part} "
            f"ORDER BY __pos) AS __rn FROM (SELECT *, row_number() OVER () AS __pos "
            f"FROM __in)) WHERE __rn = 1 ORDER BY __pos",
        )


@node("window")
class Window(Node):
    """Add a column computed from the other rows of its group. Every row and the order stay.

    Running total, rank, previous value, share of the group total. `name` is the new column
    (an existing one of that name is replaced in place). `fn` and what it needs:
    sum, avg, min, max, count of `column`: running value down the rows in `order_by` order;
    with no `order_by` the value of the whole group instead (a group total on every row).
    row_number: 1, 2, 3... in `order_by` order (ties in table order), no column.
    rank, dense_rank: like row_number but equal rows share a number; `order_by` required.
    lag, lead: `column` of the row `offset` (default 1) before or after, NULL at the edge.
    first_value, last_value: first or last non-empty `column` of the group in `order_by` order.
    share: `column` divided by the sum of `column` in the group (DOUBLE; NULL when the sum is 0).
    `partition_by` = the group columns (empty: the whole table). `order_by` = columns, a leading
    `-` is descending, NULLs last. Empty values are skipped by sum, avg, min, max, count.
    For a change from the previous month: `lag` first, then `derive` the difference.
    """

    examples = (
        (
            {
                "name": "running_qty",
                "fn": "sum",
                "column": "qty",
                "partition_by": ["product"],
                "order_by": ["day"],
            },
            "running total per product, by date",
        ),
        (
            {"name": "product_total", "fn": "sum", "column": "qty", "partition_by": ["product"]},
            "no order_by: the group total on every row",
        ),
        (
            {"name": "rank", "fn": "rank", "partition_by": ["area"], "order_by": ["-qty"]},
            "biggest qty is 1 within each area",
        ),
        (
            {"name": "previous_qty", "fn": "lag", "column": "qty", "partition_by": ["product"],
             "order_by": ["day"]},
            "value of the row before (offset 1); derive the difference after",
        ),
        (
            {"name": "share", "fn": "share", "column": "qty", "partition_by": ["area"]},
            "part of the area total, 0 to 1",
        ),
        ({"name": "n", "fn": "row_number", "order_by": ["day"]}, "row number by date"),
    )  # fmt: skip

    class Config(NodeConfig):
        name: str = Field(min_length=1)
        fn: Literal[
            "sum", "avg", "min", "max", "count", "row_number", "rank", "dense_rank", "lag",
            "lead", "first_value", "last_value", "share",
        ]  # fmt: skip
        column: str | None = None
        partition_by: list[str] = []
        order_by: list[str] = []
        offset: int | None = Field(default=None, ge=1, le=10000)

        @model_validator(mode="after")
        def _rules(self) -> Window.Config:
            fn = self.fn
            if self.name.startswith("__"):
                raise ValueError("a name starting with '__' is reserved; use another")
            if fn in ("row_number", "rank", "dense_rank"):
                if self.column is not None:
                    raise ValueError(f"{fn} numbers rows and takes no column")
            elif not self.column:
                raise ValueError(f"{fn} needs a column")
            if fn in ("rank", "dense_rank") and not self.order_by:
                raise ValueError(f"{fn} needs order_by (what to rank by)")
            if self.offset is not None and fn not in ("lag", "lead"):
                raise ValueError("offset is only for lag and lead")
            _unique(self.partition_by, "partition column", "window")
            return self

    def _over(self, schema: Schema, order: bool, frame: str = "", ties: bool = True) -> str:
        """`OVER (...)`. `order`: sort by order_by, then by table position unless `ties` is
        false (rank must give equal rows the same number). Else the group as a whole."""
        parts = []
        if self.config.partition_by:
            parts.append("PARTITION BY " + ", ".join(quote(p) for p in self.config.partition_by))
        if order:
            keys = _order_keys(self.config.order_by, schema, self.name)
            parts.append("ORDER BY " + ", ".join([*keys, "__pos"] if ties else keys))
        if frame:
            parts.append(frame)
        return f"OVER ({' '.join(parts)})"

    def _value(self, schema: Schema) -> str:
        cfg, fn = self.config, self.config.fn
        _missing(cfg.partition_by, schema, self.name)
        col = quote(cfg.column) if cfg.column else ""
        kind = ""
        if cfg.column:
            _missing([cfg.column], schema, self.name)
            kind = next(c.type for c in schema if c.name == cfg.column)
        if fn in ("sum", "avg", "share") and not NUMERIC_TYPE.match(kind):
            raise NodeError(
                f"{self.name}: {fn} of {cfg.name!r} needs a number column, but "
                f"{cfg.column!r} is {kind}; cast it first"
            )
        # ROWS, not the default RANGE: the same result here (__pos breaks every tie)
        running = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
        whole = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
        if fn == "row_number":
            return f"row_number() {self._over(schema, True)}"
        if fn in ("rank", "dense_rank"):
            return f"{fn}() {self._over(schema, True, ties=False)}"
        if fn in ("lag", "lead"):
            return f"{fn}({col}, {cfg.offset or 1}) {self._over(schema, True)}"
        if fn in ("first_value", "last_value"):
            return f"{fn}({col} IGNORE NULLS) {self._over(schema, True, whole)}"
        if fn == "share":
            return f"CAST({col} AS DOUBLE) / nullif(sum({col}) {self._over(schema, False)}, 0)"
        over = self._over(schema, bool(cfg.order_by), running if cfg.order_by else "")
        value = f"{fn}({col}) {over}"
        if fn == "sum" and INTEGER_TYPE.match(kind):
            return f"CAST({value} AS BIGINT)"  # a sum of whole numbers would be HUGEINT
        return value

    def _sql(self, view: str, schema: Schema) -> str:
        value = f"{self._value(schema)} AS {quote(self.config.name)}"
        replaced = self.config.name in {c.name for c in schema}
        inner = f"SELECT * REPLACE ({value})" if replaced else f"SELECT *, {value}"
        return (
            f"SELECT * EXCLUDE (__pos) FROM ({inner} FROM "
            f"(SELECT *, row_number() OVER () AS __pos FROM {view})) ORDER BY __pos"
        )

    def infer_schema(self, inputs: list[Schema]) -> Schema:
        (schema,) = inputs
        check_reserved(schema, self.name)
        return shape([schema], lambda n: self._sql(n[0], schema), self.name)

    def run(self, inputs: list[Relation]) -> Relation:
        (rel,) = inputs
        schema = schema_of(rel)
        self.infer_schema([schema])
        return apply([rel], lambda names: self._sql(names[0], schema))
