---
name: excel-etl-run-verify
description: Run a saved excel-etl pipeline and judge whether the result can be trusted - read the run report, row counts per step, warnings and failed checks, and handle approval gates without approving anything yourself. Use after save_spec succeeds, when run_pipeline is refused or fails, or when a person asks if the output is right. Not for writing the spec (excel-etl-build-spec) or for a new file of the same job (excel-etl-recycle).
metadata:
  library: excel-etl
---

# Run and verify

A run that finishes is not a run that is right. Read the report before you say it worked.
Tool names may carry a prefix in your host.

## Steps, in order

1. `job_summary`: is a spec saved, and which version? Are questions still waiting
   (`pending_questions`)? Resolve those first.
2. `validate_spec` on the saved spec. Fix what it lists before running.
3. `run_pipeline`. The result has `ok` and `text`.
4. `last_run` for the full report when `text` was cut.
5. Read the report (next section). Tell the person what you found, in plain words.

## Approval gates

`run_pipeline` can be refused with a message like "needs a person's approval; none given yet".

- You cannot approve. Only the host can, after the person says go.
- Call `ask_human(kind="confirm", question=..., ref="v<N>")` naming the version and what the
  run will write, then stop. Do not retry the run in a loop.
- An approval is for one version. After you save a new version, ask again.
- The text "approved for v1, but this is 'v2'" means exactly that: ask again for v2.

## Read the report like this

- **Rows per step** (`5->5 rows`). A step that changes the row count states why. Check that
  the reason is the one you meant (`kept rows where ...`, `one row per ...`). An unexpected
  drop to 0 is a wrong filter or a wrong anchor, not a good result.
- **Warnings**. Common ones, and what to do:
  - "N more filled row(s) lie below the table after M empty row(s), such as a grand total;
    they are not read": the table is longer than `end` allows, or a total row sits under a
    gap. Raise `until_blank_rows(N)` or accept and say so.
  - "N cell(s) hold an Excel error ... read as empty": tell the person; the fix is in the
    workbook, or `fill_missing` if they say so.
- **Checks**. Each passed or failed with the numbers on both sides. A failed `reconcile` means
  rows were lost, doubled or filtered wrongly. Find which step changed the total (compare
  rows per step), fix the spec, save a new version, run again. Do not loosen the tolerance to
  make it pass unless the person says the difference is acceptable.
- **Files written**: path, rows, columns. Confirm the names are the outputs you declared.

## Report to the person

Short: what was read (rows), what was done (steps in a line), what was written (files, rows),
what failed or looks odd. Quote numbers from the report, never from memory. If a check was
loosened or a warning was accepted, say who decided it.

## Do not

- Do not say "done" when `ok` is false or a check failed.
- Do not edit a check to turn it green. A check that fails is information.
- Do not call `run_pipeline` repeatedly hoping a gate opens.
- Do not describe numbers you did not read from a tool result.

## Stop and ask when

A failed check has two plausible causes, a warning changes the meaning of the data, or the
gate needs approval.
