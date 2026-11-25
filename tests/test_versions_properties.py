from __future__ import annotations

import json
import random

import pytest
from pydantic import ValidationError

from moyung.pipeline import PipelineSpec, parse_spec
from moyung.versions import Change, SpecHistory, SpecVersion, diff_specs

SRC = {
    "file_role": "f",
    "sheet": {"name": "S"},
    "region": {"header_anchor": ["A"], "header_rows": 1},
}


def make_spec(seed):
    rnd = random.Random(seed)
    steps, last = [], "a"
    for i in range(rnd.randrange(1, 6)):
        kind = rnd.choice(["filter", "derive", "select"])
        config = {
            "filter": {"where": f"A > {rnd.randrange(100)}"},
            "derive": {"name": f"d{i}", "expr": f"A * {rnd.randrange(1, 9)}"},
            "select": {"columns": ["A"]},
        }[kind]
        steps.append({"id": f"s{i}", "node": kind, "inputs": [last], "config": config})
        last = f"s{i}"
    spec = {
        "sources": {"a": SRC},
        "steps": steps,
        "outputs": {"o": {"input": last, "format": rnd.choice(["parquet", "csv"])}},
    }
    return parse_spec(json.dumps(spec))


def mutate(spec: PipelineSpec, seed: int) -> PipelineSpec:
    rnd = random.Random(seed)
    data = spec.model_dump(mode="json")
    choice = rnd.choice(["format", "anchor", "sheet", "step_config", "add_output", "rename_step"])
    if choice == "format":
        out = data["outputs"]["o"]
        out["format"] = "csv" if out["format"] == "parquet" else "parquet"
    elif choice == "anchor":
        data["sources"]["a"]["region"]["header_anchor"] = ["A", "B"]
    elif choice == "sheet":
        data["sources"]["a"]["sheet"]["name"] = "Other"
    elif choice == "step_config":
        step = data["steps"][0]
        step["config"] = (
            {"where": "A > 999"}
            if step["node"] == "filter"
            else (
                {"name": "zz", "expr": "A + 1"}
                if step["node"] == "derive"
                else {"columns": ["A", "B"]}
            )
        )
    elif choice == "add_output":
        data["outputs"]["extra"] = {"input": "a"}
    else:
        data["steps"].append(
            {
                "id": "tail",
                "node": "select",
                "inputs": [data["steps"][-1]["id"]],
                "config": {"columns": ["A"]},
            }
        )
    return parse_spec(json.dumps(data))


@pytest.mark.parametrize("seed", range(40))
class TestDiffProperties:
    def test_a_spec_has_no_difference_with_itself(self, seed):
        spec = make_spec(seed)
        assert diff_specs(spec, spec) == ()

    def test_a_copy_has_no_difference(self, seed):
        spec = make_spec(seed)
        assert diff_specs(spec, parse_spec(spec.model_dump_json())) == ()

    def test_specs_are_equal_exactly_when_the_diff_is_empty(self, seed):
        a = make_spec(seed)
        b = mutate(a, seed)
        assert (a == b) == (diff_specs(a, b) == ())
        assert diff_specs(a, b) != ()

    def test_the_diff_is_mirrored_when_the_two_specs_swap_places(self, seed):
        a = make_spec(seed)
        b = mutate(a, seed)
        forward, backward = diff_specs(a, b), diff_specs(b, a)
        assert [c.where for c in forward] == [c.where for c in backward]
        flipped = {"added": "removed", "removed": "added", "changed": "changed"}
        assert [c.kind for c in backward] == [flipped[c.kind] for c in forward]
        for f, r in zip(forward, backward, strict=True):
            if f.kind == "changed":
                assert (f.before, f.after) == (r.after, r.before)

    def test_the_diff_is_sorted_and_deterministic(self, seed):
        a = make_spec(seed)
        b = mutate(a, seed)
        first = diff_specs(a, b)
        assert first == diff_specs(a, b)
        assert list(first) == sorted(first, key=lambda c: (c.where, c.kind))

    def test_every_change_prints_one_line(self, seed):
        a = make_spec(seed)
        for change in diff_specs(a, mutate(a, seed)):
            assert "\n" not in change.text() and change.where in change.text()

    def test_a_history_built_by_adding_versions_accepts_itself_when_reloaded(self, seed):
        spec = make_spec(seed)
        history = SpecHistory.start(spec)
        history = history.add(mutate(spec, seed), "repair")
        assert SpecHistory.model_validate_json(history.model_dump_json()) == history


class TestDiffDetails:
    def _base(self):
        return make_spec(3)

    def test_an_added_removed_and_changed_path_are_named(self):
        a = self._base()
        data = a.model_dump(mode="json")
        data["outputs"]["extra"] = {"input": "a"}
        data["outputs"]["o"]["format"] = (
            "csv" if data["outputs"]["o"]["format"] == "parquet" else "parquet"
        )
        b = parse_spec(json.dumps(data))
        kinds = {c.where: c.kind for c in diff_specs(a, b)}
        assert kinds["outputs.extra"] == "added" and kinds["outputs.o.format"] == "changed"
        assert {c.kind for c in diff_specs(b, a)} >= {"removed"}

    def test_a_moved_step_is_one_order_change(self):
        steps = [
            {"id": "x", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}},
            {"id": "y", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}},
        ]
        base = {"sources": {"a": SRC}, "outputs": {"o": {"input": "x"}}}
        a = parse_spec(json.dumps({**base, "steps": steps}))
        b = parse_spec(json.dumps({**base, "steps": steps[::-1]}))
        assert [c.where for c in diff_specs(a, b)] == ["steps.order"]

    def test_a_step_that_is_only_renamed_is_one_removal_and_one_addition(self):
        steps = [{"id": "x", "node": "select", "inputs": ["a"], "config": {"columns": ["A"]}}]
        base = {"sources": {"a": SRC}, "outputs": {"o": {"input": "x"}}}
        a = parse_spec(json.dumps({**base, "steps": steps}))
        renamed = [{**steps[0], "id": "z"}]
        b = parse_spec(
            json.dumps({"sources": {"a": SRC}, "steps": renamed, "outputs": {"o": {"input": "z"}}})
        )
        kinds = sorted(c.kind for c in diff_specs(a, b))
        assert "removed" in kinds and "added" in kinds

    def test_a_list_is_compared_as_a_whole(self):
        a = self._base()
        data = a.model_dump(mode="json")
        data["sources"]["a"]["region"]["header_anchor"] = ["A", "B", "C"]
        changes = diff_specs(a, parse_spec(json.dumps(data)))
        assert [c.where for c in changes] == ["sources.a.region.header_anchor"]

    def test_a_change_at_the_end_of_a_long_value_is_still_visible(self):
        long_where = "A > 1 AND " * 30
        base = {"sources": {"a": SRC}, "outputs": {"o": {"input": "s"}}}
        step = {
            "id": "s",
            "node": "filter",
            "inputs": ["a"],
            "config": {"where": long_where + "A > 2"},
        }
        other = {**step, "config": {"where": long_where + "A > 3"}}
        a = parse_spec(json.dumps({**base, "steps": [step]}))
        b = parse_spec(json.dumps({**base, "steps": [other]}))
        (change,) = diff_specs(a, b)
        assert "2" in change.before and "3" in change.after
        assert len(change.before) <= 80 and len(change.after) <= 80

    def test_unicode_is_shown_as_it_is(self):
        data = self._base().model_dump(mode="json")
        data["sources"]["a"]["sheet"]["name"] = "Lembar é日本"
        changes = diff_specs(self._base(), parse_spec(json.dumps(data)))
        assert "Lembar é日本" in changes[0].after

    def test_two_changes_in_one_value_keep_one_entry(self):
        a = self._base()
        data = a.model_dump(mode="json")
        data["sources"]["a"]["region"] = {
            "header_anchor": ["Z"],
            "header_rows": 3,
            "end": "until_blank_rows(5)",
        }
        assert len(diff_specs(a, parse_spec(json.dumps(data)))) == 3

    def test_changes_are_immutable(self):
        change = Change(where="x", kind="added", after="1")
        with pytest.raises(ValidationError):
            change.where = "y"


class TestHistory:
    def history(self, seed=1):
        spec = make_spec(seed)
        return spec, SpecHistory.start(spec)

    def test_the_first_version_has_no_diff_and_the_default_note(self):
        _, h = self.history()
        assert h.current.number == 1 and h.current.diff == () and h.current.note == "first version"

    def test_a_new_version_gets_the_next_number_and_its_diff(self):
        spec, h = self.history()
        h2 = h.add(mutate(spec, 1), "fix the anchor")
        assert [v.number for v in h2.versions] == [1, 2] and h2.current.diff

    def test_add_returns_a_new_history_and_leaves_the_old_one(self):
        spec, h = self.history()
        h.add(mutate(spec, 1), "x")
        assert len(h.versions) == 1

    def test_an_identical_spec_is_refused(self):
        spec, h = self.history()
        with pytest.raises(ValueError, match="same as the current version"):
            h.add(parse_spec(spec.model_dump_json()), "no change")

    @pytest.mark.parametrize("note", ["", "   ", "\n\t"])
    def test_a_blank_note_is_refused(self, note):
        spec, h = self.history()
        with pytest.raises(ValueError, match="give a note"):
            h.add(mutate(spec, 1), note)

    def test_a_note_of_exactly_the_limit_is_stored_and_one_more_is_refused(self):
        spec, h = self.history()
        assert len(h.add(mutate(spec, 1), "n" * 500).current.note) == 500
        with pytest.raises(ValueError):
            h.add(mutate(spec, 1), "n" * 501)

    def test_the_stored_spec_is_a_copy_not_the_callers_object(self):
        spec, h = self.history()
        new = mutate(spec, 1)
        h2 = h.add(new, "x")
        assert h2.current.spec == new and h2.current.spec is not new

    def test_a_note_keeps_its_whitespace_as_given(self):
        spec, h = self.history()
        assert h.add(mutate(spec, 1), "  keep  ").current.note == "  keep  "

    def test_get_finds_a_version_and_names_the_range_when_missing(self):
        spec, h = self.history()
        h = h.add(mutate(spec, 1), "x")
        assert h.get(2).number == 2
        for bad in (0, 3, -1, "1", None):
            with pytest.raises(KeyError, match="versions: 1..2"):
                h.get(bad)

    def test_text_lists_every_version_with_its_changes(self):
        spec, h = self.history()
        h = h.add(mutate(spec, 1), "second")
        text = h.text()
        assert text.startswith("spec history: 2 version(s), current v2")
        assert "v1: first version (0 change(s))" in text and "v2: second" in text

    def test_a_history_must_have_a_version(self):
        with pytest.raises(ValidationError):
            SpecHistory(versions=())

    def test_numbers_must_run_one_two_three(self):
        spec = make_spec(1)
        v1 = SpecVersion(number=1, spec=spec, note="a", diff=())
        for numbers in ([2], [1, 3], [1, 1]):
            versions = tuple(SpecVersion(number=n, spec=spec, note="a", diff=()) for n in numbers)
            with pytest.raises(ValidationError):
                SpecHistory(versions=versions)
        assert SpecHistory(versions=(v1,)).current.number == 1

    def test_the_first_version_may_not_carry_a_diff(self):
        spec = make_spec(1)
        fake = (Change(where="x", kind="added", after="1"),)
        with pytest.raises(ValidationError, match="version 1 has no diff"):
            SpecHistory(versions=(SpecVersion(number=1, spec=spec, note="a", diff=fake),))

    def test_a_stored_diff_that_does_not_match_its_specs_is_refused(self):
        spec = make_spec(1)
        v1 = SpecVersion(number=1, spec=spec, note="a", diff=())
        v2 = SpecVersion(number=2, spec=mutate(spec, 1), note="b", diff=())
        with pytest.raises(ValidationError, match="does not match its spec"):
            SpecHistory(versions=(v1, v2))

    def test_a_tampered_spec_in_stored_json_is_refused_on_load(self):
        spec, h = self.history()
        h = h.add(mutate(spec, 1), "x")
        data = json.loads(h.model_dump_json())
        data["versions"][1]["spec"]["outputs"]["o"]["format"] = (
            "csv"
            if data["versions"][1]["spec"]["outputs"]["o"]["format"] == "parquet"
            else "parquet"
        )
        with pytest.raises(ValidationError, match="does not match its spec"):
            SpecHistory.model_validate(data)

    def test_a_long_chain_of_versions_stays_consistent(self):
        spec = make_spec(2)
        h = SpecHistory.start(spec)
        for i in range(25):
            data = h.current.spec.model_dump(mode="json")
            data["sources"]["a"]["sheet"]["name"] = f"Sheet{i}"
            h = h.add(parse_spec(json.dumps(data)), f"step {i}")
        assert h.current.number == 26
        assert SpecHistory.model_validate_json(h.model_dump_json()) == h
