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

**Not for the agent**: `tools.add_file`, `tools.answer`, `tools.approve`, `tools.reload_job`. They are what a person
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
   repair the spec, save_spec with a note saying what changed.
Every tool returns {"text": ...} or {"error": ...}. An error says what to change: change it.
Never guess a column name; copy it from a tool result.
```

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

## Tools

| Group | Tools |
|---|---|
| Look at a file | `workbook_map`, `sheet_regions`, `render_range`, `profile_region`, `find_text`, `compare_sheets`, `formula_lineage`, `propose_datasets` |
| Write a pipeline | `node_catalog`, `save_spec`, `get_spec`, `validate_spec`, `spec_history` |
| Run and recycle | `run_pipeline`, `last_run`, `rebind_sources` |
| People and context | `ask_human`, `pending_questions`, `lookup_knowledge`, `job_summary`, `list_files` |
| Host only | `add_file`, `answer`, `approve`, `reload_job` |

A result is `{"text": ...}`, with `truncated` and `next_offset` when paged, or `{"error": ...}`.
`save_spec` also returns `saved` and `version`; `validate_spec`, `rebind_sources` and
`run_pipeline` return `ok`; a store conflict returns `conflict: true` (reload the job and repeat).

## Checks before relying on it

```
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```
