"""Pick rows of a duckdb table to show an agent, by an explicit strategy.

Rows are numbered once by their place in the table, and every strategy chooses by that
number, so the same table and arguments give the same rows in the same order. `random` ranks
rows by `hash(place, seed)`: no repeats, and the same rows for the same seed. A strategy a
table cannot serve is an error, never a quiet fallback.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import duckdb

from excel_etl.nodes.base import NodeError, quote, schema_of
from excel_etl.nodes.sqlexpr import bind, check_expression

STRATEGIES = (
    "head",
    "tail",
    "head_tail",
    "random",
    "stratified",
    "where",
    "nulls",
    "duplicates",
)
MAX_ROWS = 1000
NULL_GROUP = "<null>"


def _count(n: object) -> int:
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_ROWS:
        raise NodeError(f"n must be an integer from 1 to {MAX_ROWS}, got {n!r}")
    return n


def _need(strategy: str, column: str | None, where: str | None) -> None:
    wants_column = strategy in ("stratified", "nulls")
    if wants_column and not column:
        raise NodeError(f"strategy {strategy} needs column")
    if not wants_column and column:
        raise NodeError(f"column is only for stratified and nulls, not {strategy}")
    if strategy == "where" and not where:
        raise NodeError('strategy where needs where, a condition such as "amount > 0"')
    if strategy != "where" and where:
        raise NodeError(f"where is only for strategy where, not {strategy}")


def _plain(v: object) -> object:
    """A cell as a JSON value: dates and times as ISO text, DECIMAL as a float."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    return v


def _numbered(table: str) -> str:
    return f"SELECT row_number() OVER () AS __pos, * FROM {quote(table)}"


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def _pick_where(con: duckdb.DuckDBPyConnection, table: str, where: str) -> str:
    cond = check_expression(where)
    (kind,) = bind(schema_of(con.table(table)), f"({cond}) AS __e", "sample where")
    if kind.type not in ("BOOLEAN", "NULL"):
        raise NodeError(
            f"sample where: {cond!r} gives {kind.type}, not true/false; "
            'write a comparison such as "col" > 0'
        )
    return f"({cond})"


def _choose(
    con: duckdb.DuckDBPyConnection,
    table: str,
    strategy: str,
    n: int,
    seed: int,
    column: str | None,
    where: str | None,
) -> tuple[str, str, list[tuple] | None]:
    """(sql of the matching rows with `__pos`, how they are picked, group sizes or None)."""
    base = f"({_numbered(table)})"
    if strategy in ("head", "tail", "head_tail"):
        return base, strategy, None
    if strategy == "random":
        return base, f"random(seed={seed})", None
    if strategy == "where":
        cond = _pick_where(con, table, where or "")
        return (
            f"(SELECT * FROM {base} WHERE {cond})",
            f"where({check_expression(where or '')})",
            None,
        )
    names = [c.name for c in schema_of(con.table(table))]
    if column is not None and column not in names:
        raise NodeError(f"column {column!r} not in the table; columns: {', '.join(names[:12])}")
    if strategy == "nulls":
        q = quote(column or "")
        return f"(SELECT * FROM {base} WHERE {q} IS NULL)", f"nulls({column})", None
    if strategy == "duplicates":
        every = ", ".join(quote(c) for c in names)
        dup = (
            f"(SELECT * FROM (SELECT *, count(*) OVER (PARTITION BY {every}) AS __dups "
            f"FROM {base}) WHERE __dups > 1)"
        )
        return dup, "duplicates", None
    q = quote(column or "")
    groups = _fetch(
        con,
        f"SELECT coalesce(CAST({q} AS VARCHAR), '{NULL_GROUP}'), count(*) "
        f"FROM {base} GROUP BY 1 ORDER BY 1",
    )
    return base, f"stratified({column}, seed={seed})", groups


def _positions(
    con: duckdb.DuckDBPyConnection,
    source: str,
    strategy: str,
    n: int,
    seed: int,
    column: str | None,
) -> list[int]:
    if strategy == "head" or strategy in ("where", "nulls", "duplicates"):
        sql = f"SELECT __pos FROM {source} ORDER BY __pos LIMIT {n}"
    elif strategy == "tail":
        sql = f"SELECT __pos FROM (SELECT __pos FROM {source} ORDER BY __pos DESC LIMIT {n})"
    elif strategy == "head_tail":
        half = n // 2
        sql = (
            f"SELECT __pos FROM (SELECT __pos, row_number() OVER (ORDER BY __pos) AS a, "
            f"count(*) OVER () AS total FROM {source}) WHERE a <= {n - half} "
            f"OR a > total - {half}"
        )
    elif strategy == "random":
        sql = f"SELECT __pos FROM {source} ORDER BY hash(__pos, {seed}), __pos LIMIT {n}"
    else:
        q = quote(column or "")
        sql = (
            f"SELECT __pos FROM (SELECT __pos, row_number() OVER (PARTITION BY {q} "
            f"ORDER BY hash(__pos, {seed}), __pos) AS r FROM {source}) WHERE r <= {n} "
            f"ORDER BY r, hash(__pos, {seed}), __pos LIMIT {MAX_ROWS}"
        )
    return sorted(p for (p,) in _fetch(con, sql))


def sample_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    strategy: str,
    n: int = 10,
    seed: int = 0,
    column: str | None = None,
    where: str | None = None,
) -> dict:
    """Rows of `table` picked by `strategy`: `rows` in table order, `positions` (1-based place
    in the table), `columns`, `matched` (rows the strategy could pick from), `basis` (what was
    done, for the record), `seed`, and for stratified `groups` (value -> size)."""
    if strategy not in STRATEGIES:
        raise NodeError(f"strategy must be one of {', '.join(STRATEGIES)}; got {strategy!r}")
    n = _count(n)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise NodeError(f"seed must be an integer, got {seed!r}")
    _need(strategy, column, where)
    source, how, groups = _choose(con, table, strategy, n, seed, column, where)
    matched = _fetch(con, f"SELECT count(*) FROM {source}")[0][0]
    positions = _positions(con, source, strategy, n, seed, column)
    if strategy == "stratified" and groups is not None and n * len(groups) > MAX_ROWS:
        how += f", capped at {MAX_ROWS} rows (fewer than n per group)"
    names = [c.name for c in schema_of(con.table(table))]
    rows = []
    if positions:
        marks = ", ".join("?" for _ in positions)
        cols = ", ".join(quote(c) for c in names)
        rows = _fetch(
            con,
            f"SELECT {cols} FROM ({_numbered(table)}) WHERE __pos IN ({marks}) ORDER BY __pos",
            positions,
        )
    out: dict = {
        "columns": names,
        "rows": [[_plain(v) for v in r] for r in rows],
        "positions": positions,
        "matched": matched,
        "seed": seed,
        "basis": f"{how}, {matched} rows matched, {len(rows)} shown",
    }
    if groups is not None:
        out["groups"] = {k: v for k, v in groups}
    return out
