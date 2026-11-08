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

## Where an output goes in a database

Only when the person wants the result in a warehouse; the `excel-etl-load` skill covers the
rest. The database, schema and login are never in the spec.

```json
{"input": "by_group", "format": "parquet",
 "load": {"table": "sales_by_group", "mode": "merge", "keys": ["Group"]}}
```
