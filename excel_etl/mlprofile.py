"""A column-by-column view of a table for choosing the target, features and ids of `ml`."""

from __future__ import annotations

import duckdb

from excel_etl.leakage import MIN_ROWS, id_like, temporal
from excel_etl.ml import MAX_CLASSES, max_categories
from excel_etl.nodes.base import Column, quote, schema_of

_SPARSE = 0.5
_RARE = 0.01
_CONTINUOUS = ("FLOAT", "DOUBLE", "DECIMAL")


def _cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ").replace("\r", " ")


def _notes(column: Column, rows: int, nulls: int, distinct: int) -> tuple[list[str], str]:
    """The notes on one column and the role it suggests (`ids`, `exclude` or empty)."""
    filled = rows - nulls
    if rows and nulls == rows:
        return ["all empty"], "exclude"
    notes: list[str] = []
    role = ""
    if distinct == 1 and nulls == 0:
        notes.append("constant: carries no signal")
        role = "exclude"
    elif distinct == 1:
        notes.append(
            "one value, empty otherwise: whether it is empty may reveal the target (a column "
            "filled in after the outcome); check before using"
        )
        role = "exclude"
    if rows and nulls / rows > _SPARSE:
        notes.append(f"{nulls * 100 // rows}% empty")
        role = role or "exclude"
    elif nulls:
        notes.append("has empty values: needs a missing rule or the row is cut")
    if id_like(column, filled, distinct):
        notes.append("almost every value differs: an id? list it under ids")
        role = "ids"
    elif column.type == "VARCHAR" and distinct > max_categories():
        notes.append(
            f"more than {max_categories()} distinct values: free text or a key; exclude it, "
            "one_hot would refuse it"
        )
    if temporal(column):
        notes.append("date: not preprocessed; derive a number, or use it as the split `by`")
    return notes, role


def profile(
    con: duckdb.DuckDBPyConnection, table: str, target: str, offset: int, limit: int
) -> tuple[list[str], int | None]:
    """The lines about `table`, and the offset of the next page (None at the end): one line
    per column from `offset` (`name|type|empty|distinct|notes`), then a suggestion of ids and
    exclude, then, when `target` names a column, how it could serve as one. Raises ValueError
    for an unknown target."""
    columns = schema_of(con.table(table))
    rows = int(con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0])
    chosen = None
    if target:
        found = [c for c in columns if c.name == target] or [
            c for c in columns if c.name.casefold() == target.casefold()
        ]
        if not found:
            raise ValueError(
                f"no column {target!r}; columns: {', '.join(c.name for c in columns[:30])}"
            )
        chosen = found[0]
    page = columns[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(columns) else None
    lines = [f"{rows} rows|{len(columns)} columns|columns {offset + 1}-{offset + len(page)}"]
    lines.append("column|type|empty|distinct|notes")
    suggest: dict[str, list[str]] = {"ids": [], "exclude": []}
    if page:
        parts = ", ".join(
            f"count(*) - count({quote(c.name)}), count(DISTINCT {quote(c.name)})" for c in page
        )
        stats = con.execute(f"SELECT {parts} FROM {quote(table)}").fetchall()[0]
        for i, column in enumerate(page):
            nulls, distinct = int(stats[2 * i]), int(stats[2 * i + 1])
            notes, role = _notes(column, rows, nulls, distinct)
            if role and (chosen is None or column.name != chosen.name):
                suggest[role].append(column.name)
            line = f"{_cell(column.name)}|{column.type}|{nulls}|{distinct}|{'; '.join(notes)}"
            lines.append(line)
    hints = [
        f"{role}: {', '.join(_cell(n) for n in names)}" for role, names in suggest.items() if names
    ]
    if hints:
        lines.append(
            "suggested from the notes (the target and features are your call): " + "; ".join(hints)
        )
    if chosen is not None:
        lines.append(_target_line(con, table, chosen))
    return lines, nxt


def _target_line(con: duckdb.DuckDBPyConnection, table: str, target: Column) -> str:
    t = quote(target.name)
    filled, distinct = (
        int(v)
        for v in con.execute(
            f"SELECT count({t}), count(DISTINCT {t}) FROM {quote(table)}"
        ).fetchall()[0]
    )
    rows = int(con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0])
    head = f"target {_cell(target.name)!r} ({target.type}): "
    problems: list[str] = []
    if rows - filled:
        problems.append(
            f"{rows - filled} empty value(s): fill or drop those rows before ml; a target needs "
            "a value in every row"
        )
    if not filled:
        return head + "no values" + ("; " + "; ".join(problems) if problems else "")
    if distinct > MAX_CLASSES or target.type.startswith(_CONTINUOUS) and distinct > 2:
        kind = (
            "a number to estimate (regression)"
            if target.type.startswith(_CONTINUOUS)
            else (f"more than {MAX_CLASSES} distinct values, not a few classes")
        )
        return (
            head
            + f"{distinct} distinct values: {kind}"
            + ("; " + "; ".join(problems) if problems else "")
        )
    counts = con.execute(
        f"SELECT CAST({t} AS VARCHAR), count(*) FROM {quote(table)} WHERE {t} IS NOT NULL "
        "GROUP BY ALL ORDER BY 2 DESC, 1"
    ).fetchall()
    shown = ", ".join(f"{_cell(v)}={n}" for v, n in counts)
    rare = [_cell(v) for v, n in counts if len(counts) > 1 and n < _RARE * filled]
    if len(counts) == 1:
        problems.append("a single value: nothing to learn")
    if rare and filled >= MIN_ROWS:
        problems.append(
            f"rare class(es) {', '.join(rare[:5])} (under 1%): stratified keeps them in every "
            "part, random may not"
        )
    return head + f"classes {shown}" + ("; " + "; ".join(problems) if problems else "")
