"""Optional MCP integration: serve the excel-etl agent tools over stdio, for any MCP client.

The library itself knows nothing about MCP; this file is a thin host around `JobTools`.

    uv run --extra mcp python integrations/mcp_server.py --state-dir S --allow-dir D serve
    uv run python integrations/mcp_server.py --state-dir S --allow-dir D approve CHECKPOINT BY

The second form is the human side (`answer`, `approve`, `record_load`, or any tool by name).
Those calls are never given to the model, so it cannot approve its own work.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout

from excel_etl.state import FileStore, Job  # noqa: E402
from excel_etl.tools import JobTools  # noqa: E402


class DiskFiles:
    """`file_id` is a path on disk, allowed only under `root` (the trust boundary)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def open(self, file_id: str) -> Path:
        path = Path(file_id).expanduser()
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError(f"{file_id!r} is outside the allowed folder {self.root}")
        return path


def make_tools(args: argparse.Namespace) -> Callable[[], JobTools]:
    store, files = FileStore(args.state_dir), DiskFiles(Path(args.allow_dir))
    out = args.out_dir or str(Path(args.state_dir) / "out" / args.job)
    # A fresh JobTools per call reads the state again, so a human's `approve` from the other
    # process is seen at once instead of ending in a version conflict.
    return lambda: JobTools(Job.open(args.job, store), files, out_dir=out, per_version=True)


def build_server(fresh: Callable[[], JobTools]):  # -> MCPServer, kept untyped: mcp is optional
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "excel-etl",
        instructions="Read raw Excel, write a pipeline spec, run it. Register a file with "
        "add_file(path, filename) first. Approvals come from the human, never from you.",
    )
    tools = fresh()
    for method in [tools.add_file, *tools.agent_tools()]:
        name = method.__name__

        def call(*a, _name: str = name, **kw) -> dict:
            return getattr(fresh(), _name)(*a, **kw)

        # signature + resolved hints come from the real method, so the schema stays flat
        call = functools.wraps(method)(functools.partial(call))
        call.__signature__ = inspect.signature(method, eval_str=True)  # type: ignore[attr-defined]
        call.__annotations__ = {
            p.name: p.annotation
            for p in call.__signature__.parameters.values()  # type: ignore[attr-defined]
        } | {"return": dict}
        server.add_tool(call, name=name, description=inspect.getdoc(method))
    return server


def run_host(fresh: Callable[[], JobTools], name: str, raw: list[str]) -> dict:
    """One call from the human side; text arguments are cast to the tool's int parameters."""
    method = getattr(fresh(), name, None)
    if not callable(method) or name.startswith("_"):
        return {"error": f"no tool {name!r}"}
    params = list(inspect.signature(method, eval_str=True).parameters.values())
    values = [int(v) if p.annotation is int else v for p, v in zip(params, raw, strict=False)]
    return method(*values)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--state-dir", required=True, help="where job state JSON is kept")
    ap.add_argument("--allow-dir", required=True, help="Excel files are read only from here")
    ap.add_argument("--out-dir", default="", help="default: <state-dir>/out/<job>")
    ap.add_argument("--job", default="default")
    ap.add_argument("command", help="`serve`, or a tool name to run as the human")
    ap.add_argument("args", nargs="*")
    args = ap.parse_args(argv)
    fresh = make_tools(args)
    if args.command == "serve":
        build_server(fresh).run("stdio")
        return 0
    result = run_host(fresh, args.command, args.args)
    print(result.get("text") or json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
