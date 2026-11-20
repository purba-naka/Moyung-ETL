"""Warnings about patterns that often leak the target into the features.

These are heuristics over the rows before the split. A quiet result is not proof of a clean
dataset, and a warning can be a false alarm; domain knowledge decides."""

from __future__ import annotations

import duckdb

from excel_etl.nodes.base import Column, quote

ID_SHARE = 0.95
MIN_ROWS = 20
_SHOWN = 5
_AFTER_SHARE = 0.5
_PURE = 0.99
_BALANCED = 0.95
_CONTINUOUS = ("FLOAT", "DOUBLE", "DECIMAL")


def temporal(column: Column) -> bool:
    return column.type.startswith(("DATE", "TIMESTAMP"))


def id_like(column: Column, filled: int, distinct: int) -> bool:
    """A column whose value differs in almost every row, such as a key."""
    return (
        filled >= MIN_ROWS
        and distinct >= ID_SHARE * filled
        and not column.type.startswith(_CONTINUOUS)
        and not temporal(column)
    )


def _list(names: list[str]) -> str:
    more = f" and {len(names) - _SHOWN} more" if len(names) > _SHOWN else ""
    return ", ".join(repr(n) for n in names[:_SHOWN]) + more


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(con.execute(sql).fetchall()[0][0] or 0)


def _emptiness_decides(con: duckdb.DuckDBPyConnection, base: str, f: str, t: str) -> bool:
    """True when whether `f` is empty almost fixes the target, and the target is not already
    one-sided."""
    total, top = con.execute(
        f"SELECT sum(__ml_n), max(__ml_n) FROM (SELECT count(*) AS __ml_n FROM {base} "
        f"WHERE {t} IS NOT NULL GROUP BY {t})"
    ).fetchall()[0]
    pure, groups = con.execute(
        f"SELECT sum(__ml_m), count(*) FROM (SELECT max(__ml_n) AS __ml_m FROM (SELECT "
        f"({f} IS NULL) AS __ml_e, count(*) AS __ml_n FROM {base} WHERE {t} IS NOT NULL "
        f"GROUP BY __ml_e, {t}) GROUP BY __ml_e)"
    ).fetchall()[0]
    if not total or groups < 2 or total < MIN_ROWS:
        return False
    return pure / total >= _PURE and top / total <= _BALANCED


def _predictors(
    con: duckdb.DuckDBPyConnection, base: str, target: Column, features: list[Column]
) -> tuple[list[str], list[str], list[str]]:
    """Features that equal or fix the target, features whose emptiness fixes it, and features
    that look like row ids."""
    t = quote(target.name)
    perfect: list[str] = []
    by_empty: list[str] = []
    ids: list[str] = []
    target_values = _scalar(con, f"SELECT count(DISTINCT {t}) FROM {base}")
    for feature in features:
        f = quote(feature.name)
        pair_rows = f"FROM {base} WHERE {f} IS NOT NULL AND {t} IS NOT NULL"
        if _scalar(con, f"SELECT count(*) {pair_rows}") and not _scalar(
            con, f"SELECT count(*) {pair_rows} AND CAST({f} AS VARCHAR) <> CAST({t} AS VARCHAR)"
        ):
            perfect.append(feature.name)
            continue
        filled, distinct = (
            int(v)
            for v in con.execute(f"SELECT count({f}), count(DISTINCT {f}) FROM {base}").fetchall()[
                0
            ]
        )
        pairs = _scalar(con, f"SELECT count(*) FROM (SELECT DISTINCT {f}, {t} {pair_rows})")
        if (
            filled >= MIN_ROWS
            and distinct >= 2
            and target_values >= 2
            and pairs == distinct
            and distinct * 2 <= filled
        ):
            perfect.append(feature.name)
        elif id_like(feature, filled, distinct):
            ids.append(feature.name)
        elif filled > 0 and _emptiness_decides(con, base, f, t):
            by_empty.append(feature.name)
    return perfect, by_empty, ids


def _after_reference(
    con: duckdb.DuckDBPyConnection, base: str, reference: Column, features: list[Column]
) -> list[str]:
    """Date features that are later than the reference date in most rows."""
    ref = quote(reference.name)
    late: list[str] = []
    for feature in features:
        if not temporal(feature):
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


def _spread(
    con: duckdb.DuckDBPyConnection,
    base: str,
    part: str,
    row: str,
    cols: list[str],
    filled: bool,
) -> int:
    """Rows whose values in `cols` also occur in another partition; with `filled`, rows with
    an empty value in `cols` do not count."""
    keys = ", ".join(f"b.{quote(c)}" for c in cols)
    where = " WHERE " + " AND ".join(f"b.{quote(c)} IS NOT NULL" for c in cols) if filled else ""
    return _scalar(
        con,
        f"SELECT sum(__ml_n) FROM (SELECT count(*) AS __ml_n, count(DISTINCT p.__ml_p) AS __ml_d "
        f"FROM {base} b JOIN {part} p USING ({row}){where} GROUP BY {keys}) WHERE __ml_d > 1",
    )


def check(
    con: duckdb.DuckDBPyConnection,
    *,
    base: str,
    part: str,
    row: str,
    columns: list[Column],
    target: Column,
    features: list[Column],
    ids: list[Column],
    by: Column | None,
    strategy: str,
) -> list[str]:
    """Leakage warnings for the declared columns. `base` holds the rows (`row` numbers them,
    `columns` are all its other columns), `part` the partition of each row."""
    found: list[str] = []
    perfect, by_empty, id_named = _predictors(con, base, target, features)
    if perfect:
        found.append(
            f"feature(s) {_list(perfect)} equal or fully determine {target.name!r} in these rows; "
            "if they are known only after the outcome, they leak it: move them to exclude"
        )
    if by_empty:
        found.append(
            f"whether feature(s) {_list(by_empty)} are empty almost fixes {target.name!r}; a "
            "column filled in only after the outcome (such as a cancel reason) leaks it: move "
            "it to exclude"
        )
    if id_named:
        found.append(
            f"feature(s) {_list(id_named)} have a different value in almost every row, like an "
            "identifier; a model can memorize them instead of learning: list them under ids or "
            "exclude"
        )
    reference = target if temporal(target) else by if strategy == "time" and by else None
    if reference is not None:
        late = _after_reference(con, base, reference, [f for f in features if f is not reference])
        if late:
            what = "the target" if reference is target else f"the split column {reference.name!r}"
            found.append(
                f"date feature(s) {_list(late)} fall after {what} in most rows; information from "
                "after the outcome is not available when predicting: check them"
            )
    if strategy != "group":
        names = [c.name for c in ids]
        if names and (n := _spread(con, base, part, row, names, True)):
            found.append(
                f"{n} row(s) share their ids ({_list(names)}) with a row in another partition: "
                "the same entity is on both sides of the split; use a group split on that id"
            )
        if n := _spread(con, base, part, row, [c.name for c in columns], False):
            found.append(
                f"{n} row(s) are identical in every column to a row in another partition: "
                "duplicates across the split make a score look better than it is; deduplicate "
                "first"
            )
    return found
