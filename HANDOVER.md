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
agent_functions = tools.agent_tools()  # 21 plain methods; give them to the agent
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
| `tools.approve(checkpoint, by, ref)` | The person said "go". `ref` names what they approved, e.g. `v3`. |
| `tools.reload_job()` | After an error with `"conflict": true`: another writer saved first. Repeat the failed call after. |

Do not give these four to the agent. If it could approve, the checkpoints would mean nothing.

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
| `on_rebind_drift` | required | reminder in the `rebind_sources` text; does not block |

Approvals are tied to `ref`: `run_pipeline` passes `v<N>` of the saved spec, so an approval given
for `v1` does not cover `v2`. A question still waiting also blocks a required checkpoint.

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
- A failed run writes no file: outputs are written under a temporary name and moved into place
  only when every step and check passed.
- Files are identified by sha256. A file that changed after it was registered is refused until
  you `add_file` it again.
- `FileStore` is safe for several processes on one machine. A shared store of yours needs
  compare-and-set on the version (see Limits).

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
- Who owns the target schema (names and types) once data goes to Snowflake?
- How long are spec versions and run reports kept?

Loading to Snowflake is not in this library: it ends at parquet/csv plus the report.
