"""Which columns of two duckdb tables share values: candidates for a join, as numbers.

Only columns of the same type family are paired (number with number, text with text, date
with date; a timestamp counts as its day). For each pair: how many distinct values both hold,
how many of each side the other lacks (with a few examples), and the cardinality from whether
each side is unique. Text is matched exactly; `overlap_folded` also counts values equal after
trim and lower case, to show near misses. Nothing is decided: the agent asks a person first.
"""

from __future__ import annotations

import duckdb

from moyung.nodes.base import NodeError, quote
from moyung.profile import is_numeric

MAX_PAIRS = 400
EXAMPLES = 3
_CARDINALITY = {
    (True, True): "1:1",
    (True, False): "1:N",
    (False, True): "N:1",
    (False, False): "N:M",
}


def _family(kind: str) -> str | None:
    if is_numeric(kind):
        return "number"
    if kind == "VARCHAR":
        return "text"
    if kind in ("DATE", "TIMESTAMP"):
        return "date"
    return None


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    try:
        rows = con.execute(f"DESCRIBE SELECT * FROM {quote(table)}").fetchall()
    except duckdb.Error as e:
        raise NodeError(f"table {table!r}: {str(e).splitlines()[0]}") from None
    return [(str(r[0]), str(r[1])) for r in rows]


def _expr(col: str, family: str) -> str:
    """The column as the value two tables are matched on."""
    q = quote(col)
    return f"CAST({q} AS DATE)" if family == "date" else q


def _side(con: duckdb.DuckDBPyConnection, table: str, col: str) -> tuple[int, bool]:
    """(distinct non-null values, every non-null value occurs once)."""
    q = quote(col)
    distinct, filled = con.execute(
        f"SELECT count(DISTINCT {q}), count({q}) FROM {quote(table)}"
    ).fetchall()[0]
    return distinct, filled > 0 and distinct == filled


def _distinct(table: str, col: str, family: str) -> str:
    e = _expr(col, family)
    return f"SELECT DISTINCT {e} AS v FROM {quote(table)} WHERE {quote(col)} IS NOT NULL"


def _pair(
    con: duckdb.DuckDBPyConnection, a: str, ca: str, b: str, cb: str, family: str
) -> tuple[int, int]:
    """(distinct values both hold, the same when text ignores spaces and letter case)."""
    left, right = _distinct(a, ca, family), _distinct(b, cb, family)
    shared = con.execute(f"SELECT count(*) FROM ({left}) JOIN ({right}) USING (v)").fetchall()[0][0]
    if family != "text":
        return shared, shared
    fold_l = f"SELECT DISTINCT lower(trim(v)) AS v FROM ({left})"
    fold_r = f"SELECT DISTINCT lower(trim(v)) AS v FROM ({right})"
    folded = con.execute(f"SELECT count(*) FROM ({fold_l}) JOIN ({fold_r}) USING (v)").fetchall()[
        0
    ][0]
    return shared, folded


def _unmatched(
    con: duckdb.DuckDBPyConnection, a: str, ca: str, b: str, cb: str, family: str
) -> list[str]:
    """Up to EXAMPLES values of `a.ca` that `b.cb` lacks, in sorted order."""
    left, right = _distinct(a, ca, family), _distinct(b, cb, family)
    rows = con.execute(
        f"SELECT CAST(v AS VARCHAR) AS s FROM ({left}) WHERE v NOT IN (SELECT v FROM ({right})) "
        f"ORDER BY s LIMIT {EXAMPLES}"
    ).fetchall()
    return [r[0] for r in rows]


def relate(con: duckdb.DuckDBPyConnection, a: str, b: str) -> dict:
    """Column pairs of tables `a` and `b` that share at least one value (exact or folded),
    best first (most shared values, then column order). Date and timestamp columns match by day.
    `checked` and `pairs_total` say how many same-family pairs exist and how many were compared
    (at most MAX_PAIRS; pairs with the same column name are compared first)."""
    cols_a, cols_b = _columns(con, a), _columns(con, b)
    candidates = [
        (ca, cb, fa)
        for ca, ta in cols_a
        if (fa := _family(ta))
        for cb, tb in cols_b
        if _family(tb) == fa
    ]
    candidates.sort(key=lambda c: c[0].casefold() != c[1].casefold())
    sides: dict[tuple[str, str], tuple[int, bool]] = {}
    pairs = []
    for ca, cb, family in candidates[:MAX_PAIRS]:
        shared, folded = _pair(con, a, ca, b, cb, family)
        if not shared and not folded:
            continue
        da, ua = sides.setdefault((a, ca), _side(con, a, ca))
        db, ub = sides.setdefault((b, cb), _side(con, b, cb))
        pairs.append(
            {
                "a": ca,
                "b": cb,
                "overlap": shared,
                "overlap_folded": folded,
                "a_distinct": da,
                "b_distinct": db,
                "a_missing": da - shared,
                "b_missing": db - shared,
                "cardinality": _CARDINALITY[ua, ub],
                "a_unmatched": _unmatched(con, a, ca, b, cb, family) if da - shared else [],
                "b_unmatched": _unmatched(con, b, cb, a, ca, family) if db - shared else [],
            }
        )
    order = {name: i for i, (name, _) in enumerate(cols_a)}
    order_b = {name: i for i, (name, _) in enumerate(cols_b)}
    pairs.sort(key=lambda p: (-p["overlap"], -p["overlap_folded"], order[p["a"]], order_b[p["b"]]))
    return {
        "pairs": pairs,
        "checked": min(len(candidates), MAX_PAIRS),
        "pairs_total": len(candidates),
    }
