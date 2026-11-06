from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import openpyxl
import pytest


def make_xlsx(
    sheets: dict[str, list[list[Any]]],
    merged: dict[str, list[str]] | None = None,
    hidden: tuple[str, ...] = (),
) -> bytes:
    """Build a workbook in memory. Rows start at A1; use None for empty cells."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
        for rng in (merged or {}).get(name, []):
            ws.merge_cells(rng)
        if name in hidden:
            ws.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def sample_path() -> Path:
    """The real BALANSITAS file. It is internal data, so it never lives in the repo."""
    path = os.environ.get("EXCEL_ETL_SAMPLE")
    if not path or not Path(path).is_file():
        pytest.skip("EXCEL_ETL_SAMPLE not set")
    return Path(path)
