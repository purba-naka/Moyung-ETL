from __future__ import annotations

import json

import pytest

from excel_etl.assets import asset_metadata
from excel_etl.pipeline import SOURCE_KINDS, SpecError, dump_spec, parse_spec
from excel_etl.state import Job, add_file, new_state
from excel_etl.tools import JobTools
from excel_etl.workbook import Workbook
from tests.conftest import make_xlsx

SPEC = {
    "sources": {
        "sales": {
            "file_role": "main",
            "sheet": {"name": "Sales"},
            "region": {"header_anchor": ["Item"]},
        }
    },
    "steps": [],
    "outputs": {},
}


class _Provider:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, file_id: str) -> bytes:
        return self.data


def _book() -> Workbook:
    return Workbook.open(
        make_xlsx(
            {
                "Sales": [["Item", "Qty"], ["a", 1], ["b", 2]],
                "Hidden": [["x"]],
                "Empty": [],
            },
            hidden=("Hidden",),
        )
    )


def test_old_spec_without_kind_is_still_excel_and_roundtrips():
    spec = parse_spec(json.dumps(SPEC))
    assert spec.sources["sales"].kind == "excel"
    assert parse_spec(dump_spec(spec)) == spec


def test_unknown_kind_lists_supported_kinds():
    bad = json.loads(json.dumps(SPEC))
    bad["sources"]["sales"]["kind"] = "csv"
    with pytest.raises(SpecError, match="excel"):
        parse_spec(json.dumps(bad))


def test_metadata_describes_excel_asset_with_capabilities():
    wb = _book()
    meta = asset_metadata(wb, "F1", "sales.xlsx", "main")
    assert meta.asset_id == "F1"
    assert meta.source_type == "excel"
    assert meta.sha256 == wb.sha256
    assert meta.display_name == "sales.xlsx"
    assert meta.role == "main"
    assert meta.details == {"sheets": 3, "hidden_sheets": 1}
    assert {"sheets", "regions", "formulas", "merged_cells"} <= set(meta.capabilities)
    assert "credentials" not in json.dumps(meta.model_dump())


def test_metadata_without_role_has_none():
    assert asset_metadata(_book(), "F1", "x.xlsx", None).role is None


def test_source_kinds_is_single_source_of_truth():
    assert SOURCE_KINDS == ("excel",)


def test_list_files_shows_source_type():
    data = make_xlsx({"Sales": [["Item"], ["a"]]})
    state = add_file(new_state("j"), "id1", "sales.xlsx", Workbook.open(data).sha256, "main")
    tools = JobTools(Job(state), _Provider(data))
    text = tools.list_files()["text"]
    assert text == "F1|sales.xlsx|role main|excel"
