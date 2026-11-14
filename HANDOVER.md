# Handover: connecting excel-etl to a host

For the person who wires `excel-etl` into PINTER (or any agent host). It says what you give the
library, what you call, what it guarantees and what it does not. `README.md` has the snippets
for ADK; `DESIGN.md` has the reasons; `IMPLEMENTATION.md` has the history.

## What you get

A pure Python library, no server, no agent framework, no state of its own. Dependencies:
`python-calamine`, `openpyxl`, `duckdb`, `pydantic>=2`. Python 3.12.

An agent that has these tools can read a raw Excel file (many sheets, many tables, odd shapes),
write a pipeline spec, run it to parquet/csv with quality checks, and bind the same spec again
when next month's file moves things around.

## What you give it

| You provide | How | Notes |
|---|---|---|
| Files | an object with `open(file_id) -> path or bytes` | Files never enter the state. Only id, name and sha256 do. |
| State | `Job.from_state(json)` (you keep the JSON) or `Job.open(job_id, store)` | A store has `load(job_id)` and `save(job_id, json, expected_version)`. |
| Output place | `out_dir` | `per_version=True` writes to `out_dir/v<N>`. |
| People | your UI | The library never waits: it returns a question id and stops. |

```python
from excel_etl.state import Job, FileStore
from excel_etl.tools import JobTools

tools = JobTools(Job.open("job1", FileStore("state")), my_files, out_dir="out/job1")
tools.add_file("upload-17", "Balance Jan.xlsx", role="main")
agent_functions = tools.agent_tools()  # 25 plain methods; give them to the agent
```

Job ids are lower case letters, digits and `_ . -` (so they are safe file names on Windows).

## One agent turn, from the host side

1. Build `JobTools` from the saved state (a new object per turn is fine and cheap).
2. Let the agent call tools. Every result is a dict: `{"text": ...}` or `{"error": ...}`.
3. Stateless mode only: save `tools.job.state_json()` after the turn. With a store each change
   is saved at once.

The tools are methods with docstrings and primitive arguments; with ADK, `FunctionTool(fn)` on
each one gives a flat schema (checked for all of them: no `anyOf`, `oneOf`, `$ref`).

## What the host does that the agent must not

| Call | When |
|---|---|
| `tools.add_file(file_id, filename, role)` | A person uploaded a file. Same id and role again = a new version of it. |
| `tools.answer(question_id, reply)` | The person answered what the agent asked with `ask_human`. |
| `tools.approve(checkpoint, by, ref)` | The person said "go". `ref` names what they approved, e.g. `v3`; left empty it is the saved spec version at that moment. |
| `tools.reload_job()` | After an error with `"conflict": true`: another writer saved first. Repeat the failed call after. |
| `tools.record_load(output, status, rows, note)` | You ran the steps of a load plan. Works in a later turn too: the plan is kept in the state. |

Do not give these five to the agent. If it could approve, the checkpoints would mean nothing.

### Approval flow

`ask_human` returns `{"question_id": "q1", ...}` and tells the agent to stop. Show the question
to the person. When they reply, call `tools.answer("q1", reply)` and start the next turn. The
reply is kept in the knowledge log, so the agent finds it later with `lookup_knowledge`.

Checkpoints (default policy):

| Checkpoint | Default mode | Enforced by |
|---|---|---|
| `after_workbook_map` | off | nothing (reserved) |
| `after_propose_datasets` | required | `run_pipeline` refuses until approved |
| `after_spec` | optional | `run_pipeline` refuses only if you set it to `required` |
| `after_run` | required_if_fail | reminder in the `run_pipeline` text; does not block |
| `before_load` | required | `plan_load` refuses until approved for this spec version |

`plan_load` also refuses a run that is out of date: one made for another spec version, or
from a file that was registered again since. The agent then runs the pipeline again. A job
saved before this check existed has no version on its last run, so it needs one more
`run_pipeline` before its first `plan_load`. `plan_load` saves the plan in the state, so with
a store it can get a conflict error like the other tools that change the job.
| `on_rebind_drift` | required | reminder in the `rebind_sources` text; does not block |
| `accept_exclusions` | required | `plan_load` refuses while the spec's `excluded` is not approved for this spec version |

Approvals are tied to `ref`: `run_pipeline` passes `v<N>` of the saved spec, so an approval given
for `v1` does not cover `v2`. Approve after the spec is saved; an approval recorded before any spec
exists has no version and is refused by `run_pipeline`. A question still waiting also blocks a required checkpoint.

Change the policy and add context through `job.update`; there is no agent tool for it:

```python
from excel_etl.hitl import HitlPolicy
from excel_etl.knowledge import Knowledge

tools.job.update(
    lambda s: s.model_copy(
        update={
            "knowledge": Knowledge(
                brief="Monthly output, tons.",
                glossary={"PKG": "Petrokimia Gresik"},
                rules=["Ignore sheets named Grafik*"],
            ),
            "hitl": s.hitl.model_copy(
                update={
                    "policy": HitlPolicy(
                        checkpoints={"after_propose_datasets": "required", "after_spec": "required"}
                    )
                }
            ),
        }
    )
)
```

## Guarantees

- A tool never raises. A bug inside the library comes back as `internal error (...) ... library
  bug`; send those to us.
- A tool result's `text` is at most 24000 characters (about 6k tokens), with `truncated: true`.
- Ids are stable: `F1` (file), `F1.S12` (sheet), `F1.S12.R2` (table).
- Expressions the agent writes are scalar SQL checked before use: no subqueries, no file or
  system functions, no multiple statements. duckdb runs with external access off.
- Every run is checked for lost data, apart from the spec's own checks: each source column
  holding non-zero numbers must reach an output, or be listed in the spec's `excluded` with a
  reason. `plan_load` refuses a run with a lost column (`verify_run`, `last_run.verify`).
  Excluded columns need `approve("accept_exclusions", by, ref)`. The check matches by name and
  sum, so a column both renamed and filtered reads as lost: it blocks, it never lets a loss by.
- A failed run writes no file: outputs are written under a temporary name and moved into place
  only when every step and check passed.
- Files are identified by sha256. A file that changed after it was registered is refused until
  you `add_file` it again.
- `FileStore` is safe for several processes on one machine. A shared store of yours needs
  compare-and-set on the version (see Limits).

## Optional: as an MCP server

For any MCP client (Claude Code, Cursor, Maki, ...). Install with the extra: `uv sync --extra mcp`.

```json
{"mcpServers": {"excel-etl": {"command": "uv", "args": ["run", "--directory", "<path>/excel-etl",
  "--extra", "mcp", "python", "integrations/mcp_server.py",
  "--state-dir", "<state>", "--allow-dir", "<excel folder>", "serve"]}}}
```

The model gets `add_file` (a path under `--allow-dir`) plus the 25 agent tools. The human side
runs as a command on the same state: `python integrations/mcp_server.py --state-dir <state>
--allow-dir <dir> approve after_propose_datasets gilbert` (also `answer`, `record_load`).

## Limits you should know

| Limit | Effect | What to do |
|---|---|---|
| Sheet with 200k rows | about 8 s to read | acceptable; split the file if it grows a lot |
| Workbook sheet over 25 million cells (A1 to the last used cell) | refused with a message | split or trim the file |
| State grows with spec versions | `Job.update` takes about 40 ms at 150 KB | cut old versions on your side if a job lives for years |
| No limit on sources or steps in a spec | 3000 sources take 6 s to validate | cap them in your UI if needed |
| `FileStore` is for development and tests | a lock file on one disk | write a store for your database (two methods) |
| Excel errors (`#DIV/0!`) read as empty | the run warns with a count | fix in the workbook or use `fill_missing` |
| `rebind_sources` looks at headers, not values | a column turning from numbers to text is not drift | set `type` in `expect_columns`; `validate` and `run` check it |
| `reconcile` against a sheet's TOTAL row | the agent writes the `where` itself | there is no automatic detection of which row is the total |
| LiteLLM tool schemas | checked through ADK only | task 6.3: try two other providers |

## Checks before you trust a new build

```
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
# with the real workbook (internal data, never in the repo):
EXCEL_ETL_SAMPLE="path/to/BALANSITAS.xlsx" uv run pytest -q -m sample
```

## Open questions for the product side

- How many datasets may one job produce?
- Who owns the target schema (names and types) in the warehouse?
- How long are spec versions and run reports kept?

The library does not connect to a warehouse. It ends at parquet/csv plus the report, and `plan_load` plans the load into Snowflake, Databricks or BigQuery for your side to run (README, "Loading the result into a warehouse"). Not tested on a live account yet.
