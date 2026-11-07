from __future__ import annotations

from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import a1
from excel_etl.inspect.regions import detect_regions
from excel_etl.workbook import Workbook


def _ref(box: tuple[int, int, int, int]) -> str:
    return f"{a1(box[0], box[1])}:{a1(box[2], box[3])}"


def test_grouped_header_with_dashboard_noise() -> None:
    rows = [
        ["NPK PKG", "Penjualan", None, None, None],
        ["DASHBOARD", 10, 0, 20, 0],
        ["TANGGAL", "Phonska 1", None, "Phonska 2", None],
        [None, "Ton", "Formula", "Ton", "Formula"],
        *[[date(2026, 1, d), d, "sub c", d * 2, "sub c"] for d in range(1, 6)],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}, merged={"S": ["B3:C3", "D3:E3"]}))
    (r,) = detect_regions(wb, "S")
    assert _ref(r.box) == "A3:E9"
    assert r.header_rows == (2, 3)
    assert len(r.merged_header) == 2
    assert [_ref(n) for n in r.noise] == ["A1:E2"]
    (g,) = r.groups
    assert (g.start, g.period, g.count, g.labels) == (1, 2, 2, ("Ton", "Formula"))
    assert g.parents == ("Phonska 1", "Phonska 2")


def test_total_row_and_repeated_header_split() -> None:
    hdr = [["Deskripsi", "JANUARI", None, "FEBRUARI", None], [None, "PKG", "PKC", "PKG", "PKC"]]
    body = [["1.1. Subsidi", 1, 2, 3, 4], ["1.2. Retail", 5, 6, 7, 8], ["Total", 6, 8, 10, 12]]
    rows = [*hdr, *body, [None] * 5, *hdr, *body]
    wb = Workbook.open(make_xlsx({"S": rows}))
    a, b = detect_regions(wb, "S")
    assert (_ref(a.box), _ref(b.box)) == ("A1:E5", "A7:E11")
    assert a.header_rows == (0, 1) and b.header_rows == (6, 7)
    assert a.total_rows == (4,) and b.total_rows == (10,)
    assert a.orientation == "matrix"
    assert a.groups[0].parents == ("JANUARI", "FEBRUARI")


def test_side_by_side_tables_and_tiny_notes() -> None:
    rows = [
        ["a", "b", None, None, "x", "y"],
        [1, 2, None, None, 3, 4],
        [1, 2, None, None, 3, 4],
        [1, 2, None, None, 3, 4],
        [None] * 6,
        ["note"],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}))
    assert [_ref(r.box) for r in detect_regions(wb, "S")] == ["A1:B4", "E1:F4"]


def test_empty_sheet() -> None:
    wb = Workbook.open(make_xlsx({"S": []}))
    assert detect_regions(wb, "S") == []


@pytest.mark.sample
def test_sample_layouts(sample_path) -> None:
    wb = Workbook.open(sample_path)
    pkg = detect_regions(wb, "(N) PKG")[0]
    assert _ref(pkg.box).startswith("A4:") and pkg.header_rows == (3, 4)
    assert pkg.groups[0].labels == ("Ton", "Formula") and pkg.groups[0].count >= 9

    big = [r for r in detect_regions(wb, "Per Bulan") if r.header_rows]
    assert len(big) >= 3
    assert {_ref(r.box).split(":")[0] for r in big} >= {"A4", "A58"}
    assert big[0].groups[0].parents[:2] == ("JANUARI", "FEBRUARI")

    rkap = detect_regions(wb, "RKAP 2026")[0]
    assert _ref(rkap.box).startswith("B3:") and rkap.header_rows == (2, 3)

    rekap = detect_regions(wb, "REKAP BALANS")
    assert _ref(rekap[0].box) == "A2:N9" and rekap[0].header_rows == (1, 2)
    assert len(rekap[0].noise) == 1  # "Periode | Jul"


# --- tables made only of text (master and mapping tables) ------------------------------------


def _text_book(rows, bold=True, col0=0, row0=0):
    import io

    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if v is not None:
                ws.cell(row=row0 + r + 1, column=col0 + c + 1, value=v)
    if bold:
        for cell in ws[row0 + 1]:
            if cell.value is not None:
                cell.font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return Workbook.open(buf.getvalue())


def _regions(wb):
    return [(r.header_rows, r.orientation) for r in detect_regions(wb, "S")]


_MASTERS = {
    "product master": [["Kode", "Nama", "Kategori", "Satuan"]]
    + [[f"P{i:03d}", f"Produk {i}", "Pupuk" if i % 2 else "Kimia", "kg"] for i in range(30)],
    "mapping of two columns": [["Plant", "Region"]]
    + [[f"Plant {i}", "Jawa Timur" if i % 3 else "Kalimantan"] for i in range(8)],
    "indonesian headers": [["No Rek", "Nama Akun", "Jenis"]]
    + [[f"1-{i:04d}", f"Akun {i}", "Aset" if i % 2 else "Beban"] for i in range(12)],
    "five rows only": [["Kode", "Nama"]] + [[f"K{i}", f"N{i}"] for i in range(3)],
}


@pytest.mark.parametrize("name", list(_MASTERS))
def test_a_table_of_texts_with_a_bold_header_is_found(name):
    assert _regions(_text_book(_MASTERS[name])) == [((0,), "rows")]


@pytest.mark.parametrize("name", ["product master", "mapping of two columns"])
def test_a_plain_header_is_found_when_no_header_text_repeats_below(name):
    assert _regions(_text_book(_MASTERS[name], bold=False)) == [((0,), "rows")]


def test_a_table_of_texts_away_from_a_corner_keeps_its_box():
    wb = _text_book(_MASTERS["product master"], col0=3, row0=5)
    (reg,) = detect_regions(wb, "S")
    assert reg.box == (5, 3, 35, 6) and reg.header_rows == (5,)


def test_the_header_is_listed_and_proposed_with_a_source_role():
    from excel_etl.inspect.encode import propose_datasets, sheet_regions

    wb = _text_book(_MASTERS["product master"])
    assert (
        "hdr 1" in sheet_regions(wb, "F1", "S")["text"]
        and "rows/source" in sheet_regions(wb, "F1", "S")["text"]
    )
    text = propose_datasets(wb, "F1")["text"]
    assert "1 datasets to choose from (1 source" in text and "A1:D31|source|30|4|Kode, Nama" in text


@pytest.mark.parametrize(
    "name,rows",
    [
        (
            "two lines of prose",
            [["Catatan:", "harga belum termasuk pajak"], ["Dibuat oleh", "tim keuangan"]],
        ),
        ("a short list", [["Kode", "Nama"], ["A", "x"], ["B", "y"]]),
        (
            "a column of repeated words",
            [["a", "a"], ["a", "a"], ["a", "a"], ["a", "a"], ["a", "a"]],
        ),
        (
            "sparse notes",
            [
                ["Judul", None, None],
                [None, "x", None],
                [None, None, "y"],
                ["z", None, None],
                [None, "w", None],
            ],
        ),
    ],
)
def test_text_that_is_not_a_table_does_not_become_one(name, rows):
    assert all(not hdr for hdr, _ in _regions(_text_book(rows, bold=False))), name


def test_a_slice_of_a_numeric_table_is_not_taken_for_a_master():
    rows = [
        ["MAXIMUS", "SO", 3800],
        ["MAXIMUS", "SO", 4000],
        ["MAXIMUS", "SO", 4100],
        ["MAXIMUS", "SO", 4200],
    ]
    assert all(not hdr for hdr, _ in _regions(_text_book(rows, bold=False)))


def test_a_header_text_repeated_lower_in_its_column_without_bold_is_not_a_header():
    rows = [["Item", "Item"], ["a", "b"], ["Item", "c"], ["d", "e"], ["f", "g"]]
    assert all(not hdr for hdr, _ in _regions(_text_book(rows, bold=False)))
    # the same block is read as a table once the top row is bold, which is the signal people use
    assert _regions(_text_book([["Item", "Kind"], *rows[1:]], bold=True)) == [((0,), "rows")]


def test_a_formula_inside_a_text_block_keeps_the_numeric_path_not_the_master_rule():
    """`_text_header` is only for blocks with no data row. A formula makes a row count as
    data, so the older rule decides (here: one data row, and the block stays headerless)."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    for r in [["Kode", "Nama"], ["a", "x"], ["b", "y"], ["c", "z"], ["d", "=A2&B2"]]:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    book = Workbook.open(buf.getvalue())
    from excel_etl.inspect.regions import SheetGrid, _text_header

    g = SheetGrid(book, "S")
    assert _text_header(g, (0, 0, 4, 1)) is False  # the master rule itself refuses formulas


def _grid(rows, bold=False):
    from excel_etl.inspect.regions import SheetGrid

    return SheetGrid(_text_book(rows, bold=bold), "S")


def _verdict(rows, bold=False):
    from excel_etl.inspect.regions import _text_header

    g = _grid(rows, bold)
    width = max(len(r) for r in rows)
    return _text_header(g, (0, 0, len(rows) - 1, width - 1))


_GOOD = [["Kode", "Nama"]] + [[f"K{i}", f"Nama {i}"] for i in range(6)]


def test_each_rule_of_the_master_table_alone_turns_a_good_block_down():
    assert _verdict(_GOOD) is True
    assert _verdict([["Kode", "Kode"], *_GOOD[1:]], bold=True) is False  # two columns, one name
    sparse = [
        ["Kode", "Nama", "Jenis"],
        ["a", None, None],
        ["b", None, None],
        ["c", None, None],
        ["d", None, None],
        ["e", "x", "y"],
    ]
    assert _verdict(sparse, bold=True) is False  # most rows are half empty
    repeated = [["Kode", "Nama"], ["a", "x"], ["Kode", "y"], ["c", "z"], ["d", "w"], ["e", "v"]]
    assert _verdict(repeated, bold=False) is False  # the header text is also a record
    assert _verdict(repeated, bold=True) is True  # bold is the signal that settles it
    assert _verdict(_GOOD[:3]) is False  # too short to be a table


def test_a_number_or_a_formula_anywhere_in_the_block_turns_it_down():
    from excel_etl.inspect.regions import SheetGrid, _text_header

    with_number = [["Kode", "Nama"], ["a", "x"], ["b", "y"], ["c", "z"], ["d", 7]]
    g = _grid(with_number, bold=True)
    assert _text_header(g, (0, 0, 4, 1)) is False
    # the same block without the number is accepted, so the number is what refused it
    assert _verdict([*with_number[:4], ["d", "w"]], bold=True) is True
    g2 = SheetGrid(_text_book(_GOOD, bold=True), "S")
    g2.formulas = frozenset({(3, 1)})  # a formula cell with no cached value
    assert _text_header(g2, (0, 0, 6, 1)) is False


def test_a_single_column_of_texts_is_a_list_not_a_table():
    # the block is 1 wide, so detect_regions drops it before the master rule is asked; the
    # rule itself must still say no if it is ever asked
    one = [["Nama"]] + [[f"x{i}"] for i in range(6)]
    assert _verdict(one, bold=True) is False
    assert all(not hdr for hdr, _ in _regions(_text_book(one)))
