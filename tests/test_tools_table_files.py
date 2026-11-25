from __future__ import annotations

import inspect
import io
import tempfile
from pathlib import Path

import duckdb
import pytest
from test_tools import Provider

from moyung.state import Job, new_state
from moyung.tools import JobTools

CSV = b"Item,Qty\npen,2\nink,1\npad,3\n"
JSONA = b'[{"Item":"pen","Qty":2},{"Item":"ink","Qty":1}]'
JSONL = b'{"Item":"pen","Qty":2}\n{"Item":"ink","Qty":1}\n'
LATIN1 = b"a,b\n1,caf\xe9\n"


def _parquet() -> bytes:
    path = Path(tempfile.mkdtemp()) / "t.parquet"
    duckdb.sql(f"COPY (SELECT 'pen' Item, 2 Qty) TO '{path.as_posix()}' (FORMAT PARQUET)")
    return path.read_bytes()


FILES = {
    "csv": ("sales.csv", CSV),
    "tsv": ("sales.tsv", CSV.replace(b",", b"\t")),
    "json": ("sales.json", JSONA),
    "jsonl": ("sales.jsonl", JSONL),
    "parquet": ("sales.parquet", _parquet()),
}
FORMAT_IN_MESSAGE = {"tsv": "csv"}
SHEET_TOOLS = {
    "sheet_regions": {"file": "F1", "sheet": "S1"},
    "render_range": {"file": "F1", "ref": "S1", "range": "A1:B3"},
    "find_text": {"file": "F1", "query": "pen"},
    "compare_sheets": {"file": "F1", "a": "x", "b": "y"},
    "formula_lineage": {"file": "F1"},
    "propose_datasets": {"file": "F1"},
}
REGION_TOOLS = {
    "profile_region": {"region": "F1"},
    "sample_data": {"region": "F1"},
}


def _tools(tmp_path, filename, data, second=None):
    blobs = {"c": data, "d": second if second is not None else data}
    tools = JobTools(Job(new_state("j")), Provider(blobs), tmp_path / "out")
    assert "registered" in tools.add_file("c", filename, "f")["text"]
    return tools


def _refused(tmp_path, filename, data):
    tools = JobTools(Job(new_state("j")), Provider({"c": data}), tmp_path / "out")
    return tools.add_file("c", filename, "f")


def _call(tools, name, given):
    fn = getattr(tools, name)
    params = inspect.signature(fn).parameters
    return fn(**{k: v for k, v in given.items() if k in params})


@pytest.mark.parametrize("kind", FILES)
class TestEveryKind:
    def test_list_files_names_the_kind(self, tmp_path, kind):
        text = _tools(tmp_path, *FILES[kind]).list_files()["text"]
        assert text.startswith("F1|")
        assert text.rstrip().endswith(("csv", "parquet", "json", "jsonl"))

    def test_workbook_map_shows_the_columns(self, tmp_path, kind):
        text = _tools(tmp_path, *FILES[kind]).workbook_map("F1")["text"]
        assert "columns" in text and "Item" in text and "Qty" in text

    @pytest.mark.parametrize("tool", list(SHEET_TOOLS))
    def test_a_sheet_tool_refuses_with_the_way_forward(self, tmp_path, kind, tool):
        result = _call(_tools(tmp_path, *FILES[kind]), tool, SHEET_TOOLS[tool])
        assert "no sheets or regions" in result["error"]
        assert "workbook_map" in result["error"]
        assert FORMAT_IN_MESSAGE.get(kind, kind) in result["error"]

    @pytest.mark.parametrize("tool", list(REGION_TOOLS))
    def test_a_region_tool_refuses_without_an_internal_error(self, tmp_path, kind, tool):
        result = _call(_tools(tmp_path, *FILES[kind]), tool, REGION_TOOLS[tool])
        assert "error" in result and "internal error" not in result["error"]

    def test_the_file_can_be_found_by_position_id_or_name(self, tmp_path, kind):
        tools = _tools(tmp_path, *FILES[kind])
        refs = ("F1", "f1", "c", FILES[kind][0])
        assert len({tools.workbook_map(ref)["text"] for ref in refs}) == 1

    def test_a_second_file_of_the_same_kind_is_F2(self, tmp_path, kind):
        tools = _tools(tmp_path, *FILES[kind])
        suffix = FILES[kind][0].split(".")[-1]
        tools.add_file("d", f"other.{suffix}", "g")
        assert tools.list_files()["text"].count("\nF2|") == 1

    def test_ml_candidates_profiles_the_table(self, tmp_path, kind):
        text = _tools(tmp_path, *FILES[kind]).ml_candidates("F1")["text"]
        assert "Item|VARCHAR" in text and "Qty|BIGINT" in text


class TestBrokenTableFiles:
    @pytest.mark.xfail(
        strict=True, reason="BUG-001: a non-UTF-8 csv leaks InvalidInputException from the tools"
    )
    @pytest.mark.parametrize("tool", ["workbook_map", "ml_candidates"])
    def test_a_latin1_csv_registers_and_reads_as_a_clean_error(self, tmp_path, tool):
        result = getattr(_tools(tmp_path, "s.csv", LATIN1), tool)("F1")
        assert "error" in result and "internal error" not in result["error"]

    def test_a_latin1_csv_is_never_an_exception(self, tmp_path):
        tools = _tools(tmp_path, "s.csv", LATIN1)
        assert isinstance(tools.workbook_map("F1"), dict)
        assert isinstance(tools.ml_candidates("F1"), dict)

    @pytest.mark.parametrize(
        "name, data, needle",
        [
            ("s.csv", b"\xff\xfea\x00,\x00b\x00\n\x00", "UTF-8"),
            ("s.csv", b"", "empty"),
            ("s.csv", b"a\x00b", "looks binary"),
            ("s.parquet", CSV, "not a Parquet file"),
            ("s.xlsx", CSV, "not a readable Excel file"),
            ("s.json", b'{"a":1}', "array of objects"),
            ("s.json", b'[{"a":1}', "cut off"),
            ("s.jsonl", b"[1]", "should start with {"),
        ],
    )
    def test_a_file_that_cannot_be_opened_is_refused_when_it_is_registered(
        self, tmp_path, name, data, needle
    ):
        result = _refused(tmp_path, name, data)
        assert "cannot be opened" in result["error"] and needle in result["error"]

    def test_a_refused_file_leaves_the_job_without_files(self, tmp_path):
        tools = JobTools(Job(new_state("j")), Provider({"c": b""}), tmp_path / "out")
        tools.add_file("c", "s.csv", "f")
        assert tools.job.state.files == []

    def test_a_workbook_named_csv_is_refused_without_an_internal_error(self, tmp_path):
        import openpyxl

        buf = io.BytesIO()
        wb = openpyxl.Workbook()
        wb.active.append(["a", "b"])
        wb.save(buf)
        result = _refused(tmp_path, "s.csv", buf.getvalue())
        assert "internal error" not in str(result.get("error", ""))


class TestRelateAndSourceNeedRegionIds:
    @pytest.mark.parametrize("tool", ["relate_tables", "source_for_region"])
    def test_a_file_id_is_not_a_region_id(self, tmp_path, tool):
        tools = _tools(tmp_path, *FILES["csv"])
        result = _call(tools, tool, {"a": "F1", "b": "F1", "region_id": "F1"})
        assert "not a region id" in result["error"]

    @pytest.mark.xfail(
        strict=True, reason="BUG-004: relate_tables demands F1.S2.R1 even for a table file"
    )
    def test_relate_tables_accepts_two_table_files(self, tmp_path):
        tools = _tools(tmp_path, *FILES["csv"])
        tools.add_file("d", "more.csv", "g")
        assert "error" not in tools.relate_tables("F1", "F2")
