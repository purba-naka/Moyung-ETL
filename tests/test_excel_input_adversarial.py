from __future__ import annotations

import datetime as dt
import json

import duckdb
import pytest
from conftest import make_xlsx

from moyung.engine import run
from moyung.pipeline import parse_spec
from moyung.workbook import Workbook


def _read(tmp_path, rows, anchor=("A",), header_rows=1, end="until_blank_rows(2)", merged=None):
    wb = Workbook.open(make_xlsx({"S": rows}, merged=merged))
    source = {
        "file_role": "f",
        "sheet": {"name": "S"},
        "region": {"header_anchor": list(anchor), "header_rows": header_rows, "end": end},
    }
    spec = parse_spec(
        json.dumps({"sources": {"t": source}, "outputs": {"o": {"input": "t", "format": "csv"}}})
    )
    rep = run(spec, {"f": wb}, tmp_path / "out")
    path = tmp_path / "out" / "o.csv"
    return rep, path.read_text(encoding="utf-8").splitlines() if path.exists() else None


def _rows(rep):
    return rep.nodes[0].rows_out


def _schema(rep):
    return rep.nodes[0].schema_out


class TestTableEnd:
    def test_one_blank_row_inside_a_table_stays_as_an_empty_row(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1, 2], [None, None], [3, 4]])
        assert _rows(rep) == 3 and lines == ["A,B", "1,2", ",", "3,4"]

    def test_two_blank_rows_end_the_table_and_the_rest_is_warned_about(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1, 2], [None] * 2, [None] * 2, [3, 4]])
        assert _rows(rep) == 1 and lines == ["A,B", "1,2"]
        assert any("1 more filled row(s) lie below the table" in w for w in rep.warnings)

    def test_a_larger_gap_setting_reads_across_the_blank_rows(self, tmp_path):
        rows = [["A", "B"], [1, 2], [None] * 2, [None] * 2, [3, 4]]
        rep, lines = _read(tmp_path, rows, end="until_blank_rows(3)")
        assert _rows(rep) == 4 and lines[-1] == "3,4"

    def test_a_second_table_with_the_same_header_is_not_merged_but_is_warned(self, tmp_path):
        rows = [["A", "B"], [1, 2], [None] * 2, [None] * 2, ["A", "B"], [5, 6]]
        rep, lines = _read(tmp_path, rows)
        assert _rows(rep) == 1 and any("2 more filled row(s)" in w for w in rep.warnings)

    def test_a_total_row_is_kept_and_flagged(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1, 2], [3, 4], ["Total", 6]])
        assert lines[-1] == "Total,6"
        assert any("total label" in w for w in rep.warnings)

    def test_a_table_of_a_single_column_is_not_found(self, tmp_path):
        rep, lines = _read(tmp_path, [["A"], [1], [2]])
        assert rep.status == "failed" and "not inside a table" in rep.issues[0].message
        assert lines is None

    @pytest.mark.xfail(
        strict=True, reason="BUG-014: the error never says a table needs at least two columns"
    )
    def test_the_single_column_error_names_the_two_column_rule(self, tmp_path):
        rep, _ = _read(tmp_path, [["A"], [1], [2]])
        assert "two columns" in rep.issues[0].message or "2 columns" in rep.issues[0].message

    def test_a_header_without_rows_is_not_a_table(self, tmp_path):
        rep, _ = _read(tmp_path, [["A", "B"]])
        assert rep.status == "failed" and rep.outputs == ()

    def test_a_header_taller_than_the_sheet_does_not_crash(self, tmp_path):
        rep, _ = _read(tmp_path, [["A", "B"], [1, 2]], header_rows=5)
        assert rep.status in ("ok", "failed")


class TestHeaders:
    def test_a_blank_header_cell_gets_a_placeholder_name(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", None, "C"], [1, 2, 3]])
        assert lines[0] == "A,-,C"

    def test_numeric_and_date_headers_become_text(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", 2026, dt.date(2027, 1, 1)], [1, 2, 3]])
        assert lines[0] == "A,2026,2027-01-01"

    def test_a_line_break_in_a_header_becomes_a_space(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "Qty\nTon"], [1, 2]])
        assert lines[0] == "A,Qty Ton"

    def test_repeated_headers_get_a_counter(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "A", "A"], [1, 2, 3]])
        assert lines[0] == "A,A (2),A (3)"

    @pytest.mark.parametrize("cell", ["a", " A ", "A\u00a0", "A  "])
    def test_anchor_matching_ignores_case_spaces_and_nbsp(self, tmp_path, cell):
        rep, lines = _read(tmp_path, [[cell, "b"], [1, 2]])
        assert rep.status == "ok" and _rows(rep) == 1

    def test_unicode_headers(self, tmp_path):
        rep, lines = _read(tmp_path, [["Kolom é", "日本語 ☃"], [1, 2]], anchor=("Kolom é",))
        assert lines[0] == "Kolom é,日本語 ☃"

    def test_a_two_row_header_joins_with_a_pipe(self, tmp_path):
        rows = [["G", "G"], ["A", "B"], [1, 2]]
        rep, lines = _read(tmp_path, rows, anchor=("A", "B"), header_rows=2)
        assert rep.status == "ok" and lines[0] == "G|A,G|B"

    def test_an_anchor_that_is_not_there(self, tmp_path):
        rep, _ = _read(tmp_path, [["A", "B"], [1, 2]], anchor=("Zzz",))
        assert rep.status == "failed" and "not found on sheet" in rep.issues[0].message


class TestValues:
    def test_booleans_times_and_datetimes(self, tmp_path):
        rows = [["A", "B", "C", "D"], [True, dt.time(8, 30), dt.datetime(2026, 1, 1, 5, 6, 7), 1]]
        rep, lines = _read(tmp_path, rows)
        assert lines[1] == "true,08:30:00,2026-01-01 05:06:07,1"

    def test_text_that_looks_like_a_number_keeps_its_zeros(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], ["001", 1], ["002", 2]])
        assert lines[1:] == ["001,1", "002,2"] and _schema(rep)[0] == "A VARCHAR"

    def test_a_column_that_mixes_numbers_and_text_is_text_and_loses_nothing(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1, 1], ["x", 2], [3.5, 3]])
        assert _schema(rep)[0] == "A VARCHAR" and lines[1:] == ["1,1", "x,2", "3.5,3"]

    def test_an_all_blank_column_stays_and_is_null(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B", "C"], [1, None, 3], [2, None, 4]])
        assert lines == ["A,B,C", "1,,3", "2,,4"]

    def test_a_very_long_text_cell(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], ["x" * 32767, 1]])
        assert len(lines[1]) == 32767 + 2

    def test_negative_zero_and_tiny_floats(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [-0.0, 1e-320], [0.5, 2]])
        assert lines[1] == "0.0,1e-320" and rep.status == "ok"

    def test_whole_floats_become_integers(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1e15, 1], [2e15, 1]])
        assert _schema(rep)[0] == "A BIGINT" and lines[1] == "1000000000000000,1"

    def test_floats_beyond_two_to_the_53_stay_double_when_mixed_with_fractions(self, tmp_path):
        rep, _ = _read(tmp_path, [["A", "B"], [2.0**53 + 2, 1], [1.5, 1]])
        assert _schema(rep)[0] == "A DOUBLE"

    @pytest.mark.xfail(
        strict=True, reason="BUG-015: whole floats past 2**63 turn the column into text"
    )
    @pytest.mark.parametrize("value", [1e19, 1e25, 1e308, 2.0**63])
    def test_a_huge_number_stays_a_number(self, tmp_path, value):
        rep, _ = _read(tmp_path, [["A", "B"], [value, 1], [5, 1]])
        assert _schema(rep)[0] in ("A DOUBLE", "A HUGEINT", "A DECIMAL(38,0)")

    def test_a_huge_number_today_becomes_the_exact_digits_of_the_float_as_text(self, tmp_path):
        rep, lines = _read(tmp_path, [["A", "B"], [1e25, 1], [5, 1]])
        assert _schema(rep)[0] == "A VARCHAR" and lines[1] == "10000000000000000905969664,1"

    def test_a_merged_cell_in_data_fills_only_its_first_row(self, tmp_path):
        rows = [["A", "B"], ["x", 1], [None, 2], [None, 3]]
        rep, lines = _read(tmp_path, rows, merged={"S": ["A2:A4"]})
        assert lines == ["A,B", "x,1", ",2", ",3"]

    @pytest.mark.xfail(strict=True, reason="BUG-016: merged data cells give NULLs with no warning")
    def test_a_merged_cell_in_data_is_reported(self, tmp_path):
        rows = [["A", "B"], ["x", 1], [None, 2], [None, 3]]
        rep, _ = _read(tmp_path, rows, merged={"S": ["A2:A4"]})
        assert any("merged" in w.lower() for w in rep.warnings)

    def test_five_thousand_rows(self, tmp_path):
        rows = [["A", "B"]] + [[i, f"v{i}"] for i in range(5000)]
        rep, lines = _read(tmp_path, rows)
        assert _rows(rep) == 5000 and lines[-1] == "4999,v4999"


class TestSheets:
    def _spec(self, sheet, anchor="A"):
        return parse_spec(
            json.dumps(
                {
                    "sources": {
                        "t": {
                            "file_role": "f",
                            "sheet": sheet,
                            "region": {"header_anchor": [anchor]},
                        }
                    },
                    "outputs": {"o": {"input": "t"}},
                }
            )
        )

    def _book(self, **sheets):
        return Workbook.open(make_xlsx(sheets))

    def test_a_regex_that_matches_no_sheet_fails_the_source(self, tmp_path):
        wb = self._book(Jan=[["A", "B"], [1, 2]])
        spec = self._spec({"name_regex": "[0-9]{4}", "sheet_column": "period"})
        rep = run(spec, {"f": wb}, tmp_path / "out")
        assert rep.status == "failed" and rep.issues[0].where == "sources.t"

    def test_a_regex_reads_every_matching_sheet_and_tags_the_rows(self, tmp_path):
        wb = self._book(
            **{"2025": [["A", "B"], [1, 2]], "2026": [["A", "B"], [3, 4]], "Notes": [["x"]]}
        )
        spec = self._spec({"name_regex": "[0-9]{4}", "sheet_column": "period"})
        rep = run(spec, {"f": wb}, tmp_path / "out")
        assert rep.status == "ok" and _rows(rep) == 2
        got = duckdb.sql(
            f"SELECT period, A FROM '{(tmp_path / 'out' / 'o.parquet').as_posix()}' ORDER BY A"
        ).fetchall()
        assert got == [("2025", 1), ("2026", 3)]

    def test_the_regex_must_match_the_whole_sheet_name(self, tmp_path):
        wb = self._book(**{"2026 draft": [["A", "B"], [1, 2]]})
        spec = self._spec({"name_regex": "[0-9]{4}", "sheet_column": "period"})
        assert run(spec, {"f": wb}, tmp_path / "out").status == "failed"

    def test_matching_sheets_with_different_columns_do_not_crash(self, tmp_path):
        wb = self._book(**{"2025": [["A", "B"], [1, 2]], "2026": [["A", "C"], [3, 4]]})
        spec = self._spec({"name_regex": "[0-9]{4}", "sheet_column": "period"})
        rep = run(spec, {"f": wb}, tmp_path / "out")
        assert rep.status in ("ok", "failed")

    def test_a_sheet_name_with_spaces_and_unicode(self, tmp_path):
        wb = self._book(**{"Data é 1": [["A", "B"], [1, 2]]})
        rep = run(self._spec({"name": "Data é 1"}), {"f": wb}, tmp_path / "out")
        assert rep.status == "ok"

    def test_a_hidden_sheet_can_still_be_read(self, tmp_path):
        data = make_xlsx({"Open": [["x"]], "Secret": [["A", "B"], [1, 2]]}, hidden=("Secret",))
        rep = run(self._spec({"name": "Secret"}), {"f": Workbook.open(data)}, tmp_path / "out")
        assert rep.status == "ok"

    def test_a_sheet_name_matches_ignoring_case_when_no_exact_name_exists(self, tmp_path):
        wb = self._book(Data=[["A", "B"], [1, 2]])
        assert run(self._spec({"name": "data"}), {"f": wb}, tmp_path / "out").status == "ok"

    def test_name_like_is_a_normalised_match_not_a_substring(self, tmp_path):
        wb = self._book(**{"Report Jan": [["A", "B"], [1, 2]]})
        spec = self._spec({"name_like": "report"})
        assert run(spec, {"f": wb}, tmp_path / "a").status == "failed"
        spec = self._spec({"name_like": "  REPORT   jan "})
        assert run(spec, {"f": wb}, tmp_path / "b").status == "ok"

    def test_a_failed_sheet_lookup_lists_the_sheets(self, tmp_path):
        wb = self._book(One=[["A", "B"], [1, 2]], Two=[["A", "B"], [1, 2]])
        rep = run(self._spec({"name": "Three"}), {"f": wb}, tmp_path / "out")
        assert "One" in rep.issues[0].message and "Two" in rep.issues[0].message

    def test_fallback_finds_the_only_sheet_holding_every_text(self, tmp_path):
        wb = self._book(One=[["A", "B"], [1, 2]], Two=[["x", "y"]])
        spec = self._spec({"name": "Gone", "fallback_contains": ["a", "b"]})
        assert run(spec, {"f": wb}, tmp_path / "out").status == "ok"

    def test_fallback_with_two_candidates_is_refused(self, tmp_path):
        wb = self._book(One=[["A", "B"], [1, 2]], Two=[["A", "B"], [3, 4]])
        spec = self._spec({"name": "Gone", "fallback_contains": ["a", "b"]})
        rep = run(spec, {"f": wb}, tmp_path / "out")
        assert rep.status == "failed" and "2 sheets hold" in rep.issues[0].message
