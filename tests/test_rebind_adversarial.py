from __future__ import annotations

import json

import pytest
from conftest import make_xlsx

from moyung.pipeline import parse_spec
from moyung.rebind import BindRecord, BindReport, SourceBind, rebind
from moyung.tablefile import TableFile

BASE = [["Item", "Qty", "Price"], ["pen", 1, 2.5], ["ink", 2, 3.0]]


def spec(**source):
    body = {
        "file_role": "f",
        "sheet": {"name": "Data"},
        "region": {"header_anchor": ["Item"], "header_rows": 1},
        **source,
    }
    return parse_spec(json.dumps({"sources": {"s": body}}))


def bind(rows, previous=None, sp=None, sheet="Data"):
    return rebind(sp or spec(), {"f": make_xlsx({sheet: rows})}, previous).sources["s"]


def previous_from(rows=BASE, sp=None):
    return rebind(sp or spec(), {"f": make_xlsx({"Data": rows})}).records()


class TestMonthToMonth:
    def test_the_same_file_again_is_exact(self):
        assert bind(BASE, previous_from()).status == "exact"

    def test_binding_with_the_records_of_a_bind_is_exact_every_time(self):
        first = rebind(spec(), {"f": make_xlsx({"Data": BASE})})
        again = rebind(spec(), {"f": make_xlsx({"Data": BASE})}, first.records())
        third = rebind(spec(), {"f": make_xlsx({"Data": BASE})}, again.records())
        assert again.sources["s"].status == third.sources["s"].status == "exact"
        assert again.records() == first.records()

    def test_without_previous_records_only_the_contract_is_judged(self):
        assert bind(BASE).status == "exact"

    def test_a_table_moved_down_and_right_is_shifted_with_both_addresses(self):
        rows = [[None] * 4, [None, "Item", "Qty", "Price"], [None, "pen", 1, 2.5]]
        s = bind(rows, previous_from())
        assert s.status == "shifted" and s.diff == ("header A1 -> B2",)

    def test_the_order_of_columns_does_not_matter(self):
        s = bind([["Item", "Price", "Qty"], ["pen", 2.5, 1]], previous_from())
        assert s.status == "exact"

    def test_trailing_spaces_in_a_header_do_not_matter(self):
        assert bind([["Item", "Qty ", "Price"], ["pen", 1, 2.5]], previous_from()).status == "exact"

    def test_a_new_column_the_spec_does_not_cover_is_drift_and_names_it(self):
        s = bind([["Item", "Qty", "Price", "Tax"], ["pen", 1, 2.5, 0.1]], previous_from())
        assert s.status == "drift" and any(
            "added that no expect_columns" in d and "Tax" in d for d in s.diff
        )

    def test_a_new_column_covered_by_expect_columns_is_only_shifted(self):
        sp = spec(expect_columns=[{"match": "Tax", "required": False}])
        prev = previous_from(sp=sp)
        s = bind([["Item", "Qty", "Price", "Tax"], ["pen", 1, 2.5, 0.1]], prev, sp)
        assert s.status == "shifted"

    def test_a_removed_column_the_spec_does_not_cover_is_drift(self):
        s = bind([["Item", "Qty"], ["pen", 1]], previous_from())
        assert s.status == "drift" and any("removed that no expect_columns" in d for d in s.diff)

    def test_a_required_column_that_is_gone_is_drift(self):
        sp = spec(expect_columns=[{"match": "Qty"}])
        s = bind([["Item", "Price"], ["pen", 2.5]], sp=sp)
        assert s.status == "drift" and any("required column" in d for d in s.diff)

    def test_a_required_column_found_by_regex(self):
        sp = spec(expect_columns=[{"match_regex": "^Qty"}])
        assert bind([["Item", "Qty (ton)", "Price"], ["pen", 1, 2.5]], sp=sp).status == "exact"

    def test_the_sheet_missing_is_missing_with_the_sheet_names(self):
        s = bind(BASE, previous_from(), sheet="Other")
        assert s.status == "missing" and "Other" in s.message and s.record is None

    def test_a_header_with_no_rows_below_is_missing_not_drift(self):
        s = bind([["Item", "Qty", "Price"]], previous_from())
        assert s.status == "missing" and "not inside a table" in s.message

    def test_a_sheet_found_by_its_content_is_drift_until_a_person_confirms(self):
        sp = spec(sheet={"name": "Gone", "fallback_contains": ["Item", "Qty"]})
        s = bind(BASE, sp=sp, sheet="Renamed")
        assert s.status == "drift" and any("found by its content" in d for d in s.diff)

    def test_a_sheet_renamed_is_shifted_when_the_name_pattern_still_matches(self):
        sp = spec(sheet={"name_regex": "[0-9]{4}", "sheet_column": "period"})
        prev = rebind(sp, {"f": make_xlsx({"2025": BASE})}).records()
        s = rebind(sp, {"f": make_xlsx({"2026": BASE})}, prev).sources["s"]
        assert s.status in ("exact", "shifted")


class TestRowCounts:
    def big(self, n):
        return [["Item", "Qty", "Price"]] + [[f"i{i}", i, 1.5] for i in range(n)]

    def test_a_fall_below_half_of_a_big_table_is_noted(self):
        prev = previous_from(self.big(200))
        s = bind(self.big(99), prev)
        assert s.status == "shifted" and "row count fell from 200 to 99" in s.diff[0]

    @pytest.mark.parametrize("now", [100, 150, 200, 250])
    def test_half_or_more_is_quiet(self, now):
        assert bind(self.big(now), previous_from(self.big(200))).status == "exact"

    def test_a_small_table_may_fall_without_a_note(self):
        assert bind(self.big(2), previous_from(self.big(19))).status == "exact"

    def test_a_table_of_exactly_the_minimum_size_is_watched(self):
        s = bind(self.big(9), previous_from(self.big(20)))
        assert s.status == "shifted"


class TestTypes:
    def test_a_whole_number_column_that_gains_a_fraction_is_a_safe_widening(self):
        s = bind([["Item", "Qty", "Price"], ["pen", 1.5, 2.5]], previous_from())
        assert s.status == "shifted" and "safe widening" in s.diff[0]

    def test_a_number_column_that_turns_into_text_is_drift_and_says_to_ask(self):
        s = bind([["Item", "Qty", "Price"], ["pen", "n/a", 2.5]], previous_from())
        assert s.status == "drift" and "ask the person before casting" in s.diff[0]

    def test_a_date_column_that_gains_times_is_a_safe_widening(self):
        import datetime as dt

        old = [["Item", "When"], ["a", dt.date(2026, 1, 1)], ["b", dt.date(2026, 1, 2)]]
        new = [
            ["Item", "When"],
            ["a", dt.datetime(2026, 1, 1, 5, 0)],
            ["b", dt.datetime(2026, 1, 2, 6, 0)],
        ]
        assert bind(new, previous_from(old)).status == "shifted"

    def test_a_decimal_column_that_is_whole_this_month_is_not_drift(self):
        s = bind([["Item", "Qty", "Price"], ["pen", 1, 2.0], ["ink", 2, 3.0]], previous_from())
        assert s.status in ("exact", "shifted")

    @pytest.mark.xfail(
        strict=True, reason="BUG-025: a header that only changes letter case is called drift"
    )
    def test_a_header_that_only_changes_case_is_not_drift(self):
        s = bind([["Item", "QTY", "Price"], ["pen", 1, 2.5]], previous_from())
        assert s.status in ("exact", "shifted")

    @pytest.mark.xfail(
        strict=True, reason="BUG-026: a column that is empty this month turns VARCHAR and is drift"
    )
    def test_a_column_that_is_empty_this_month_is_not_drift(self):
        s = bind(
            [["Item", "Qty", "Price"], ["pen", None, 2.5], ["ink", None, 1.5]], previous_from()
        )
        assert s.status in ("exact", "shifted")


class TestRecords:
    def drifted(self):
        return rebind(
            spec(),
            {"f": make_xlsx({"Data": [["Item", "Qty", "Price", "Tax"], ["pen", 1, 2.5, 1]]})},
            previous_from(),
        )

    def test_a_drifted_source_is_not_remembered_by_records(self):
        report = self.drifted()
        assert report.sources["s"].status == "drift" and report.records() == {}

    def test_accepted_records_keep_the_drifted_place_for_a_person_who_approved(self):
        report = self.drifted()
        assert report.accepted_records()["s"].columns == ("Item", "Qty", "Price", "Tax")

    def test_a_missing_source_has_no_record_at_all(self):
        report = rebind(spec(), {"f": make_xlsx({"Other": BASE})})
        assert report.accepted_records() == {} and not report.ok

    def test_the_record_holds_the_types_the_pipeline_reads(self):
        record = previous_from()["s"]
        assert record.types == {"item": "VARCHAR", "qty": "BIGINT", "price": "DOUBLE"}
        assert record.rows == 2 and (record.header_row, record.first_col) == (0, 0)

    def test_a_report_survives_a_json_round_trip(self):
        report = self.drifted()
        assert BindReport.model_validate_json(report.model_dump_json()) == report

    def test_records_are_frozen(self):
        record = previous_from()["s"]
        with pytest.raises(ValueError):
            record.sheet = "x"

    def test_the_text_names_each_source_and_its_status(self):
        text = self.drifted().text()
        assert text.startswith("rebind: needs repair") and "s|drift at Data!A1" in text

    def test_ok_means_every_source_is_exact_or_shifted(self):
        ok = SourceBind(status="exact", record=None, diff=())
        bad = SourceBind(status="drift", record=None, diff=())
        assert BindReport(sources={"a": ok}).ok and not BindReport(sources={"a": ok, "b": bad}).ok
        assert BindReport(sources={}).ok


class TestFilesArgument:
    def test_a_missing_role_lists_the_roles_given(self):
        s = rebind(spec(), {"other": make_xlsx({"Data": BASE})}).sources["s"]
        assert s.status == "missing" and "files given: other" in s.message

    def test_no_files_at_all(self):
        s = rebind(spec(), {}).sources["s"]
        assert s.status == "missing" and "files given: none" in s.message

    @pytest.mark.parametrize("files", [None, [], "f", 5])
    def test_files_that_are_not_a_dict_are_reported_not_raised(self, files):
        s = rebind(spec(), files).sources["s"]
        assert s.status == "missing" and "must be a dict" in s.message

    @pytest.mark.parametrize("blob", [b"", b"not excel", b"PK\x03\x04", b"a,b\n1,2\n"])
    def test_a_file_that_is_not_a_workbook_is_missing_not_an_exception(self, blob):
        s = rebind(spec(), {"f": blob}).sources["s"]
        assert s.status == "missing" and s.message.startswith("file 'f'")

    def test_a_path_to_nothing_is_missing(self, tmp_path):
        s = rebind(spec(), {"f": str(tmp_path / "nope.xlsx")}).sources["s"]
        assert s.status == "missing"

    def test_a_path_works_like_bytes(self, tmp_path):
        path = tmp_path / "f.xlsx"
        path.write_bytes(make_xlsx({"Data": BASE}))
        assert rebind(spec(), {"f": str(path)}).sources["s"].status == "exact"

    def test_two_sources_of_one_file_open_it_once_and_both_bind(self):
        body = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["Item"]}}
        two = parse_spec(json.dumps({"sources": {"a": body, "b": body}}))
        report = rebind(two, {"f": make_xlsx({"Data": BASE})})
        assert {k: v.status for k, v in report.sources.items()} == {"a": "exact", "b": "exact"}

    def test_a_spec_with_no_sources_binds_to_nothing_and_is_ok(self):
        report = rebind(parse_spec("{}"), {})
        assert report.sources == {} and report.ok


class TestTableFiles:
    def csv(self, text, **source):
        body = {"kind": "csv", "file_role": "c", **source}
        sp = parse_spec(json.dumps({"sources": {"s": body}}))
        return sp, TableFile.open(text.encode(), "csv")

    def test_a_csv_is_bound_by_its_columns_and_types(self):
        sp, tf = self.csv("a,b\n1,x\n2,y\n")
        s = rebind(sp, {"c": tf}).sources["s"]
        assert s.status == "exact" and s.record.types == {"a": "BIGINT", "b": "VARCHAR"}

    def test_a_new_csv_column_is_drift_unless_covered(self):
        sp, tf = self.csv("a,b\n1,x\n")
        prev = rebind(sp, {"c": tf}).records()
        _, wider = self.csv("a,b,c\n1,x,9\n")
        assert rebind(sp, {"c": wider}, prev).sources["s"].status == "drift"

    def test_a_csv_column_that_turns_to_text_is_drift(self):
        sp, tf = self.csv("a,b\n1,x\n")
        prev = rebind(sp, {"c": tf}).records()
        _, text = self.csv("a,b\nn/a,x\n")
        assert rebind(sp, {"c": text}, prev).sources["s"].status == "drift"

    def test_a_missing_csv_role_is_missing(self):
        sp, _ = self.csv("a\n1\n")
        assert rebind(sp, {}).sources["s"].status == "missing"

    def test_a_workbook_given_to_a_csv_source_is_missing_with_the_reason(self):
        sp, _ = self.csv("a\n1\n")
        s = rebind(sp, {"c": make_xlsx({"S": [[1]]})}).sources["s"]
        assert s.status in ("missing",) or "Excel" in s.message

    def test_a_csv_that_loses_all_rows_is_drift(self):
        sp, tf = self.csv("a,b\n1,x\n2,y\n")
        prev = rebind(sp, {"c": tf}).records()
        _, empty = self.csv("a,b\n")
        assert rebind(sp, {"c": empty}, prev).sources["s"].status == "drift"

    def test_a_csv_required_column_that_is_gone_is_drift(self):
        sp, _ = self.csv("a,b\n1,x\n", expect_columns=[{"match": "z"}])
        _, tf = self.csv("a,b\n1,x\n")
        s = rebind(sp, {"c": tf}).sources["s"]
        assert s.status == "drift" and "required column" in s.diff[0]

    def test_the_record_of_a_table_file_has_no_sheet(self):
        sp, tf = self.csv("a\n1\n")
        record = rebind(sp, {"c": tf}).records()["s"]
        assert isinstance(record, BindRecord) and record.sheet == ""
