# Excel shape to nodes

Each block is a shape that raw workbooks have, and the steps that fix it. Column names are
placeholders: copy the real ones from a tool result. Every snippet here is parsed and checked
by `tests/test_skills.py`, so it stays valid when a node changes.

## Months (or products) as columns

Wide table `Plant | JAN|Ton | JAN|Rate | FEB|Ton | FEB|Rate`. Two steps: columns to rows,
then split the old name.

```json
{"id": "long", "node": "unpivot", "inputs": ["raw"],
 "config": {"keep": ["Plant"], "pattern": "[|]", "name_column": "col", "value_column": "val",
            "value_type": "DOUBLE"}}
```

```json
{"id": "split", "node": "split_header_token", "inputs": ["long"],
 "config": {"column": "col", "into": ["month", "measure"], "separator": "|", "drop_source": true}}
```

When the same measures repeat per block (`A|Ton, A|Rate, B|Ton, B|Rate`), one row per block
with one column per measure is usually what you want:

```json
{"id": "blocks", "node": "unpivot_groups", "inputs": ["raw"],
 "config": {"pattern": "^(?P<group>.+)[|](?P<measure>[^|]+)$", "keep": ["Plant"],
            "group_column": "month"}}
```

## Merged cells leave labels blank

```json
{"id": "filled", "node": "fill_down", "inputs": ["raw"], "config": {"columns": ["Area"]}}
```

## Total and subtotal rows among the data

```json
{"id": "rows", "node": "drop_rows", "inputs": ["filled"],
 "config": {"column": "Item", "drop_regex": "(?i)^total"}}
```

A date column holding `JAN` labels: `{"column": "Date", "keep_type": "date"}`. Reconcile the
total row against the detail rows (see Checks) before you drop it.

## Outline codes (1, 1.1, 1.1.1)

```json
{"id": "tree", "node": "parse_hierarchy", "inputs": ["rows"],
 "config": {"column": "Code", "separator": "."}}
```

## Text and dates held as text

```json
{"id": "dated", "node": "derive", "inputs": ["rows"],
 "config": {"name": "day", "expr": "try_strptime(\"Date\", '%d/%m/%Y')", "type": "DATE"}}
```

More working expressions: `node_catalog("derive")`.

## Running total, rank, previous value, share of the group

```json
{"id": "running", "node": "window", "inputs": ["dated"],
 "config": {"name": "running_qty", "fn": "sum", "column": "Qty",
            "partition_by": ["Product"], "order_by": ["day"]}}
```

`fn`: `sum avg min max count` (running with `order_by`, group total without), `row_number`,
`rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value`, `share`. A change from the
previous period is `lag` then `derive`.

## Combine and summarise

```json
{"id": "joined", "node": "join", "inputs": ["sales", "items"],
 "config": {"on": ["Item"], "how": "left"}}
```

```json
{"id": "by_group", "node": "group_by", "inputs": ["joined"],
 "config": {"by": ["Group"], "aggs": [{"name": "Total", "fn": "sum", "column": "Qty"}]}}
```

## CSV, Parquet, JSON and JSONL files

A file that already is a table needs no `sheet` or `region`. Column types are read the same
way as for Excel (whole numbers BIGINT, decimals DOUBLE), so steps and checks work the same.

```json
{"kind": "csv", "file_role": "orders", "delimiter": ";", "header": true}
```

```json
{"kind": "parquet", "file_role": "prices"}
```

A `json` file is one array of objects, a `jsonl` file has one object per line. Keys become
columns (a key missing from some objects is NULL there); nested objects and lists stay as
STRUCT and LIST columns.

```json
{"kind": "jsonl", "file_role": "events"}
```

A csv without a header row: `"header": false` names the columns `column0`, `column1`, ...

A csv guesses each column's type from its values, so a total row or one stray word turns a
number column into text, and a later file without it reads as numbers again. Pin the types you
rely on, and set `decimal` when numbers use a comma:

```json
{"kind": "csv", "file_role": "sales", "delimiter": ";", "decimal": ",", "types": {"order": "text"}}
```

`sheet_regions`, `render_range` and the other sheet tools refuse such a file; `workbook_map`
shows its columns, row count and first rows.

## Checks

The total row of a sheet against the rows above it (each side picks its rows with `where`):

```json
{"kind": "reconcile",
 "left": {"table": "sales", "column": "Qty", "where": "\"Item\" <> 'Total'"},
 "right": {"table": "sales", "column": "Qty", "where": "\"Item\" = 'Total'"}}
```

Nothing lost between input and output:

```json
{"kind": "reconcile",
 "left": {"table": "sales", "column": "Qty"},
 "right": {"table": "by_group", "column": "Total"},
 "abs_tol": 0.5}
```

```json
{"kind": "not_null", "table": "by_group", "columns": ["Group"]}
```

```json
{"kind": "unique", "table": "by_group", "columns": ["Group"]}
```

```json
{"kind": "range", "table": "sales", "column": "Qty", "min": 0, "severity": "warn"}
```

```json
{"kind": "accepted_values", "table": "sales", "column": "Unit", "values": ["KG", "TON"]}
```

```json
{"kind": "regex", "table": "sales", "column": "Ref", "pattern": "^SO-[0-9]+$"}
```

```json
{"kind": "row_count", "table": "sales", "min": 1, "max": 100000, "severity": "warn"}
```

```json
{"kind": "duplicate_rows", "table": "sales", "severity": "warn"}
```

```json
{"kind": "foreign_key", "table": "lines", "column": "Ref", "ref_table": "sales", "ref_column": "Ref"}
```

## A dataset for machine learning (train, validation, test)

The `ml` section cuts one table into partitions and writes what describes them. It only
prepares data: nothing here trains or scores a model. Preprocessing (below) is optional
and is learned from the train rows only, then applied to train, validation and test alike.
Every column of `input` must be placed
under `features`, `ids` (kept, not a feature), `exclude` (dropped on purpose) or be the
`target`; a column left out is an error. The partitions are tables named `<name>_train`,
`<name>_validation` (when `validation_size` is set) and `<name>_test`: write them with
`outputs` and check them with `checks`. A run also writes `feature_schema.json`,
`preparation_report.json` and `preprocessing_metadata.json` next to them.

```json
{"name": "churn", "input": "clean", "target": "churn",
 "features": ["age", "spend", "segment"], "ids": ["customer_id"], "exclude": ["note"],
 "split": {"strategy": "stratified", "test_size": 0.2, "validation_size": 0.1, "seed": 42},
 "missing": {"age": {"strategy": "median"}},
 "numerical": {"spend": {"strategy": "standard_scale"}},
 "categorical": {"segment": {"strategy": "one_hot", "unknown": "ignore"}}}
```

Preprocessing rules (only for `features`; the target and the ids are never changed). Per
column the order is: drop rows with an empty value, fill empty values, then scale or encode.
`missing`: `drop` (the row is removed from every partition), `constant` (with `"value"`),
`mean` or `median` (numbers), `mode` (text, boolean, whole numbers). `numerical`:
`standard_scale`, `minmax` (0 at the train minimum, 1 at the train maximum; test values can
fall outside), `log1p` (values must be above -1). `categorical`: `one_hot` (columns named
`<column>_<value>`, which exist only after the run, so a check cannot name them) or
`ordinal` (position, from 0, in the train categories sorted by value: numbers by size, text
alphabetically; it is not a ranking such as low/mid/high, use `one_hot` then); `unknown` says
what a value not seen in train becomes: `ignore` (all zeros, or empty) or `error`. A column is either `numerical` or
`categorical`, not both. An empty value that is not filled stays empty (`ordinal`) or gives
all zeros (`one_hot`); a `constant` fill that equals a category is that category. The mean and
standard deviation include outliers: clean them with a `filter` step before `ml`. A date
feature is not preprocessed: derive a number from it (`derive`) before `ml`. `drop` also
removes the rows of new data: `apply_ml_preprocessing` says how many. The numbers learned (fill values, mean, std, min, max, categories)
are in `preprocessing_metadata.json`: the same file prepares new data later
(`apply_ml_preprocessing`), so a model sees new rows exactly as it saw the training rows.

Choosing roles: `ids` are keys kept for tracing (an id left among the features leaks);
`exclude` is for personal data, free-text notes and columns that would give the answer away;
`features` is the rest the model may use. `validation_size` is optional: without it there is
no `<name>_validation` table. `seed` is required (any integer): the same rows in the same
order, seed and settings always give the same partitions; add a `sort` step before `ml` when
the order is not already fixed. `node_catalog("ml")` lists the fields. To change only the
seed, send the whole `ml` section in `edit_spec`.
`random` and `stratified` (keeps the target's class balance) take no `by`; `time` (the newest
rows go to test) and `group` (a group never spans two partitions) need `"by": "<column>"`.

Before writing the section, `ml_candidates(table, target)` shows each column's empty and
distinct counts with notes (constant, id-like, mostly empty) and how the target is spread;
`table` is a CSV, Parquet or JSON file, or an output of the last run. After a run,
`apply_ml_preprocessing(data, name)` prepares new rows with the numbers learned from train
(from the last run's `preprocessing_metadata.json`) and writes `prepared/<name>.parquet`.

A run also warns about likely leaks: a feature that equals or fully determines the target, a feature whose being empty tells
the target (a cancel reason filled in only after the person left),
a feature with a different value in almost every row (like an id), a date feature later than
the target or the `time` column, and the same ids or the same feature and target values on
both sides of the split. These are hints from the data, not proof: a quiet run does not mean
the dataset is clean, and a warning can be a false alarm. Show them to the person. A date left in `features` also warns: derive a number from it.

## Where an output goes in a database

Only when the person wants the result in a warehouse; the `excel-etl-load` skill covers the
rest. The database, schema and login are never in the spec.

```json
{"input": "by_group", "format": "parquet",
 "load": {"table": "sales_by_group", "mode": "merge", "keys": ["Group"]}}
```
