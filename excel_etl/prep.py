"""Preprocessing for a prepared dataset: learn from the train rows, apply to any table.

`fit` reads the train partition only and returns plain JSON (the parameters it learned:
fill values, means, category lists). `apply` builds a table from a table and that JSON, so
train, validation, test and later new data all go through the same code with the same
numbers. Nothing here trains or scores a model; it changes values and columns of a dataset.

Per feature column the order is fixed: drop rows with an empty value, fill empty values, then
scale a number or encode a category. The target and the id columns are never changed.

The metadata (`preprocessing_metadata.json`, key `preprocessing`) has this layout:
`version`, `ids` and `target` (names), `features` (one entry per feature in order: `column`,
`type`, `drop_null`, and `missing` {strategy, value}, `numeric` {strategy and mean/std or
min/max} or `categorical` {strategy, categories, unknown}, each null when the column has no
such rule), and `output_columns` (name, type, role and, for an encoded column, `derived_from`).
`apply_preprocessing` checks all of it before using it. A `drop` rule removes rows of new data
too: the new table can have fewer rows than the table it came from.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from excel_etl.nodes.base import (
    INTEGER_TYPE,
    NUMERIC_TYPE,
    Column,
    Schema,
    describe,
    quote,
    schema_of,
    sql_text,
)

if TYPE_CHECKING:
    from excel_etl.ml import MlSpec

_FIT = "__ml_fit"
_ROW = "__ml_rn"
METADATA_VERSION = 1
_SHOWN = 5


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissingRule(_Model):
    """What to do with an empty value: `drop` the row, fill with a `constant` (give `value`),
    or with the train `mean` or `median` (numbers) or `mode` (text, boolean, whole numbers)."""

    strategy: Literal["drop", "constant", "mean", "median", "mode"]
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def _value_only_for_constant(self) -> MissingRule:
        if (self.strategy == "constant") != (self.value is not None):
            raise ValueError("`value` goes with strategy constant, and only with it")
        return self


class NumericRule(_Model):
    """Scale a number: `standard_scale` ((x - mean) / std), `minmax` (0 at the train minimum,
    1 at the train maximum, not clipped) or `log1p` (ln(1 + x), needs x above -1)."""

    strategy: Literal["standard_scale", "minmax", "log1p"]


class CategoricalRule(_Model):
    """Encode a category: `one_hot` (a 0/1 column per train category, named `<column>_<value>`)
    or `ordinal` (the position in the sorted train categories). A value not seen in train is
    `ignore`d (all zeros, or empty) or an `error`. An empty value is never a category."""

    strategy: Literal["one_hot", "ordinal"]
    unknown: Literal["ignore", "error"] = "ignore"
    max_categories: int = Field(default=50, ge=2, le=1000)


def _by_name(schema: Schema) -> dict[str, Column]:
    return {c.name.casefold(): c for c in schema}


def _rule[T](rules: dict[str, T], column: str) -> T | None:
    return next((r for name, r in rules.items() if name.casefold() == column.casefold()), None)


def _is_category_type(kind: str) -> bool:
    return kind in ("VARCHAR", "BOOLEAN") or bool(INTEGER_TYPE.match(kind))


def check_rules(ml: MlSpec, schema: Schema) -> list[str]:
    """What is wrong with the rules against the input's columns, as sentences that say what
    to change."""
    by_name = _by_name(schema)
    features = {f.casefold() for f in ml.features}
    problems: list[str] = []
    for section, rules in (
        ("missing", ml.missing),
        ("numerical", ml.numerical),
        ("categorical", ml.categorical),
    ):
        for column, rule in rules.items():
            if column.casefold() not in by_name:
                problems.append(f"{section} names {column!r}, which {ml.input!r} does not have")
                continue
            if column.casefold() not in features:
                problems.append(
                    f"{section} names {column!r}, which is not a feature: only features are "
                    "preprocessed (not the target, the ids or excluded columns)"
                )
                continue
            kind = by_name[column.casefold()].type
            numeric = bool(NUMERIC_TYPE.match(kind))
            if section == "numerical" and not numeric:
                problems.append(f"{rule.strategy} needs a number, but {column!r} is {kind}")
            if section == "categorical" and not _is_category_type(kind):
                problems.append(
                    f"{rule.strategy} needs text, boolean or whole numbers, "
                    f"but {column!r} is {kind}"
                )
            if section == "missing":
                problems += _check_missing(column, kind, rule)
                if rule.strategy in ("mean", "median") and _rule(ml.categorical, column):
                    problems.append(
                        f"a {rule.strategy} fill makes {column!r} decimal, which cannot also be a "
                        "category: use mode or constant"
                    )
    return problems


def _check_missing(column: str, kind: str, rule: MissingRule) -> list[str]:
    numeric = bool(NUMERIC_TYPE.match(kind))
    if rule.strategy in ("mean", "median") and not numeric:
        return [f"{rule.strategy} needs a number, but {column!r} is {kind}"]
    if rule.strategy == "mode" and not _is_category_type(kind):
        return [f"mode needs text, boolean or whole numbers, but {column!r} is {kind}"]
    if rule.strategy == "constant":
        value = rule.value
        fits = (
            (numeric and isinstance(value, int | float) and not isinstance(value, bool))
            or (kind == "VARCHAR" and isinstance(value, str))
            or (kind == "BOOLEAN" and isinstance(value, bool))
        )
        if not fits:
            return [f"the constant {value!r} does not fit {column!r}, which is {kind}"]
    return []


def planned_columns(ml: MlSpec, schema: Schema) -> Schema:
    """The columns of a partition as far as they are known before the data is read: a one_hot
    column has no fixed names until its categories are learned, so it is left out."""
    by_name = _by_name(schema)
    out = [by_name[c.casefold()] for c in ml.ids]
    for name in ml.features:
        column = by_name[name.casefold()]
        categorical, numeric = _rule(ml.categorical, name), _rule(ml.numerical, name)
        missing = _rule(ml.missing, name)
        if categorical is not None:
            if categorical.strategy == "ordinal":
                out.append(Column(column.name, "BIGINT"))
        elif numeric is not None or (missing and _fills_decimal(missing, column)):
            out.append(Column(column.name, "DOUBLE"))
        else:
            out.append(column)
    out.append(by_name[ml.target.casefold()])
    return tuple(out)


def _fills_decimal(rule: MissingRule, column: Column) -> bool:
    """Whether the fill turns the column into DOUBLE (a mean or median, or a decimal constant
    in a whole-number column)."""
    if rule.strategy in ("mean", "median"):
        return True
    whole = bool(INTEGER_TYPE.match(column.type))
    return rule.strategy == "constant" and isinstance(rule.value, float) and whole


@dataclass
class PrepResult:
    """What `apply_preprocessing` did: `problems` (then no table was made), `warnings` (things
    to read, such as rows dropped or values not seen in train) and the row counts."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_in: int = 0
    rows_out: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _finite(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None  # type: ignore[arg-type]


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"CAST({value!r} AS DOUBLE)"
    return sql_text(str(value))


def _imputed(q: str, missing: dict[str, Any] | None) -> str:
    """Column `q` (already quoted) with its empty values filled when the rule says so."""
    return q if missing is None else f"coalesce({q}, {_literal(missing['value'])})"


def _why_no_number(raw: object) -> str:
    if raw is None:
        return "has no value in train to learn from"
    return "has NaN or infinite values in train; clean them with a step first"


def _learn_missing(
    con: duckdb.DuckDBPyConnection, column: Column, rule: MissingRule
) -> tuple[object, str | None]:
    """The fill value of one column from the train rows, or the reason there is none."""
    q = quote(column.name)
    if rule.strategy == "constant":
        return rule.value, None
    if rule.strategy in ("mean", "median"):
        fn = "avg" if rule.strategy == "mean" else "median"
        raw = con.execute(f"SELECT CAST({fn}({q}) AS DOUBLE) FROM {_FIT}").fetchall()[0][0]
        value = _finite(raw)
        if value is None:
            return None, _why_no_number(raw)
        return value, None
    row = con.execute(
        f"SELECT {q} FROM {_FIT} WHERE {q} IS NOT NULL GROUP BY {q} "
        f"ORDER BY count(*) DESC, {q} LIMIT 1"
    ).fetchall()
    return (row[0][0], None) if row else (None, "has no value in train to learn from")


def fit(
    con: duckdb.DuckDBPyConnection, ml: MlSpec, schema: Schema, train: str
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Learn the parameters from table `train` (the train rows, every input column).
    Returns the metadata, the problems (then there is no metadata) and the warnings."""
    by_name = _by_name(schema)
    drops = [
        by_name[f.casefold()].name
        for f in ml.features
        if (r := _rule(ml.missing, f)) is not None and r.strategy == "drop"
    ]
    keep = " AND ".join(f"{quote(c)} IS NOT NULL" for c in drops) or "TRUE"
    problems: list[str] = []
    warnings: list[str] = []
    try:
        con.execute(f"DROP TABLE IF EXISTS {_FIT}")
        con.execute(f"CREATE TABLE {_FIT} AS SELECT * FROM {quote(train)} WHERE {keep}")
        if not con.execute(f"SELECT count(*) FROM {_FIT}").fetchall()[0][0]:
            return None, ["no train rows are left after dropping rows with empty values"], []
        features = []
        for name in ml.features:
            column = by_name[name.casefold()]
            entry, found, notes = _fit_column(con, ml, column)
            features.append(entry)
            problems += [f"{column.name!r} {p}" for p in found]
            warnings += [f"{column.name!r} {w}" for w in notes]
        if problems:
            return None, problems, warnings
        if repeat := _repeated_names(ml, by_name, features):
            return None, [repeat], warnings
        metadata = {
            "version": METADATA_VERSION,
            "ids": [by_name[c.casefold()].name for c in ml.ids],
            "target": by_name[ml.target.casefold()].name,
            "features": features,
            "output_columns": [],
        }
        return metadata, [], warnings
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_FIT}")


def _repeated_names(
    ml: MlSpec, by_name: dict[str, Column], features: list[dict[str, Any]]
) -> str | None:
    """A message when two output columns would get the same name (letter case ignored), such
    as the categories `A` and `a`, or an encoded name equal to a column that exists."""
    names = [by_name[c.casefold()].name for c in ml.ids]
    names += [out for entry in features for out, _ in _items(entry, "x")]
    names.append(by_name[ml.target.casefold()].name)
    lower = [n.casefold() for n in names]
    clash = sorted({n for n, key in zip(names, lower, strict=True) if lower.count(key) > 1})
    if not clash:
        return None
    return (
        f"output columns would repeat: {', '.join(clash[:_SHOWN])}; categories that differ only "
        "in letter case, or an encoded name equal to an existing column, cannot be told apart: "
        "clean the values with a step first"
    )


def _fit_column(
    con: duckdb.DuckDBPyConnection, ml: MlSpec, column: Column
) -> tuple[dict[str, Any], list[str], list[str]]:
    miss = _rule(ml.missing, column.name)
    numeric = _rule(ml.numerical, column.name)
    categorical = _rule(ml.categorical, column.name)
    entry: dict[str, Any] = {
        "column": column.name,
        "type": column.type,
        "drop_null": miss is not None and miss.strategy == "drop",
        "missing": None,
        "numeric": None,
        "categorical": None,
    }
    problems: list[str] = []
    warnings: list[str] = []
    if miss is not None and miss.strategy != "drop":
        value, why = _learn_missing(con, column, miss)
        if why:
            return entry, [why], []
        entry["missing"] = {"strategy": miss.strategy, "value": value}
    expr = _imputed(quote(column.name), entry["missing"])
    if numeric is not None:
        stats = con.execute(
            f"SELECT avg(CAST({expr} AS DOUBLE)), stddev_pop(CAST({expr} AS DOUBLE)), "
            f"min(CAST({expr} AS DOUBLE)), max(CAST({expr} AS DOUBLE)) FROM {_FIT}"
        ).fetchall()[0]
        mean, std, low, high = (_finite(v) for v in stats)
        if mean is None or std is None or low is None or high is None:
            return entry, [_why_no_number(None if stats[0] is None else float("nan"))], []
        params: dict[str, Any] = {"strategy": numeric.strategy}
        if numeric.strategy == "standard_scale":
            params |= {"mean": mean, "std": std}
            if std == 0:
                warnings.append("is constant in train: standard_scale turns it into 0.0")
        elif numeric.strategy == "minmax":
            params |= {"min": low, "max": high}
            if low == high:
                warnings.append("is constant in train: minmax turns it into 0.0")
        elif low <= -1:
            problems.append("has values at or below -1 in train: log1p needs values above -1")
        entry["numeric"] = params
    if categorical is not None:
        rows = con.execute(
            f"SELECT CAST(e AS VARCHAR) FROM (SELECT DISTINCT {expr} AS e FROM {_FIT} "
            f"WHERE {expr} IS NOT NULL) ORDER BY e"
        ).fetchall()
        values = [r[0] for r in rows]
        if not values:
            return entry, ["has no value in train to learn from"], []
        if len(values) > categorical.max_categories:
            problems.append(
                f"has {len(values)} distinct values in train, above max_categories "
                f"{categorical.max_categories}: raise it, or leave the column out of the features"
            )
        entry["categorical"] = {
            "strategy": categorical.strategy,
            "categories": values,
            "unknown": categorical.unknown,
        }
    return entry, problems, warnings


def _items(entry: dict[str, Any], q: str) -> list[tuple[str, str]]:
    """The (output name, SQL) pairs one feature column becomes."""
    column = entry["column"]
    expr = _imputed(q, entry["missing"])
    numeric, categorical = entry["numeric"], entry["categorical"]
    if numeric is not None:
        value = f"CAST({expr} AS DOUBLE)"
        match numeric["strategy"]:
            case "standard_scale":
                std = _literal(numeric["std"])
                mean = _literal(numeric["mean"])
                sql = f"CASE WHEN {std} = 0 THEN 0.0 ELSE ({value} - {mean}) / {std} END"
            case "minmax":
                low, high = _literal(numeric["min"]), _literal(numeric["max"])
                span = f"({high} - {low})"
                sql = f"CASE WHEN {high} = {low} THEN 0.0 ELSE ({value} - {low}) / {span} END"
            case _:
                sql = f"ln(1 + {value})"
        return [(column, sql)]
    if categorical is not None:
        text = f"CAST({expr} AS VARCHAR)"
        cats = categorical["categories"]
        if categorical["strategy"] == "ordinal":
            whens = " ".join(f"WHEN {_literal(c)} THEN {i}" for i, c in enumerate(cats))
            return [(column, f"CAST(CASE {text} {whens} END AS BIGINT)")]
        return [
            (
                f"{column}_{c}",
                f"CAST(CASE WHEN {text} = {_literal(c)} THEN 1 ELSE 0 END AS BIGINT)",
            )
            for c in cats
        ]
    return [(column, expr)]


def apply(
    con: duckdb.DuckDBPyConnection, table: str, metadata: dict[str, Any], name: str
) -> list[str]:
    """Create table `name` from table `table` using `metadata`. Columns are matched by name
    ignoring letter case; the ids and the target are kept when the table has them (new data
    often has no target). Returns the problems; then no table was made."""
    if name.casefold() == table.casefold():
        return [f"the new table needs another name than {table!r}: the input is kept as it is"]
    schema = schema_of(con.table(table))
    by_name = _by_name(schema)
    problems: list[str] = []
    items: list[tuple[str, str]] = []
    filters: list[str] = []
    checks: list[tuple[str, str, str]] = []
    for ident in metadata["ids"]:
        if ident.casefold() in by_name:
            items.append((ident, f"b.{quote(by_name[ident.casefold()].name)}"))
    for entry in metadata["features"]:
        found = by_name.get(entry["column"].casefold())
        if found is None:
            problems.append(f"column {entry['column']!r} is missing; the preprocessing needs it")
            continue
        problems += _check_type(entry, found)
        q = f"b.{quote(found.name)}"
        if entry["drop_null"]:
            filters.append(f"{q} IS NOT NULL")
        items += _items(entry, q)
        checks += _row_checks(entry, q)
    target = metadata["target"]
    if target and target.casefold() in by_name:
        items.append((target, f"b.{quote(by_name[target.casefold()].name)}"))
    names = [n.casefold() for n, _ in items]
    if clash := sorted({n for n in names if names.count(n) > 1}):
        problems.append(
            f"output columns would repeat: {', '.join(clash[:_SHOWN])}; rename a column"
        )
    if problems:
        return problems
    where = " AND ".join(filters) or "TRUE"
    for label, bad, example in checks:
        count, seen = con.execute(
            f"SELECT count(*), list(DISTINCT {example})[1:{_SHOWN}] FROM {quote(table)} b "
            f"WHERE {where} AND {bad}"
        ).fetchall()[0]
        if count:
            problems.append(f"{label}: {count} row(s), e.g. {', '.join(map(str, seen or []))}")
    if problems:
        return problems
    select = ", ".join(f"{sql} AS {quote(out)}" for out, sql in items)
    order = f" ORDER BY b.{_ROW}" if _ROW in by_name else ""
    con.execute(f"DROP TABLE IF EXISTS {quote(name)}")
    con.execute(
        f"CREATE TABLE {quote(name)} AS SELECT {select} FROM {quote(table)} b WHERE {where}{order}"
    )
    return []


def _check_type(entry: dict[str, Any], found: Column) -> list[str]:
    numeric = entry["numeric"] is not None or (
        entry["missing"] is not None and entry["missing"]["strategy"] in ("mean", "median")
    )
    if numeric and not NUMERIC_TYPE.match(found.type):
        return [f"column {found.name!r} is {found.type}, but the preprocessing needs a number"]
    if entry["categorical"] is not None and not _is_category_type(found.type):
        return [f"column {found.name!r} is {found.type}, but the preprocessing needs a category"]
    return []


def _row_checks(entry: dict[str, Any], q: str) -> list[tuple[str, str, str]]:
    """(message, SQL condition on a bad row, SQL of the example) for values that cannot be
    encoded: a value below the log1p range, or a category not seen in train under `error`."""
    out: list[tuple[str, str, str]] = []
    column = entry["column"]
    expr = _imputed(q, entry["missing"])
    numeric, categorical = entry["numeric"], entry["categorical"]
    if numeric is not None and numeric["strategy"] == "log1p":
        value = f"CAST({expr} AS DOUBLE)"
        out.append((f"{column!r} has values at or below -1 (log1p)", f"{value} <= -1", value))
    if categorical is not None and categorical["unknown"] == "error":
        known = ", ".join(_literal(c) for c in categorical["categories"])
        text = f"CAST({expr} AS VARCHAR)"
        out.append(
            (
                f"{column!r} has values not seen in train",
                f"{expr} IS NOT NULL AND {text} NOT IN ({known})",
                text,
            )
        )
    return out


class _NumericMeta(_Model):
    strategy: Literal["standard_scale", "minmax", "log1p"]
    mean: float | None = Field(default=None, allow_inf_nan=False)
    std: float | None = Field(default=None, allow_inf_nan=False)
    min: float | None = Field(default=None, allow_inf_nan=False)
    max: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _has_its_numbers(self) -> _NumericMeta:
        needs = {"standard_scale": ("mean", "std"), "minmax": ("min", "max"), "log1p": ()}
        if lacking := [k for k in needs[self.strategy] if getattr(self, k) is None]:
            raise ValueError(f"{self.strategy} needs {', '.join(lacking)}")
        return self


class _CategoricalMeta(_Model):
    strategy: Literal["one_hot", "ordinal"]
    categories: list[str] = Field(min_length=1)
    unknown: Literal["ignore", "error"]


class _MissingMeta(_Model):
    strategy: Literal["constant", "mean", "median", "mode"]
    value: str | int | float | bool

    @model_validator(mode="after")
    def _finite_value(self) -> _MissingMeta:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be a finite number")
        return self


class _FeatureMeta(_Model):
    column: str = Field(min_length=1)
    type: str
    drop_null: bool
    missing: _MissingMeta | None
    numeric: _NumericMeta | None
    categorical: _CategoricalMeta | None


class _Metadata(_Model):
    version: int
    ids: list[str]
    target: str | None
    features: list[_FeatureMeta]
    output_columns: list[dict[str, str]] = []


def apply_preprocessing(
    con: duckdb.DuckDBPyConnection, table: str, metadata: str | dict[str, Any], name: str
) -> PrepResult:
    """Apply stored preprocessing to a table of new data: `metadata` is the text or the parsed
    content of `preprocessing_metadata.json`. Creates table `name` and returns a PrepResult:
    the `problems` (empty when it worked), `warnings` (rows dropped by a `drop` rule, values
    not seen in train that were encoded as unknown) and the row counts. The new table has the
    same columns as the train table, except an id or the target the new data does not have."""
    problems = _check_metadata(metadata)
    if isinstance(problems, list):
        return PrepResult(problems=problems)
    try:
        found = apply(con, table, problems.model_dump(), name)
        if found:
            return PrepResult(problems=found)
        return _summarize(con, table, name, problems.model_dump())
    except duckdb.Error as e:
        return PrepResult(problems=[str(e).splitlines()[0]])


def _check_metadata(metadata: str | dict[str, Any]) -> list[str] | _Metadata:
    """The metadata as a checked model, or the problems with it."""
    try:
        parsed = json.loads(metadata) if isinstance(metadata, str) else metadata
    except ValueError as e:
        return [f"the preprocessing metadata is not JSON: {e}"]
    inner = parsed.get("preprocessing") if isinstance(parsed, dict) else None
    inner = inner if inner is not None else parsed
    needed = ("ids", "target", "features")
    if not isinstance(inner, dict) or any(k not in inner for k in needed):
        return ["the preprocessing metadata needs ids, target and features: is it the file?"]
    if inner.get("version") != METADATA_VERSION:
        return [
            f"the preprocessing metadata has version {inner.get('version')!r}, "
            f"not {METADATA_VERSION}"
        ]
    try:
        return _Metadata.model_validate(inner)
    except ValidationError as e:
        return [f"the preprocessing metadata is not valid:\n{describe(e, 'metadata')}"]


def _summarize(
    con: duckdb.DuckDBPyConnection, table: str, name: str, metadata: dict[str, Any]
) -> PrepResult:
    """Row counts and the warnings a person should read after preparing new data."""
    rows_in = con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0]
    rows_out = con.execute(f"SELECT count(*) FROM {quote(name)}").fetchall()[0][0]
    by_name = _by_name(schema_of(con.table(table)))
    warnings: list[str] = []
    if rows_out < rows_in:
        warnings.append(
            f"{rows_in - rows_out} of {rows_in} row(s) were dropped because a column with a "
            "drop rule is empty: match the predictions to the rows that are left"
        )
    for entry in metadata["features"]:
        categorical = entry["categorical"]
        if categorical is None or categorical["unknown"] != "ignore":
            continue
        q = f"{quote(by_name[entry['column'].casefold()].name)}"
        expr = _imputed(q, entry["missing"])
        known = ", ".join(_literal(c) for c in categorical["categories"])
        count, seen = con.execute(
            f"SELECT count(*), list(DISTINCT CAST({expr} AS VARCHAR))[1:{_SHOWN}] "
            f"FROM {quote(table)} WHERE {expr} IS NOT NULL AND CAST({expr} AS VARCHAR) "
            f"NOT IN ({known})"
        ).fetchall()[0]
        if count:
            warnings.append(
                f"{entry['column']!r}: {count} row(s) have a value not seen in train "
                f"({', '.join(map(str, seen))}) and are encoded as unknown"
            )
    return PrepResult([], warnings, rows_in, rows_out)
