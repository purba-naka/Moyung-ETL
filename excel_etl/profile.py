"""Column statistics of a duckdb table: what the data looks like, as evidence, not as advice.

`profile_table` works on any table in a connection, so every source kind shares it. A finding
(an outlier, an odd length) is a candidate for a person to judge; nothing is dropped or fixed.
"""

from __future__ import annotations

import math

import duckdb

from excel_etl.nodes.base import quote

NUMERIC = frozenset(
    {
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
        "FLOAT", "DOUBLE", "DECIMAL",
    }
)  # fmt: skip
TOP_VALUES = 3
KEY_COLUMNS = 30


def is_numeric(kind: str) -> bool:
    return kind.split("(")[0] in NUMERIC


def _num(v: object) -> float | int | None:
    """A statistic as a JSON number; None for no value, NaN or infinity."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    f = float(v)
    return round(f, 4) if math.isfinite(f) else None


def _numeric_stats(con: duckdb.DuckDBPyConnection, table: str, col: str, kind: str) -> dict:
    """Statistics over the finite values only; NaN and infinity are counted as `non_finite`."""
    q = quote(col)
    floating = kind in ("FLOAT", "DOUBLE")
    finite = f"{quote(table)} WHERE NOT (isnan({q}) OR isinf({q}))" if floating else quote(table)
    non_finite = 0
    if floating:
        non_finite = con.execute(
            f"SELECT count(*) FROM {quote(table)} WHERE isnan({q}) OR isinf({q})"
        ).fetchall()[0][0]
    row = con.execute(
        f"SELECT min({q}), max({q}), avg({q}), median({q}), stddev_pop({q}), "
        f"quantile_cont({q}, 0.25), quantile_cont({q}, 0.75) FROM {finite}"
    ).fetchall()[0]
    lo, hi, mean, median, std, q25, q75 = (_num(v) for v in row)
    out: dict = {"min": lo, "max": hi, "mean": mean, "median": median, "std": std}
    out["q25"], out["q75"] = q25, q75
    if non_finite:
        out["non_finite"] = non_finite
    if q25 is not None and q75 is not None:
        iqr = q75 - q25
        low, high = q25 - 1.5 * iqr, q75 + 1.5 * iqr
        sep = " AND " if "WHERE" in finite else " WHERE "
        out["outliers"] = con.execute(
            f"SELECT count(*) FROM {finite}{sep}({q} < ? OR {q} > ?)", [low, high]
        ).fetchall()[0][0]
    return out


def _text_stats(con: duckdb.DuckDBPyConnection, table: str, col: str) -> dict:
    q = quote(col)
    lo, hi, folded = con.execute(
        f"SELECT min(length({q})), max(length({q})), count(DISTINCT lower(trim({q}))) "
        f"FROM {quote(table)}"
    ).fetchall()[0]
    top = con.execute(
        f"SELECT {q}, count(*) AS n FROM {quote(table)} WHERE {q} IS NOT NULL "
        f"GROUP BY {q} ORDER BY n DESC, {q} LIMIT {TOP_VALUES}"
    ).fetchall()
    return {
        "len_min": lo,
        "len_max": hi,
        "distinct_folded": folded,
        "top": [[str(v), n] for v, n in top],
    }


def _range_stats(con: duckdb.DuckDBPyConnection, table: str, col: str) -> dict:
    q = quote(col)
    lo, hi = con.execute(f"SELECT min({q}), max({q}) FROM {quote(table)}").fetchall()[0]
    return {"min": None if lo is None else str(lo), "max": None if hi is None else str(hi)}


def profile_table(con: duckdb.DuckDBPyConnection, table: str) -> dict:
    """Rows, duplicate rows and per column: type, null count, distinct, and by type the range,
    mean/median/std/quartiles and IQR outlier count (numbers), top values and text length
    (text), min/max (dates). Deterministic; values are plain JSON types."""
    described = con.execute(f"DESCRIBE SELECT * FROM {quote(table)}").fetchall()
    rows = con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0]
    distinct_rows = con.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT * FROM {quote(table)})"
    ).fetchall()[0][0]
    names = [str(d[0]) for d in described]
    counts = con.execute(
        "SELECT "
        + ", ".join(f"count({quote(n)}), count(DISTINCT {quote(n)})" for n in names)
        + f" FROM {quote(table)}"
    ).fetchall()[0]
    columns = []
    for i, (name, kind, *_) in enumerate(described):
        nulls, distinct = rows - counts[2 * i], counts[2 * i + 1]
        col: dict = {"name": name, "type": str(kind), "null": nulls, "distinct": distinct}
        if nulls < rows:
            if is_numeric(str(kind)):
                col |= _numeric_stats(con, table, name, str(kind))
            elif str(kind) == "VARCHAR":
                col |= _text_stats(con, table, name)
            elif str(kind) != "BOOLEAN":
                col |= _range_stats(con, table, name)
        col["unique"] = bool(rows) and nulls == 0 and distinct == rows
        columns.append(col)
    return {
        "rows": rows,
        "duplicate_rows": rows - distinct_rows,
        "columns": columns,
        "key_candidates": _keys(con, table, columns, rows, rows - distinct_rows),
    }


def _keys(
    con: duckdb.DuckDBPyConnection, table: str, columns: list[dict], rows: int, duplicates: int
) -> list[list[str]]:
    """Columns that tell every row apart: each unique non-null column, or when there is none,
    each pair of non-null columns whose combination is unique (from the first KEY_COLUMNS
    such columns). Empty when rows repeat, since then nothing can be a key."""
    if not rows or duplicates:
        return []
    single = [[c["name"]] for c in columns if c["unique"]]
    if single:
        return single
    usable = [c["name"] for c in columns if c["null"] == 0 and 1 < c["distinct"] < rows]
    usable = usable[:KEY_COLUMNS]
    pairs = [(a, b) for i, a in enumerate(usable) for b in usable[i + 1 :]]
    if not pairs:
        return []
    counts = con.execute(
        "SELECT "
        + ", ".join(f"count(DISTINCT ({quote(a)}, {quote(b)}))" for a, b in pairs)
        + f" FROM {quote(table)}"
    ).fetchall()[0]
    return [[a, b] for (a, b), n in zip(pairs, counts, strict=True) if n == rows]


def column_line(col: dict, rows: int) -> str:
    """One text line for a column of a `profile_table` result."""
    if col["null"] == rows:
        return f"{col['type']} all null" if rows else f"{col['type']} no data"
    parts = [col["type"]]
    if "min" in col and "mean" not in col:
        parts.append(f"{col['min']}..{col['max']}")
    if "mean" in col:
        parts.append(f"min {_f(col['min'])} max {_f(col['max'])}")
        parts.append(f"mean {_f(col['mean'])} median {_f(col['median'])} std {_f(col['std'])}")
        parts.append(f"q25 {_f(col['q25'])} q75 {_f(col['q75'])}")
        if col.get("outliers"):
            parts.append(f"outliers {col['outliers']} (IQR 1.5, candidates only)")
    if "top" in col:
        parts.append("top " + ",".join(f"{_one_line(v)}:{n}" for v, n in col["top"]))
        parts.append(f"len {col['len_min']}..{col['len_max']}")
    distinct = f"distinct {col['distinct']}"
    if col.get("distinct_folded", col["distinct"]) < col["distinct"]:
        distinct += f" ({col['distinct_folded']} after trim+lower)"
    parts.append(distinct)
    if col.get("non_finite"):
        parts.append(f"nan/inf {col['non_finite']} (left out of the numbers)")
    if col["null"]:
        parts.append(f"null {col['null']} ({round(100 * col['null'] / rows)}%)")
    if col["unique"]:
        parts.append("unique key candidate")
    return " ".join(parts)


def _one_line(text: str, limit: int = 40) -> str:
    flat = " ".join(text.replace("|", "/").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _f(v: float | int | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1e15 or (v != 0 and abs(v) < 1e-4):
        return f"{v:.4g}"
    return str(int(v)) if float(v).is_integer() else str(round(v, 4))
