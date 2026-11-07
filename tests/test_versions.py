from __future__ import annotations

import json

import pytest

from excel_etl.pipeline import PipelineSpec, parse_spec
from excel_etl.versions import SpecHistory, diff_specs


def _spec(**over) -> PipelineSpec:
    base = {
        "sources": {
            "s": {
                "file_role": "f",
                "sheet": {"name": "Data"},
                "region": {"header_anchor": ["Item"]},
            }
        },
        "steps": [
            {"id": "a", "node": "select", "inputs": ["s"], "config": {"columns": ["Item"]}},
            {"id": "b", "node": "sort", "inputs": ["a"], "config": {"by": ["Item"]}},
        ],
        "outputs": {"o": {"input": "b"}},
    }
    base.update(over)
    return parse_spec(json.dumps(base))


def _where(a: PipelineSpec, b: PipelineSpec) -> list[tuple[str, str]]:
    return [(c.where, c.kind) for c in diff_specs(a, b)]


def test_same_spec_has_no_diff() -> None:
    assert diff_specs(_spec(), _spec()) == ()


def test_a_changed_anchor_is_named_by_path() -> None:
    src = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["Produk"]}}
    ch = diff_specs(_spec(), _spec(sources={"s": src}))
    assert [c.where for c in ch] == ["sources.s.region.header_anchor"]
    assert ch[0].before == '["Item"]' and ch[0].after == '["Produk"]'
    assert ch[0].text() == '~ sources.s.region.header_anchor: ["Item"] -> ["Produk"]'


def test_sources_added_and_removed() -> None:
    other = {"file_role": "g", "sheet": {"name": "X"}, "region": {"header_anchor": ["k"]}}
    both = _spec(sources={**_spec().model_dump(mode="json")["sources"], "t": other})
    assert _where(_spec(), both) == [("sources.t", "added")]
    assert _where(both, _spec()) == [("sources.t", "removed")]


def test_a_step_config_change_is_found_by_step_id() -> None:
    steps = _spec().model_dump(mode="json")["steps"]
    steps[1]["config"] = {"by": ["Item"], "descending": True}
    ch = diff_specs(_spec(), _spec(steps=steps))
    assert [(c.where, c.kind) for c in ch] == [("steps.b.config.descending", "added")]


def test_steps_are_matched_by_id_not_position() -> None:
    steps = _spec().model_dump(mode="json")["steps"]
    extra = {"id": "c", "node": "dedupe", "inputs": ["a"], "config": {}}
    new = _spec(steps=[steps[0], extra, steps[1]])
    assert _where(_spec(), new) == [("steps.c", "added")]  # b did not change, only moved down


def test_reordering_the_same_steps_is_one_change() -> None:
    sw = _spec(
        steps=[
            {"id": "b", "node": "sort", "inputs": ["s"], "config": {"by": ["Item"]}},
            {"id": "a", "node": "select", "inputs": ["b"], "config": {"columns": ["Item"]}},
        ],
        outputs={"o": {"input": "a"}},
    )
    got = _where(_spec(), sw)
    assert ("steps.order", "changed") in got


def test_long_values_are_cut() -> None:
    cols = [f"column_{i}" for i in range(40)]
    steps = _spec().model_dump(mode="json")["steps"]
    steps[0]["config"] = {"columns": cols}
    c = diff_specs(_spec(), _spec(steps=steps))[0]
    assert c.after is not None and len(c.after) == 80 and c.after.endswith("...")


def test_history_adds_versions_and_keeps_every_old_one() -> None:
    v1 = _spec()
    src = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["Produk"]}}
    v2 = _spec(sources={"s": src})
    h = SpecHistory.start(v1)
    h2 = h.add(v2, "header renamed Item -> Produk in the March file")
    assert len(h.versions) == 1 and len(h2.versions) == 2  # add does not change h
    assert h2.current.number == 2 and h2.get(1).spec == v1 and h2.current.spec == v2
    assert h2.get(1).diff == () and len(h2.current.diff) == 1
    assert "v2: header renamed" in h2.text()
    assert "~ sources.s.region.header_anchor" in h2.text()


def test_a_repair_that_changes_nothing_or_has_no_note_is_refused() -> None:
    h = SpecHistory.start(_spec())
    with pytest.raises(ValueError, match="same as the current"):
        h.add(_spec(), "no-op")
    src = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["P"]}}
    with pytest.raises(ValueError, match="give a note"):
        h.add(_spec(sources={"s": src}), "  ")
    with pytest.raises(KeyError, match="versions: 1..1"):
        h.get(9)


def test_history_survives_json_for_the_host_store() -> None:
    src = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": ["P"]}}
    h = SpecHistory.start(_spec()).add(_spec(sources={"s": src}), "renamed")
    back = SpecHistory.model_validate_json(h.model_dump_json())
    assert back == h and back.current.number == 2


def test_version_numbers_keep_counting() -> None:
    def with_anchor(a: str) -> PipelineSpec:
        src = {"file_role": "f", "sheet": {"name": "Data"}, "region": {"header_anchor": [a]}}
        return _spec(sources={"s": src})

    h = SpecHistory.start(_spec()).add(with_anchor("B"), "one").add(with_anchor("C"), "two")
    assert [v.number for v in h.versions] == [1, 2, 3]
    assert h.get(3).diff[0].before == '["B"]' and h.get(3).diff[0].after == '["C"]'


def test_a_stored_history_with_wrong_numbers_or_diff_is_refused() -> None:
    h = SpecHistory.start(_spec())
    h = h.add(_spec(outputs={"o": {"input": "a"}}), "rewire")
    data = json.loads(h.model_dump_json())
    SpecHistory.model_validate(data)
    bad = json.loads(h.model_dump_json())
    bad["versions"][1]["number"] = 5
    with pytest.raises(ValueError, match="numbers"):
        SpecHistory.model_validate(bad)
    bad = json.loads(h.model_dump_json())
    bad["versions"][1]["diff"] = []
    with pytest.raises(ValueError, match="diff"):
        SpecHistory.model_validate(bad)


def test_history_refuses_a_note_over_the_cap_however_it_is_built():
    import pytest as _pytest
    from pydantic import ValidationError

    from excel_etl.versions import MAX_NOTE

    with _pytest.raises(ValidationError):
        SpecHistory.start(_spec(), "x" * (MAX_NOTE + 1))
    h = SpecHistory.start(_spec(), "x" * MAX_NOTE)
    with _pytest.raises(ValidationError):
        h.add(_spec(outputs={"o": {"input": "a"}}), "y" * (MAX_NOTE + 1))
