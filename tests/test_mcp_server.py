from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

import pytest
from conftest import make_xlsx

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrations"))
import mcp_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _setup(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sales.xlsx").write_bytes(
        make_xlsx({"Sales": [["Date", "Qty"], ["2026-01-01", 3], ["2026-01-02", 5]]})
    )
    state = str(tmp_path / "state")
    ns = argparse.Namespace(state_dir=state, allow_dir=str(data), out_dir="", job="default")
    return ["--state-dir", state, "--allow-dir", str(data)], mcp_server.make_tools(ns)


def _text(result) -> str:
    return result.content[0].text


@pytest.mark.anyio
async def test_mcp_tools_flat_and_flow(tmp_path: Path) -> None:
    argv, fresh = _setup(tmp_path)
    async with Client(mcp_server.build_server(fresh)) as c:
        tools = (await c.list_tools()).tools
        names = [t.name for t in tools]
        assert names[0] == "add_file" and len(names) == 26
        assert not {"answer", "approve", "record_load", "reload_job"} & set(names)
        for t in tools:
            schema = json.dumps(t.input_schema)
            assert not any(k in schema for k in ('"anyOf"', '"oneOf"', '"$ref"')), t.name

        added = await c.call_tool("add_file", {"file_id": "sales.xlsx", "filename": "s.xlsx"})
        assert "registered" in _text(added)
        outside = await c.call_tool("add_file", {"file_id": "../x.xlsx", "filename": "x"})
        assert "outside the allowed folder" in _text(outside)
        assert "Sales" in _text(await c.call_tool("workbook_map", {"file": "F1"}))

    # the human side runs in another process; the server sees it without a reload
    assert mcp_server.main([*argv, "approve", "after_propose_datasets", "gilbert"]) == 0
    async with Client(mcp_server.build_server(fresh)) as c:
        assert "after_propose_datasets" in _text(await c.call_tool("job_summary", {}))


def test_library_never_imports_mcp() -> None:
    for path in (ROOT / "excel_etl").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            assert not any(m.split(".")[0] == "mcp" for m in mods), path
