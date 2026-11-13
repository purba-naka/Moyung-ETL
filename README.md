# excel-etl

The GitHub repository is `Moyung-ETL`; the Python package and the working folder are
`excel-etl` (`import excel_etl`).

Pure Python library of tools for LLM agents: understand a raw Excel file (many sheets, many
tables, irregular shapes), build an ETL pipeline in duckdb, run it to parquet/csv, and bind
the same pipeline again when the workbook changes. The library holds no state, starts no
server and imports no agent framework. The host wraps it.

Read `HANDOVER.md` first if you are connecting this to a host. `AGENTS.md` has the rules,
`DESIGN.md` the design, `IMPLEMENTATION.md` the status.

## What the host provides

1. A **file provider**: an object with `open(file_id) -> path or bytes`. Files never enter the
   state; only `id`, name and sha256 do.
2. A place for the **state** (one JSON string per job), or a `StateStore` with `load`/`save`.
3. An **out_dir** where a run writes its parquet/csv files.

```python
from excel_etl.state import Job, FileStore
from excel_etl.tools import JobTools


class Files:  # your storage: disk, S3, a database ...
    def open(self, file_id: str) -> str | bytes:
        return f"C:/uploads/{file_id}.xlsx"


job = Job.open("job1", FileStore("C:/state"))  # saved after every change
tools = JobTools(job, Files(), out_dir="C:/out/job1")
tools.add_file("up-17", "Balance Jan.xlsx", role="main")  # host call: register an upload
```

Without a store, keep the JSON yourself:

```python
job = Job.from_state(saved_json)  # or Job(new_state("job1")) for a new one
tools = JobTools(job, Files(), out_dir="C:/out/job1")
...  # the agent calls tools
saved_json = tools.job.state_json()  # store this after every agent turn
```

`file_role` in a spec is a free label ("main"); `add_file(..., role="main")` says which upload
plays it. A new month's file: `add_file` again with the same id and the same role, then
`rebind_sources`. After a conflict error call `tools.reload_job()` and repeat the call.
Pass `per_version=True` to `JobTools` to write each run into `out_dir/v<N>`.

## Give the tools to an agent

Every tool is a plain method with a docstring, simple argument types and a `dict` result, so
most frameworks take it as is. With Google ADK:

```python
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

agent = LlmAgent(
    name="excel_etl",
    model="gemini-2.5-pro",
    instruction=EXCEL_ETL_INSTRUCTION,  # see below
    tools=[FunctionTool(fn) for fn in tools.agent_tools()],
)
```

For one ADK session keep one `JobTools` (or rebuild it from the session's saved JSON on every
turn). The generated schema of all tools is flat: no `anyOf`, `oneOf` or `$ref`. With LiteLLM
the same functions work through its function-to-schema helper; `tests/test_tools.py` checks
that argument types stay primitive, enum or list of strings.

**Not for the agent**: `tools.add_file`, `tools.answer`, `tools.approve`, `tools.reload_job`, `tools.record_load`. They are what a person
does through your UI. When the agent calls `ask_human` the tool returns a `question_id` and the
agent must stop; show the question, then call `tools.answer(question_id, reply)` and start the
next turn.

### Instruction to start from

```
You turn raw Excel files into clean tables with a pipeline spec.
1. job_summary, then list_files. Look before you act: workbook_map, sheet_regions,
   profile_region, find_text, render_range (compressed first).
2. propose_datasets, then ask_human (choose) which tables the person wants.
3. node_catalog shows the step types. Write the spec as JSON text and call save_spec; read the
   problems it lists and fix them. get_spec shows what is saved.
4. Add `checks` (not_null, unique, range, accepted_values, reconcile) for what must hold.
5. run_pipeline. If a tool says a person must approve, ask_human and stop.
6. A new file for the same job: rebind_sources. exact/shifted = run again. drift/missing =
   repair the spec with edit_spec (only the changed steps) and a note saying what changed.
Every tool returns {"text": ...} or {"error": ...}. An error says what to change: change it.
Never guess a column name; copy it from a tool result.
```

### Skills for coding agents

`skills/` holds five playbooks in the open Agent Skills format (a folder per skill with a
`SKILL.md`; spec at agentskills.io). Any agent that reads that format can load them: copy or
link the folders into its skills directory. They tell the agent the order of work, not the
tool arguments (the tool docstrings and `node_catalog` do that).

| Skill | When |
|---|---|
| `excel-etl-understand` | a person gives a workbook; find the tables worth loading |
| `excel-etl-build-spec` | write and fix the spec (`references/patterns.md`: Excel shape to nodes) |
| `excel-etl-run-verify` | run, read the report, handle approval gates |
| `excel-etl-recycle` | a new file for a job that has a spec |
| `excel-etl-load` | plan the load of a clean output into Snowflake, Databricks or BigQuery |

`tests/test_skills.py` keeps them honest: the format rules, every tool, node and setting
named in them must exist, every JSON snippet must parse and build, and every agent tool must
appear in at least one skill.

## Loading the result into a warehouse

The library ends at a parquet file and a report. To put the file in a database, an output
names its target and `plan_load` turns it into steps; the host runs them with its own login.
Nothing in the library connects to anything or holds a password.

```json
{"outputs": {"clean": {"input": "by_group", "format": "parquet",
   "load": {"table": "sales_clean", "mode": "merge", "keys": ["Plant", "Product"]}}}}
```

`mode`: `replace` (swap the table), `append` (add rows), `merge` (update rows with the same
`keys`, add the rest). The agent calls `plan_load(platform, output, database, schema, stage)`
after a person approves `before_load` for that spec version, and only when the last run was
made from that version and the files registered now; the result lists the steps (send the
file, make the table, copy, merge) with the SQL of each. The host runs them and calls
`tools.record_load(output, status, rows, note)`, in the same turn or a later one.

| Platform | Notes |
|---|---|
| `snowflake` | `PUT` to a stage, `COPY INTO ... MATCH_BY_COLUMN_NAME`; `replace` is one transaction |
| `databricks` | `COPY INTO` a Delta table (made first); `replace` is `CREATE OR REPLACE TABLE` |
| `bigquery` | `LOAD DATA INTO` or `OVERWRITE` from a Cloud Storage file; a failed overwrite leaves the table |

A column the platform cannot hold (`INTERVAL`, nested types, huge integers, a name BigQuery
rejects) is an error that names it. Pass the target table's `DESCRIBE` as `existing_columns`
and an `append` or `merge` into a table that does not fit the file is refused.

**Not proven on a live account.** The SQL follows each vendor's documentation, parses in that
platform's dialect and runs the same flow on a local duckdb in `tests/test_load_flow.py`, but
it has not been run on Snowflake, Databricks or BigQuery. Add a new platform with one `Profile`
in `excel_etl/load.py`.

## A complete spec

Names (`sales`, `items`, `by_group`) are letters, digits and `_`. `inputs` is always a list;
an output's `input` is one name. Column names are copied from a tool result, never guessed.

```json
{
  "sources": {
    "sales": {
      "file_role": "main",
      "sheet": {"name": "Sales"},
      "region": {"header_anchor": ["Item", "Qty"], "header_rows": 1,
                 "end": "until_blank_rows(2)"},
      "expect_columns": [{"match": "Qty", "type": "float"}]
    },
    "items": {
      "file_role": "main",
      "sheet": {"name": "Items"},
      "region": {"header_anchor": ["Item", "Group"]}
    }
  },
  "steps": [
    {"id": "joined", "node": "join", "inputs": ["sales", "items"],
     "config": {"on": ["Item"], "how": "left"}},
    {"id": "by_group", "node": "group_by", "inputs": ["joined"],
     "config": {"by": ["Group"], "aggs": [{"name": "Total", "fn": "sum", "column": "Qty"}]}}
  ],
  "outputs": {"totals": {"input": "by_group", "format": "parquet"}},
  "checks": {
    "no_lost_qty": {"kind": "reconcile",
                    "left": {"table": "sales", "column": "Qty"},
                    "right": {"table": "by_group", "column": "Total"}},
    "groups_known": {"kind": "not_null", "table": "by_group", "columns": ["Group"]}
  }
}
```

To compare a sheet's own total row with the rows above it, give each side a `where`
(`"where": "\"Item\" <> 'Total'"` on one side, `"\"Item\" = 'Total'"` on the other). Excel error
cells (`#DIV/0!`) are read as empty and the run warns how many there were. A step that
changes the row count says why in the run report.

A two-row header (a `Sales` over `Qty`) needs `"header_rows": 2` and gives columns named
`Sales|Qty`. A total row below a gap of two empty rows needs a larger `end`
(`until_blank_rows(5)`); a run warns when filled rows lie below the end.

For the same table repeated across sheets, use one source instead of one source per sheet:

```json
"sheet": {"name_regex": "day-[0-9]{2}", "sheet_column": "source_sheet"}
```

The whole sheet name must match. Sheets are stacked in workbook order, rows keep their order
within each sheet, and `source_sheet` is the first output column. Columns missing from some
sheets become NULL; mixed column types use a common duckdb type. A matching sheet whose table
cannot be found fails the run. A later workbook may have more or fewer matching sheets without
causing rebind drift. `sheet_column` is required with `name_regex` and cannot be used without it.

## Steps a spec can use

A source reads one table by its header text (`read_region`). Steps then change it. The agent
gets the exact fields of each step from the `node_catalog` tool.

| Group | Steps |
|---|---|
| Tidy Excel shapes | `drop_rows`, `fill_down`, `unpivot`, `unpivot_groups`, `split_header_token`, `parse_hierarchy` |
| General | `select`, `rename`, `cast`, `filter`, `derive`, `fill_missing`, `sort`, `dedupe`, `window` |
| Combine and summarise | `join`, `union`, `group_by`, `pivot` |

`filter` and `derive` take one scalar SQL expression (duckdb): text functions (`split_part`,
`regexp_extract`, `regexp_replace`, `substr`), dates (`strptime`, `date_trunc`, `strftime`,
`date_diff`), `CASE`, `coalesce`, `round`; `node_catalog("derive")` and `("filter")` show
working examples. Not allowed: subqueries, aggregates, window functions, and functions that
read files or the system. Running totals, ranks, the previous row's value and the share of a
group total are the `window` step: a fixed list of functions (`sum`, `avg`, `min`, `max`,
`count`, `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value`,
`share`), a group (`partition_by`) and an order (`order_by`), no free SQL. Rows and their
order are kept.

## Tools

| Group | Tools |
|---|---|
| Look at a file | `workbook_map`, `sheet_regions`, `render_range`, `profile_region`, `find_text`, `compare_sheets`, `formula_lineage`, `propose_datasets`, `source_for_region` |
| Write a pipeline | `node_catalog`, `save_spec`, `edit_spec`, `get_spec`, `validate_spec`, `spec_history` |
| Run and recycle | `run_pipeline`, `last_run`, `rebind_sources`, `plan_load` |
| People and context | `ask_human`, `pending_questions`, `lookup_knowledge`, `job_summary`, `list_files` |
| Host only | `add_file`, `answer`, `approve`, `reload_job`, `record_load` |

A result is `{"text": ...}`, with `truncated` and `next_offset` when paged, or `{"error": ...}`.
`save_spec` also returns `saved` and `version`; `validate_spec`, `rebind_sources` and
`run_pipeline` return `ok`; a store conflict returns `conflict: true` (reload the job and repeat).

## Where the ideas come from

Nothing below is a dependency. The library depends only on `python-calamine`, `openpyxl`,
`duckdb` and `pydantic`. These are the works whose ideas shaped it, and what we took from each.

| Source | What we took | What we did not take |
|---|---|---|
| [SpreadsheetLLM: Encoding Spreadsheets for Large Language Models](https://arxiv.org/abs/2407.09025) (Dong et al., Microsoft, EMNLP 2024) | The three compression ideas behind `render_range`: structural anchors (keep the rows and columns at table edges, drop the homogeneous middle), an inverted index (one text, many addresses) and aggregation of number cells by type and range. | The fine-tuned model and the Chain of Spreadsheet question answering. |
| [SheetBrain](https://github.com/microsoft/SheetBrain) (Microsoft; AAAI 2026) | The Understand, Execute, Validate cycle, which is the shape of the tool flow here (`workbook_map` to `render_range`, then `save_spec` and `run_pipeline`, then `checks`), and running agent code in a restricted sandbox. | The package itself: it depends on OpenAI, tiktoken and multimodal input, and its execution step runs free Python. Here the agent writes a spec and scalar SQL, never code. |
| [sheetwise](https://github.com/Khushiyant/sheetwise) (PyPI) | A practical reference for the SpreadsheetLLM encoding; we measured it on the sample workbook and found the inverted index useful. | The package: it reads a pandas DataFrame, so merged cells, styles and table regions are already gone, and it pulls in heavy dependencies. Our views are about 35 times smaller on the large sample sheet. |
| [DocETL](https://github.com/ucbepic/docetl) (UC Berkeley EPIC Data Lab) | Confirmation of the design: a declarative pipeline spec, a schema known before running, deterministic code steps, caching by hash. Ideas kept for later: run on a sample before the full run, and a review screen for human checkpoints. | The package: its steps are LLM calls, which is the opposite of the deterministic transforms wanted here. It is a candidate only for unstructured documents. |
| `pinter-be-refactor/modules/parse_excel/parser.py` (this project's backend) | The base of `detect_regions`: stacked headers, vertical merges, total rows, several tables on one sheet. Copied and changed, not imported. | Its reader and agent tools: they answer questions about one file, while this library builds and reruns pipelines. |

The sample workbook used for checks is internal data and is not in the repository. The
tests build their own small workbooks in many shapes, because one real file is one example,
not the rule.

## Checks before relying on it

```
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```
