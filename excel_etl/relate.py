"""Which columns of two duckdb tables share values: candidates for a join, as numbers.

Only columns of the same type family are paired (number with number, text with text, date
with date). For each pair: how many distinct values both hold, how many of each side the
other lacks, and the cardinality from whether each side is unique. Text is matched exactly;
`overlap_folded` also counts values equal after trim and lower case, to show near misses.
Nothing is decided: the agent asks a person before joining.
"""

from __future__ import annotations

import duckdb

from excel_etl.nodes.base import NodeError, quote
from excel_etl.profile import is_numeric

MAX_PAIRS = 400
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


def _side(con: duckdb.DuckDBPyConnection, table: str, col: str) -> tuple[int, bool]:
    """(distinct non-null values, every non-null value occurs once)."""
    q = quote(col)
    distinct, filled = con.execute(
        f"SELECT count(DISTINCT {q}), count({q}) FROM {quote(table)}"
    ).fetchall()[0]
    return distinct, filled > 0 and distinct == filled


def _pair(
    con: duckdb.DuckDBPyConnection, a: str, ca: str, b: str, cb: str, text: bool
) -> tuple[int, int]:
    qa, qb = quote(ca), quote(cb)
    exact = (
        f"SELECT count(*) FROM (SELECT DISTINCT {qa} AS v FROM {quote(a)} WHERE {qa} IS NOT NULL) "
        f"JOIN (SELECT DISTINCT {qb} AS v FROM {quote(b)}) USING (v)"
    )
    shared = con.execute(exact).fetchall()[0][0]
    if not text:
        return shared, shared
    folded = (
        f"SELECT count(*) FROM (SELECT DISTINCT lower(trim({qa})) AS v FROM {quote(a)} "
        f"WHERE {qa} IS NOT NULL) JOIN (SELECT DISTINCT lower(trim({qb})) AS v FROM {quote(b)}) "
        "USING (v)"
    )
    return shared, con.execute(folded).fetchall()[0][0]


def relate(con: duckdb.DuckDBPyConnection, a: str, b: str) -> dict:
    """Column pairs of tables `a` and `b` that share at least one value (exact or folded),
    best first (most shared values, then column order). `checked` and `pairs_total` say how
    many same-family pairs exist and how many were compared (at most MAX_PAIRS)."""
    cols_a, cols_b = _columns(con, a), _columns(con, b)
    candidates = [
        (ca, cb, fa == "text")
        for ca, ta in cols_a
        if (fa := _family(ta))
        for cb, tb in cols_b
        if _family(tb) == fa
    ]
    sides: dict[tuple[str, str], tuple[int, bool]] = {}
    pairs = []
    for ca, cb, text in candidates[:MAX_PAIRS]:
        shared, folded = _pair(con, a, ca, b, cb, text)
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
