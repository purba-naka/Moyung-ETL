"""Warnings about patterns that often leak the target into the features.

These are heuristics over the rows before the split. A quiet result is not proof of a clean
dataset, and a warning can be a false alarm; domain knowledge decides."""

from __future__ import annotations

import duckdb

from excel_etl.nodes.base import Column, quote

_SHOWN = 5
_ID_SHARE = 0.95
_ID_MIN_ROWS = 20
_AFTER_SHARE = 0.5
_CONTINUOUS = ("FLOAT", "DOUBLE", "DECIMAL")


def _temporal(column: Column) -> bool:
    return column.type.startswith(("DATE", "TIMESTAMP"))


def _list(names: list[str]) -> str:
    more = f" and {len(names) - _SHOWN} more" if len(names) > _SHOWN else ""
    return ", ".join(repr(n) for n in names[:_SHOWN]) + more


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(con.execute(sql).fetchall()[0][0] or 0)


def _predictors(
    con: duckdb.DuckDBPyConnection, base: str, target: Column, features: list[Column]
) -> tuple[list[str], list[str]]:
    """Features that equal the target or fix its value, and features that look like row ids."""
    t = quote(target.name)
    perfect: list[str] = []
    id_like: list[str] = []
    for feature in features:
        f = quote(feature.name)
        if _scalar(
            con,
            f"SELECT count(*) FROM {base} WHERE {f} IS NOT NULL AND {t} IS NOT NULL "
            f"AND CAST({f} AS VARCHAR) <> CAST({t} AS VARCHAR)",
        ) == 0 and _scalar(
            con, f"SELECT count(*) FROM {base} WHERE {f} IS NOT NULL AND {t} IS NOT NULL"
        ):
            perfect.append(feature.name)
            continue
        filled, distinct = (
            int(v)
            for v in con.execute(f"SELECT count({f}), count(DISTINCT {f}) FROM {base}").fetchall()[
                0
            ]
        )
        pairs = _scalar(
            con,
            f"SELECT count(*) FROM (SELECT DISTINCT {f}, {t} FROM {base} "
            f"WHERE {f} IS NOT NULL AND {t} IS NOT NULL)",
        )
        target_values = _scalar(con, f"SELECT count(DISTINCT {t}) FROM {base}")
        if distinct >= 2 and target_values >= 2 and pairs == distinct and distinct * 2 <= filled:
            perfect.append(feature.name)
        elif (
            filled >= _ID_MIN_ROWS
            and distinct >= _ID_SHARE * filled
            and not feature.type.startswith(_CONTINUOUS)
            and not _temporal(feature)
        ):
            id_like.append(feature.name)
    return perfect, id_like


def _after_reference(
    con: duckdb.DuckDBPyConnection, base: str, reference: Column, features: list[Column]
) -> list[str]:
    """Date features that are later than the reference date in most rows."""
    ref = quote(reference.name)
    late: list[str] = []
    for feature in features:
        if not _temporal(feature):
            continue
        f = quote(feature.name)
        both = _scalar(
            con, f"SELECT count(*) FROM {base} WHERE {f} IS NOT NULL AND {ref} IS NOT NULL"
        )
        after = _scalar(
            con,
            f"SELECT count(*) FROM {base} WHERE {f} IS NOT NULL AND {ref} IS NOT NULL "
            f"AND {f} > {ref}",
        )
        if both and after / both > _AFTER_SHARE:
            late.append(feature.name)
    return late


def _spread(con: duckdb.DuckDBPyConnection, base: str, part: str, row: str, cols: list[str]) -> int:
    """Rows whose values in `cols` also occur in another partition."""
    keys = ", ".join(f"b.{quote(c)}" for c in cols)
    return _scalar(
        con,
        f"SELECT sum(n) FROM (SELECT count(*) AS n, count(DISTINCT p.__ml_p) AS d FROM {base} b "
        f"JOIN {part} p USING ({row}) GROUP BY {keys}) WHERE d > 1",
    )


def check(
    con: duckdb.DuckDBPyConnection,
    *,
    base: str,
    part: str,
    row: str,
    target: Column,
    features: list[Column],
    ids: list[Column],
    by: Column | None,
    strategy: str,
) -> list[str]:
    """Leakage warnings for the declared columns; `base` holds the rows, `part` their partition."""
    found: list[str] = []
    perfect, id_like = _predictors(con, base, target, features)
    if perfect:
        found.append(
            f"feature(s) {_list(perfect)} equal or fully determine {target.name!r} in these rows; "
            "if they are known only after the outcome, they leak it: move them to exclude"
        )
    if id_like:
        found.append(
            f"feature(s) {_list(id_like)} have a different value in almost every row, like an "
            "identifier; a model can memorize them instead of learning: list them under ids or "
            "exclude"
        )
    reference = target if _temporal(target) else by if strategy == "time" and by else None
    if reference is not None:
        late = _after_reference(con, base, reference, [f for f in features if f is not reference])
        if late:
            what = "the target" if reference is target else f"the split column {reference.name!r}"
            found.append(
                f"date feature(s) {_list(late)} fall after {what} in most rows; information from "
                "after the outcome is not available when predicting: check them"
            )
    if ids:
        names = [c.name for c in ids]
        if strategy != "group" and (n := _spread(con, base, part, row, names)):
            found.append(
                f"{n} row(s) share their ids ({_list(names)}) with a row in another partition: "
                "the same entity is on both sides of the split; use a group split on that id"
            )
    cols = [c.name for c in (*features, target)]
    if strategy != "group" and (n := _spread(con, base, part, row, cols)):
        found.append(
            f"{n} row(s) have the same feature and target values as a row in another partition: "
            "duplicates across the split make a score look better than it is; deduplicate first"
        )
    return found
