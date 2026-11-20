"""Checks on the tables of a run: rules each table must keep, and totals that must agree.

A check is data in the spec (`checks`), named like a source. It reads a table made by a source
or step and counts the rows that break the rule; a check with 0 such rows passes. A failed
check of severity `error` fails the run before any file is written; `warn` is only reported.

    not_null         no NULL in the columns
    unique           no two rows share the same values in the columns (NULL equals NULL here)
    range            numbers or dates within min / max (NULL is skipped: use not_null for that)
    accepted_values  values are among a list (whole numbers compared exactly, other
                     numbers as DOUBLE, everything else as text); NULL is skipped
    regex            text values contain a match of the pattern (RE2 syntax: no lookaround,
                     no backreferences; anchor with ^ and $ to check the whole value;
                     NULL does not match: pair with not_null)
    row_count        the table has between min and max rows (give one or both)
    duplicate_rows   no two rows are the same in every column
    foreign_key      values of a column all exist in another table (NULL is skipped)
    reconcile        an aggregate of one table equals that of another table, or a number,
                     optionally per group; tolerance absolute and/or relative

ponytail: `reconcile` compares aggregates as DOUBLE (about 15 significant digits), so give
an `abs_tol` for money or large totals; NaN and infinity on either side always fail. It handles
one aggregate per check. Upgrade: exact decimal aggregates, a list of aggregates.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from excel_etl.nodes.base import (
    DECIMAL_TYPE,
    INTEGER_TYPE,
    NUMERIC_TYPE,
    NodeError,
    Schema,
    quote,
)
from excel_etl.nodes.sqlexpr import bind, check_expression

_ORDERED = re.compile(r"^(DATE|TIME|TIMESTAMP.*)$")
_TEMPORAL = re.compile(r"^(DATE|TIMESTAMP.*)$")
_EXAMPLES = 5
_HUGEINT_MAX = 2**127 - 1

type Scalar = str | int | float


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Base(_Model):
    severity: Literal["error", "warn"] = "error"


class _OnTable(_Base):
    table: str = Field(min_length=1)


class NotNull(_OnTable):
    kind: Literal["not_null"]
    columns: list[str] = Field(min_length=1)


class Unique(_OnTable):
    kind: Literal["unique"]
    columns: list[str] = Field(min_length=1)


class Range(_OnTable):
    kind: Literal["range"]
    column: str = Field(min_length=1)
    min: Scalar | None = None
    max: Scalar | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_boolean_bound(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(isinstance(data.get(k), bool) for k in ("min", "max")):
            raise ValueError("min and max are numbers or dates, not true/false")
        return data

    @model_validator(mode="after")
    def _some_bound(self) -> Range:
        if self.min is None and self.max is None:
            raise ValueError("give min, max or both")
        try:
            above = self.min is not None and self.max is not None and self.min > self.max
        except TypeError:
            above = False
        if above:
            raise ValueError(f"min {self.min} is above max {self.max}; no row could pass")
        return self


class AcceptedValues(_OnTable):
    kind: Literal["accepted_values"]
    column: str = Field(min_length=1)
    values: list[str | int | float | bool] = Field(min_length=1)


class Regex(_OnTable):
    kind: Literal["regex"]
    column: str = Field(min_length=1)
    pattern: str = Field(min_length=1)

    @model_validator(mode="after")
    def _compiles(self) -> Regex:
        try:
            with duckdb.connect() as con:
                con.execute("SELECT regexp_matches('', ?)", [self.pattern]).fetchall()
        except duckdb.Error as e:
            first = str(e).splitlines()[0]
            raise ValueError(
                f"pattern {self.pattern!r} is not valid RE2 (no lookaround, no backreferences): "
                f"{first}"
            ) from e
        return self


class RowCount(_OnTable):
    kind: Literal["row_count"]
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _some_bound(self) -> RowCount:
        if self.min is None and self.max is None:
            raise ValueError("give min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min {self.min} is above max {self.max}; no count could pass")
        return self


class DuplicateRows(_OnTable):
    kind: Literal["duplicate_rows"]


class ForeignKey(_OnTable):
    kind: Literal["foreign_key"]
    column: str = Field(min_length=1)
    ref_table: str = Field(min_length=1)
    ref_column: str = Field(min_length=1)


class Side(_Model):
    """One side of a reconcile: an aggregate of a column, over all rows or the rows that pass
    `where` (a scalar SQL expression such as `"Item" = 'Total'`; this is how the total row of
    a sheet is compared with the sum of the rows above it)."""

    table: str = Field(min_length=1)
    agg: Literal["sum", "count", "avg", "min", "max"] = "sum"
    column: str | None = None
    where: str | None = None

    @model_validator(mode="after")
    def _where_is_safe(self) -> Side:
        if self.where is not None:
            try:
                self.where = check_expression(self.where)
            except NodeError as e:
                raise ValueError(str(e)) from None
        return self


class Reconcile(_Base):
    kind: Literal["reconcile"]
    left: Side
    right: Side | None = None
    value: float | None = Field(default=None, allow_inf_nan=False)
    by: list[str] = []
    abs_tol: float = Field(default=0, ge=0, allow_inf_nan=False)
    rel_tol: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _shape(self) -> Reconcile:
        if (self.right is None) == (self.value is None):
            raise ValueError("give either right (another table) or value (a number)")
        if self.value is not None and self.by:
            raise ValueError("by compares two tables; it cannot be used with value")
        for side in (self.left, self.right):
            if side is not None and side.agg != "count" and not side.column:
                raise ValueError(f"{side.agg} of {side.table!r} needs a column")
        return self


type Check = Annotated[
    NotNull
    | Unique
    | Range
    | AcceptedValues
    | Regex
    | RowCount
    | DuplicateRows
    | ForeignKey
    | Reconcile,
    Field(discriminator="kind"),
]


def tables_of(c: Check) -> list[str]:
    """Every table a check reads."""
    if isinstance(c, Reconcile):
        return [c.left.table, *([c.right.table] if c.right else [])]
    if isinstance(c, ForeignKey):
        return [c.table, c.ref_table]
    return [c.table]  # type: ignore[union-attr]


class CheckResult(_Model):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    severity: Literal["error", "warn"]
    passed: bool
    bad_rows: int
    message: str


class _Bad(Exception):
    """A check that cannot be planned (unknown table or column, wrong type)."""


@dataclass
class _Plan:
    sql: str
    params: list[Any]
    judge: Callable[[list[tuple]], tuple[int, str]]


def _cols(schemas: dict[str, Schema], table: str, names: list[str]) -> list[str]:
    schema = schemas.get(table)
    if schema is None:
        known = ", ".join(sorted(schemas)) or "none"
        raise _Bad(f"table {table!r} is not known (tables: {known})")
    by_key = {c.name.casefold(): c.name for c in schema}
    out = []
    for n in names:
        if n.casefold() not in by_key:
            have = ", ".join(c.name for c in schema[:30])
            raise _Bad(f"{table!r} has no column {n!r} (columns: {have})")
        out.append(by_key[n.casefold()])
    return out


def _type(schemas: dict[str, Schema], table: str, column: str) -> str:
    return next(c.type for c in schemas[table] if c.name == column)


def _list(items: list[str], limit: int = 8) -> str:
    return ", ".join(items[:limit]) + (f", ... ({len(items)})" if len(items) > limit else "")


def _plan_not_null(c: NotNull, schemas: dict[str, Schema]) -> _Plan:
    cols = _cols(schemas, c.table, c.columns)
    parts = [f"count(*) FILTER (WHERE {quote(n)} IS NULL)" for n in cols]
    any_null = " OR ".join(f"{quote(n)} IS NULL" for n in cols)
    sql = f"SELECT count(*) FILTER (WHERE {any_null}), {', '.join(parts)} FROM {quote(c.table)}"

    def judge(rows: list[tuple]) -> tuple[int, str]:
        total, *per = rows[0]
        shown = [f"{n}: {k}" for n, k in zip(cols, per, strict=True) if k]
        return total, f"{total} row(s) with NULL ({_list(shown)})"

    return _Plan(sql, [], judge)


def _plan_unique(c: Unique, schemas: dict[str, Schema]) -> _Plan:
    cols = _cols(schemas, c.table, c.columns)
    keys = ", ".join(quote(n) for n in cols)
    sql = (
        f"SELECT {keys}, count(*) FROM {quote(c.table)} GROUP BY {keys} HAVING count(*) > 1 "
        f"ORDER BY count(*) DESC"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        shown = [f"{r[:-1]} x{r[-1]}" for r in rows[:_EXAMPLES]]
        return len(rows), f"{len(rows)} repeated key(s) in ({_list(cols)}): {_list(shown)}"

    return _Plan(sql, [], judge)


def _exact_bound(kind: str, bound: Scalar) -> tuple[str, str] | None:
    """The SQL type and text to compare a whole-number or decimal column with `bound` without
    rounding through DOUBLE, or None when DOUBLE is the only sensible way."""
    try:
        number = Decimal(str(bound).strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if kind == "FLOAT":
        return "FLOAT", str(number)
    if INTEGER_TYPE.match(kind) and kind != "UHUGEINT":
        if number != number.to_integral_value() or abs(int(number)) > _HUGEINT_MAX:
            return None
        return "HUGEINT", str(int(number))
    if m := DECIMAL_TYPE.match(kind):
        width, scale = int(m[1]), int(m[2])
        places = max(0, -int(number.as_tuple().exponent))
        if places <= scale and number.adjusted() + 1 <= width - scale:
            return f"DECIMAL({width},{scale})", format(number, "f")
    return None


def _check_bound(bound: Scalar, kind: str, col: str) -> None:
    """A bound that cannot be read as the column's kind is a spec error, not a run failure."""
    if NUMERIC_TYPE.match(kind):
        try:
            ok = math.isfinite(float(bound))
        except ValueError:
            ok = False
        if not ok:
            raise _Bad(f"range bound {bound!r} is not a finite number; {col!r} is {kind}")
        return
    parse = time if kind == "TIME" else date if kind == "DATE" else datetime
    try:
        if isinstance(bound, bool | int | float):
            raise ValueError
        parse.fromisoformat(bound)
    except ValueError:
        shape = {"TIME": "10:30:00", "DATE": "2026-01-31"}.get(kind, "2026-01-31 10:30:00")
        raise _Bad(
            f"range bound {bound!r} is not a {kind} such as '{shape}'; column {col!r}"
        ) from None


def _plan_range(c: Range, schemas: dict[str, Schema]) -> _Plan:
    (col,) = _cols(schemas, c.table, [c.column])
    kind = _type(schemas, c.table, col)
    if not (NUMERIC_TYPE.match(kind) or _ORDERED.match(kind)):
        raise _Bad(f"range needs a number or date column; {col!r} is {kind}")
    q, bad, params, bounds = quote(col), [], [], []
    for sign, bound in (("<", c.min), (">", c.max)):
        if bound is not None:
            _check_bound(bound, kind, col)
            exact = _exact_bound(kind, bound) if NUMERIC_TYPE.match(kind) else None
            if exact is not None:
                sql_type, text = exact
                side = f"CAST({q} AS HUGEINT)" if sql_type == "HUGEINT" else q
                bad.append(f"{side} {sign} CAST(? AS {sql_type})")
                params.append(text)
            else:
                bad.append(
                    f"{q} {sign} CAST(? AS {'DOUBLE' if NUMERIC_TYPE.match(kind) else kind})"
                )
                params.append(str(bound))
            bounds.append(f"{'min' if sign == '<' else 'max'} {bound}")
    if kind in ("FLOAT", "DOUBLE"):
        bad.append(f"isnan({q})")
    cond = " OR ".join(bad)
    sql = (
        f"SELECT count(*) FILTER (WHERE {cond}), min({q}) FILTER (WHERE {cond}), "
        f"max({q}) FILTER (WHERE {cond}) FROM {quote(c.table)}"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, lo, hi = rows[0]
        return n, f"{n} row(s) of {col!r} outside {', '.join(bounds)}; lowest {lo}, highest {hi}"

    return _Plan(sql, params * 3, judge)


def _is_exact_kind(kind: str) -> bool:
    """Columns whose values are compared without DOUBLE: whole numbers, decimals, FLOAT."""
    return bool(
        (INTEGER_TYPE.match(kind) and kind != "UHUGEINT")
        or DECIMAL_TYPE.match(kind)
        or kind == "FLOAT"
    )


def _plan_accepted(c: AcceptedValues, schemas: dict[str, Schema]) -> _Plan:
    (col,) = _cols(schemas, c.table, [c.column])
    q = quote(col)
    kind = _type(schemas, c.table, col)
    marks = ", ".join("?" * len(c.values))
    if NUMERIC_TYPE.match(kind):
        bad = [v for v in c.values if isinstance(v, bool) or not _is_number(v)]
        if bad:
            raise _Bad(f"{col!r} is {kind}; accepted_values must be numbers, got {bad[0]!r}")
        if _is_exact_kind(kind):
            pairs = [e for v in c.values if (e := _exact_bound(kind, v)) is not None]
            want: list[Any] = [text for _, text in pairs]
            sql_type = pairs[0][0] if pairs else ""
            col = f"CAST({q} AS HUGEINT)" if sql_type == "HUGEINT" else q
            marks = ", ".join(f"CAST(? AS {sql_type})" for _ in want)
            cond = f"{col} NOT IN ({marks})" if want else f"{q} IS NOT NULL"
        else:
            want = [float(v) for v in c.values]
            cond = f"CAST({q} AS DOUBLE) NOT IN ({marks})"
    else:
        want = [str(v).lower() if isinstance(v, bool) else str(v) for v in c.values]
        cond = f"CAST({q} AS VARCHAR) NOT IN ({marks})"
    sql = (
        f"SELECT count(*) FILTER (WHERE {cond}), "
        f"list(DISTINCT CAST({q} AS VARCHAR)) FILTER (WHERE {cond}) FROM {quote(c.table)}"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, seen = rows[0]
        shown = sorted(seen or [])[:_EXAMPLES]
        return n, f"{n} row(s) of {col!r} not among the accepted values; seen: {_list(shown)}"

    return _Plan(sql, want * 2, judge)


def _is_number(v: object) -> bool:
    try:
        return math.isfinite(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _plan_regex(c: Regex, schemas: dict[str, Schema]) -> _Plan:
    (col,) = _cols(schemas, c.table, [c.column])
    kind = _type(schemas, c.table, col)
    if kind not in ("VARCHAR", "NULL"):
        raise _Bad(f"regex needs a text column; {col!r} is {kind}")
    q = quote(col)
    bad = f"{q} IS NULL OR NOT regexp_matches(CAST({q} AS VARCHAR), ?)"
    sql = (
        f"SELECT count(*) FILTER (WHERE {bad}), "
        f"list_sort(list(DISTINCT {q}) FILTER (WHERE {bad}))[1:{_EXAMPLES}] "
        f"FROM {quote(c.table)}"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, seen = rows[0]
        shown = [str(v) for v in (seen or [])]
        return n, f"{n} value(s) of {col!r} do not match /{c.pattern}/: {_list(shown)}"

    return _Plan(sql, [c.pattern, c.pattern], judge)


def _plan_row_count(c: RowCount, schemas: dict[str, Schema]) -> _Plan:
    sql = f"SELECT count(*) FROM {quote(c.table)}"
    if c.min is not None and c.max is not None:
        bounds = f"between {c.min} and {c.max}"
    elif c.min is not None:
        bounds = f"at least {c.min}"
    else:
        bounds = f"at most {c.max}"

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n = rows[0][0]
        short = max(0, (c.min or 0) - n)
        over = max(0, n - (c.max if c.max is not None else n))
        bad = short + over
        side = "short" if short else "over"
        return bad, f"table has {n} row(s), expected {bounds}: {side} by {bad}"

    return _Plan(sql, [], judge)


def _plan_duplicate_rows(c: DuplicateRows, schemas: dict[str, Schema]) -> _Plan:
    sql = (
        f"SELECT coalesce(sum(k - 1), 0), count(*) FROM "
        f"(SELECT count(*) AS k FROM (SELECT t AS row FROM {quote(c.table)} t) "
        f"GROUP BY row HAVING count(*) > 1)"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        bad, groups = rows[0]
        return bad, f"{bad} duplicate row(s) in {c.table!r} ({groups} group(s) of them)"

    return _Plan(sql, [], judge)


def _plan_foreign_key(c: ForeignKey, schemas: dict[str, Schema]) -> _Plan:
    (col,) = _cols(schemas, c.table, [c.column])
    (ref,) = _cols(schemas, c.ref_table, [c.ref_column])
    kind, ref_kind = _type(schemas, c.table, col), _type(schemas, c.ref_table, ref)
    both_numeric = NUMERIC_TYPE.match(kind) and NUMERIC_TYPE.match(ref_kind)
    both_temporal = _TEMPORAL.match(kind) and _TEMPORAL.match(ref_kind)
    if "NULL" not in (kind, ref_kind) and kind != ref_kind and not (both_numeric or both_temporal):
        raise _Bad(f"{c.table}.{col} is {kind} but {c.ref_table}.{ref} is {ref_kind}")
    q, r = quote(col), quote(ref)
    sql = (
        f"SELECT count(*), list_sort(list(DISTINCT t.{q}))[1:{_EXAMPLES}] "
        f"FROM {quote(c.table)} t "
        f"LEFT JOIN {quote(c.ref_table)} p ON t.{q} = p.{r} "
        f"WHERE t.{q} IS NOT NULL AND p.{r} IS NULL"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, seen = rows[0]
        shown = [str(v) for v in (seen or [])]
        where = f"{c.table}.{col} missing from {c.ref_table}.{ref}"
        return n, f"{n} row(s) with a value of {where}: {_list(shown)}"

    return _Plan(sql, [], judge)


def _side_sql(schemas: dict[str, Schema], side: Side, by: list[str]) -> tuple[str, list[str], str]:
    """`SELECT k0.., v FROM table GROUP BY ..` for one side; the group columns as written."""
    keys = _cols(schemas, side.table, by)
    if side.agg == "count" and not side.column:
        agg = "count(*)"
    else:
        (col,) = _cols(schemas, side.table, [side.column or ""])
        kind = _type(schemas, side.table, col)
        if side.agg != "count" and not NUMERIC_TYPE.match(kind):
            raise _Bad(f"{side.agg} needs a number column; {side.table}.{col} is {kind}")
        agg = f"{side.agg}({quote(col)})"
        if side.agg == "sum":
            agg = f"coalesce({agg}, 0)"
    label = f"{side.agg}({side.column or '*'}) of {side.table}"
    selects = [f"{quote(k)} AS k{i}" for i, k in enumerate(keys)]
    group = f" GROUP BY {', '.join(quote(k) for k in keys)}" if keys else ""
    cols = ", ".join([*selects, f"CAST({agg} AS DOUBLE) AS v"])
    if side.where is not None:
        try:
            (kind,) = bind(schemas[side.table], f"({side.where}) AS __e", "reconcile")
        except NodeError as e:
            raise _Bad(f"where of {side.table!r}: {e}") from None
        if kind.type not in ("BOOLEAN", "NULL"):
            raise _Bad(f"where {side.where!r} gives {kind.type}, not true/false")
        label += f" where {side.where}"
        where = f" WHERE ({side.where})"
    else:
        where = ""
    sql = f"SELECT {cols} FROM {quote(side.table)}{where}{group}"
    return sql, keys, label


def _plan_reconcile(c: Reconcile, schemas: dict[str, Schema]) -> _Plan:
    lsql, _, llabel = _side_sql(schemas, c.left, c.by)
    params: list[Any] = []
    n = len(c.by)
    if c.right is not None:
        rsql, _, rlabel = _side_sql(schemas, c.right, c.by)
        on = " AND ".join(f"l.k{i} IS NOT DISTINCT FROM r.k{i}" for i in range(n)) or "true"
        ks = "".join(f"coalesce(l.k{i}, r.k{i}) AS k{i}, " for i in range(n))
        zero = {"sum", "count"}
        lv = "coalesce(l.v, 0)" if c.left.agg in zero else "l.v"
        rv = "coalesce(r.v, 0)" if c.right.agg in zero else "r.v"
        body = f"({lsql}) l FULL OUTER JOIN ({rsql}) r ON {on}"
    else:
        rlabel, ks, lv, rv = f"{c.value}", "", "l.v", "CAST(? AS DOUBLE)"
        body = f"({lsql}) l"
        params.append(c.value)
    sql = f"SELECT {ks}{lv} AS lv, {rv} AS rv FROM {body}"
    bad = (
        "(lv IS NULL) <> (rv IS NULL) "
        "OR (lv IS NOT NULL AND NOT isfinite(lv)) OR (rv IS NOT NULL AND NOT isfinite(rv)) "
        "OR abs(lv - rv) > greatest(?, ? * greatest(abs(lv), abs(rv)))"
    )
    sql = f"SELECT * FROM ({sql}) WHERE {bad} ORDER BY 1"
    params += [c.abs_tol, c.rel_tol]
    tol = f"tolerance abs {c.abs_tol:g}, rel {c.rel_tol:g}"

    def judge(rows: list[tuple]) -> tuple[int, str]:
        if not rows:
            return 0, ""
        if not c.by:
            lv_, rv_ = rows[0][-2:]
            return 1, f"{llabel} = {lv_} but {rlabel} = {rv_} ({tol})"
        shown = [f"{r[:-2]}: {r[-2]} vs {r[-1]}" for r in rows[:_EXAMPLES]]
        head = f"{len(rows)} group(s) by ({_list(c.by)}) differ, {llabel} vs {rlabel} ({tol})"
        return len(rows), f"{head}: {_list(shown, _EXAMPLES)}"

    return _Plan(sql, params, judge)


_PLANS: dict[str, Callable[[Any, dict[str, Schema]], _Plan]] = {
    "not_null": _plan_not_null,
    "unique": _plan_unique,
    "range": _plan_range,
    "accepted_values": _plan_accepted,
    "regex": _plan_regex,
    "row_count": _plan_row_count,
    "duplicate_rows": _plan_duplicate_rows,
    "foreign_key": _plan_foreign_key,
    "reconcile": _plan_reconcile,
}


def check_schema(checks: dict[str, Check], schemas: dict[str, Schema]) -> dict[str, str]:
    """Problems found without running anything: name -> message. A check whose table is
    missing from `schemas` because an earlier step failed is skipped, not reported again.
    """
    out: dict[str, str] = {}
    for name, c in checks.items():
        if any(t not in schemas for t in tables_of(c)):
            continue
        try:
            _PLANS[c.kind](c, schemas)
        except _Bad as e:
            out[name] = str(e)
    return out


def run_checks(
    checks: dict[str, Check], con: duckdb.DuckDBPyConnection, schemas: dict[str, Schema]
) -> tuple[list[CheckResult], dict[str, str]]:
    """Run every check. Returns the results and the checks that could not run (name -> why)."""
    results: list[CheckResult] = []
    broken: dict[str, str] = {}
    for name, c in checks.items():
        if any(t not in schemas for t in tables_of(c)):
            continue
        try:
            plan = _PLANS[c.kind](c, schemas)
            rows = con.execute(plan.sql, plan.params).fetchall()
            bad, message = plan.judge(rows)
        except _Bad as e:
            broken[name] = str(e)
            continue
        except duckdb.Error as e:
            broken[name] = f"could not run: {str(e).splitlines()[0]}"
            continue
        results.append(
            CheckResult(
                name=name,
                kind=c.kind,
                severity=c.severity,
                passed=bad == 0,
                bad_rows=bad,
                message="" if bad == 0 else message,
            )
        )
    return results, broken
