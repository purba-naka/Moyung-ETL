"""Synthesis paths the main synth tests do not reach."""

from __future__ import annotations

import io
from datetime import datetime

import openpyxl
import pytest
from test_synth import _item_qty, _two_groups, book, target

from moyung.pipeline import parse_spec
from moyung.synth import (
    Target,
    TargetColumn,
    draft_targets,
    match_columns,
    synthesize,
)
from moyung.workbook import Workbook


def _code_price(**kwargs) -> Target:
    return target(
        [TargetColumn(name="code", type="text"), TargetColumn(name="price", type="float")],
        ["code"],
        **kwargs,
    )


class TestTargetShape:
    def test_two_from_header_columns_are_refused(self):
        with pytest.raises(ValueError, match="one from_header column"):
            target(
                [
                    TargetColumn(name="a", type="text", from_header=True),
                    TargetColumn(name="b", type="text", from_header=True),
                    TargetColumn(name="v", type="float"),
                ],
                ["a"],
            )

    def test_from_header_needs_a_value_column(self):
        with pytest.raises(ValueError, match="needs at least one value column"):
            target(
                [
                    TargetColumn(name="k", type="text"),
                    TargetColumn(name="m", type="text", from_header=True),
                ],
                ["k", "m"],
            )

    def test_a_from_header_grain_column_must_be_text(self):
        with pytest.raises(ValueError, match="must have type text"):
            target(
                [
                    TargetColumn(name="m", type="int", from_header=True),
                    TargetColumn(name="v", type="float"),
                ],
                ["m"],
            )

    def test_a_bad_total_regex_is_refused(self):
        with pytest.raises(ValueError, match="total_regex is invalid"):
            _code_price(total_regex="(unclosed")


class TestColumnScores:
    def test_the_leaf_of_a_grouped_header_scores_below_an_exact_name(self):
        t = target([TargetColumn(name="qty", type="float")], ["qty"])
        mapping, scores, _ = match_columns(t, ["Plant A|qty"], ["DOUBLE"])
        assert mapping == {"qty": "Plant A|qty"} and scores["qty"] == 0.9

    def test_a_header_that_starts_with_the_name_scores_lower_still(self):
        t = target([TargetColumn(name="qty", type="float")], ["qty"])
        _, scores, _ = match_columns(t, ["qty kg"], ["DOUBLE"])
        assert scores["qty"] == 0.85


class TestBuildAndEvaluate:
    def test_filters_become_a_filter_step_before_the_select(self):
        wb = book([["Code", "Price"], ["a", 1], ["b", 2]])
        report = synthesize(_code_price(filters=["price > 1"]), wb)
        assert report.status == "ok", report.text()
        nodes = [s.node for s in parse_spec(report.best.spec).steps]
        assert nodes[-2:] == ["filter", "select"]
        assert report.best.preview == [{"code": "b", "price": 2.0}]

    def test_a_filter_that_keeps_nothing_drops_the_candidate_and_says_why(self):
        wb = book([["Code", "Price"], ["a", 1], ["b", 2]])
        report = synthesize(_code_price(filters=["price > 100"]), wb)
        assert report.status == "none" and report.best is None
        assert any("no data rows after the steps" in n for n in report.notes)

    def test_duplicate_grain_is_a_problem_and_a_low_score_asks_for_the_table(self):
        wb = book([["Code", "Price"], ["a", 1], ["a", 2], ["a", 3], ["a", 4]])
        report = synthesize(_code_price(), wb)
        assert report.status == "ask" and report.best is not None
        assert "Grain has duplicate rows." in report.best.problems
        assert [q.id for q in report.questions] == ["region"]
        assert report.questions[0].options == [report.best.region]

    def test_dates_in_the_preview_are_iso_text(self):
        wb = book([["Day", "Price"], [datetime(2026, 1, 2), 1]])
        t = target(
            [TargetColumn(name="day", type="date"), TargetColumn(name="price", type="float")],
            ["day"],
        )
        report = synthesize(t, wb)
        assert report.status == "ok", report.text()
        assert report.best.preview == [{"day": "2026-01-02", "price": 1.0}]

    def test_report_text_lists_best_reasons_problems_questions_and_notes(self):
        wb = book([["Code", "Price"], ["a", 1], ["a", 2], ["a", 3], ["a", 4]])
        text = synthesize(_code_price(), wb).text()
        lines = text.splitlines()
        assert lines[0] == "synth: ask" and lines[1].startswith("best: ")
        assert "  problem: Grain has duplicate rows." in lines
        assert any(line.startswith("  question region:") for line in lines)


def _many_tables(n: int) -> Workbook:
    xl = openpyxl.Workbook()
    xl.remove(xl.active)
    for i in range(n):
        ws = xl.create_sheet(f"s{i}")
        ws.append(["Code", "Price", *[f"x{j}" for j in range(i)]])
        ws.append(["a", 1, *range(i)])
    buf = io.BytesIO()
    xl.save(buf)
    return Workbook.open(buf.getvalue())


def test_only_the_first_forty_tables_are_tried_and_the_report_says_so():
    report = synthesize(_code_price(), _many_tables(42))
    assert any(n.startswith("Only the first 40 of 42 tables") for n in report.notes)


class TestMergedRegions:
    def test_an_unknown_region_is_named(self):
        report = synthesize(_item_qty(), _two_groups(), answers={"region": "R1,nope"})
        assert report.status == "none"
        assert report.notes[0].startswith("No such table:") and "nope" in report.notes[0]

    def test_a_ticked_table_missing_a_column_is_reported_not_skipped(self):
        wb = _two_groups()
        ids = [t["region"] for t in draft_targets(wb) if t["sheets"] > 1]
        t = target(
            [
                TargetColumn(name="item", type="text", hints=["Item", "Barang"]),
                TargetColumn(name="qty", type="float", hints=["Qty", "Jumlah"]),
                TargetColumn(name="price", type="float"),
            ],
            ["item"],
        )
        report = synthesize(t, wb, answers={"region": ",".join(ids)})
        assert report.status == "none" and report.best is None
        assert "no header matches price" in report.notes[0]


class TestDraftTargets:
    def test_grouped_headers_become_one_from_header_column_and_measures(self):
        wb = book(
            [
                ["Date", "Plant A", "Plant A", "Plant B", "Plant B"],
                ["Date", "Ton", "Note", "Ton", "Note"],
                ["2026-01-01", 10, "x", 20, "y"],
            ],
            ["B1:C1", "D1:E1"],
        )
        draft = draft_targets(wb)[0]["target"]
        cols = {c["name"]: c for c in draft["columns"]}
        assert cols["group"]["from_header"] is True
        assert cols["ton"]["type"] == "float" and cols["note"]["type"] == "text"
        assert "group" in draft["grain"] and "ton" not in draft["grain"]
        assert Target.model_validate(draft)

    def test_a_header_that_is_not_a_name_gets_a_prefix_and_numbers_get_a_grain(self):
        wb = book([["2026", "Price"], [1, 2]])
        draft = draft_targets(wb)[0]["target"]
        names = [c["name"] for c in draft["columns"]]
        assert names[0] == "c_2026"
        assert draft["grain"] == ["c_2026"]


def test_a_required_flat_column_with_no_header_is_named_not_a_cast_error():
    wb = book([["Code", "Price"], ["a", 1]])
    t = target(
        [
            TargetColumn(name="code", type="text"),
            TargetColumn(name="price", type="float"),
            TargetColumn(name="weight", type="float"),
        ],
        ["code"],
    )
    report = synthesize(t, wb)
    assert report.status != "ok" and report.best is None
    assert not any("cast" in n for n in report.notes)
    assert [q.id for q in report.questions] == ["col:weight"]
    assert "no header matches weight." in report.notes[0]
