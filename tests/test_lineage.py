from __future__ import annotations

import io
import zipfile

import pytest
from conftest import make_xlsx

from excel_etl.inspect.lineage import classify, lineage, region_role, sheet_role
from excel_etl.workbook import Workbook, WorkbookError


def _wb(sheets: dict, names: list[tuple[str, str, int | str | None]] | None = None) -> Workbook:
    """`names` = (name, refers_to, localSheetId or None); written into workbook.xml by hand."""
    data = make_xlsx(sheets)
    if names:
        tags = "".join(
            f'<definedName name="{n}"'
            + (f' localSheetId="{i}"' if i is not None else "")
            + f">{ref}</definedName>"
            for n, ref, i in names
        )
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
            for item in src.infolist():
                body = src.read(item.filename)
                if item.filename == "xl/workbook.xml":
                    block = f"</sheets><definedNames>{tags}</definedNames>".encode()
                    body = body.replace(b"</sheets>", block)
                dst.writestr(item, body)
        data = out.getvalue()
    return Workbook.open(data)


def _edges(wb: Workbook) -> dict[tuple[str, str], int]:
    return lineage(wb).edges


@pytest.mark.parametrize(
    ("formula", "reads"),
    [
        ("=Input!B2*2", {"Input"}),
        ("='Data 2026'!B2+1", {"Data 2026"}),
        ("='It''s'!B2", {"It's"}),
        ("=SUM(Input!B2:B9)+Input!C1", {"Input"}),
        ("=SUM(Input:Third!A1)", {"Input", "Data 2026", "Third"}),  # 3D: every sheet in between
        ('=IF(A1="Input!A1",1,0)', set()),  # a string that only looks like a reference
        ("=#REF!+1", set()),
        ("=SUM(B1:B3)", set()),  # same sheet
        ("=[1]Ext!A1+1", {"[file]"}),  # another workbook
        ("='[1]Ext sheet'!A1", {"[file]"}),
        ("=Missing!A1", {"[file]"}),  # not a sheet of this file, so it can only be a link
    ],
)
def test_formula_reads(formula: str, reads: set[str]) -> None:
    sheets = {
        "Input": [[1, 2, 3]],
        "Data 2026": [[1]],
        "Third": [[1]],
        "It's": [[1]],
        "Calc": [[None], [formula]],
    }
    got = {dst for (src, dst) in _edges(_wb(sheets)) if src == "Calc"}
    assert got == reads


def test_defined_name_reaches_its_sheet() -> None:
    sheets = {"Input": [[1], [2]], "Other": [[1]], "Calc": [["=Total*2"], ["=Plain+1"]]}
    wb = _wb(sheets, [("Total", "Input!$A$1:$A$2", None), ("Plain", "42", None)])
    assert _edges(wb) == {("Calc", "Input"): 1}


def test_local_name_beats_global_name() -> None:
    sheets = {"A": [[1]], "B": [[1]], "Calc": [["=Rate"]], "Calc2": [["=Rate"]]}
    # Calc is the third sheet (id 2): its own `Rate` points at B, the workbook-wide one at A
    wb = _wb(sheets, [("Rate", "A!$A$1", None), ("Rate", "B!$A$1", 2)])
    assert _edges(wb) == {("Calc", "B"): 1, ("Calc2", "A"): 1}


def test_name_local_to_another_sheet_is_not_visible() -> None:
    sheets = {"A": [[1]], "B": [["=Rate"]], "C": [["=Rate"]]}
    wb = _wb(sheets, [("Rate", "A!$A$1", 2)])  # local to C only
    assert _edges(wb) == {("C", "A"): 1}


def test_shared_formula_children_count() -> None:
    sheets = {"Input": [[1], [2], [3]], "Calc": [["=Input!A1"], ["=Input!A2"], ["=Input!A3"]]}
    data = make_xlsx(sheets)
    # turn the 3 formulas into one shared group, the way Excel writes them
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename.endswith("sheet2.xml"):
                body = body.replace(
                    b"<f>Input!A1</f>", b'<f t="shared" ref="A1:A3" si="0">Input!A1</f>'
                )
                body = body.replace(b"<f>Input!A2</f>", b'<f t="shared" si="0"/>')
                body = body.replace(b"<f>Input!A3</f>", b'<f t="shared" si="0"/>')
            dst.writestr(item, body)
    wb = Workbook.open(out.getvalue())
    assert wb.formulas("Calc") == {(0, 0): "Input!A1", (1, 0): "Input!A1", (2, 0): "Input!A1"}
    assert _edges(wb) == {("Calc", "Input"): 3}


def test_formula_text_is_unescaped() -> None:
    wb = _wb({"Input": [[1]], "Calc": [['=IF(Input!A1>0,"a&b","c")']]})
    assert wb.formulas("Calc") == {(0, 0): 'IF(Input!A1>0,"a&b","c")'}


@pytest.mark.parametrize(
    ("typed", "local", "cross", "role"),
    [
        (0, 0, 0, "empty"),
        (50, 0, 0, "source"),
        (50, 40, 0, "source"),  # typed data with totals is still where data comes from
        (0, 50, 0, "derived"),  # only adds itself up
        (0, 0, 50, "derived"),
        (5, 0, 95, "derived"),
        (30, 10, 60, "mixed"),
        (60, 10, 30, "mixed"),
        (95, 0, 5, "source"),
    ],
)
def test_classify(typed: int, local: int, cross: int, role: str) -> None:
    assert classify(typed, local, cross) == role


def test_roles_per_sheet_and_region() -> None:
    rows = [["Item", "Ton"], ["a", 1], ["b", 2], ["c", 3]]
    wb = _wb(
        {
            "Input": rows,
            "Copy": [["Item", "Ton"], *[[f"x{i}", f"=Input!B{i + 2}"] for i in range(3)]],
            "Totals": [["Item", "Ton"], ["a", 1], ["b", 2], ["Total", "=SUM(B2:B3)"]],
        }
    )
    assert sheet_role(wb, "Input") == "source"
    assert sheet_role(wb, "Copy") == "derived"
    assert sheet_role(wb, "Totals") == "source"
    assert region_role(wb, "Copy", (0, 0, 3, 1)) == "derived"
    assert region_role(wb, "Input", (0, 0, 0, 0)) == "empty"  # a window with only a label


def test_lineage_is_cached_and_xls_free() -> None:
    wb = _wb({"A": [[1]], "B": [["=A!A1"]]})
    assert lineage(wb) is lineage(wb)
    assert lineage(wb).cross == {"A": frozenset(), "B": frozenset({(0, 0)})}


@pytest.mark.parametrize("local", ["abc", "1.5", "-1", "99"])
def test_bad_localsheetid_is_workbook_error(local) -> None:
    # a damaged scope id must not crash with a ValueError or bind to the wrong sheet
    wb = _wb({"Data": [[1]]}, names=[("N", "Data!$A$1", local)])
    with pytest.raises(WorkbookError):
        lineage(wb)


@pytest.mark.sample
def test_sample_lineage(sample_path) -> None:
    wb = Workbook.open(sample_path)
    edges = lineage(wb).edges
    reads_bb = sum(n for (_, d), n in edges.items() if d == "(BB) CR")
    assert reads_bb > 10_000  # the raw-material sheet feeds many others
    assert sheet_role(wb, "REKAP BALANS") == "derived"
    assert sheet_role(wb, "Daftar Kapal") == "source"
    assert region_role(wb, "(N) PKG", (3, 0, 237, 28)) == "source"  # R1: typed daily tons
    assert region_role(wb, "Per Bulan", (3, 77, 55, 164)) == "derived"


def test_a_block_of_texts_is_a_source_but_one_label_or_one_row_is_not():
    from excel_etl.inspect.lineage import region_role
    from excel_etl.workbook import Workbook

    book = Workbook.open(
        make_xlsx({"S": [["Kode", "Nama"], ["a", "x"], ["b", "y"], [None, None], ["solo", None]]})
    )
    assert region_role(book, "S", (0, 0, 2, 1)) == "source"  # 2 columns, 3 rows of text
    assert region_role(book, "S", (0, 0, 0, 1)) == "empty"  # one row
    assert region_role(book, "S", (0, 0, 2, 0)) == "empty"  # one column
    assert region_role(book, "S", (4, 0, 4, 0)) == "empty"  # one label
    assert region_role(book, "S", (0, 0, 1, 1)) == "source"  # 4 texts is the least
    assert region_role(book, "S", (1, 0, 1, 1)) == "empty"


def test_the_text_table_guard_needs_two_rows_two_columns_and_four_texts():
    from excel_etl.inspect.lineage import region_role
    from excel_etl.workbook import Workbook

    book = Workbook.open(
        make_xlsx(
            {
                "S": [
                    ["a", "b", "c", "d", "e"],
                    ["f", None, None, None, None],
                    ["g", None, None, None, None],
                    ["h", None, None, None, None],
                    ["i", None, None, None, None],
                ]
            }
        )
    )
    assert region_role(book, "S", (0, 0, 0, 4)) == "empty"  # 5 texts in one row: no
    assert region_role(book, "S", (0, 0, 4, 0)) == "empty"  # 5 texts in one column: no
    assert region_role(book, "S", (0, 0, 1, 1)) == "empty"  # a 2x2 box holding 3 texts: no
    assert region_role(book, "S", (0, 0, 1, 2)) == "source"  # 2x3 holding 4 texts: yes
