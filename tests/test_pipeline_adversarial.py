from __future__ import annotations

import copy
import json

import pytest

from moyung.pipeline import SpecError, dump_spec, parse_spec, patch_spec

SRC = {
    "file_role": "f",
    "sheet": {"name": "S"},
    "region": {"header_anchor": ["A"], "header_rows": 1},
}


def _step(sid, *inputs, cols=("A",)):
    return {"id": sid, "node": "select", "inputs": list(inputs), "config": {"columns": list(cols)}}


def _base(**over):
    spec = {
        "sources": {"a": SRC},
        "steps": [_step("s1", "a"), _step("s2", "s1")],
        "outputs": {"o": {"input": "s2"}},
    }
    spec.update(over)
    return spec


def _parse(spec):
    return parse_spec(json.dumps(spec))


class TestParseInput:
    @pytest.mark.parametrize("text", [None, 5, b"{}", {}, [], 1.5])
    def test_only_text_is_accepted(self, text):
        with pytest.raises(SpecError, match="must be a JSON string"):
            parse_spec(text)

    @pytest.mark.parametrize("text", ["", " ", "{", "[]", "null", "42", '"x"', "true"])
    def test_text_that_is_not_a_spec_object(self, text):
        with pytest.raises(SpecError):
            parse_spec(text)

    def test_yaml_gets_a_hint(self):
        with pytest.raises(SpecError, match="YAML is not supported"):
            parse_spec("sources:\n  a: 1")

    def test_an_empty_object_is_a_valid_empty_spec(self):
        spec = parse_spec("{}")
        assert (spec.sources, spec.steps, spec.outputs) == ({}, [], {})

    def test_unknown_top_level_keys_are_refused(self):
        with pytest.raises(SpecError):
            _parse({**_base(), "surprise": 1})

    def test_nan_literal_does_not_become_a_spec(self):
        with pytest.raises(SpecError):
            parse_spec('{"sources": NaN}')

    def test_deep_nesting_is_an_error_not_a_crash(self):
        with pytest.raises(SpecError):
            parse_spec('{"sources":' + "[" * 5000 + "]" * 5000 + "}")

    def test_a_long_chain_of_steps_parses(self):
        steps = [_step("s0", "a")] + [_step(f"s{i}", f"s{i - 1}") for i in range(1, 300)]
        spec = _parse(_base(steps=steps, outputs={"o": {"input": "s299"}}))
        assert len(spec.steps) == 300

    def test_the_message_is_bounded(self):
        bad = {f"o{i}": {"input": "ghost"} for i in range(30)}
        with pytest.raises(SpecError) as e:
            _parse(_base(outputs=bad))
        assert len(e.value.args[0].splitlines()) <= 14


class TestNames:
    @pytest.mark.parametrize(
        "name", ["a b", "1a", "a-b", "a.b", "", "é", "a" * 65, "_", "__", "_-", "a\n"]
    )
    def test_bad_source_names(self, name):
        with pytest.raises(SpecError):
            _parse(_base(sources={name: SRC}, steps=[], outputs={}))

    @pytest.mark.parametrize("name", ["a", "A1", "a_b", "a" * 63, "_a", "_1", "_" + "a" * 63])
    def test_good_source_names(self, name):
        assert name in _parse(_base(sources={name: SRC}, steps=[], outputs={})).sources

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-011: the error says 1-64 characters, a letter-first name stops at 63",
    )
    def test_a_64_character_name_starting_with_a_letter_is_accepted_as_the_message_says(self):
        name = "a" * 64
        assert name in _parse(_base(sources={name: SRC}, steps=[], outputs={})).sources

    def test_sources_that_differ_only_in_case(self):
        with pytest.raises(SpecError, match="differ only in letter case"):
            _parse(_base(sources={"a": SRC, "A": SRC}, steps=[], outputs={}))

    def test_a_step_named_like_a_source(self):
        with pytest.raises(SpecError, match="already used"):
            _parse(_base(steps=[_step("a", "a")], outputs={}))

    def test_a_step_that_differs_from_a_source_only_in_case(self):
        with pytest.raises(SpecError, match="differ only in letter case"):
            _parse(_base(steps=[_step("A", "a")], outputs={}))

    def test_two_steps_with_one_id(self):
        with pytest.raises(SpecError, match="already used"):
            _parse(_base(steps=[_step("s", "a"), _step("s", "a")], outputs={}))

    def test_a_step_cannot_read_itself(self):
        with pytest.raises(SpecError, match="not a source or an earlier step"):
            _parse(_base(steps=[_step("s", "s")], outputs={}))

    def test_a_step_cannot_read_a_later_step(self):
        with pytest.raises(SpecError, match="not a source or an earlier step"):
            _parse(_base(steps=[_step("s1", "s2"), _step("s2", "a")], outputs={}))

    def test_a_step_needs_at_least_one_input(self):
        with pytest.raises(SpecError):
            _parse(_base(steps=[{"id": "s", "node": "select", "inputs": []}], outputs={}))

    def test_an_output_reading_nothing_known(self):
        with pytest.raises(SpecError, match="not defined"):
            _parse(_base(outputs={"o": {"input": "ghost"}}))

    def test_input_names_are_case_sensitive(self):
        with pytest.raises(SpecError, match="not defined"):
            _parse(_base(outputs={"o": {"input": "S2"}}))

    @pytest.mark.parametrize("name", ["con", "CON", "Nul", "aux", "com1", "LPT9", "prn"])
    def test_windows_device_names_are_not_output_names(self, name):
        with pytest.raises(SpecError, match="reserved file name"):
            _parse(_base(outputs={name: {"input": "s2"}}))

    def test_outputs_that_would_be_one_file(self):
        with pytest.raises(SpecError, match="would be the same file"):
            _parse(_base(outputs={"o": {"input": "s2"}, "O": {"input": "s2"}}))

    @pytest.mark.parametrize("fmt", ["xlsx", "json", "", None, 5])
    def test_unknown_output_formats(self, fmt):
        with pytest.raises(SpecError):
            _parse(_base(outputs={"o": {"input": "s2", "format": fmt}}))

    def test_an_output_without_input(self):
        with pytest.raises(SpecError):
            _parse(_base(outputs={"o": {}}))


class TestExcludedAndChecks:
    def test_excluded_must_name_a_source(self):
        with pytest.raises(SpecError, match="not a source"):
            _parse(_base(excluded={"ghost": {"X": "because reasons"}}))

    def test_excluded_needs_a_column(self):
        with pytest.raises(SpecError, match="at least one column"):
            _parse(_base(excluded={"a": {}}))

    def test_a_reason_must_have_three_characters(self):
        with pytest.raises(SpecError):
            _parse(_base(excluded={"a": {"X": "no"}}))
        assert _parse(_base(excluded={"a": {"X": "ok!"}})).excluded == {"a": {"X": "ok!"}}

    def test_a_check_must_read_a_defined_table(self):
        check = {"kind": "row_count", "table": "ghost", "min": 1}
        with pytest.raises(SpecError, match="not defined"):
            _parse(_base(checks={"c": check}))

    def test_a_check_may_read_any_source_or_step(self):
        check = {"kind": "row_count", "table": "s1", "min": 1}
        assert "c" in _parse(_base(checks={"c": check})).checks

    def test_a_check_with_an_unknown_kind(self):
        with pytest.raises(SpecError):
            _parse(_base(checks={"c": {"kind": "magic", "table": "a"}}))


class TestLoad:
    def _load(self, table, fmt="parquet", name="o"):
        return {name: {"input": "s2", "format": fmt, "load": {"table": table, "mode": "replace"}}}

    def test_csv_cannot_be_loaded(self):
        with pytest.raises(SpecError, match="only a parquet file can be loaded"):
            _parse(_base(outputs=self._load("t", "csv")))

    def test_two_outputs_cannot_load_into_one_table(self):
        outputs = {**self._load("T", name="o"), **self._load("t", name="p")}
        with pytest.raises(SpecError, match="same table"):
            _parse(_base(outputs=outputs))

    def test_merge_needs_keys_and_other_modes_refuse_them(self):
        merge = {"o": {"input": "s2", "load": {"table": "t", "mode": "merge"}}}
        with pytest.raises(SpecError, match="merge needs keys"):
            _parse(_base(outputs=merge))
        keyed = {"o": {"input": "s2", "load": {"table": "t", "mode": "append", "keys": ["A"]}}}
        with pytest.raises(SpecError, match="only for merge"):
            _parse(_base(outputs=keyed))

    def test_duplicate_merge_keys_ignoring_case(self):
        dup = {"o": {"input": "s2", "load": {"table": "t", "mode": "merge", "keys": ["A", "a"]}}}
        with pytest.raises(SpecError, match="twice"):
            _parse(_base(outputs=dup))

    def test_a_load_survives_the_round_trip(self):
        spec = _parse(_base(outputs=self._load("t")))
        assert parse_spec(dump_spec(spec)) == spec


class TestSourceKinds:
    def test_an_excel_source_needs_sheet_and_region(self):
        with pytest.raises(SpecError, match="needs sheet and region"):
            _parse(_base(sources={"a": {"file_role": "f"}}, steps=[], outputs={}))

    def test_a_csv_source_refuses_sheet_settings(self):
        bad = {"file_role": "f", "kind": "csv", "sheet": {"name": "S"}}
        with pytest.raises(SpecError):
            _parse(_base(sources={"a": bad}, steps=[], outputs={}))

    @pytest.mark.parametrize("delimiter", ["", ",,", ";;"])
    def test_a_delimiter_is_one_character(self, delimiter):
        bad = {"file_role": "f", "kind": "csv", "delimiter": delimiter}
        with pytest.raises(SpecError):
            _parse(_base(sources={"a": bad}, steps=[], outputs={}))

    def test_a_file_role_cannot_be_empty(self):
        with pytest.raises(SpecError):
            _parse(_base(sources={"a": {**SRC, "file_role": ""}}, steps=[], outputs={}))

    def test_one_role_of_two_kinds_parses_and_is_left_to_the_run(self):
        sources = {"a": SRC, "b": {"file_role": "f", "kind": "csv"}}
        assert set(_parse(_base(sources=sources, steps=[], outputs={})).sources) == {"a", "b"}


class TestRoundTrip:
    def test_dump_then_parse_is_equal(self):
        spec = _parse(_base())
        assert parse_spec(dump_spec(spec)) == spec

    def test_dump_leaves_out_unset_fields(self):
        text = dump_spec(_parse(_base()))
        assert "null" not in text and "excluded" not in text and '"ml"' not in text

    def test_unicode_survives(self):
        spec = _parse(_base(excluded={"a": {"Kolom é": "alasan 😀 jelas"}}))
        assert parse_spec(dump_spec(spec)).excluded == {"a": {"Kolom é": "alasan 😀 jelas"}}

    def test_json_key_order_does_not_matter_for_equality(self):
        one = _parse(_base())
        reordered = json.loads(json.dumps(_base()))
        two = _parse(dict(reversed(list(reordered.items()))))
        assert one == two


class TestPatch:
    def _spec(self):
        return _parse(_base())

    def _patch(self, patch, spec=None):
        return patch_spec(spec or self._spec(), json.dumps(patch))

    @pytest.mark.parametrize("text", ["", "{", "[]", "null", "42", '"x"', None, 5])
    def test_a_patch_must_be_a_json_object(self, text):
        with pytest.raises(SpecError, match="JSON object"):
            patch_spec(self._spec(), text)

    def test_a_patch_given_as_bytes_is_read_like_text(self):
        assert patch_spec(self._spec(), b"{}") == self._spec()

    def test_an_empty_patch_changes_nothing(self):
        assert self._patch({}) == self._spec()

    def test_the_original_spec_is_never_changed(self):
        spec = self._spec()
        before = copy.deepcopy(spec.model_dump(mode="json"))
        self._patch({"remove": ["s2"], "outputs": {"o": {"input": "s1"}}}, spec)
        assert spec.model_dump(mode="json") == before

    @pytest.mark.parametrize("key", ["checks", "steps_", "REMOVE", "name", ""])
    def test_unknown_patch_keys_are_named(self, key):
        with pytest.raises(SpecError, match="is not a setting here"):
            self._patch({key: 1})

    @pytest.mark.parametrize("remove", ["s1", 5, None, [1], [None], {"id": "s1"}])
    def test_remove_must_be_a_list_of_ids(self, remove):
        with pytest.raises(SpecError, match="remove must be a list"):
            self._patch({"remove": remove})

    @pytest.mark.parametrize("steps", ["s", 5, None, [1], ["s1"], {"id": "x"}])
    def test_steps_must_be_a_list_of_objects(self, steps):
        with pytest.raises(SpecError, match="steps must be a list"):
            self._patch({"steps": steps})

    @pytest.mark.parametrize("key", ["sources", "outputs", "excluded"])
    @pytest.mark.parametrize("value", [[1], "x", 5])
    def test_maps_must_be_objects(self, key, value):
        with pytest.raises(SpecError, match="must be an object"):
            self._patch({key: value})

    def test_removing_an_unknown_step(self):
        with pytest.raises(SpecError, match="no step 'ghost'"):
            self._patch({"remove": ["ghost"]})

    def test_removing_the_same_step_twice(self):
        with pytest.raises(SpecError, match="no step 's2'"):
            self._patch({"remove": ["s2", "s2"], "outputs": {"o": None}})

    def test_removing_a_step_that_another_still_reads(self):
        with pytest.raises(SpecError, match="not a source or an earlier step"):
            self._patch({"remove": ["s1"]})

    def test_removing_a_step_an_output_still_reads(self):
        with pytest.raises(SpecError, match="not defined"):
            self._patch({"remove": ["s2"]})

    def test_removing_a_step_with_its_output_works(self):
        spec = self._patch({"remove": ["s2"], "outputs": {"o": {"input": "s1"}}})
        assert [s.id for s in spec.steps] == ["s1"] and spec.outputs["o"].input == "s1"

    def test_a_step_without_an_id(self):
        with pytest.raises(SpecError, match="each step needs an id"):
            self._patch({"steps": [{"node": "select", "inputs": ["a"]}]})

    def test_an_existing_step_is_replaced_in_place(self):
        spec = self._patch({"steps": [_step("s1", "a", cols=("A", "B"))]})
        assert [s.id for s in spec.steps] == ["s1", "s2"]
        assert spec.steps[0].config["columns"] == ["A", "B"]

    def test_a_new_step_is_added_at_the_end(self):
        spec = self._patch({"steps": [_step("s3", "s2")]})
        assert [s.id for s in spec.steps] == ["s1", "s2", "s3"]

    def test_steps_are_reordered_after_what_they_read(self):
        spec = self._patch({"steps": [_step("s0", "a"), _step("s1", "s0")]})
        assert [s.id for s in spec.steps] == ["s0", "s1", "s2"]

    def test_a_cycle_is_named(self):
        with pytest.raises(SpecError, match="cycle"):
            self._patch({"steps": [_step("s1", "s2")]})

    def test_a_step_reading_itself_is_a_cycle(self):
        with pytest.raises(SpecError):
            self._patch({"steps": [_step("s9", "s9")]})

    def test_the_same_step_twice_in_one_patch_keeps_the_last(self):
        spec = self._patch({"steps": [_step("s3", "s2"), _step("s3", "s1", cols=("A", "B"))]})
        assert spec.steps[-1].id == "s3" and spec.steps[-1].inputs == ["s1"]

    def test_null_removes_entries_and_a_missing_name_is_fine(self):
        spec = self._patch(
            {"remove": ["s1", "s2"], "outputs": {"o": None, "ghost": None}, "sources": {"a": None}}
        )
        assert spec.sources == {} and spec.outputs == {} and spec.steps == []

    def test_a_new_output_is_added(self):
        spec = self._patch({"outputs": {"p": {"input": "s1", "format": "csv"}}})
        assert set(spec.outputs) == {"o", "p"} and spec.outputs["p"].format == "csv"

    def test_an_output_is_replaced_whole(self):
        spec = self._patch({"outputs": {"o": {"input": "s1"}}})
        assert spec.outputs["o"].input == "s1"

    def test_a_bad_value_in_a_patch_is_a_spec_error(self):
        with pytest.raises(SpecError):
            self._patch({"outputs": {"o": {"input": "s1", "format": "xlsx"}}})

    def test_excluded_can_be_added_and_removed(self):
        spec = self._patch({"excluded": {"a": {"X": "not needed"}}})
        assert spec.excluded == {"a": {"X": "not needed"}}
        assert self._patch({"excluded": {"a": None}}, spec).excluded == {}

    def test_ml_null_removes_the_section(self):
        assert self._patch({"ml": None}).ml is None

    def test_a_patch_that_names_a_new_source(self):
        assert set(self._patch({"sources": {"b": SRC}}).sources) == {"a", "b"}

    def test_a_patched_spec_round_trips(self):
        spec = self._patch({"steps": [_step("s3", "s2")], "outputs": {"p": {"input": "s3"}}})
        assert parse_spec(dump_spec(spec)) == spec
