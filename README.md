# moyung

Tools for LLM agents that turn raw Excel files (many sheets, many tables, irregular shapes) into
clean tables. The agent looks at the workbook, writes a pipeline spec (JSON), runs it in duckdb
to parquet/csv, and reruns the same spec on next month's file. Pure Python: no server, no state
kept inside, no agent framework imported. Repository `Moyung-ETL`, package `moyung`.

## What it does

1. **Understand**: map sheets, find tables, show a compressed view of a range, profile columns.
2. **Build**: the agent writes a spec (sources, steps, outputs, checks); `save_spec` validates it.
3. **Run and check**: duckdb runs the spec to parquet/csv; checks (`not_null`, `unique`, `range`,
   `accepted_values`, `regex`, `row_count`, `duplicate_rows`, `foreign_key`, `reconcile`) prove
   the output.
4. **Recycle**: a new file for the same job is bound to the saved spec; only drift needs a fix
   (a changed column type is drift; a safe widening, such as a whole number read as a double,
   is only reported).
5. **Load** (plan only): SQL steps to load an output into Snowflake, Databricks or BigQuery.
   The host runs them with its own login.

A check that fails does not always stop everything. It blocks only the outputs whose lineage
(steps, and `ml` tables through `ml.input`) includes the checked table; the other outputs are
written and the run status is `partial` (`RunReport.blocked`). Files of held-back outputs from
earlier runs are deleted, and `plan_load` names the blocking check and asks for `after_run`
approval before loading an output written by a partial run. A failed check outside every output's
lineage, or one that blocks all outputs, gives status `failed`.

A person stays in the loop: the agent asks questions and waits for approvals it cannot give itself.

## Tools

| Group | Tools |
|---|---|
| Look at a file | `workbook_map`, `sheet_regions`, `render_range`, `profile_region`, `sample_data`, `relate_tables`, `find_text`, `compare_sheets`, `formula_lineage`, `propose_datasets`, `source_for_region`, `ml_candidates` |
| Write a pipeline | `node_catalog`, `save_spec`, `edit_spec`, `get_spec`, `validate_spec`, `spec_history` |
| Run and recycle | `run_pipeline`, `last_run`, `verify_run`, `rebind_sources`, `plan_load`, `apply_ml_preprocessing` |
| People and context | `ask_human`, `pending_questions`, `lookup_knowledge`, `job_summary`, `list_files` |
| Host only | `add_file`, `answer`, `approve`, `reload_job`, `record_load`, `build_context` |

A result is `{"text": ...}` (with `truncated`/`next_offset` when paged) or `{"error": ...}` saying
what to change. A store conflict returns `conflict: true`: call `reload_job` and repeat.

## Install

```
pip install "git+https://github.com/purba-naka/Moyung-ETL.git"
```

Python 3.12+. The MCP server needs the `mcp` extra (`pip install ".[mcp]"`).

## Integrate with an agent

The host provides a **file provider** (`open(file_id) -> path or bytes`), a place for the
**state** (one JSON per job, or a `StateStore`) and an **out_dir**.

```python
from moyung.state import Job, FileStore
from moyung.tools import JobTools


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

**Give `tools.agent_tools()` to the agent.** Each is a plain function with a docstring, flat
argument types and a `dict` result, so ADK, LiteLLM and most frameworks take it as is:

```python
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

agent = LlmAgent(
    name="moyung",
    model="gemini-2.5-pro",
    instruction=INSTRUCTION,
    tools=[FunctionTool(fn) for fn in tools.agent_tools()],
)
```

**Host-only calls** (`add_file`, `answer`, `approve`, `reload_job`, `record_load`, `build_context`) belong to your
UI, never to the model. When the agent calls `ask_human` it gets a `question_id` and stops; show
the question, call `tools.answer(question_id, reply)`, start the next turn.

**MCP** (Claude Desktop, Cursor, any MCP client):

```
uv run --extra mcp python integrations/mcp_server.py --state-dir S --allow-dir D serve
uv run python integrations/mcp_server.py --state-dir S --allow-dir D approve CHECKPOINT BY
```

The second form is the human side; Excel files are read only from `--allow-dir`.

**Skills**: `skills/` holds five playbooks in the Agent Skills format (`SKILL.md` per folder):
`moyung-understand`, `moyung-build-spec`, `moyung-run-verify`, `moyung-recycle`,
`moyung-load`. Copy them into the agent's skills directory; they give the order of work.

Short instruction to start from:

```
You turn raw Excel files into clean tables with a pipeline spec.
1. job_summary, list_files, then look: workbook_map, sheet_regions, profile_region, render_range.
2. propose_datasets, then ask_human which tables the person wants.
3. node_catalog shows the steps. Write the spec as JSON, save_spec, fix the problems it lists.
4. Add checks for what must hold. run_pipeline; if approval is needed, ask_human and stop.
5. New file for the same job: rebind_sources. drift/missing: edit_spec with a note. run_pipeline
   also refuses a drifted source until a person approves that exact drift (its `drift:` ref).
Never guess a column name; copy it from a tool result. An error says what to change.
```

## A spec

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

Same table across many sheets: `"sheet": {"name_regex": "day-[0-9]{2}", "sheet_column": "source_sheet"}`.
Two-row header: `"header_rows": 2` gives columns like `Sales|Qty`.

CSV, Parquet, JSON, JSONL: `{"kind": "csv", "file_role": "f"}` (or `"parquet"`, `"json"`,
`"jsonl"`), no `sheet` or `region`;
a csv also takes `delimiter`, `header`, `decimal` (`","` for 1,5) and `types` (column name to
text/int/float/date/datetime/bool: pin a column whose guessed type changes between files). The kind of a registered file comes from its
extension (`.csv` `.tsv` `.txt`, `.parquet` `.pq`, `.json` (an array of objects), `.jsonl`
`.ndjson`; anything else is a workbook). Differences from
Excel: no sheets, regions or formulas, so `workbook_map` is the only discovery tool (it lists
columns, row count and first rows) and `rebind_sources` judges columns and types only.
Integer types narrower than 64 bits read as BIGINT and FLOAT as DOUBLE. UTF-8 text only.

| Group | Steps |
|---|---|
| Tidy Excel shapes | `drop_rows`, `fill_down`, `unpivot`, `unpivot_groups`, `split_header_token`, `parse_hierarchy` |
| General | `select`, `rename`, `cast`, `filter`, `derive`, `fill_missing`, `sort`, `dedupe`, `window` |
| Combine and summarise | `join`, `union`, `group_by`, `pivot` |

`filter` and `derive` take one scalar duckdb expression (no subqueries, aggregates or file
access). `node_catalog` gives each step's exact fields and examples.

## Run without an agent

Once a spec is saved, a new file of the same shape needs no agent: every spec in a directory is
tried with `rebind`, and the first whose sources are all found is run. If none fits, nothing is
written and the report names the closest spec.

```
python -m moyung.auto <file.xlsx> <specs_dir> <out_dir>
```

`moyung.synth` can also draft a spec from the output table a person describes, without an
LLM; the result is an ordinary spec that stays editable.

## Load into a warehouse

```json
{"outputs": {"clean": {"input": "by_group", "format": "parquet",
   "load": {"table": "sales_clean", "mode": "merge", "keys": ["Plant", "Product"]}}}}
```

`mode`: `replace`, `append` or `merge` (by `keys`). After a person approves `before_load`,
`plan_load(platform, output, database, schema, stage)` returns the SQL steps; the host runs
them and calls `tools.record_load(...)`. The library never connects or holds a password.

| Platform | Method |
|---|---|
| `snowflake` | `PUT` to a stage, `COPY INTO ... MATCH_BY_COLUMN_NAME` |
| `databricks` | `COPY INTO` a Delta table |
| `bigquery` | `LOAD DATA` from Cloud Storage |

Not yet run on a live account; the SQL is tested against each dialect and a local duckdb.

## Where the ideas come from

Nothing below is a dependency. Runtime deps: `python-calamine`, `openpyxl`, `duckdb`, `pydantic`.

| Source | Taken |
|---|---|
| [SpreadsheetLLM](https://arxiv.org/abs/2407.09025) (Microsoft, EMNLP 2024) | Compression behind `render_range`: structural anchors, inverted index, number aggregation |
| [SheetBrain](https://github.com/microsoft/SheetBrain) (Microsoft, AAAI 2026) | Understand, Execute, Validate cycle; agent writes a spec, never free code |
| [sheetwise](https://github.com/Khushiyant/sheetwise) | Reference encoding; ours is ~35x smaller on the large sample sheet |
| [DocETL](https://github.com/ucbepic/docetl) (UC Berkeley) | Declarative spec, deterministic steps, caching by hash |

## Checks before relying on it

```
python -m ruff check . && python -m ruff format --check .
python -m pytest -q
```
