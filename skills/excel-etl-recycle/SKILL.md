---
name: excel-etl-recycle
description: Reuse a saved excel-etl pipeline when a new version of the Excel file arrives (next month, moved tables, renamed headers, new columns) - rebind the sources by their header anchors, repair only what drifted, keep the spec history. Use when a person adds a file for a job that already has a spec, or when a run fails after the workbook changed. Not for a first-time build (excel-etl-understand, excel-etl-build-spec).
metadata:
  library: excel-etl
---

# Recycle a pipeline for a new file

The spec stores header text, not cell addresses, so most monthly files fit without edits.
Your job is to find out which sources still fit, repair the rest, and leave a trail.
Tool names may carry a prefix in your host.

## Steps, in order

1. `job_summary`, `list_files`. The host has registered the new file under the role the spec
   uses (`file_role`). If it has not, say so and stop; you cannot add files.
2. `rebind_sources`. One status per source:

| Status | Meaning | Action |
|---|---|---|
| `exact` | table is where it was, contract holds | nothing |
| `shifted` | sheet name or position moved, contract holds | nothing; the spec runs as is |
| `drift` | contract broke or columns changed | repair this source |
| `missing` | sheet, header or file not found | look for candidates, then ask |

3. All sources `exact` or `shifted`: go to `excel-etl-run-verify`. Do not rewrite a spec that
   works.
4. For each `drift` or `missing` source, look at the new file with the tools of
   `excel-etl-understand`: `workbook_map`, `find_text` for the old header text,
   `sheet_regions`, `profile_region`, `compare_sheets` (old sheet against the candidate).
5. Decide the smallest change: a new header text in `header_anchor`, a changed
   `header_rows`, a new `expect_columns` entry, a renamed column in a later step.
6. `edit_spec(patch_json, note)` with only the steps that drifted (same id replaces it), with a `note` that says what
   changed and why ("header 'Ton' renamed 'Tonnage' in sheet Production").
7. `spec_history` shows the versions and diffs. Check that the diff holds only what you meant.
8. `validate_spec`, then run and verify (`excel-etl-run-verify`).

## Judge a drift

- A renamed header, same data: repair the anchor and the later steps that name the column.
- A new column: usually nothing breaks. Decide if it belongs in the output; ask if unsure.
- A removed column that a step or check uses: do not invent a replacement. Ask.
- A sheet replaced by a differently shaped one: this is a rebuild of that source, with the
  person's confirmation.
- Two candidates for a missing table: `ask_human(kind="choose")`, with sheet names and ranges.

## Do not

- Do not rewrite the whole spec because one source drifted.
- Do not loosen `expect_columns` or checks to make a drifted file pass.
- Do not overwrite history. A repair is a new version with a note.
- Do not guess a column rename from similar looking text without checking values with
  `profile_region`.

## Stop and ask when

A source is `missing`, a check depends on a column that disappeared, or the repair changes
what the output means (a different unit, period, or plant). Policy may also require approval
at `on_rebind_drift`: ask the person with `ask_human` and stop.
