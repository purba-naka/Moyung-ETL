---
name: excel-etl-understand
description: Explore a raw Excel workbook (many sheets, several tables per sheet, merged or multi-row headers) with the excel-etl tools and find which tables are worth loading as datasets. Use first, when a person gives an .xlsx and asks what is in it, what to load, or to clean it. Not for writing the pipeline spec (use excel-etl-build-spec).
metadata:
  library: excel-etl
---

# Understand a workbook

Goal: a short list of tables the person confirms, with a note of each one's quirks (header
rows, merged labels, total rows, units). Tool names may carry a prefix in your host.

## Steps, in order

1. `job_summary`, then `list_files`. A new job has files but no spec. A returning job may
   already have a spec and questions: continue it, do not start over.
2. `lookup_knowledge` with the topic. The person may already have told us the terms and rules.
3. `workbook_map(file)`. One line per sheet: size, tables, share of formulas, guessed role.
   Roles: `source` (typed data), `derived` (formulas over other sheets), `mixed` (both),
   `empty`. A `derived` sheet is not a source. It is what a check compares against later.
   A `mixed` sheet needs a look at its tables one by one.
4. `formula_lineage` when roles are unclear: which sheet reads which.
5. `sheet_regions(file, sheet)` for each candidate sheet: ranges, header rows, total rows.
6. `profile_region(file, region)` per candidate table: type, range and empty count per column.
7. `render_range` only to settle a doubt. `compressed` first. `raw` for one small range
   (`range="B4:N20"`), never for a whole sheet.
8. `find_text` to locate a label or term the person named.
9. `compare_sheets` when two sheets look like copies or versions.
10. `propose_datasets`, then `ask_human(kind="choose", options=[...])` with the table names.
    Stop and wait. The answer arrives in a later turn.

## Read the output like this

- Ids are stable: `F1`, `F1.S2`, `F1.S2.R1`. Copy them from a result; never type one from memory.
- `truncated: true` with `next_offset`: call again with that offset only if you need the rest.
- A column header such as `JAN|Ton` is a two-row header joined with `|`.
- A sheet that reads like a report (titles, blank gaps, totals) holds several small tables.

## Do not

- Do not read a sheet raw to "see everything". Use the maps and profiles; they cost a tenth.
- Do not guess a column name or a sheet name. Copy it from a result.
- Do not treat a total row as data. Note its label for the checks.
- Do not decide between two plausible tables yourself. Ask.

## Stop and ask when

- Two tables look like the same data (a copy, an old version).
- A sheet is `derived` and the person might want its values as the source.
- Units, a period, or a plant name cannot be read from the cells.

## Hand over

State per chosen table: sheet, region id, header rows, columns that matter, total row label,
quirks. The next skill is `excel-etl-build-spec`.
