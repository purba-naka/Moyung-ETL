from __future__ import annotations

import json

import pytest

from moyung.engine import run
from moyung.pipeline import parse_spec
from moyung.tablefile import TableFile
from moyung.verify import VerifyReport

CSV = "plant,tons,rate,note\nA,10,0.5,x\nB,20,0.25,y\nC,30,0.75,z\n"


def verify_of(tmp_path, csv=CSV, steps=(), outputs=None, excluded=None, extra_sources=None):
    spec = {
        "sources": {"s": {"kind": "csv", "file_role": "c"}, **(extra_sources or {})},
        "steps": list(steps),
        "outputs": outputs or {"o": {"input": steps[-1]["id"] if steps else "s"}},
    }
    if excluded:
        spec["excluded"] = excluded
    files = {"c": TableFile.open(csv.encode(), "csv")}
    if extra_sources:
        files["d"] = TableFile.open(b"k,amount\n1,100\n2,200\n", "csv")
    report = run(parse_spec(json.dumps(spec)), files, tmp_path / "out")
    assert report.status in ("ok", "failed"), report.issues
    return report


def lost_names(rep):
    return [item.split(" (")[0] for item in rep.verify.lost]


def select(columns, input="s", ident="sel"):
    return {"id": ident, "node": "select", "inputs": [input], "config": {"columns": columns}}


class TestLostColumns:
    def test_an_output_that_keeps_everything_is_ok(self, tmp_path):
        rep = verify_of(tmp_path)
        assert rep.verify.status == "ok" and rep.verify.checked == 2 and rep.verify.lost == ()

    def test_a_dropped_number_column_is_lost_and_named(self, tmp_path):
        rep = verify_of(tmp_path, steps=[select(["plant", "tons"])])
        assert rep.verify.status == "unexplained" and lost_names(rep) == ["s.rate"]

    def test_a_dropped_text_column_is_not_a_loss(self, tmp_path):
        rep = verify_of(tmp_path, steps=[select(["plant", "tons", "rate"])])
        assert rep.verify.status == "ok"

    def test_the_spec_can_say_why_a_column_was_dropped(self, tmp_path):
        rep = verify_of(
            tmp_path,
            steps=[select(["plant", "tons"])],
            excluded={"s": {"rate": "a lookup value, not a measure"}},
        )
        assert rep.verify.status == "explained" and rep.verify.excluded and not rep.verify.lost

    def test_an_all_zero_column_is_never_lost(self, tmp_path):
        rep = verify_of(tmp_path, csv="a,z\n1,0\n2,0\n", steps=[select(["a"])])
        assert rep.verify.status == "ok" and rep.verify.checked == 1

    def test_an_all_empty_column_is_never_lost(self, tmp_path):
        rep = verify_of(tmp_path, csv="a,z\n1,\n2,\n", steps=[select(["a"])])
        assert rep.verify.status == "ok"

    def test_values_that_cancel_to_a_zero_sum_still_count(self, tmp_path):
        rep = verify_of(tmp_path, csv="a,z\n1,5\n2,-5\n", steps=[select(["a"])])
        assert lost_names(rep) == ["s.z"]

    def test_one_lost_column_among_many_is_still_found(self, tmp_path):
        cols = [f"c{i}" for i in range(30)]
        csv = (
            ",".join(["id", *cols])
            + "\n"
            + ",".join(["1", *(str(i + 1) for i in range(30))])
            + "\n"
        )
        keep = ["id", *cols[:-1]]
        rep = verify_of(tmp_path, csv=csv, steps=[select(keep)])
        assert lost_names(rep) == ["s.c29"]

    def test_two_sources_each_judged_on_its_own_columns(self, tmp_path):
        source_d = {"kind": "csv", "file_role": "d"}
        rep = verify_of(
            tmp_path,
            extra_sources={"d": source_d},
            outputs={"o": {"input": "s"}},
        )
        assert rep.verify.status == "unexplained"
        assert set(lost_names(rep)) == {"d.k", "d.amount"}

    def test_a_second_output_can_carry_the_column_the_first_dropped(self, tmp_path):
        rep = verify_of(
            tmp_path,
            steps=[select(["plant", "tons"], ident="a"), select(["plant", "rate"], ident="b")],
            outputs={"x": {"input": "a"}, "y": {"input": "b"}},
        )
        assert rep.verify.status == "ok"


class TestRecognisedFates:
    def test_a_renamed_column_with_the_same_sum_is_used(self, tmp_path):
        steps = [
            {
                "id": "r",
                "node": "rename",
                "inputs": ["s"],
                "config": {"mapping": {"tons": "volume"}},
            }
        ]
        assert verify_of(tmp_path, steps=steps).verify.status == "ok"

    def test_a_renamed_and_filtered_column_reads_as_lost_which_is_blocking_not_silent(
        self, tmp_path
    ):
        steps = [
            {"id": "f", "node": "filter", "inputs": ["s"], "config": {"where": "tons > 10"}},
            {
                "id": "r",
                "node": "rename",
                "inputs": ["f"],
                "config": {"mapping": {"tons": "volume"}},
            },
        ]
        assert "s.tons" in lost_names(verify_of(tmp_path, steps=steps))

    def test_a_filter_that_keeps_the_name_is_used(self, tmp_path):
        steps = [{"id": "f", "node": "filter", "inputs": ["s"], "config": {"where": "tons > 10"}}]
        assert verify_of(tmp_path, steps=steps).verify.status == "ok"

    def test_a_sum_by_group_under_the_same_name_is_used(self, tmp_path):
        steps = [
            {
                "id": "g",
                "node": "group_by",
                "inputs": ["s"],
                "config": {
                    "by": ["plant"],
                    "aggs": [
                        {"name": "tons", "fn": "sum", "column": "tons"},
                        {"name": "rate", "fn": "sum", "column": "rate"},
                    ],
                },
            }
        ]
        assert verify_of(tmp_path, steps=steps).verify.status == "ok"

    def test_columns_unpivoted_into_rows_are_used_by_name_and_value(self, tmp_path):
        steps = [
            {
                "id": "u",
                "node": "unpivot",
                "inputs": ["s"],
                "config": {"keep": ["plant"], "columns": ["tons", "rate"]},
            }
        ]
        assert verify_of(tmp_path, steps=steps).verify.status == "ok"

    def test_a_unit_conversion_is_only_a_note_on_the_values(self, tmp_path):
        steps = [
            {
                "id": "d",
                "node": "derive",
                "inputs": ["s"],
                "config": {"name": "tons", "expr": "tons * 1000"},
            }
        ]
        rep = verify_of(tmp_path, steps=steps)
        assert rep.verify.status in ("ok", "unexplained")
        assert rep.verify.values or rep.verify.status == "ok"

    def test_a_cast_to_text_drops_the_column_from_the_numbers(self, tmp_path):
        steps = [
            {"id": "c", "node": "cast", "inputs": ["s"], "config": {"types": {"tons": "VARCHAR"}}}
        ]
        rep = verify_of(tmp_path, steps=steps)
        assert rep.verify.status in ("ok", "unexplained")


class TestTextColumnsWithNumbers:
    def test_a_text_column_holding_numbers_is_listed_as_skipped(self, tmp_path):
        csv = "a,t\n1,10\n2,n/a\n3,5\n"
        rep = verify_of(tmp_path, csv=csv, steps=[select(["a", "t"])])
        assert any("s.t" in item and "n/a" in item for item in rep.verify.skipped)

    def test_a_text_column_with_no_numbers_is_not_skipped(self, tmp_path):
        rep = verify_of(tmp_path, csv="a,t\n1,x\n2,y\n", steps=[select(["a", "t"])])
        assert rep.verify.skipped == ()


class TestReport:
    def report(self, **kw):
        defaults = {"status": "ok", "checked": 3}
        return VerifyReport(**{**defaults, **kw})

    def test_a_clean_report_is_one_line(self):
        assert self.report().text() == "verify: ok, 3 number columns of the sources checked"

    def test_lost_columns_are_listed_with_advice(self):
        text = self.report(status="unexplained", lost=("s.a", "s.b")).text()
        assert "LOST (no output has it): 2" in text and "    s.a" in text and "`excluded`" in text

    def test_a_long_list_is_cut_at_thirty_with_a_count(self):
        lost = tuple(f"s.c{i}" for i in range(45))
        text = self.report(status="unexplained", lost=lost).text()
        assert "... 15 more" in text and "s.c29" in text and "s.c30" not in text

    def test_skipped_columns_show_five_and_count_the_rest(self):
        skipped = tuple(f"s.t{i} (e.g. 'x')" for i in range(8))
        text = self.report(skipped=skipped).text()
        assert "skipped 8 text columns" in text and "+3 more" in text

    def test_the_report_is_frozen_and_survives_json(self):
        rep = self.report(status="explained", excluded=("s.a (why)",))
        assert VerifyReport.model_validate_json(rep.model_dump_json()) == rep
        with pytest.raises(ValueError):
            rep.status = "ok"

    def test_a_status_outside_the_three_is_refused(self):
        with pytest.raises(ValueError):
            VerifyReport(status="maybe", checked=0)


class TestSkippedOutputs:
    def test_a_held_back_output_is_not_counted_against_the_columns_only_it_keeps(self, tmp_path):
        spec = {
            "sources": {"s": {"kind": "csv", "file_role": "c"}},
            "steps": [select(["plant", "tons"], ident="a"), select(["plant", "rate"], ident="b")],
            "outputs": {"x": {"input": "a"}, "y": {"input": "b"}},
            "checks": {"hold": {"kind": "row_count", "table": "b", "min": 99}},
        }
        rep = run(
            parse_spec(json.dumps(spec)),
            {"c": TableFile.open(CSV.encode(), "csv")},
            tmp_path / "out",
        )
        assert rep.status == "partial"
        assert rep.verify.status in ("ok", "explained", "unexplained")
        assert not any(name.endswith(".rate") for name in rep.verify.lost) or rep.verify.excluded
