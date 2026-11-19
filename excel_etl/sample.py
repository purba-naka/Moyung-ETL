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
LITERAL_NULL_GROUP = "<null> (text)"


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


def _helper(names: list[str], base: str) -> str:
    """A column name for the library's own use that no column of the table has."""
    taken = {n.casefold() for n in names}
    name = base
    while name.casefold() in taken:
        name = "_" + name
    return name


class _Table:
    """A table with each row numbered by its place. The number and the duplicate count live
    in columns named so they cannot meet a user column; user names always go through quote."""

    def __init__(self, con: duckdb.DuckDBPyConnection, table: str) -> None:
        self.table = table
        self.names = [c.name for c in schema_of(con.table(table))]
        self.pos = quote(_helper(self.names, "__pos"))
        self.dups = quote(_helper(self.names, "__dups"))
        self.rank = quote(_helper(self.names, "__r"))
        self.numbered = f"SELECT row_number() OVER () AS {self.pos}, * FROM {quote(table)}"


def _group_key(value: str | None) -> str:
    if value is None:
        return NULL_GROUP
    return LITERAL_NULL_GROUP if value == NULL_GROUP else value


def _fetch(
    con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None, who: str = "sample"
) -> list[tuple]:
    try:
        return con.execute(sql, params or []).fetchall()
    except duckdb.Error as e:
        raise NodeError(f"{who}: {str(e).splitlines()[0]}") from None


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
    t: _Table,
    strategy: str,
    seed: int,
    column: str | None,
    where: str | None,
) -> tuple[str, str, list[tuple] | None]:
    """(sql of the matching rows with the position column, how they are picked, group sizes
    or None)."""
    base = f"({t.numbered})"
    if strategy in ("head", "tail", "head_tail"):
        return base, strategy, None
    if strategy == "random":
        return base, f"random(seed={seed})", None
    if strategy == "where":
        cond = _pick_where(con, t.table, where or "")
        hit = f"SELECT {t.pos} FROM {base} WHERE {cond}"
        return f"({hit})", f"where({check_expression(where or '')})", None
    if column is not None and column not in t.names:
        raise NodeError(f"column {column!r} not in the table; columns: {', '.join(t.names[:12])}")
    if strategy == "nulls":
        hit = f"SELECT {t.pos} FROM {base} WHERE {quote(column or '')} IS NULL"
        return f"({hit})", f"nulls({column})", None
    if strategy == "duplicates":
        every = ", ".join(quote(c) for c in t.names)
        inner = f"SELECT {t.pos}, count(*) OVER (PARTITION BY {every}) AS {t.dups} FROM {base}"
        return f"(SELECT {t.pos} FROM ({inner}) WHERE {t.dups} > 1)", "duplicates", None
    q = quote(column or "")
    groups = _fetch(
        con,
        f"SELECT g, count(*) FROM (SELECT CASE WHEN {q} IS NULL THEN NULL "
        f"ELSE CAST({q} AS VARCHAR) END AS g FROM {quote(t.table)}) GROUP BY g "
        "ORDER BY g NULLS FIRST",
    )
    return base, f"stratified({column}, seed={seed})", groups


def _positions(
    con: duckdb.DuckDBPyConnection,
    t: _Table,
    source: str,
    strategy: str,
    n: int,
    seed: int,
    column: str | None,
    who: str,
) -> list[int]:
    p = t.pos
    if strategy in ("head", "where", "nulls", "duplicates"):
        sql = f"SELECT {p} FROM {source} ORDER BY {p} LIMIT {n}"
    elif strategy == "tail":
        sql = f"SELECT {p} FROM (SELECT {p} FROM {source} ORDER BY {p} DESC LIMIT {n})"
    elif strategy == "head_tail":
        half, a = n // 2, quote(_helper(t.names, "__a"))
        sql = (
            f"SELECT {p} FROM (SELECT {p}, row_number() OVER (ORDER BY {p}) AS {a}, "
            f"count(*) OVER () AS total FROM {source}) WHERE {a} <= {n - half} "
            f"OR {a} > total - {half}"
        )
    elif strategy == "random":
        sql = f"SELECT {p} FROM {source} ORDER BY hash({p}, {seed}), {p} LIMIT {n}"
    else:
        r = t.rank
        sql = (
            f"SELECT {p} FROM (SELECT {p}, row_number() OVER (PARTITION BY {quote(column or '')} "
            f"ORDER BY hash({p}, {seed}), {p}) AS {r} FROM {source}) WHERE {r} <= {n} "
            f"ORDER BY {r}, hash({p}, {seed}), {p} LIMIT {MAX_ROWS}"
        )
    return sorted(v for (v,) in _fetch(con, sql, who=who))


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
    done, for the record), `seed`, and for stratified `groups` (value -> size; real NULLs are
    listed as `<null>`)."""
    if strategy not in STRATEGIES:
        raise NodeError(f"strategy must be one of {', '.join(STRATEGIES)}; got {strategy!r}")
    n = _count(n)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise NodeError(f"seed must be an integer, got {seed!r}")
    _need(strategy, column, where)
    t = _Table(con, table)
    source, how, groups = _choose(con, t, strategy, seed, column, where)
    who = "sample where" if strategy == "where" else "sample"
    matched = _fetch(con, f"SELECT count(*) FROM {source}", who=who)[0][0]
    positions = _positions(con, t, source, strategy, n, seed, column, who)
    if strategy == "stratified" and groups is not None and n * len(groups) > MAX_ROWS:
        how += f", capped at {MAX_ROWS} rows (fewer than n per group)"
    rows = []
    if positions:
        marks = ", ".join("?" for _ in positions)
        cols = ", ".join(quote(c) for c in t.names)
        rows = _fetch(
            con,
            f"SELECT {cols} FROM ({t.numbered}) WHERE {t.pos} IN ({marks}) ORDER BY {t.pos}",
            positions,
        )
    out: dict = {
        "columns": t.names,
        "rows": [[_plain(v) for v in r] for r in rows],
        "positions": positions,
        "matched": matched,
        "seed": seed,
        "basis": f"{how}, {matched} rows matched, {len(rows)} shown",
    }
    if groups is not None:
        out["groups"] = {_group_key(k): v for k, v in groups}
    return out
