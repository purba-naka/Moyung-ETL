"""Checks on the tables of a run: rules each table must keep, and totals that must agree.

A check is data in the spec (`checks`), named like a source. It reads a table made by a source
or step and counts the rows that break the rule; a check with 0 such rows passes. A failed
check of severity `error` fails the run before any file is written; `warn` is only reported.

    not_null         no NULL in the columns
    unique           no two rows share the same values in the columns (NULL equals NULL here)
    range            numbers or dates within min / max (NULL is skipped: use not_null for that)
    accepted_values  values (compared as text) are among a list
    reconcile        an aggregate of one table equals that of another table, or a number,
                     optionally per group; tolerance absolute and/or relative

ponytail: `accepted_values` compares text, so a DOUBLE 1.0 does not equal the value 1.
`reconcile` handles one aggregate per check. Upgrade: typed comparison, a list of aggregates.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Annotated, Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from excel_etl.nodes.base import NUMERIC_TYPE, Schema, quote

_ORDERED = re.compile(r"^(DATE|TIME|TIMESTAMP.*)$")
_EXAMPLES = 5

type Scalar = str | int | float


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Base(_Model):
    severity: Literal["error", "warn"] = "error"


class _OnTable(_Base):
    table: str = Field(min_length=1)  # a source or step id


class NotNull(_OnTable):
    kind: Literal["not_null"]
    columns: list[str] = Field(min_length=1)


class Unique(_OnTable):
    kind: Literal["unique"]
    columns: list[str] = Field(min_length=1)


class Range(_OnTable):
    kind: Literal["range"]
    column: str = Field(min_length=1)
    min: Scalar | None = None  # a date as "2026-01-31"
    max: Scalar | None = None

    @model_validator(mode="after")
    def _some_bound(self) -> Range:
        if self.min is None and self.max is None:
            raise ValueError("give min, max or both")
        return self


class AcceptedValues(_OnTable):
    kind: Literal["accepted_values"]
    column: str = Field(min_length=1)
    values: list[str | int | float | bool] = Field(min_length=1)


class Side(_Model):
    table: str = Field(min_length=1)
    agg: Literal["sum", "count", "avg", "min", "max"] = "sum"
    column: str | None = None  # not needed for `count` (counts rows)


class Reconcile(_Base):
    kind: Literal["reconcile"]
    left: Side
    right: Side | None = None
    value: float | None = None  # compare with a number instead of another table
    by: list[str] = []  # compare per group: these columns must exist on both sides
    abs_tol: float = Field(default=0, ge=0)
    rel_tol: float = Field(default=0, ge=0)  # fraction of the larger side, 0.01 = 1%

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
    NotNull | Unique | Range | AcceptedValues | Reconcile, Field(discriminator="kind")
]


def tables_of(c: Check) -> list[str]:
    """Every table a check reads."""
    if isinstance(c, Reconcile):
        return [c.left.table, *([c.right.table] if c.right else [])]
    return [c.table]  # type: ignore[union-attr]


class CheckResult(_Model):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    severity: Literal["error", "warn"]
    passed: bool
    bad_rows: int  # rows that break the rule; for reconcile, groups that differ
    message: str  # why it failed; empty when it passed


class _Bad(Exception):
    """A check that cannot be planned (unknown table or column, wrong type)."""


@dataclass
class _Plan:
    sql: str
    params: list[Any]
    # rows of the query -> (number of bad rows, message when it is above 0)
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
            # numbers compare as DOUBLE so min=2.5 on an integer column is not rounded
            bad.append(f"{q} {sign} CAST(? AS {'DOUBLE' if NUMERIC_TYPE.match(kind) else kind})")
            params.append(str(bound))
            bounds.append(f"{'min' if sign == '<' else 'max'} {bound}")
    cond = " OR ".join(bad)
    sql = (
        f"SELECT count(*) FILTER (WHERE {cond}), min({q}) FILTER (WHERE {cond}), "
        f"max({q}) FILTER (WHERE {cond}) FROM {quote(c.table)}"
    )

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, lo, hi = rows[0]
        return n, f"{n} row(s) of {col!r} outside {', '.join(bounds)}; lowest {lo}, highest {hi}"

    return _Plan(sql, params * 3, judge)


def _plan_accepted(c: AcceptedValues, schemas: dict[str, Schema]) -> _Plan:
    (col,) = _cols(schemas, c.table, [c.column])
    q = quote(col)
    marks = ", ".join("?" * len(c.values))
    cond = f"CAST({q} AS VARCHAR) NOT IN ({marks})"  # NULL is skipped, as in range
    sql = (
        f"SELECT count(*) FILTER (WHERE {cond}), "
        f"list(DISTINCT CAST({q} AS VARCHAR)) FILTER (WHERE {cond}) FROM {quote(c.table)}"
    )
    want = [str(v).lower() if isinstance(v, bool) else str(v) for v in c.values]

    def judge(rows: list[tuple]) -> tuple[int, str]:
        n, seen = rows[0]
        shown = sorted(seen or [])[:_EXAMPLES]
        return n, f"{n} row(s) of {col!r} not among the accepted values; seen: {_list(shown)}"

    return _Plan(sql, want * 2, judge)


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
            agg = f"coalesce({agg}, 0)"  # the sum of no rows is 0, as in a total row
    label = f"{side.agg}({side.column or '*'}) of {side.table}"
    selects = [f"{quote(k)} AS k{i}" for i, k in enumerate(keys)]
    group = f" GROUP BY {', '.join(quote(k) for k in keys)}" if keys else ""
    cols = ", ".join([*selects, f"CAST({agg} AS DOUBLE) AS v"])
    sql = f"SELECT {cols} FROM {quote(side.table)}{group}"
    return sql, keys, label


def _plan_reconcile(c: Reconcile, schemas: dict[str, Schema]) -> _Plan:
    lsql, _, llabel = _side_sql(schemas, c.left, c.by)
    params: list[Any] = []
    n = len(c.by)
    if c.right is not None:
        rsql, _, rlabel = _side_sql(schemas, c.right, c.by)
        on = " AND ".join(f"l.k{i} IS NOT DISTINCT FROM r.k{i}" for i in range(n)) or "true"
        ks = "".join(f"coalesce(l.k{i}, r.k{i}) AS k{i}, " for i in range(n))
        # a group missing on one side counts as 0 for sum and count, as a difference otherwise
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
        "(lv IS NULL) <> (rv IS NULL) OR abs(lv - rv) > greatest(?, ? * greatest(abs(lv), abs(rv)))"
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
