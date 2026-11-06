from __future__ import annotations

import copy
import json

import pytest

from excel_etl.pipeline import PipelineSpec, SpecError, dump_spec, parse_spec

_SPEC = {
    "sources": {
        "produksi_npk_pkg": {
            "file_role": "balansitas",
            "sheet": {"name_like": "(N) PKG", "fallback_contains": ["TANGGAL", "Phonska"]},
            "region": {
                "header_anchor": ["TANGGAL", "Phonska 1"],
                "header_rows": 2,
                "end": "until_blank_rows(2)",
            },
            "expect_columns": [
                {"match": "TANGGAL", "type": "date"},
                {"match_regex": "^Phonska \\d+$", "group": "pabrik", "required": False},
            ],
        }
    },
    "steps": [
        {"id": "flat", "node": "flatten_header", "inputs": ["produksi_npk_pkg"]},
        {"id": "long", "node": "unpivot", "inputs": ["flat"], "config": {"keys": ["TANGGAL"]}},
    ],
    "outputs": {"npk": {"input": "long", "format": "csv"}},
}


def _text(**patch) -> str:
    spec = copy.deepcopy(_SPEC)
    spec.update(patch)
    return json.dumps(spec)


def _error(text: str) -> str:
    with pytest.raises(SpecError) as e:
        parse_spec(text)
    return str(e.value)


def test_parse_and_roundtrip() -> None:
    spec = parse_spec(json.dumps(_SPEC))
    assert list(spec.sources) == ["produksi_npk_pkg"]
    assert [s.id for s in spec.steps] == ["flat", "long"]
    assert spec.sources["produksi_npk_pkg"].expect_columns[1].required is False
    assert spec.outputs["npk"].format == "csv"
    assert parse_spec(dump_spec(spec)) == spec


def test_defaults_and_empty_spec() -> None:
    assert parse_spec("{}") == PipelineSpec()
    spec = parse_spec(
        _text(
            sources={
                "s": {
                    "file_role": "f",
                    "sheet": {"name": "Data"},
                    "region": {"header_anchor": ["A"]},
                }
            },
            steps=[],
            outputs={"o": {"input": "s"}},
        )
    )
    region = spec.sources["s"].region
    assert (region.header_rows, region.end) == (1, "until_blank_rows(2)")
    assert spec.outputs["o"].format == "parquet"


def test_a_misspelled_key_is_an_error() -> None:
    bad = copy.deepcopy(_SPEC)
    bad["sources"]["produksi_npk_pkg"]["region"]["header_ancor"] = ["x"]
    msg = _error(json.dumps(bad))
    assert "sources.produksi_npk_pkg.region.header_ancor" in msg


def test_error_says_where() -> None:
    bad = copy.deepcopy(_SPEC)
    bad["steps"][1]["inputs"] = ["nope"]
    assert _error(json.dumps(bad)) == (
        "spec: step 'long' reads 'nope', which is not a source or an earlier step"
    )


def test_steps_must_read_earlier_names() -> None:
    bad = copy.deepcopy(_SPEC)
    bad["steps"].reverse()  # `flat` is now read before it is defined
    assert "reads 'flat'" in _error(json.dumps(bad))


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        (
            {"steps": [{"id": "produksi_npk_pkg", "node": "x", "inputs": ["produksi_npk_pkg"]}]},
            "already used",
        ),
        ({"steps": [{"id": "a b", "node": "x", "inputs": ["produksi_npk_pkg"]}]}, "steps.0.id"),
        ({"steps": [{"id": "a", "node": "x", "inputs": []}]}, "steps.0.inputs"),
        ({"outputs": {"o": {"input": "ghost"}}}, "output 'o' reads 'ghost'"),
        ({"outputs": {"o": {"input": "long", "format": "xlsx"}}}, "outputs.o.format"),
        ({"outputs": {"bad name": {"input": "long"}}}, "outputs.bad name"),
        ({"sources": {"1x": _SPEC["sources"]["produksi_npk_pkg"]}}, "sources.1x"),
    ],
)
def test_wiring_and_names(patch: dict, needle: str) -> None:
    assert needle in _error(_text(**patch))


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda s: s["sheet"].clear(), "give name, name_like or fallback_contains"),
        (lambda s: s["region"].update(end="when_done"), "end must look like"),
        (lambda s: s["region"].update(header_anchor=[]), "region.header_anchor"),
        (lambda s: s["region"].update(header_rows=0), "region.header_rows"),
        (lambda s: s.update(file_role=""), "file_role"),
        (lambda s: s["expect_columns"][0].clear(), "give exactly one of match or match_regex"),
        (
            lambda s: s["expect_columns"][0].update(match_regex="x"),
            "give exactly one of match or match_regex",
        ),
        (lambda s: s["expect_columns"][1].update(match_regex="(["), "is not a regex"),
        (lambda s: s["expect_columns"][0].update(type="money"), "expect_columns.0.type"),
    ],
)
def test_source_rules(edit, needle: str) -> None:
    bad = copy.deepcopy(_SPEC)
    edit(bad["sources"]["produksi_npk_pkg"])
    assert needle in _error(json.dumps(bad))


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"sources": 3}', "{'a': 1}"])
def test_not_a_spec(text: str) -> None:
    assert _error(text)


def test_yaml_gets_a_hint() -> None:
    assert "JSON" in _error("sources:\n  a: 1")


def test_non_string_is_a_spec_error() -> None:
    with pytest.raises(SpecError):
        parse_spec(None)  # type: ignore[arg-type]


def test_many_errors_are_capped() -> None:
    steps = [{"id": f"s{i}", "node": "x", "inputs": []} for i in range(30)]
    msg = _error(json.dumps({"steps": steps}))
    assert len(msg.splitlines()) <= 11
    assert "(+20 more)" in msg


def test_config_stays_free_form_until_the_node_registry_exists() -> None:
    spec = parse_spec(
        _text(
            steps=[
                {
                    "id": "x",
                    "node": "anything",
                    "inputs": ["produksi_npk_pkg"],
                    "config": {"a": [1, {"b": 2}]},
                }
            ],
            outputs={},
        )
    )
    assert spec.steps[0].config == {"a": [1, {"b": 2}]}
