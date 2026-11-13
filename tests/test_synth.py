"""Target-driven spec synthesis across unrelated synthetic workbook shapes."""

from __future__ import annotations

import io

import openpyxl
import pytest

from excel_etl.knowledge import Knowledge
from excel_etl.pipeline import parse_spec
from excel_etl.synth import Target, TargetColumn, match_columns, synthesize
from excel_etl.workbook import Workbook


def book(rows: list[list[object]], merges: list[str] | None = None) -> Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Input"
    for row in rows:
        ws.append(row)
    for ref in merges or []:
        ws.merge_cells(ref)
    buf = io.BytesIO()
    wb.save(buf)
    return Workbook.open(buf.getvalue())


def target(columns: list[TargetColumn], grain: list[str], **kwargs) -> Target:
    return Target(name="clean_data", columns=columns, grain=grain, **kwargs)


def wide() -> Workbook:
    return book(
        [
            ["Region", "Item", "Jan", "Feb"],
            ["North", "pen", 10, 12],
            [None, "ink", 5, 6],
            ["Total", None, 15, 18],
        ]
    )


def wide_target() -> Target:
    return target(
        [
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="month", type="text", from_header=True),
            TargetColumn(name="quantity", type="float"),
        ],
        ["region", "item", "month"],
    )


def test_target_rejects_bad_grain_names_and_unsafe_filters():
    with pytest.raises(ValueError, match="grain"):
        target([TargetColumn(name="qty", type="float")], ["missing"])
    with pytest.raises(ValueError, match="read_text"):
        target(
            [TargetColumn(name="qty", type="float")],
            ["qty"],
            filters=["read_text('secret')"],
        )
    with pytest.raises(ValueError, match="differ only"):
        target(
            [TargetColumn(name="Qty", type="float"), TargetColumn(name="qty", type="int")],
            ["Qty"],
        )


def test_match_uses_hints_glossary_leaf_and_answers():
    t = target(
        [TargetColumn(name="quantity", type="float", hints=["Amount"])],
        ["quantity"],
    )
    mapping, _, questions = match_columns(
        t, ["Date", "Amount"], ["DATE", "DOUBLE"], {}, Knowledge()
    )
    assert mapping == {"quantity": "Amount"} and not questions
    _, _, questions = match_columns(t, ["Amount", "Amt"], ["DOUBLE", "DOUBLE"], {}, Knowledge())
    assert not questions  # an exact hint is stronger than a fuzzy alternative
    mapping, _, _ = match_columns(
        t, ["Plant|Amount"], ["DOUBLE"], {"col:quantity": "Plant|Amount"}, None
    )
    assert mapping["quantity"] == "Plant|Amount"


def test_wide_synthesis_builds_long_output_without_agent():
    report = synthesize(wide_target(), wide())
    assert report.status == "ok", report.text()
    assert report.best is not None
    spec = parse_spec(report.best.spec)
    assert [s.node for s in spec.steps] == [
        "fill_down",
        "drop_rows",
        "drop_rows",
        "unpivot",
        "rename",
        "cast",
        "select",
    ]
    assert len(report.best.preview) == 4
    assert report.best.preview[0] == {
        "region": "North",
        "item": "pen",
        "month": "Jan",
        "quantity": 10.0,
    }
    assert report.best.score == 1.0


def test_grouped_header_synthesis_uses_unpivot_groups():
    wb = book(
        [
            ["Date", "Plant A", "Plant A", "Plant B", "Plant B"],
            ["Date", "Ton", "Rate", "Ton", "Rate"],
            ["2026-01-01", 10, 0.5, 20, 0.7],
            ["2026-01-02", 11, 0.6, 21, 0.8],
        ],
        ["B1:C1", "D1:E1"],
    )
    t = target(
        [
            TargetColumn(name="day", type="text", hints=["Date"]),
            TargetColumn(name="plant", type="text", from_header=True),
            TargetColumn(name="tonnage", type="float", hints=["Ton"]),
            TargetColumn(name="rate", type="float"),
        ],
        ["day", "plant"],
    )
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert any(s.node == "unpivot_groups" for s in parse_spec(report.best.spec).steps)
    assert len(report.best.preview) == 4
    assert report.best.preview[0]["plant"] == "Plant A"


def test_flat_synthesis_and_determinism():
    wb = book([["Code", "Price"], ["a", 1], ["b", 2]])
    t = target(
        [TargetColumn(name="code", type="text"), TargetColumn(name="price", type="float")],
        ["code"],
    )
    a, b = synthesize(t, wb), synthesize(t, wb)
    assert a.status == "ok"
    assert a.model_dump() == b.model_dump()
    assert a.best.preview == [
        {"code": "a", "price": 1.0},
        {"code": "b", "price": 2.0},
    ]


def test_no_match_returns_problems_not_a_fake_spec():
    wb = book([["Code"], ["a"]])
    t = target([TargetColumn(name="quantity", type="float")], ["quantity"])
    report = synthesize(t, wb)
    assert report.status == "none"
    assert report.best is None


def test_grouped_header_with_several_key_columns_and_text_measure():
    # Two key columns (Region, Code) sit left of the Plant blocks, like a stock sheet.
    wb = book(
        [
            ["Region", "Code", "Plant A", "Plant A", "Plant B", "Plant B"],
            ["Region", "Code", "Ton", "Rate", "Ton", "Rate"],
            ["North", 1, 10, 0.5, 20, 0.7],
            ["South", 2, 11, 0.6, 21, 0.8],
        ],
        ["C1:D1", "E1:F1"],
    )
    t = target(
        [
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="code", type="int", hints=["Code"]),
            TargetColumn(name="plant", type="text", from_header=True),
            TargetColumn(name="tonnage", type="text", hints=["Ton"]),
        ],
        ["region", "code", "plant"],
    )
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert len(report.best.preview) == 4


def test_one_workbook_read_from_many_threads():
    # The lab runs synth in a thread pool and caches workbooks, so two quick clicks read the
    # same calamine reader at once. That used to fail with "Already borrowed".
    from concurrent.futures import ThreadPoolExecutor

    xl = openpyxl.Workbook()
    for i in range(30):
        ws = xl.create_sheet(f"S{i}")
        for r in range(200):
            ws.append([r, f"x{r}", r * 2])
    buf = io.BytesIO()
    xl.save(buf)
    wb = Workbook.open(buf.getvalue())
    names = [s.name for s in wb.sheets]
    with ThreadPoolExecutor(8) as pool:
        sizes = list(pool.map(lambda n: len(wb.values(n)) + len(wb.merged(n)), names * 4))
    assert len(sizes) == len(names) * 4


def test_repeated_sheets_are_read_together_and_odd_ones_skipped():
    # Daily sheets share one layout; one day was typed wrong. Synthesis should read all the
    # good days into one table and leave the broken one out, not fall back to a single sheet.
    xl = openpyxl.Workbook()
    xl.active.title = "Notes"
    xl.active.append(["How to fill this file"])
    for day in range(1, 13):
        ws = xl.create_sheet(f"2026-01-{day:02d}")
        ws.append(["Region", "Code", "Plant A", "Plant A", "Plant B", "Plant B"])
        ws.append(["Region", "Code", "Ton", "Rate", "Ton", "Rate"])
        ws.append(["North", 1, day, 0.5, 20, 0.7])
        ws.append(["South", 2, day, 0.6, 21, 0.8])
        ws.merge_cells("C1:D1")
        ws.merge_cells("E1:F1")
    bad = xl.create_sheet("2026-01-13")
    bad.append(["oops", "nothing", "here"])
    bad.append([1, 2, 3])
    buf = io.BytesIO()
    xl.save(buf)
    wb = Workbook.open(buf.getvalue())
    t = target(
        [
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="code", type="int", hints=["Code"]),
            TargetColumn(name="plant", type="text", from_header=True),
            TargetColumn(name="tonnage", type="float", hints=["Ton"]),
        ],
        ["region", "code", "plant"],
    )
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    sheet = parse_spec(report.best.spec).sources["input"].sheet
    assert sheet.name_regex and sheet.sheet_column == "source_sheet"
    assert "2026-01-13" not in {r["source_sheet"] for r in report.best.preview}


def _simple_book(rows=None, title="Input") -> Workbook:
    rows = rows or [["Item", "Qty"], ["pen", 1], ["ink", 2], ["cap", 3]]
    return book(rows)


def test_optional_column_missing_from_the_sheet_is_left_out_not_fatal():
    t = target(
        [
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="float", hints=["Qty"]),
            TargetColumn(name="note", type="text", hints=["Note"], required=False),
        ],
        ["item"],
    )
    report = synthesize(t, _simple_book())
    assert report.status == "ok", report.text()
    assert "note" not in report.best.preview[0]
    assert any("note" in p for p in report.best.problems)


def test_an_explicit_answer_wins_over_a_header_named_like_the_target():
    # Header "prov" equals the target name (score 1.0) and "Provinsi" is the answer (1.0).
    wb = book([["Provinsi", "Nilai", "prov"], ["a", 1, "x"], ["b", 2, "y"]])
    t = target(
        [
            TargetColumn(name="prov", type="text", hints=["Provinsi"]),
            TargetColumn(name="val", type="float", hints=["Nilai"]),
        ],
        ["prov"],
    )
    first = synthesize(t, wb)
    asked = {q.id for q in first.questions}
    assert first.status != "none" or asked, "silent none: the user cannot fix it"
    report = synthesize(t, wb, answers={"col:prov": "Provinsi", "col:val": "Nilai"})
    assert report.status == "ok", report.text()
    assert [r["prov"] for r in report.best.preview] == ["a", "b"]


def test_target_column_called_source_sheet_does_not_clash_with_the_sheet_column():
    xl = openpyxl.Workbook()
    xl.remove(xl.active)
    for d in range(1, 5):
        ws = xl.create_sheet(f"d{d}")
        ws.append(["Provinsi", "Source_Sheet", "Nilai", "Jumlah"])
        ws.append(["a", "x", 1, 2])
        ws.append(["b", "y", 3, 4])
    buf = io.BytesIO()
    xl.save(buf)
    t = target(
        [
            TargetColumn(name="provinsi", type="text", hints=["Provinsi"]),
            TargetColumn(name="nilai", type="float", hints=["Nilai"]),
            TargetColumn(name="source_sheet", type="text", hints=["Source_Sheet"]),
        ],
        ["provinsi"],
    )
    report = synthesize(t, Workbook.open(buf.getvalue()))
    assert report.status == "ok", report.text()


def test_a_dropped_candidate_says_why():
    # Nothing in the sheet resembles the target, so there is no candidate; the report must
    # still say which headers it saw instead of a bare "none".
    t = target([TargetColumn(name="zzz", type="text", hints=["zzz"])], ["zzz"])
    report = synthesize(t, _simple_book())
    assert report.status in ("none", "ask")
    assert report.notes, "a silent none gives the user nothing to fix"


def test_numbers_typed_as_text_with_comma_decimals_are_read_when_asked():
    rows = [["Item", "Qty"], ["a", "1.234,56"], ["b", "2.000"], ["c", "-"], ["d", "300,5"]]
    cols = [
        TargetColumn(name="item", type="text", hints=["Item"]),
        TargetColumn(name="qty", type="float", hints=["Qty"]),
    ]
    plain = synthesize(target(cols, ["item"]), book(rows))
    assert any("empty or could not be cast" in p for p in plain.best.problems)
    report = synthesize(target(cols, ["item"], decimal=","), book(rows))
    assert report.status == "ok", report.text()
    got = {r["item"]: r["qty"] for r in report.best.preview}
    assert got == {"a": 1234.56, "b": 2000.0, "c": None, "d": 300.5}


def _month_sheets(names: list[str], odd: str | None = None) -> Workbook:
    xl = openpyxl.Workbook()
    xl.remove(xl.active)
    for n, name in enumerate(names, start=1):
        ws = xl.create_sheet(name)
        ws.append(["Item", "Qty"])
        for i in range(4):
            ws.append([f"i{i}", n * 10 + i])
    if odd:
        ws = xl.create_sheet(odd)
        ws.append(["something", "else"])
        ws.append([1, 2])
    buf = io.BytesIO()
    xl.save(buf)
    return Workbook.open(buf.getvalue())


@pytest.mark.parametrize(
    "names",
    [
        ["Jan", "Feb", "Mar", "Apr"],
        [f"Hari {d}" for d in range(1, 13)],  # 1 and 2 digits: no single digit pattern
    ],
)
def test_sheets_named_without_a_common_pattern_are_still_all_read(names):
    t = target(
        [
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="float", hints=["Qty"]),
        ],
        ["item"],
    )
    wb = _month_sheets(names, odd="Notes")
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    import tempfile

    import duckdb

    from excel_etl.engine import run

    with tempfile.TemporaryDirectory() as tmp:
        run(parse_spec(report.best.spec), {"main": wb}, tmp)
        table = f"read_parquet('{tmp}/clean_data.parquet')"
        got = duckdb.sql(f"select count(*), count(distinct source_sheet) from {table}").fetchall()
    assert got == [(4 * len(names), len(names))]


def test_warnings_from_the_run_and_rounding_reach_the_report():
    rows = [["Item", "Qty"], ["a", 1.5], ["b", 2.7], ["c", 3.2]]
    t = target(
        [
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="int", hints=["Qty"]),
        ],
        ["item"],
    )
    report = synthesize(t, book(rows))
    assert report.status == "ok", report.text()
    assert any("rounded" in r for r in report.best.reasons)

    # rows far below the table are not read; run() warns and synth must pass that on
    gap = [["Item", "Qty"], ["a", 1], ["b", 2], ["c", 3], [None, None], [None, None], ["z", 99]]
    t2 = target(t.columns, ["item"])
    warned = synthesize(t2, book(gap))
    assert any("below the table" in p for p in warned.best.problems), warned.text()


def test_an_empty_template_sheet_does_not_stand_for_the_group():
    # Template sheets share the layout of the real days but hold no numbers; the table to
    # propose must come from a sheet that has data.
    xl = openpyxl.Workbook()
    xl.remove(xl.active)
    for name, filled in [("Template", False)] + [(f"2026-01-{d:02d}", True) for d in range(1, 13)]:
        ws = xl.create_sheet(name)
        ws.append(["Item", "Qty"])
        for i in range(4):
            ws.append([f"i{i}", i + 1 if filled else None])
    buf = io.BytesIO()
    xl.save(buf)
    t = target(
        [
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="float", hints=["Qty"]),
        ],
        ["item"],
    )
    report = synthesize(t, Workbook.open(buf.getvalue()))
    assert report.status == "ok", report.text()
    assert report.best.preview and report.best.preview[0]["qty"] == 1.0


def _sum_run(report, wb, col):
    import tempfile

    import duckdb

    from excel_etl.engine import run

    with tempfile.TemporaryDirectory() as tmp:
        rep = run(parse_spec(report.best.spec), {"main": wb}, tmp)
        assert rep.status == "ok", rep.text()
        table = f"read_parquet('{tmp}/clean_data.parquet')"
        return duckdb.sql(f"select count(*), sum({col}) from {table}").fetchall()[0]


def test_a_total_column_on_the_right_is_not_unpivoted_as_a_month():
    rows = [["Region", "Jan", "Feb", "Mar", "Total"], ["N", 1, 2, 3, 6], ["S", 4, 5, 6, 15]]
    t = target(
        [
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="month", type="text", from_header=True),
            TargetColumn(name="qty", type="float"),
        ],
        ["region", "month"],
    )
    wb = book(rows)
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert _sum_run(report, wb, "qty") == (6, 21)


def test_a_total_block_in_grouped_headers_is_not_unpivoted():
    wb = book(
        [
            ["Item", "A", "A", "B", "B", "TOTAL", "TOTAL"],
            ["Item", "Ton", "Rate", "Ton", "Rate", "Ton", "Rate"],
            ["x", 1, 0.1, 2, 0.2, 3, 0.3],
            ["y", 4, 0.4, 5, 0.5, 9, 0.9],
        ],
        ["B1:C1", "D1:E1", "F1:G1"],
    )
    t = target(
        [
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="plant", type="text", from_header=True),
            TargetColumn(name="ton", type="float", hints=["Ton"]),
        ],
        ["item", "plant"],
    )
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert _sum_run(report, wb, "ton") == (4, 12)


@pytest.mark.parametrize(
    "label", ["Jml. Pupuk", "Rekap Keseluruhan", "Total Keseluruhan", "JUMLAH"]
)
def test_total_rows_with_other_common_labels_are_dropped(label):
    rows = [["Wilayah", "Item", "Qty"], ["A", "Padi", 1], [None, "Jagung", 2], [label, None, 3]]
    t = target(
        [
            TargetColumn(name="wilayah", type="text", hints=["Wilayah"]),
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="float", hints=["Qty"]),
        ],
        ["wilayah", "item"],
    )
    wb = book(rows)
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert _sum_run(report, wb, "qty") == (2, 3)


def test_a_total_label_in_a_second_key_column_is_dropped_too():
    rows = [["Wilayah", "Item", "Qty"], ["A", "Padi", 1], ["A", "Jagung", 2], ["A", "Subtotal", 3]]
    t = target(
        [
            TargetColumn(name="wilayah", type="text", hints=["Wilayah"]),
            TargetColumn(name="item", type="text", hints=["Item"]),
            TargetColumn(name="qty", type="float", hints=["Qty"]),
        ],
        ["wilayah", "item"],
    )
    wb = book(rows)
    report = synthesize(t, wb)
    assert report.status == "ok", report.text()
    assert _sum_run(report, wb, "qty") == (2, 3)


def test_a_drafted_target_builds_without_edits():
    from excel_etl.synth import draft_targets

    wb = book([["Region", "Item", "Qty"], ["North", "pen", 10], ["South", "ink", 5]])
    [table] = draft_targets(wb)
    t = Target.model_validate(table["target"])
    assert [c.name for c in t.columns] == ["region", "item", "qty"]
    assert t.grain == ["region", "item"]
    assert synthesize(t, wb, answers={"region": table["region"]}).status == "ok"


def test_pivot_subtotals_with_the_word_last_are_dropped():
    wb = book([["Prov", "Qty"], ["ACEH", 1], ["ACEH Total", 1], ["BALI", 2]])
    t = target(
        [TargetColumn(name="prov", type="text"), TargetColumn(name="qty", type="float")], ["prov"]
    )
    report = synthesize(t, wb)
    assert [r["prov"] for r in report.best.preview] == ["ACEH", "BALI"]


def _two_groups() -> Workbook:
    xl = openpyxl.Workbook()
    xl.remove(xl.active)
    for d in range(1, 61):
        ws = xl.create_sheet(f"2026-03-{d:02d}" if d <= 31 else f"2026-04-{d - 31:02d}")
        ws.append(["Item", "Qty"])
        ws.append(["a", 1])
        ws.append(["b", 2])
    for m in ("Jan", "Feb", "Mar"):
        ws = xl.create_sheet(m)
        ws.append(["Barang", "Jumlah"])
        ws.append(["a", 10])
    ws = xl.create_sheet("2026-04-31")  # same name shape, other layout: left out, but shown
    ws.append(["Barang", "Jumlah", "Extra"])
    ws.append(["a", 1, 1])
    buf = io.BytesIO()
    xl.save(buf)
    return Workbook.open(buf.getvalue())


def _item_qty() -> Target:
    return target(
        [
            TargetColumn(name="item", type="text", hints=["Item", "Barang"]),
            TargetColumn(name="qty", type="float", hints=["Qty", "Jumlah"]),
        ],
        ["item"],
    )


def test_draft_lists_groups_with_size_range_and_left_out_sheets():
    from excel_etl.synth import draft_targets

    tables = draft_targets(_two_groups())
    daily = next(t for t in tables if t["sheets"] == 60)
    assert (daily["first"], daily["last"]) == ("2026-03-01", "2026-04-29")
    assert daily["left_out"] == ["2026-04-31"]


def test_several_ticked_groups_are_stacked_into_one_output():
    from excel_etl.synth import draft_targets

    wb = _two_groups()
    ids = [t["region"] for t in draft_targets(wb) if t["sheets"] > 1]
    assert len(ids) == 2
    report = synthesize(_item_qty(), wb, answers={"region": ",".join(ids)})
    assert report.status == "ok", report.text()
    assert report.best.region == ",".join(ids)
    assert "union" in report.best.spec
    # 60 daily sheets x (1 + 2) + 3 months x 10 -- both groups end up in the one output
    assert _sum_run(report, wb, "qty") == (60 * 2 + 3, 60 * 3 + 30)


def test_draft_detects_each_sheet_at_most_once(monkeypatch):
    from excel_etl.inspect import regions
    from excel_etl.synth import draft_targets

    wb = _two_groups()
    wb.memo.clear()  # the cache may hold this file from another test
    calls = []
    real = regions._analyze
    monkeypatch.setattr(regions, "_analyze", lambda g, box: calls.append(g) or real(g, box))
    first = draft_targets(wb)
    assert len({id(g) for g in calls}) <= len(wb.sheets)  # one grid per sheet at most
    assert draft_targets(wb) == first
