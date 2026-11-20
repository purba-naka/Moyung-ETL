"""Values that cannot fit an INTEGER column, and headers with a double quote, go through the
public engine and synth API as issues or results, never as exceptions or SQL errors."""

from __future__ import annotations

import io
import json

import openpyxl
import pytest
from conftest import make_xlsx

from moyung.engine import validate
from moyung.pipeline import parse_spec
from moyung.synth import Target, TargetColumn, synthesize
from moyung.workbook import Workbook

SOURCE = {
    "file_role": "f",
    "sheet": {"name": "S"},
    "region": {"header_anchor": ["Item"], "header_rows": 1},
}


@pytest.fixture
def book() -> Workbook:
    rows = [["Item", "Code", "Qty"], ["a", 1, 2], ["b", 2, 3]]
    return Workbook.open(make_xlsx({"S": rows}))


def _spec_text(node: str, config: str) -> str:
    step = f'{{"id": "p", "node": "{node}", "inputs": ["s"], "config": {config}}}'
    return f'{{"sources": {{"s": {json.dumps(SOURCE)}}}, "steps": [{step}], "outputs": {{}}}}'


@pytest.mark.parametrize(
    ("values", "bad"),
    [("[NaN]", "nan"), ("[1.4, 1.2]", "1.4")],
)
def test_pivot_values_that_do_not_fit_an_integer_column_are_an_issue(book, values, bad) -> None:
    config = f'{{"by": ["Item"], "column": "Code", "value": "Qty", "values": {values}}}'
    rep = validate(parse_spec(_spec_text("pivot", config)), {"f": book})
    assert not rep.ok
    (issue,) = rep.issues
    assert issue.where == "steps.p"
    assert "pivot" in issue.message and "'Code'" in issue.message and bad in issue.message


def test_fill_missing_fraction_on_an_integer_column_is_an_issue(book) -> None:
    config = '{"values": {"Code": 1.7}}'
    rep = validate(parse_spec(_spec_text("fill_missing", config)), {"f": book})
    assert not rep.ok
    (issue,) = rep.issues
    assert issue.where == "steps.p"
    assert "fill_missing" in issue.message and "'Code'" in issue.message
    assert "1.7" in issue.message


def _quoted_book(rows: list[list[object]]) -> Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Input"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return Workbook.open(buf.getvalue())


def test_synthesize_with_a_double_quote_in_a_hinted_header() -> None:
    wb = _quoted_book([["Region", 'Rev "gross"'], ["N", 10], ["S", 20]])
    target = Target(
        name="clean_data",
        columns=[
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="revenue", type="float", hints=['Rev "gross"']),
        ],
        grain=["region"],
    )
    report = synthesize(target, wb)
    assert report.status == "ok", report.text()
    assert report.best is not None
    assert report.best.preview == [
        {"region": "N", "revenue": 10.0},
        {"region": "S", "revenue": 20.0},
    ]


def test_synthesize_unpivots_headers_that_contain_double_quotes() -> None:
    wb = _quoted_book([["Region", 'Jan "a"', 'Feb "b"'], ["N", 10, 11], ["S", 20, 21]])
    target = Target(
        name="clean_data",
        columns=[
            TargetColumn(name="region", type="text", hints=["Region"]),
            TargetColumn(name="month", type="text", from_header=True),
            TargetColumn(name="quantity", type="float"),
        ],
        grain=["region", "month"],
    )
    report = synthesize(target, wb)
    assert report.status == "ok", report.text()
    assert report.best is not None
    assert [r["month"] for r in report.best.preview] == ['Jan "a"', 'Feb "b"'] * 2
    assert [r["quantity"] for r in report.best.preview] == [10.0, 11.0, 20.0, 21.0]
