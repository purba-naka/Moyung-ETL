---
name: excel-etl-load
description: Plan how the clean output of an excel-etl run is loaded into a warehouse table (Snowflake, Databricks, BigQuery) - set the load target in the spec, ask the person for the database details, call plan_load, and show the steps for approval. Use after a successful run when the person wants the result in a database. Not for writing the pipeline (excel-etl-build-spec) or judging the run (excel-etl-run-verify).
metadata:
  library: excel-etl
---

# Plan a load

The library connects to nothing. `plan_load` returns the steps and SQL; the host runs them
with its own login and then reports the result. You plan, a person approves, the host runs.
Tool names may carry a prefix in your host.

## Steps, in order

1. The last run must be `ok` and its checks must have passed (`excel-etl-run-verify`). Never
   plan a load from a run that failed or whose checks you did not read.
2. `lookup_knowledge` for the platform, database, schema and where files are staged. If the
   person never said, `ask_human(kind="clarify", ...)` and stop. Never invent them.
3. Put the target in the spec output (see below), `save_spec` with a note, run again. The
   file that is loaded is the one the run wrote for that spec version.
4. `ask_human(kind="confirm", ref="v<N>")`: say the table, the mode and the row count, then
   stop. `before_load` needs the person's approval for this exact version; you cannot give it.
5. After approval, `plan_load(platform, output, database, schema, stage)`. Show the person the
   steps. Quote the SQL exactly as returned; do not rewrite it.
6. The host runs the steps and calls `record_load`. You cannot. Tell the person what to expect
   and wait.

## The target in the spec

```json
{"input": "clean", "format": "parquet",
 "load": {"table": "sales_clean", "mode": "replace"}}
```

- Only a parquet output can be loaded. The table name is letters, digits and `_`.
- The database, schema, stage and login are not in the spec. They come from the host.
- `mode` decides what happens to rows already in the table:

| Mode | Use when | Risk |
|---|---|---|
| `replace` | every file holds the whole data | none for duplicates; old rows are gone |
| `append` | every file holds only new rows | the same file loaded twice doubles the rows |
| `merge` | a file may repeat rows already loaded | needs `keys` that are unique in the file |

- `merge` needs `keys`: the columns that identify one row, for example `["Plant", "Product",
  "Month"]`. Check with `profile_region` or a `unique` check that they really are unique. A
  key column must not hold empty values.
- If the person does not know which mode, ask the question in these words: "When a new file
  comes for the same period, should the old rows be replaced, added to, or updated by key?"

## Read the plan like this

- Each step has a kind: `upload` (the host sends the file), `create`, `copy`, `merge`,
  `cleanup`. The order matters; the host runs them one by one.
- `warnings` are real limits of that platform. Repeat them to the person.
- A column the platform cannot hold (`INTERVAL`, nested types, huge integers, a name BigQuery
  rejects) makes `plan_load` return an error that names it. Fix it in the spec with `cast`
  or `rename`, run again, plan again.
- Pass `existing_columns` (the target table's `name TYPE` lines from `DESCRIBE`) when the
  table already exists. `append` and `merge` then refuse a table that does not fit the file.
  `replace` makes the table again and the plan says so.
- The platform notes differ: Snowflake and Databricks skip a file they already loaded, so a
  rerun with the same file name adds nothing; BigQuery `replace` leaves the table unchanged
  when the load fails.

## Do not

- Do not guess database, schema, stage, bucket or table names.
- Do not call `plan_load` before the person approved `before_load` for this version.
- Do not run, edit or "fix" the SQL yourself. If a step looks wrong, say which and why.
- Do not choose `replace` just because it is the default: it deletes the old rows.
- Do not plan a load of a table whose run had failed checks.

## Stop and ask when

The platform or the mode is unknown, `merge` has no clear keys, the plan warns about a column
that changes meaning (a number turned to text), or the person has not approved.

## What is not proven

The SQL follows each vendor's documentation and parses in that platform's dialect, but it has
not been run on a live account. After the first real load, check the row count and a sample of
rows, and tell the person the load was not tested before.
