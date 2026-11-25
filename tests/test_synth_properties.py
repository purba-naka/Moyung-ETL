from __future__ import annotations

import random
import string

import pytest

from moyung.synth import Target, TargetColumn, match_columns

TARGET = Target(
    name="out",
    columns=[
        TargetColumn(name="plant", type="text"),
        TargetColumn(name="product", type="text"),
        TargetColumn(name="tons", type="float"),
        TargetColumn(name="period", type="date", required=False),
    ],
    grain=["plant", "product"],
)
TYPE_OF = {"plant": "VARCHAR", "product": "VARCHAR", "tons": "DOUBLE", "period": "DATE"}
REQUIRED = ["plant", "product", "tons"]


def headers(names, decoys=0, seed=0):
    rnd = random.Random(seed)
    pool = [(n, TYPE_OF[n.strip().lower()]) for n in names]
    pool += [(f"zzz{i}_{rnd.randrange(10**6)}", "DOUBLE") for i in range(decoys)]
    rnd.shuffle(pool)
    return [n for n, _ in pool], [t for _, t in pool]


def match(names, types, **kw):
    return match_columns(TARGET, names, types, **kw)


@pytest.mark.parametrize("seed", range(15))
class TestExactHeaders:
    def test_the_headers_are_matched_to_themselves_in_any_order(self, seed):
        names, types = headers(["plant", "product", "tons"], decoys=seed % 5, seed=seed)
        mapping, scores, questions = match(names, types)
        assert mapping == {"plant": "plant", "product": "product", "tons": "tons"}
        assert questions == [] and all(s == 1.0 for s in scores.values())

    def test_letter_case_and_edge_spaces_do_not_matter(self, seed):
        rnd = random.Random(seed)
        spelled = {
            n: "".join(rnd.choice([c.upper(), c.lower()]) for c in n) + rnd.choice(["", " ", "  "])
            for n in ("plant", "product", "tons")
        }
        names, types = headers(list(spelled.values()), decoys=2, seed=seed)
        mapping, _, questions = match(names, types)
        assert questions == []
        assert {k: v.strip().lower() for k, v in mapping.items()} == {
            "plant": "plant",
            "product": "product",
            "tons": "tons",
        }

    def test_the_result_does_not_depend_on_the_order_of_the_headers(self, seed):
        names, types = headers(["plant", "product", "tons", "period"], decoys=3, seed=seed)
        reference = match(names, types)
        order = list(range(len(names)))
        random.Random(seed + 1).shuffle(order)
        shuffled = match([names[i] for i in order], [types[i] for i in order])
        assert shuffled[0] == reference[0] and shuffled[1] == reference[1]

    def test_an_optional_column_is_matched_when_present_and_silent_when_absent(self, seed):
        names, types = headers(["plant", "product", "tons", "period"], seed=seed)
        mapping, _, questions = match(names, types)
        assert mapping["period"] == "period" and questions == []
        names, types = headers(["plant", "product", "tons"], seed=seed)
        mapping, _, questions = match(names, types)
        assert "period" not in mapping and all(q.id != "col:period" for q in questions)


@pytest.mark.parametrize("seed", range(40))
class TestRandomHeaders:
    def _random_names(self, seed):
        rnd = random.Random(seed)
        alphabet = string.ascii_letters + string.digits + " _-|.é日"
        count = rnd.randrange(0, 12)
        names = [
            "".join(rnd.choice(alphabet) for _ in range(rnd.randrange(0, 15))) for _ in range(count)
        ]
        types = [rnd.choice(["VARCHAR", "DOUBLE", "BIGINT", "DATE", "BOOLEAN"]) for _ in names]
        return names, types

    def test_it_never_raises_and_every_mapped_header_exists(self, seed):
        names, types = self._random_names(seed)
        mapping, scores, questions = match(names, types)
        assert set(mapping.values()) <= set(names)
        assert set(scores) >= set(mapping)

    def test_one_header_is_never_given_to_two_target_columns(self, seed):
        names, types = self._random_names(seed)
        mapping, _, _ = match(names, types)
        assert len(set(mapping.values())) == len(mapping)

    def test_scores_stay_between_zero_and_one(self, seed):
        names, types = self._random_names(seed)
        _, scores, _ = match(names, types)
        assert all(0 <= s <= 1 for s in scores.values())

    def test_every_required_column_is_mapped_asked_about_or_a_measure(self, seed):
        names, types = self._random_names(seed)
        mapping, scores, questions = match(names, types)
        asked = {q.id for q in questions}
        for name in REQUIRED:
            assert name in mapping or f"col:{name}" in asked or scores.get(name) == 1.0

    def test_question_options_are_headers_of_the_sheet(self, seed):
        names, types = self._random_names(seed)
        _, _, questions = match(names, types)
        for question in questions:
            assert set(question.options) <= set(names)


class TestAmbiguityAndAnswers:
    def test_two_identical_headers_become_a_question_not_a_guess(self):
        mapping, _, questions = match(
            ["Plant", "Plant", "Product", "Tons"], ["VARCHAR", "VARCHAR", "VARCHAR", "DOUBLE"]
        )
        assert "plant" not in mapping and [q.id for q in questions] == ["col:plant"]

    def test_nothing_resembling_the_target_asks_about_every_required_column(self):
        _, _, questions = match(["a", "b", "c"], ["VARCHAR", "VARCHAR", "DOUBLE"])
        assert [q.id for q in questions] == ["col:plant", "col:product", "col:tons"]

    def test_an_answer_by_question_id_decides(self):
        names, types = ["Pabrik", "Produk", "Tonase"], ["VARCHAR", "VARCHAR", "DOUBLE"]
        mapping, _, questions = match(names, types, answers={"col:plant": "Pabrik"})
        assert mapping["plant"] == "Pabrik" and all(q.id != "col:plant" for q in questions)

    def test_an_answer_naming_a_header_that_is_not_there_is_not_used(self):
        names, types = ["Pabrik", "Produk", "Tonase"], ["VARCHAR", "VARCHAR", "DOUBLE"]
        mapping, _, _ = match(names, types, answers={"col:plant": "Ghost"})
        assert mapping.get("plant") != "Ghost"

    def test_an_answered_header_is_not_given_to_another_column(self):
        names, types = ["Pabrik", "Produk", "Tonase"], ["VARCHAR", "VARCHAR", "DOUBLE"]
        mapping, _, _ = match(names, types, answers={"col:plant": "Produk"})
        assert list(mapping.values()).count("Produk") == 1

    def test_a_decoy_with_the_right_type_does_not_beat_an_exact_name(self):
        names = ["x1", "tons", "x2"]
        mapping, _, _ = match(names, ["DOUBLE", "DOUBLE", "DOUBLE"])
        assert mapping["tons"] == "tons"

    def test_no_headers_at_all(self):
        mapping, scores, questions = match([], [])
        assert mapping == {} and len(questions) == 3

    def test_a_glossary_hint_can_make_an_unrelated_header_match(self):
        from moyung.knowledge import Knowledge

        names, types = ["Pabrik", "Produk", "Tonase"], ["VARCHAR", "VARCHAR", "DOUBLE"]
        knowledge = Knowledge(glossary={"plant": "Pabrik"})
        mapping, _, _ = match(names, types, knowledge=knowledge)
        assert mapping["plant"] == "Pabrik"

    def test_headers_and_types_of_unequal_length_do_not_crash_or_are_refused(self):
        try:
            match(["Plant", "Product"], ["VARCHAR"])
        except (ValueError, IndexError):
            return
