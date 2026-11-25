from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time

import openpyxl
import pytest
from conftest import make_xlsx
from openpyxl.workbook.defined_name import DefinedName as OpxName

from moyung import workbook as wbmod
from moyung.workbook import (
    CellMeta,
    DefinedName,
    Workbook,
    WorkbookError,
    _formula_texts,
    _scan_sheet,
    col_index,
)


def _rewrite(data: bytes, part: str, new: bytes | None) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            if item.filename == part:
                if new is not None:
                    z.writestr(part, new)
            else:
                z.writestr(item, src.read(item.filename))
    return out.getvalue()


def _book(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fresh_cache():
    wbmod._cache.clear()
    yield
    wbmod._cache.clear()


class TestColIndex:
    @pytest.mark.parametrize(
        "letters, expected",
        [
            ("A", 0),
            ("B", 1),
            ("Z", 25),
            ("AA", 26),
            ("AZ", 51),
            ("BA", 52),
            ("ZZ", 701),
            ("AAA", 702),
            ("XFD", 16383),
        ],
    )
    def test_known_columns(self, letters, expected):
        assert col_index(letters) == expected
        assert col_index(letters.encode()) == expected

    def test_empty_letters_is_minus_one(self):
        assert col_index("") == -1


class TestScanSheet:
    def test_formula_and_error_positions(self):
        xml = (
            b'<row><c r="A1"><v>1</v></c><c r="B1"><f>A1*2</f><v>2</v></c>'
            b'<c r="C2" t="e"><v>#DIV/0!</v></c><c r="AA3" t="e"><f>1/0</f></c></row>'
        )
        scan = _scan_sheet(xml)
        assert scan.formulas == {(0, 1), (2, 26)}
        assert scan.errors == {(1, 2), (2, 26)}
        assert scan.cells == 3 * 27

    def test_no_cells_means_zero_extent(self):
        scan = _scan_sheet(b"<sheetData/>")
        assert (scan.formulas, scan.errors, scan.cells) == (frozenset(), frozenset(), 0)

    def test_inline_string_cells_count_toward_extent(self):
        scan = _scan_sheet(b'<c r="D5" t="inlineStr"><is><t>x</t></is></c>')
        assert scan.cells == 5 * 4

    def test_cells_without_a_value_or_formula_are_not_seen(self):
        scan = _scan_sheet(b'<c r="B7" s="3"/>')
        assert scan.cells == 0


class TestFormulaTexts:
    def test_plain_formula(self):
        got = list(_formula_texts(b'<c r="B2"><f>SUM(A1:A9)</f></c>'))
        assert got == [(1, 1, "SUM(A1:A9)")]

    def test_xml_entities_are_unescaped(self):
        got = list(_formula_texts(b'<c r="A1"><f>IF(B1&lt;5,&quot;a&amp;b&quot;,0)</f></c>'))
        assert got == [(0, 0, 'IF(B1<5,"a&b",0)')]

    def test_shared_formula_children_reuse_the_master_text(self):
        xml = (
            b'<c r="B1"><f t="shared" ref="B1:B3" si="0">A1*2</f></c>'
            b'<c r="B2"><f t="shared" si="0"/></c>'
            b'<c r="B3"><f t="shared" si="0"/></c>'
        )
        assert list(_formula_texts(xml)) == [(0, 1, "A1*2"), (1, 1, "A1*2"), (2, 1, "A1*2")]

    def test_two_shared_groups_do_not_mix(self):
        xml = (
            b'<c r="A1"><f t="shared" si="0">1+1</f></c>'
            b'<c r="B1"><f t="shared" si="1">2+2</f></c>'
            b'<c r="A2"><f t="shared" si="1"/></c>'
            b'<c r="B2"><f t="shared" si="0"/></c>'
        )
        assert [t for *_, t in _formula_texts(xml)] == ["1+1", "2+2", "2+2", "1+1"]

    def test_a_shared_child_without_a_master_is_dropped(self):
        assert list(_formula_texts(b'<c r="A2"><f t="shared" si="7"/></c>')) == []

    def test_an_empty_non_shared_formula_is_dropped(self):
        assert list(_formula_texts(b'<c r="A2"><f/></c>')) == []

    def test_utf8_formula_text(self):
        got = list(_formula_texts('<c r="A1"><f>"Ünï"&amp;"é"</f></c>'.encode()))
        assert got == [(0, 0, '"Ünï"&"é"')]


class TestOpen:
    def test_open_from_bytes_path_and_str(self, tmp_path):
        data = make_xlsx({"A": [[1]]})
        path = tmp_path / "f.xlsx"
        path.write_bytes(data)
        assert Workbook.open(data).sha256 == Workbook.open(path).sha256
        assert Workbook.open(str(path)).values("A") == ((1,),)

    @pytest.mark.parametrize("source", [None, 5, 3.5, ["a"]])
    def test_wrong_source_types_are_workbook_errors(self, source):
        with pytest.raises(WorkbookError, match="cannot read file"):
            Workbook.open(source)

    def test_a_directory_is_a_workbook_error(self, tmp_path):
        with pytest.raises(WorkbookError, match="cannot read file"):
            Workbook.open(tmp_path)

    def test_nul_in_path_is_a_workbook_error(self):
        with pytest.raises(WorkbookError):
            Workbook.open("a\x00b.xlsx")

    @pytest.mark.parametrize("data", [b"", b"x", b"PK\x03\x04", b"%PDF-1.4 hello", b"a,b\n1,2\n"])
    def test_non_excel_bytes_are_workbook_errors(self, data):
        with pytest.raises(WorkbookError, match="not a readable Excel file"):
            Workbook.open(data)

    def test_a_truncated_xlsx_is_a_workbook_error(self):
        data = make_xlsx({"A": [[1, 2], [3, 4]]})
        with pytest.raises(WorkbookError):
            Workbook.open(data[: len(data) // 2])

    def test_an_xlsx_without_workbook_xml_opens_with_no_sheets(self):
        data = _rewrite(make_xlsx({"A": [[1]]}), "xl/workbook.xml", None)
        wb = Workbook.open(data)
        assert wb.sheets == ()
        with pytest.raises(WorkbookError, match="no sheet named"):
            wb.values("A")

    def test_the_cache_is_least_recently_used(self, monkeypatch):
        monkeypatch.setattr(wbmod, "_CACHE_SIZE", 2)
        a, b, c = (Workbook.open(make_xlsx({"S": [[i]]})) for i in range(3))
        assert Workbook.open(make_xlsx({"S": [[2]]})) is c
        assert Workbook.open(make_xlsx({"S": [[1]]})) is b
        assert Workbook.open(make_xlsx({"S": [[0]]})) is not a

    def test_touching_an_entry_keeps_it_alive(self, monkeypatch):
        monkeypatch.setattr(wbmod, "_CACHE_SIZE", 2)
        a = Workbook.open(make_xlsx({"S": [[0]]}))
        Workbook.open(make_xlsx({"S": [[1]]}))
        Workbook.open(make_xlsx({"S": [[0]]}))
        Workbook.open(make_xlsx({"S": [[2]]}))
        assert Workbook.open(make_xlsx({"S": [[0]]})) is a

    def test_one_changed_byte_is_a_different_workbook(self):
        data = make_xlsx({"S": [[1]]})
        other = make_xlsx({"S": [[2]]})
        assert Workbook.open(data).sha256 != Workbook.open(other).sha256


class TestSheets:
    def test_unknown_sheet_lists_names(self):
        wb = Workbook.open(make_xlsx({"A": [[1]], "B": [[2]]}))
        with pytest.raises(WorkbookError, match=r"no sheet named 'Z'; sheets: .*A.*B"):
            wb.values("Z")

    @pytest.mark.parametrize("call", ["values", "merged", "meta", "formula_cells", "error_cells"])
    def test_every_sheet_accessor_checks_the_name(self, call):
        wb = Workbook.open(make_xlsx({"A": [[1]]}))
        with pytest.raises(WorkbookError, match="no sheet named"):
            getattr(wb, call)("nope")

    def test_sheet_names_are_case_sensitive(self):
        wb = Workbook.open(make_xlsx({"Data": [[1]]}))
        with pytest.raises(WorkbookError):
            wb.values("data")

    @pytest.mark.parametrize(
        "name", ["Sheet 1", "O'Brien", "Données é", "日本語", "a&b", "x" * 31, "1"]
    )
    def test_odd_sheet_names_round_trip(self, name):
        wb = Workbook.open(make_xlsx({name: [["v", 1]]}))
        assert wb.values(name) == (("v", 1),)
        assert wb.formula_cells(name) == frozenset()

    def test_visibility_levels(self):
        def build(wb):
            wb.active.title = "V"
            wb.create_sheet("H").sheet_state = "hidden"
            wb.create_sheet("VH").sheet_state = "veryHidden"

        wb = Workbook.open(_book(build))
        assert [(s.name, s.visible) for s in wb.sheets] == [
            ("V", "visible"),
            ("H", "hidden"),
            ("VH", "very_hidden"),
        ]

    def test_a_workbook_of_empty_sheets(self):
        wb = Workbook.open(make_xlsx({"A": [], "B": []}))
        assert wb.values("A") == () and wb.values("B") == ()
        assert wb.merged("A") == ()

    def test_many_sheets_error_message_is_truncated(self):
        wb = Workbook.open(make_xlsx({f"S{i}": [[i]] for i in range(9)}))
        with pytest.raises(WorkbookError, match=r"\+4"):
            wb.values("nope")


class TestValues:
    def test_integer_floats_become_ints_and_real_floats_stay(self):
        wb = Workbook.open(make_xlsx({"S": [[3.0, 2.5, -0.0, 1e20, 1e-9]]}))
        row = wb.values("S")[0]
        assert [type(v) for v in row[:3]] == [int, float, int]
        assert row[2] == 0 and row[3] == 10**20 and row[4] == 1e-9

    def test_booleans_stay_booleans(self):
        row = Workbook.open(make_xlsx({"S": [[True, False, 1, 0]]})).values("S")[0]
        assert row[0] is True and row[1] is False
        assert type(row[2]) is int and type(row[3]) is int

    def test_dates_times_and_datetimes(self):
        row = Workbook.open(
            make_xlsx({"S": [[date(2026, 2, 28), datetime(2026, 2, 28, 13, 5, 7), time(8, 30)]]})
        ).values("S")[0]
        assert row[0] == date(2026, 2, 28)
        assert row[1] == datetime(2026, 2, 28, 13, 5, 7)
        assert row[2] == time(8, 30)

    def test_empty_strings_and_missing_cells_are_none(self):
        row = Workbook.open(make_xlsx({"S": [["", None, "x"]]})).values("S")[0]
        assert row[:2] == (None, None) and row[2] == "x"

    @pytest.mark.parametrize(
        "text", ["line1\nline2", "tab\there", "😀", "<b>&amp;</b>", "'q'", "\u00a0"]
    )
    def test_text_that_looks_special_is_kept_verbatim(self, text):
        data = make_xlsx({"S": [[text]]})
        got = Workbook.open(data).values("S")[0][0]
        assert got == text

    def test_long_strings(self):
        text = "x" * 32000
        assert Workbook.open(make_xlsx({"S": [[text]]})).values("S")[0][0] == text

    def test_ragged_rows_are_kept_as_given(self):
        rows = Workbook.open(make_xlsx({"S": [[1], [1, 2, 3]]})).values("S")
        assert rows[1] == (1, 2, 3) and rows[0][0] == 1

    def test_values_are_cached(self):
        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        assert wb.values("S") is wb.values("S")

    def test_a_stray_far_cell_hits_the_extent_limit(self, monkeypatch):
        monkeypatch.setattr(wbmod, "_MAX_CELLS", 50)
        wb = Workbook.open(make_xlsx({"S": [[1]] + [[]] * 99 + [[None] * 9 + ["x"]]}))
        with pytest.raises(WorkbookError, match="spans .* cells"):
            wb.values("S")

    def test_at_the_extent_limit_is_still_allowed(self, monkeypatch):
        monkeypatch.setattr(wbmod, "_MAX_CELLS", 4)
        wb = Workbook.open(make_xlsx({"S": [[1, 2], [3, 4]]}))
        assert wb.values("S") == ((1, 2), (3, 4))

    def test_an_oversized_part_is_refused(self, monkeypatch):
        monkeypatch.setattr(wbmod, "_MAX_PART", 10)
        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        with pytest.raises(WorkbookError, match="larger than"):
            wb.values("S")


class TestFormulas:
    def _wb(self):
        def build(wb):
            ws = wb.active
            ws.title = "S"
            ws["A1"] = 4
            ws["B1"] = "=A1*2"
            ws["C1"] = "=1/0"
            ws["A2"] = "#N/A"
            ws["B2"] = '=IF(A1<5,"a&b","c")'

        return Workbook.open(_book(build))

    def test_formula_cells(self):
        assert self._wb().formula_cells("S") == {(0, 1), (0, 2), (1, 1)}

    def test_formula_text_has_no_equals_sign(self):
        formulas = self._wb().formulas("S")
        assert formulas[(0, 1)] == "A1*2"
        assert formulas[(1, 1)] == 'IF(A1<5,"a&b","c")'

    def test_error_literal_is_an_error_cell(self):
        assert (1, 0) in self._wb().error_cells("S")

    def test_a_sheet_without_formulas(self):
        wb = Workbook.open(make_xlsx({"S": [[1, 2]]}))
        assert wb.formula_cells("S") == frozenset() and wb.formulas("S") == {}
        assert wb.error_cells("S") == frozenset()

    def test_results_are_cached(self):
        wb = self._wb()
        assert wb.formulas("S") is wb.formulas("S")
        assert wb.formula_cells("S") is wb.formula_cells("S")

    @pytest.mark.parametrize("call", ["values", "merged", "meta"])
    def test_a_damaged_sheet_part_is_a_workbook_error(self, call):
        data = _rewrite(make_xlsx({"S": [[1]]}), "xl/worksheets/sheet1.xml", b"<broken")
        with pytest.raises(WorkbookError, match="damaged file"):
            getattr(Workbook.open(data), call)("S")

    def test_the_regex_scan_reads_a_damaged_sheet_as_having_no_formulas(self):
        data = _rewrite(make_xlsx({"S": [[1]]}), "xl/worksheets/sheet1.xml", b"<broken")
        assert Workbook.open(data).formula_cells("S") == frozenset()

    def test_a_missing_sheet_part_is_a_workbook_error(self):
        data = _rewrite(make_xlsx({"S": [[1]]}), "xl/worksheets/sheet1.xml", None)
        with pytest.raises(WorkbookError):
            Workbook.open(data).formula_cells("S")

    def test_a_missing_rels_part_is_a_workbook_error(self):
        data = _rewrite(make_xlsx({"S": [[1]]}), "xl/_rels/workbook.xml.rels", None)
        with pytest.raises(WorkbookError):
            Workbook.open(data).formula_cells("S")


class TestDefinedNames:
    def test_global_and_local_names(self):
        def build(wb):
            wb.active.title = "A"
            wb.create_sheet("B")
            wb.defined_names["Total"] = OpxName("Total", attr_text="A!$A$1:$A$9")
            wb["B"].defined_names["Local"] = OpxName("Local", attr_text="B!$C$3")

        names = Workbook.open(_book(build)).defined_names()
        assert DefinedName("Total", "A!$A$1:$A$9", None) in names
        assert DefinedName("Local", "B!$C$3", "B") in names

    def test_no_names(self):
        assert Workbook.open(make_xlsx({"S": [[1]]})).defined_names() == ()

    def test_a_bad_local_sheet_id_is_a_workbook_error(self):
        def build(wb):
            wb.active.title = "A"
            wb.defined_names["N"] = OpxName("N", attr_text="A!$A$1")

        data = _book(build)
        xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/workbook.xml")
        patched = xml.replace(b"<definedName ", b'<definedName localSheetId="9" ')
        with pytest.raises(WorkbookError, match="bad localSheetId"):
            Workbook.open(_rewrite(data, "xl/workbook.xml", patched)).defined_names()

    def test_a_non_numeric_local_sheet_id_is_a_workbook_error(self):
        def build(wb):
            wb.active.title = "A"
            wb.defined_names["N"] = OpxName("N", attr_text="A!$A$1")

        data = _book(build)
        xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/workbook.xml")
        patched = xml.replace(b"<definedName ", b'<definedName localSheetId="x" ')
        with pytest.raises(WorkbookError, match="bad localSheetId"):
            Workbook.open(_rewrite(data, "xl/workbook.xml", patched)).defined_names()

    def test_names_are_cached(self):
        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        assert wb.defined_names() is wb.defined_names()


class TestMerged:
    def test_several_ranges_sorted(self):
        data = make_xlsx(
            {"S": [["a", None, None], ["b", None, None], [None, None, "c"]]},
            merged={"S": ["C3:C3", "A1:B1", "A2:A3"]},
        )
        got = Workbook.open(data).merged("S")
        assert got == tuple(sorted(got))
        assert (0, 0, 0, 1) in got and (1, 0, 2, 0) in got

    def test_a_sheet_without_merges(self):
        assert Workbook.open(make_xlsx({"S": [[1]]})).merged("S") == ()

    def test_merged_is_cached(self):
        wb = Workbook.open(make_xlsx({"S": [[1, None]]}, merged={"S": ["A1:B1"]}))
        assert wb.merged("S") is wb.merged("S")


class TestMeta:
    def test_plain_cells_are_left_out(self):
        assert Workbook.open(make_xlsx({"S": [[1, "x"]]})).meta("S") == {}

    def test_empty_sheet_meta(self):
        assert Workbook.open(make_xlsx({"S": []})).meta("S") == {}

    def test_bold_fill_border_format_and_formula(self):
        from openpyxl.styles import Border, Font, PatternFill, Side

        def build(wb):
            ws = wb.active
            ws.title = "S"
            ws["A1"] = 1
            ws["A1"].font = Font(bold=True)
            ws["B1"] = 2
            ws["B1"].fill = PatternFill("solid", fgColor="FFFF00")
            ws["C1"] = 3
            ws["C1"].border = Border(top=Side(style="thin"))
            ws["D1"] = 0.5
            ws["D1"].number_format = "0.00%"
            ws["E1"] = "=A1+B1"
            ws["F1"] = 9

        meta = Workbook.open(_book(build)).meta("S")
        assert meta[(0, 0)] == CellMeta(bold=True)
        assert meta[(0, 1)] == CellMeta(fill=True)
        assert meta[(0, 2)] == CellMeta(border=True)
        assert meta[(0, 3)] == CellMeta(number_format="0.00%")
        assert meta[(0, 4)].formula == "=A1+B1"

    @pytest.mark.xfail(
        strict=True, reason="BUG-008: meta() misses formulas past the last cached cell"
    )
    def test_a_formula_without_a_cached_value_is_in_meta(self):
        def build(wb):
            ws = wb.active
            ws.title = "S"
            ws["A1"] = 1
            ws["B1"] = "=A1*2"

        wb = Workbook.open(_book(build))
        assert (0, 1) in wb.formula_cells("S")
        assert wb.meta("S")[(0, 1)].formula == "=A1*2"

    def test_meta_is_cached(self):
        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        assert wb.meta("S") is wb.meta("S")
