"""Prepare a dataset for machine learning: declare roles, split, write the artifacts.

Only the data is prepared; nothing here trains or scores a model. The `ml` section of a spec
reads one table (`input`), keeps the columns declared as `ids`, `features` and `target`, and
cuts the rows into `<name>_train`, `<name>_validation` (when `validation_size` is set) and
`<name>_test`. Every column of the input must be placed: a column left out is an error, so
dropping one is always a decision.

A split is reproducible from its configuration: rows are ordered by the md5 of the seed and
the row position (or the group value), never by duckdb's `random()`, so the same input rows
in the same order, with the same seed and settings, give the same partitions on any duckdb
version. A step that does not fix its row order (a join, a distinct) gives no such promise:
add a `sort` step before `ml` when the order matters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args

import duckdb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

from moyung import leakage
from moyung.nodes.base import (
    _NAME_PATTERN,
    NUMERIC_TYPE,
    Column,
    Schema,
    quote,
    schema_of,
    sql_text,
)
from moyung.prep import (
    METADATA_VERSION,
    CategoricalRule,
    MissingRule,
    NumericRule,
    apply,
    check_rules,
    fit,
)

Ident = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]
PartName = Literal["train", "validation", "test"]

_ROW = "__ml_rn"
_BASE = "__ml_base"
_PART = "__ml_part"
_CONTINUOUS = ("FLOAT", "DOUBLE", "DECIMAL")
MAX_CLASSES = 20
ARTIFACT_NOTES = {
    "preprocessing_metadata.json": "split settings and what was learned from train (apply it "
    "to new data with apply_ml_preprocessing)",
    "feature_schema.json": "each output column's type and role (id, feature, target)",
    "preparation_report.json": "rows per partition, dropped columns, empty values, rows cut, "
    "target classes per partition",
}
ARTIFACT_FILES = tuple(ARTIFACT_NOTES)
EXAMPLE = {
    "name": "churn",
    "input": "clean",
    "target": "churn",
    "features": ["age", "spend", "segment"],
    "ids": ["customer_id"],
    "exclude": ["note"],
    "split": {"strategy": "stratified", "test_size": 0.2, "validation_size": 0.1, "seed": 42},
    "missing": {"age": {"strategy": "median"}},
    "numerical": {"spend": {"strategy": "standard_scale"}},
    "categorical": {"segment": {"strategy": "one_hot", "unknown": "ignore"}},
}


def _choices(rule: type[BaseModel]) -> str:
    return " | ".join(get_args(rule.model_fields["strategy"].annotation))


def max_categories() -> int:
    return CategoricalRule.model_fields["max_categories"].default


def describe() -> str:
    """What an agent needs to write the `ml` section (the same facts as the model's fields)."""
    return "\n".join(
        [
            "ml: a section of the spec (not a step) that cuts one table into train, validation "
            "and test tables. It prepares data only; nothing here trains or scores a model, and "
            "it does not fill, encode or scale values.",
            "fields (* required): name* (tables are <name>_train, <name>_validation, <name>_test), "
            "input* (a source or step), target*, features* (list), ids (list: kept for tracing, "
            "not features), exclude (list: dropped on purpose), split*",
            "split fields: strategy* (random | stratified: keeps the target's class balance | "
            "time: the newest rows go to test | group: a group never spans two partitions), "
            "test_size* (between 0 and 1), validation_size (default 0; the validation table "
            "exists only when it is above 0), seed* (any integer, such as 42), by (the column "
            "for time and group; refused for random and stratified)",
            "every column of the input must be placed in target, features, ids or exclude.",
            "to prepare new rows later with the same numbers, give preprocessing_metadata.json "
            "to apply_ml_preprocessing, never re-run the spec on the new "
            "rows: that would learn new numbers.",
            "preprocessing (optional, learned from the train rows only, applied to all "
            f"partitions): missing = {{column: {{strategy: {_choices(MissingRule)}, value for "
            f"constant}}}}; numerical = {{column: {{strategy: {_choices(NumericRule)}}}}}; "
            f"categorical = {{column: {{strategy: {_choices(CategoricalRule)}, unknown: ignore | "
            f"error, max_categories: {max_categories()}}}}}. Only features can have rules. "
            "The learned numbers are written to preprocessing_metadata.json; a one_hot column "
            "is named <column>_<value> and exists only after the run.",
            "example: " + json.dumps(EXAMPLE),
        ]
    )


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Split(_Model):
    """How rows are cut. `random` and `stratified` (balanced on the target) need no `by`;
    `time` (the newest rows go to test) and `group` (a group never spans two partitions)
    need `by`. `seed` is required so the cut can be repeated."""

    strategy: Literal["random", "stratified", "time", "group"]
    by: str | None = Field(default=None, min_length=1)
    test_size: float = Field(gt=0, lt=1)
    validation_size: float = Field(default=0, ge=0, lt=1)
    seed: Annotated[int, Field(strict=True)]

    @model_validator(mode="after")
    def _fits_strategy(self) -> Split:
        if self.strategy in ("time", "group") and self.by is None:
            raise ValueError(f"a {self.strategy} split needs `by`: the column to order or group by")
        if self.strategy in ("random", "stratified") and self.by is not None:
            raise ValueError(f"a {self.strategy} split takes no `by` (stratified uses the target)")
        if self.test_size + self.validation_size >= 1:
            raise ValueError("test_size + validation_size must stay below 1: train needs rows")
        return self


class MlSpec(_Model):
    """The dataset to prepare: `input` is a source or step; `name` gives the output tables
    `<name>_train`, `<name>_validation`, `<name>_test`."""

    name: Annotated[str, StringConstraints(pattern=_NAME_PATTERN, max_length=48)]
    input: Ident
    target: str = Field(min_length=1)
    features: list[str] = Field(min_length=1)
    ids: list[str] = []
    exclude: list[str] = []
    split: Split
    missing: dict[str, MissingRule] = {}
    numerical: dict[str, NumericRule] = {}
    categorical: dict[str, CategoricalRule] = {}

    @model_serializer(mode="wrap")
    def _omit_empty_rules(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for key in ("missing", "numerical", "categorical"):
            if not data.get(key):
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def _one_transform_per_column(self) -> MlSpec:
        both = {c.casefold() for c in self.numerical} & {c.casefold() for c in self.categorical}
        if both:
            raise ValueError(
                f"column {sorted(both)[0]!r} is under numerical and categorical: it is one or "
                "the other"
            )
        for section in (self.missing, self.numerical, self.categorical):
            keys = [c.casefold() for c in section]
            if len(keys) != len(set(keys)):
                raise ValueError("a column is listed twice (letter case is ignored)")
        return self

    @model_validator(mode="after")
    def _one_role_per_column(self) -> MlSpec:
        seen: dict[str, str] = {}
        roles = (
            ("target", [self.target]),
            ("features", self.features),
            ("ids", self.ids),
            ("exclude", self.exclude),
        )
        for role, columns in roles:
            for column in columns:
                twice = [c.casefold() for c in columns].count(column.casefold()) > 1
                if (other := seen.setdefault(column.casefold(), role)) != role or twice:
                    raise ValueError(
                        f"column {column!r} is listed twice ({other} and {role}); give it one role"
                    )
        return self

    def tables(self) -> dict[PartName, str]:
        """The output table of each partition that exists."""
        parts: dict[PartName, str] = {"train": f"{self.name}_train"}
        if self.split.validation_size > 0:
            parts["validation"] = f"{self.name}_validation"
        parts["test"] = f"{self.name}_test"
        return parts


def _lookup(schema: Schema) -> dict[str, Column]:
    return {c.name.casefold(): c for c in schema}


def check_columns(ml: MlSpec, schema: Schema) -> list[str]:
    """What is wrong with the declaration against the input's columns, as sentences that say
    what to change. Empty when the declaration fits."""
    by_name = _lookup(schema)
    if len(by_name) < len(schema):
        names = [c.name for c in schema]
        clash = sorted({n for n in names if [m.casefold() for m in names].count(n.casefold()) > 1})
        return [f"columns that differ only in letter case: {', '.join(clash)}; rename them first"]
    declared = {
        c.casefold(): role
        for role, columns in (
            ("target", [ml.target]),
            ("features", ml.features),
            ("ids", ml.ids),
            ("exclude", ml.exclude),
        )
        for c in columns
    }
    shown = ", ".join(c.name for c in schema[:12]) + (", ..." if len(schema) > 12 else "")
    problems = [
        f"{role} names {c!r}, which {ml.input!r} does not have; columns: {shown}"
        for role, columns in (
            ("target", [ml.target]),
            ("features", ml.features),
            ("ids", ml.ids),
            ("exclude", ml.exclude),
            ("split.by", [ml.split.by] if ml.split.by else []),
        )
        for c in columns
        if c.casefold() not in by_name
    ]
    if problems:
        return problems
    if reserved := [c.name for c in schema if c.name.startswith("__ml")]:
        problems.append(f"column names starting with __ml are reserved: {', '.join(reserved)}")
    if loose := [c.name for c in schema if c.name.casefold() not in declared]:
        problems.append(
            f"columns not placed: {', '.join(loose[:12])}; list each under features, ids or "
            "exclude (leaving one out has to be a decision)"
        )
    target = by_name[ml.target.casefold()]
    if ml.split.strategy == "stratified" and target.type.startswith(_CONTINUOUS):
        problems.append(
            f"stratified needs a category target, but {target.name!r} is {target.type}; "
            "use a random split, or turn the target into classes first"
        )
    problems += check_rules(ml, schema)
    if ml.split.by is not None:
        column = by_name[ml.split.by.casefold()]
        if ml.split.strategy == "time" and not (
            column.type.startswith(("DATE", "TIMESTAMP")) or NUMERIC_TYPE.match(column.type)
        ):
            problems.append(
                f"a time split needs a date or number column, but {column.name!r} is {column.type}"
            )
    return problems


@dataclass
class MlResult:
    """What a split produced, for the report and the artifacts."""

    rows_in: int
    rows: dict[str, int]
    columns: Schema
    classes: dict[str, dict[str, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    cut: dict[str, int] = field(default_factory=dict)
    missing: dict[str, dict[str, int]] = field(default_factory=dict)


def _u_expression(ml: MlSpec) -> str:
    key = f"md5({sql_text(str(ml.split.seed))} || ':' || CAST({_ROW} AS VARCHAR))"
    match ml.split.strategy:
        case "random":
            order = f"ORDER BY {key}, {_ROW}"
            return f"(row_number() OVER ({order}) - 0.5)::DOUBLE / count(*) OVER ()"
        case "stratified":
            part = f"PARTITION BY {quote(ml.target)}"
            order = f"{part} ORDER BY {key}, {_ROW}"
            return f"(row_number() OVER ({order}) - 0.5)::DOUBLE / count(*) OVER ({part})"
        case _:
            by = quote(ml.split.by or "")
            order = f"ORDER BY {by} DESC"
            return f"(rank() OVER ({order}) - 1)::DOUBLE / count(*) OVER ()"


def _part_expression(ml: MlSpec) -> str:
    test = ml.split.test_size
    both = round(test + ml.split.validation_size, 10)
    return (
        f"CASE WHEN __ml_u < {test!r} THEN 'test' "
        f"WHEN __ml_u < {both!r} THEN 'validation' ELSE 'train' END"
    )


def split(
    con: duckdb.DuckDBPyConnection, ml: MlSpec, schema: Schema
) -> tuple[MlResult | None, list[str]]:
    """Cut `ml.input` into the partition tables inside `con`. Returns the result, or None and
    the problems (a NULL where a value is needed, a partition left empty)."""
    source, strategy = quote(ml.input), ml.split.strategy
    needs = [ml.target, *([ml.split.by] if ml.split.by else [])]
    by_name = _lookup(schema)
    problems: list[str] = []
    con.execute(f"DROP TABLE IF EXISTS {_BASE}")
    con.execute(f"DROP TABLE IF EXISTS {_PART}")
    con.execute(f"CREATE TABLE {_BASE} AS SELECT row_number() OVER () AS {_ROW}, * FROM {source}")
    try:
        total = con.execute(f"SELECT count(*) FROM {_BASE}").fetchall()[0][0]
        for needed_name in needs:
            needed = quote(by_name[needed_name.casefold()].name)
            nulls = con.execute(f"SELECT count(*) FROM {_BASE} WHERE {needed} IS NULL").fetchall()
            if nulls[0][0]:
                why = "a target" if needed_name == ml.target else f"the {strategy} split"
                return None, [
                    f"{needed_name!r} has {nulls[0][0]} empty value(s) and {why} needs one in "
                    "every row; fill or drop those rows first"
                ]
        if strategy == "group":
            by = quote(by_name[(ml.split.by or "").casefold()].name)
            key = f"md5({sql_text(str(ml.split.seed))} || ':' || CAST(__ml_g AS VARCHAR))"
            con.execute(
                f"CREATE TABLE {_PART} AS SELECT b.{_ROW} AS {_ROW}, "
                f"{_part_expression(ml)} AS __ml_p "
                f"FROM {_BASE} b JOIN (SELECT __ml_g, (row_number() OVER (ORDER BY {key}, "
                f"CAST(__ml_g AS VARCHAR)) - 0.5)::DOUBLE / count(*) OVER () AS __ml_u "
                f"FROM (SELECT DISTINCT {by} AS __ml_g FROM {_BASE})) gu ON b.{by} = gu.__ml_g"
            )
        else:
            con.execute(
                f"CREATE TABLE {_PART} AS SELECT {_ROW}, {_part_expression(ml)} AS __ml_p "
                f"FROM (SELECT {_ROW}, {_u_expression(ml)} AS __ml_u FROM {_BASE})"
            )
        counts = dict(
            con.execute(f"SELECT __ml_p, count(*) FROM {_PART} GROUP BY __ml_p").fetchall()
        )
        tables = ml.tables()
        for part in tables:
            if not counts.get(part):
                problems.append(
                    f"the split leaves {part} with no rows ({total} rows in all); change "
                    "test_size or validation_size, or give it more rows"
                )
        if problems:
            return None, problems
        classes, distinct = _classes(con, ml, by_name)
        result, problems = _prepare(con, ml, schema, tables, counts)
        if result is None:
            return None, problems
        result.rows_in = total
        result.classes = classes
        found = leakage.check(
            con,
            base=_BASE,
            part=_PART,
            row=_ROW,
            columns=list(schema),
            target=by_name[ml.target.casefold()],
            features=[by_name[c.casefold()] for c in ml.features],
            ids=[by_name[c.casefold()] for c in ml.ids],
            by=by_name[ml.split.by.casefold()] if ml.split.by else None,
            strategy=strategy,
        )
        result.warnings = [
            *_warnings(ml, classes, distinct, [by_name[c.casefold()] for c in ml.features]),
            *found,
            *result.warnings,
        ]
        return result, []
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_BASE}")
        con.execute(f"DROP TABLE IF EXISTS {_PART}")


def _prepare(
    con: duckdb.DuckDBPyConnection,
    ml: MlSpec,
    schema: Schema,
    tables: dict[PartName, str],
    counts: dict[str, int],
) -> tuple[MlResult | None, list[str]]:
    """Make the partition tables: the raw rows of each partition, then the preprocessing
    learned from the train rows applied to every one of them."""
    raws = {part: f"__ml_raw_{part}" for part in tables}
    by_name = _lookup(schema)
    try:
        for part, raw in raws.items():
            con.execute(f"DROP TABLE IF EXISTS {raw}")
            con.execute(
                f"CREATE TABLE {raw} AS SELECT b.* FROM {_BASE} b "
                f"JOIN {_PART} p USING ({_ROW}) WHERE p.__ml_p = '{part}'"
            )
        metadata, problems, warnings = fit(con, ml, schema, raws["train"])
        if metadata is None:
            return None, problems
        for part, table in tables.items():
            if found := apply(con, raws[part], metadata, table):
                return None, [f"{part}: {p}" for p in found]
        final = {
            part: con.execute(f"SELECT count(*) FROM {quote(table)}").fetchall()[0][0]
            for part, table in tables.items()
        }
        if empty := [p for p, n in final.items() if not n]:
            return None, [f"dropping rows with empty values leaves {', '.join(empty)} with no rows"]
        columns = schema_of(con.table(tables["train"]))
        metadata["output_columns"] = _output_columns(ml, metadata, columns)
        missing = _missing_counts(con, ml, by_name, raws)
        result = MlResult(0, final, columns, warnings=warnings, metadata=metadata, missing=missing)
        result.cut = {
            part: counts[part] - final[part] for part in tables if counts[part] != final[part]
        }
        return result, []
    finally:
        for raw in raws.values():
            con.execute(f"DROP TABLE IF EXISTS {raw}")


def _output_columns(ml: MlSpec, metadata: dict[str, Any], columns: Schema) -> list[dict[str, str]]:
    """Each output column with its type and role; an encoded column names its source."""
    ids, target = {c.casefold() for c in ml.ids}, ml.target.casefold()
    derived = {
        f"{entry['column']}_{value}": entry["column"]
        for entry in metadata["features"]
        if entry["categorical"] and entry["categorical"]["strategy"] == "one_hot"
        for value in entry["categorical"]["categories"]
    }
    out = []
    for column in columns:
        key = column.name.casefold()
        role = "id" if key in ids else "target" if key == target else "feature"
        item = {"name": column.name, "type": column.type, "role": role}
        if column.name in derived:
            item["derived_from"] = derived[column.name]
        out.append(item)
    return out


def _missing_counts(
    con: duckdb.DuckDBPyConnection,
    ml: MlSpec,
    by_name: dict[str, Column],
    raws: dict[PartName, str],
) -> dict[str, dict[str, int]]:
    """Empty values per feature column and partition, before anything is filled."""
    names = [by_name[f.casefold()].name for f in ml.features]
    counts = ", ".join(f"count(*) FILTER (WHERE {quote(n)} IS NULL)" for n in names)
    out: dict[str, dict[str, int]] = {}
    for part, raw in raws.items():
        row = con.execute(f"SELECT {counts} FROM {raw}").fetchall()[0]
        for name, n in zip(names, row, strict=True):
            if n:
                out.setdefault(name, {})[part] = n
    return out


def _warnings(
    ml: MlSpec, classes: dict[str, dict[str, int]], distinct: int, features: list[Column]
) -> list[str]:
    """Things a split got through but the author should look at."""
    found: list[str] = []
    if dates := [c.name for c in features if leakage.temporal(c)]:
        found.append(
            f"date feature(s) {', '.join(repr(d) for d in dates[:5])} are not preprocessed and a "
            "model cannot use a date as it is: derive a number from them (derive) or move them "
            "to exclude or ids"
        )
    if ml.split.strategy == "stratified" and distinct > MAX_CLASSES:
        found.append(
            f"{ml.target!r} has {distinct} distinct values; a stratified split keeps the share "
            "of each one, which is probably not what you want: use a random split"
        )
    if classes:
        absent = sorted(
            {v for part, counts in classes.items() if part != "train" for v in counts}
            - set(classes.get("train", {}))
        )
        if absent:
            found.append(
                f"{ml.target!r} value(s) {', '.join(absent[:5])} are not in train, only in the "
                "other partitions; a class that rare cannot be learned from"
            )
    return found


def _classes(
    con: duckdb.DuckDBPyConnection, ml: MlSpec, by_name: dict[str, Column]
) -> tuple[dict[str, dict[str, int]], int]:
    """The target's value counts per partition (only when it has few distinct values), and
    how many distinct values it has."""
    target = quote(by_name[ml.target.casefold()].name)
    distinct = con.execute(f"SELECT count(DISTINCT {target}) FROM {_BASE}").fetchall()[0][0]
    if distinct > MAX_CLASSES:
        return {}, distinct
    rows = con.execute(
        f"SELECT p.__ml_p, CAST(b.{target} AS VARCHAR), count(*) FROM {_BASE} b "
        f"JOIN {_PART} p USING ({_ROW}) WHERE b.{target} IS NOT NULL GROUP BY ALL ORDER BY 1, 2"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for part, value, n in rows:
        out.setdefault(part, {})[value] = n
    return out, distinct


def _dump(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def artifacts(ml: MlSpec, schema: Schema, result: MlResult) -> dict[str, str]:
    """The JSON files that describe the prepared dataset, by file name. Keys are sorted and
    nothing depends on the clock, so the same run gives byte-identical files."""
    by_name = _lookup(schema)
    declared = {c.casefold() for c in (*ml.ids, *ml.features, ml.target)}
    dropped = [c.name for c in schema if c.name.casefold() not in declared]
    metadata = {
        "version": METADATA_VERSION,
        "dataset": ml.name,
        "split": {
            "strategy": ml.split.strategy,
            "by": ml.split.by,
            "test_size": ml.split.test_size,
            "validation_size": ml.split.validation_size,
            "seed": ml.split.seed,
        },
        "preprocessing": result.metadata,
    }
    feature_schema = {
        "version": METADATA_VERSION,
        "dataset": ml.name,
        "target": by_name[ml.target.casefold()].name,
        "columns": result.metadata["output_columns"],
    }
    report = {
        "version": METADATA_VERSION,
        "dataset": ml.name,
        "input": ml.input,
        "rows_in": result.rows_in,
        "partitions": result.rows,
        "dropped_columns": sorted(dropped),
        "rows_cut_for_empty_values": result.cut,
        "empty_values": result.missing,
        "target_classes": result.classes,
        "target_type": by_name[ml.target.casefold()].type,
    }
    return {
        ARTIFACT_FILES[0]: _dump(metadata),
        ARTIFACT_FILES[1]: _dump(feature_schema),
        ARTIFACT_FILES[2]: _dump(report),
    }
