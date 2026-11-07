from __future__ import annotations

import json

import pytest
from conftest import make_xlsx

from excel_etl.pipeline import parse_spec
from excel_etl.rebind import BindRecord, rebind


def _spec(source: dict, **extra) -> object:
    return parse_spec(json.dumps({"sources": {"s": {"file_role": "f", **source}, **extra}}))


def _src(anchor=("Item",), rows=1, sheet=None, expect=None) -> dict:
    out = {
        "sheet": sheet or {"name": "Data"},
        "region": {"header_anchor": list(anchor), "header_rows": rows},
    }
    if expect is not None:
        out["expect_columns"] = expect
    return out


_BASE = [["Item", "Qty", "Price"], ["pen", 1, 2.0], ["ink", 2, 3.0]]


def _bind(rows, spec, previous=None, sheet="Data"):
    return rebind(spec, {"f": make_xlsx({sheet: rows})}, previous)


def _first(rows, spec):
    return _bind(rows, spec).sources["s"]


def test_a_matching_file_is_exact_and_gives_a_record() -> None:
    rep = _bind(_BASE, _spec(_src()))
    s = rep.sources["s"]
    assert s.status == "exact" and rep.ok and s.diff == ()
    assert s.record == BindRecord(
        sheet="Data", header_row=0, first_col=0, columns=("Item", "Qty", "Price")
    )
    assert rep.records() == {"s": s.record}
    assert "s|exact at Data!A1" in rep.text()


def test_same_file_against_its_own_record_stays_exact() -> None:
    spec = _spec(_src())
    prev = _bind(_BASE, spec).records()
    assert _bind(_BASE, spec, prev).sources["s"].status == "exact"


@pytest.mark.parametrize(
    ("rows", "where"),
    [
        ([[None, None, None], [None, None, None], *_BASE], "A3"),  # moved down
        ([[None, *r] for r in _BASE], "B1"),  # moved right
        (
            [
                ["note", None, None, None],
                [None] * 4,
                [None, *_BASE[0]],
                *[[None, *r] for r in _BASE[1:]],
            ],
            "B3",
        ),
    ],
)
def test_a_table_that_moved_is_shifted_and_runs_as_is(rows, where) -> None:
    spec = _spec(_src())
    prev = _bind(_BASE, spec).records()
    s = _bind(rows, spec, prev).sources["s"]
    assert s.status == "shifted" and any(f"-> {where}" in d for d in s.diff)
    assert _bind(rows, spec, prev).ok


def test_a_renamed_sheet_found_by_name_like_is_shifted() -> None:
    spec = _spec(_src(sheet={"name_like": "Data 2025"}))
    prev = _bind(_BASE, spec, sheet="Data 2025").records()
    s = _bind(_BASE, spec, prev, sheet="data  2025").sources["s"]  # spacing and case only
    assert s.status == "shifted" and s.diff == ("sheet 'Data 2025' -> 'data  2025'",)
    spec2 = _spec(_src(sheet={"name": "Data", "name_like": "Data 2025"}))
    prev2 = _bind(_BASE, spec2, sheet="Data").records()
    s = _bind(_BASE, spec2, prev2, sheet="Data 2025").sources["s"]
    assert s.status == "shifted" and "sheet 'Data' -> 'Data 2025'" in s.diff


def test_a_sheet_found_only_by_content_is_drift() -> None:
    spec = _spec(_src(sheet={"name": "Gone", "fallback_contains": ["Price"]}))
    s = _first(_BASE, spec)
    assert s.status == "drift" and "found by its content" in s.diff[0]
    assert s.record is not None and s.record.sheet == "Data"


def test_a_missing_required_column_is_drift_and_names_it() -> None:
    spec = _spec(_src(expect=[{"match": "Qty"}, {"match": "Weight"}, {"match_regex": "^Pri"}]))
    s = _first(_BASE, spec)
    assert s.status == "drift" and len(s.diff) == 1
    assert s.diff[0].startswith("required column 'Weight' not found: find the new header text")
    assert not _bind(_BASE, spec).ok


def test_an_optional_column_may_be_missing() -> None:
    spec = _spec(_src(expect=[{"match": "Weight", "required": False}]))
    assert _first(_BASE, spec).status == "exact"


def test_columns_added_or_removed_matter_only_when_the_spec_does_not_cover_them() -> None:
    group = [{"match_regex": r"^Plant \d+$"}]
    spec = _spec(_src(expect=[{"match": "Item"}, *group]))
    old = [["Item", "Plant 1", "Plant 2"], ["a", 1, 2]]
    prev = _bind(old, spec).records()
    grown = [["Item", "Plant 1", "Plant 2", "Plant 3"], ["a", 1, 2, 3]]
    s = _bind(grown, spec, prev).sources["s"]
    assert s.status == "shifted" and s.diff == ("columns added: Plant 3",)  # the group covers it
    odd = [["Item", "Plant 1", "Plant 2", "Rework"], ["a", 1, 2, 3]]
    s = _bind(odd, spec, prev).sources["s"]
    assert s.status == "drift" and "no expect_columns entry covers: Rework" in s.diff[0]
    fewer = [["Item", "Plant 1"], ["a", 1]]
    s = _bind(fewer, spec, prev).sources["s"]
    assert s.status == "shifted" and s.diff == ("columns removed: Plant 2",)


def test_two_row_headers_are_compared_by_their_joined_names() -> None:
    old = [
        ["", "Q1", "Q1", "Q2", "Q2"],
        ["Item", "Sales", "Cost", "Sales", "Cost"],
        ["a", 1, 2, 3, 4],
    ]
    spec = _spec(_src(anchor=("Item", "Sales"), rows=2))
    rec = _bind(old, spec).sources["s"].record
    assert rec is not None and rec.columns[:3] == ("Item", "Q1|Sales", "Q1|Cost")
    new = [
        ["", "Q1", "Q1", "Q2", "Q2", "Q3"],
        ["Item", "Sales", "Cost", "Sales", "Cost", "Sales"],
        ["a", 1, 2, 3, 4, 5],
    ]
    s = _bind(new, spec, _bind(old, spec).records()).sources["s"]
    assert s.status == "drift" and "Q3|Sales" in s.diff[0]


def test_what_cannot_be_found_is_missing_with_a_reason() -> None:
    s = _first(_BASE, _spec(_src(anchor=("Nothing here",))))
    assert s.status == "missing" and s.record is None and "not found" in s.message
    s = _first(_BASE, _spec(_src(sheet={"name": "Nope"})))
    assert s.status == "missing" and "no sheet matches" in s.message
    rep = rebind(_spec(_src()), {"other": make_xlsx({"Data": _BASE})})
    assert rep.sources["s"].status == "missing" and not rep.ok
    assert "no file for file_role 'f'; files given: other" in rep.sources["s"].message
    rep = rebind(_spec(_src()), {"f": b"not an excel file"})
    assert rep.sources["s"].status == "missing" and "file 'f'" in rep.sources["s"].message


def test_one_bad_source_does_not_hide_the_others() -> None:
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "good": {"file_role": "f", **_src()},
                    "bad": {"file_role": "f", **_src(anchor=("Zzz",))},
                    "away": {"file_role": "g", **_src()},
                }
            }
        )
    )
    rep = rebind(spec, {"f": make_xlsx({"Data": _BASE})})
    assert {k: v.status for k, v in rep.sources.items()} == {
        "good": "exact",
        "bad": "missing",
        "away": "missing",
    }
    assert "needs repair" in rep.text()


def test_records_survive_json_so_the_host_can_store_them() -> None:
    spec = _spec(_src())
    rep = _bind(_BASE, spec)
    stored = {k: json.loads(v.model_dump_json()) for k, v in rep.records().items()}
    back = {k: BindRecord.model_validate(v) for k, v in stored.items()}
    assert back == rep.records()
    assert _bind(_BASE, spec, back).sources["s"].status == "exact"


@pytest.mark.sample
def test_sample_binds_and_shifts(sample_path) -> None:
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "pkg": {
                        "file_role": "b",
                        "sheet": {"name_like": "(N) PKG"},
                        "region": {"header_anchor": ["TANGGAL", "Phonska 1"], "header_rows": 2},
                        "expect_columns": [{"match": "TANGGAL"}],
                    }
                }
            }
        )
    )
    rep = rebind(spec, {"b": sample_path})
    assert rep.sources["pkg"].status == "exact"
    assert rebind(spec, {"b": sample_path}, rep.records()).sources["pkg"].status == "exact"


def test_records_leave_out_drifted_sources() -> None:
    spec = _spec(_src(expect=[{"match": "Gone"}]))
    rep = _bind(_BASE, spec)
    assert rep.sources["s"].status == "drift"
    assert rep.records() == {}


def test_files_that_is_not_a_dict_is_missing_not_a_crash() -> None:
    rep = rebind(_spec(_src()), None)  # type: ignore[arg-type]
    assert rep.sources["s"].status == "missing" and "dict" in rep.text()


def test_every_drift_message_ends_with_what_to_do_next() -> None:
    spec = _spec(_src(expect=[{"match": "Weight"}]))
    assert "then change expect_columns and the steps that use it" in _first(_BASE, spec).diff[0]
    spec = _spec(_src(expect=[{"match": "Item"}]))
    prev = _bind(_BASE, spec).records()
    wider = [["Item", "Qty", "Price", "Rework"], ["a", 1, 2.0, 3]]
    drift = _bind(wider, spec, prev).sources["s"].diff[0]
    assert "decide for each: add it to the steps that need it" in drift
    gone = _spec(_src(sheet={"name": "Gone", "fallback_contains": ["Price"]}))
    s = _first(_BASE, gone)
    assert s.status == "drift" and any(
        "compare_sheets" in d and "set sheet.name" in d for d in s.diff
    )
