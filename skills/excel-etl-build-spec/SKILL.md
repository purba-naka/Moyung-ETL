---
name: excel-etl-build-spec
description: Write and fix an excel-etl pipeline spec (JSON with sources, steps, outputs, checks) for tables the person already chose. Use when turning chosen Excel tables into a clean dataset - unpivot wide months, fill merged labels, drop total rows, join, group, running totals, ranks, add quality checks - or when save_spec or validate_spec reports a problem. Not for exploring a file (excel-etl-understand) or running it (excel-etl-run-verify).
metadata:
  library: excel-etl
---

# Build a spec

A spec is JSON text with four parts: `sources` (where each table is, by header text),
`steps` (what to do), `outputs` (what to write), `checks` (what must hold).
Tool names may carry a prefix in your host.

## Steps, in order

1. `lookup_knowledge` for rules and terms the person gave.
2. For every node you plan to use, `node_catalog(node)`: exact config fields (`*` = required)
   and working examples. Never write a node config from memory.
3. Sources first: `source_for_region(region_id)` gives the entry for a table from
   `sheet_regions` with its header texts and sheet name read from the file; paste it into the
   spec instead of typing them (9 of 15 refused saves in real jobs were header texts that were
   not on the sheet). Then adjust (see "Sources").
4. Add steps one concern at a time, in this order: tidy the shape, fix types, derive, combine,
   summarise. `references/patterns.md` maps an Excel shape to the nodes that fix it.
5. `save_spec(spec_json, note)`. It validates against the files and does not save a spec
   with problems. Read every listed problem, fix them all, save again.
6. Add `checks` (see "Checks"), save again with a `note` saying what changed. For a small
   change after the first version use `edit_spec(patch_json, note)`: `{"steps": [...]}` (same
   id replaces, a new id is placed before the step that reads it), `"remove": [ids]`,
   `"sources"`/`"outputs"` (a name, or null to remove).
7. `get_spec` shows what is saved. `validate_spec` checks a draft without saving it.

## Sources

```json
{"file_role": "main", "sheet": {"name": "Sales"},
 "region": {"header_anchor": ["Item", "Qty"], "header_rows": 1, "end": "until_blank_rows(2)"},
 "expect_columns": [{"match": "Qty", "type": "float"}]}
```

- `header_anchor`: texts that must all be in the header. Pick ones unlikely to change.
- `header_rows`: 2 for a two-row header. Columns then read `PARENT|LEAF`.
- `end`: the table stops after N empty rows. A total row under a bigger gap needs a larger N.
  The run warns when filled rows lie below the end.
- `expect_columns`: the contract. A missing required column or a wrong type is found at
  validate time and again when a new file arrives.
- `file_role` is a free label; the host maps it to a file.

## Rules

- `inputs` is always a list (`["sales"]`). An output's `input` is one name.
- Names (source, step, check): letters, digits, `_`, starting with a letter.
- A step reads sources and earlier steps only. Order the steps top to bottom.
- Column names are copied from a tool result. Quote odd ones with double quotes in SQL.
- Expressions (`derive`, `filter`, check `where`) are one scalar SQL expression. No
  subquery, no aggregate (use `group_by`), no window (use the `window` node).
- Divide by `nullif(x, 0)`. A plain zero divisor gives `inf`, not an error.
- `cast` turns a value that does not fit into NULL. Check NULL counts after it.
- `sort` before `dedupe` or `first`/`last` when "first" must mean something.

## Checks

Add at least one per output. Examples with all kinds are in `references/patterns.md`.

- `not_null`, `unique`, `range`, `accepted_values`: facts about one table.
- `reconcile`: a total in the sheet against the sum of the rows. Give each side a `where`
  to pick the total row on one side and the detail rows on the other. This is the check
  that catches a lost or doubled row.
- `severity: "warn"` for things worth a look; the default `error` stops the write.

## Read validate output like this

- A problem names the step or source: `steps.by_item: no column 'Qty'; columns: ...`. Fix
  that one place. Problems of later steps that depend on it vanish after the fix.
- "approved for no version" or approval text is a host matter. See `excel-etl-run-verify`.

## Do not

- Do not store cell addresses (`B5:N40`). The spec must survive a shifted table.
- Do not hide a problem with a `fill_missing` or `cast` to make validate pass. Ask what the
  empty or odd value means.
- Do not copy an example from this skill with its column names. Examples show shape only.

## Stop and ask when

A rule is ambiguous (what a negative quantity means, which of two columns is the real date,
whether a total row should be kept). `ask_human`, then stop.
