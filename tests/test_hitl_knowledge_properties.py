from __future__ import annotations

import itertools
import random

import pytest

from moyung.hitl import (
    CHECKPOINTS,
    HitlError,
    HitlPolicy,
    HitlState,
    answer,
    approve,
    ask_human,
    gate,
    should_ask,
)
from moyung.knowledge import QA, Example, Knowledge, lookup_knowledge

MODES = ["off", "optional", "required", "required_if_fail"]


def state_with(mode, approval=None, pending=0, checkpoint="before_load"):
    policy = HitlPolicy(checkpoints={**HitlPolicy().checkpoints, checkpoint: mode})
    state = HitlState(policy=policy)
    for i in range(pending):
        _, state = ask_human(state, "clarify", f"what is {i}?")
    if approval is not False and approval != "none":
        state = approve(state, checkpoint, "ann", ref=approval)
    return state


def expected_gate(mode, approval, gate_ref, failed, pending):
    if mode in ("off", "optional") or (mode == "required_if_fail" and not failed):
        return "go"
    if approval == "none":
        return "no approval"
    if gate_ref is not None and approval != gate_ref:
        return "stale"
    if pending:
        return "pending"
    return "go"


class TestGateMatrix:
    @pytest.mark.parametrize(
        "mode, approval, gate_ref, failed, pending",
        list(
            itertools.product(
                MODES, ["none", None, "v1"], [None, "v1", "v2"], [False, True], [0, 2]
            )
        ),
    )
    def test_every_combination_matches_the_decision_table(
        self, mode, approval, gate_ref, failed, pending
    ):
        state = state_with(mode, approval, pending)
        reason = gate(state, "before_load", failed=failed, ref=gate_ref)
        want = expected_gate(mode, approval, gate_ref, failed, pending)
        if want == "go":
            assert reason is None
        else:
            assert reason is not None
            marker = {
                "no approval": "needs a person's approval",
                "stale": "ask again",
                "pending": "still waiting",
            }[want]
            assert marker in reason

    @pytest.mark.parametrize("checkpoint", CHECKPOINTS)
    def test_the_default_policy_for_every_checkpoint(self, checkpoint):
        mode = HitlPolicy().checkpoints[checkpoint]
        blocked = gate(HitlState(), checkpoint, failed=False) is not None
        assert blocked == (mode == "required")

    @pytest.mark.parametrize("checkpoint", ["", "nope", "AFTER_SPEC", None, 5])
    def test_an_unknown_checkpoint_is_refused_by_gate_and_approve(self, checkpoint):
        with pytest.raises(HitlError, match="unknown checkpoint"):
            gate(HitlState(), checkpoint)
        with pytest.raises(HitlError, match="unknown checkpoint"):
            approve(HitlState(), checkpoint, "ann")

    def test_an_approval_for_one_checkpoint_does_not_open_another(self):
        state = approve(HitlState(), "before_load", "ann")
        assert gate(state, "on_rebind_drift") is not None

    def test_a_new_approval_replaces_the_old_one(self):
        state = approve(HitlState(), "before_load", "ann", ref="v1")
        state = approve(state, "before_load", "bob", ref="v2")
        assert gate(state, "before_load", ref="v1") is not None
        assert gate(state, "before_load", ref="v2") is None
        assert state.approvals["before_load"].by == "bob"

    def test_the_approval_records_who_when_and_for_what(self):
        state = approve(HitlState(), "after_spec", "  ann  lee ", ref="v3", at="2026-01-01")
        assert state.approvals["after_spec"].model_dump() == {
            "by": "ann lee",
            "at": "2026-01-01",
            "ref": "v3",
        }

    @pytest.mark.parametrize("by", ["", "   ", None, 5, [], "x" * 1001])
    def test_a_bad_approver_is_refused(self, by):
        with pytest.raises(HitlError):
            approve(HitlState(), "after_spec", by)

    def test_approve_does_not_change_the_state_it_is_given(self):
        before = HitlState()
        approve(before, "after_spec", "ann")
        assert before.approvals == {}

    def test_a_missing_policy_entry_means_off(self):
        state = HitlState(policy=HitlPolicy(checkpoints={"after_spec": "required"}))
        assert gate(state, "before_load") is None
        assert gate(state, "after_spec") is not None


class TestAsk:
    def test_ids_count_up_and_are_never_reused_after_an_answer(self):
        state, knowledge = HitlState(), Knowledge()
        ids = []
        for i in range(5):
            q, state = ask_human(state, "clarify", f"q{i}?")
            ids.append(q.id)
            state, knowledge = answer(state, knowledge, q.id, "ok")
        assert ids == ["q1", "q2", "q3", "q4", "q5"] and state.next_id == 6

    def test_the_same_open_question_returns_the_first_one(self):
        first, state = ask_human(HitlState(), "choose", "which?", ["a", "b"], ref="v1")
        again, same = ask_human(state, "choose", "  which?  ", ["a", "b"], ref="v1")
        assert again == first and same is state and len(state.pending) == 1

    @pytest.mark.parametrize(
        "second",
        [
            ("confirm", "which?", [], "v1"),
            ("choose", "which?", ["a", "c"], "v1"),
            ("choose", "which?", ["a", "b"], "v2"),
            ("choose", "which?", ["a", "b"], None),
            ("choose", "Which?", ["a", "b"], "v1"),
        ],
    )
    def test_anything_different_is_a_new_question(self, second):
        _, state = ask_human(HitlState(), "choose", "which?", ["a", "b"], ref="v1")
        kind, question, options, ref = second
        new, state = ask_human(state, kind, question, options, ref=ref)
        assert new.id == "q2" and len(state.pending) == 2

    def test_a_question_asked_again_after_it_was_answered_is_new(self):
        q, state = ask_human(HitlState(), "confirm", "go?")
        state, knowledge = answer(state, Knowledge(), q.id, "yes")
        again, state = ask_human(state, "confirm", "go?")
        assert again.id == "q2"

    def test_at_most_twenty_questions_wait(self):
        state = HitlState()
        for i in range(20):
            _, state = ask_human(state, "clarify", f"q{i}?")
        with pytest.raises(HitlError, match="20 questions are already waiting"):
            ask_human(state, "clarify", "one more?")
        again, same = ask_human(state, "clarify", "q3?")
        assert same is state and again.question == "q3?"

    @pytest.mark.parametrize(
        "kind, options",
        [
            ("choose", []),
            ("choose", ["only"]),
            ("choose", ["a", "A"]),
            ("choose", ["a", "  a "]),
            ("confirm", ["yes"]),
            ("clarify", ["x"]),
            ("choose", [f"o{i}" for i in range(21)]),
            ("choose", "ab"),
            ("choose", ["a", ""]),
            ("choose", ["a", None]),
            ("ask", []),
            (None, []),
        ],
    )
    def test_a_badly_formed_question_is_refused(self, kind, options):
        with pytest.raises(HitlError):
            ask_human(HitlState(), kind, "which?", options)

    @pytest.mark.parametrize("question", ["", "  ", None, 5, [], "x" * 1001])
    def test_a_bad_question_text_is_refused(self, question):
        with pytest.raises(HitlError):
            ask_human(HitlState(), "clarify", question)

    def test_a_question_of_exactly_the_limit_is_fine(self):
        q, _ = ask_human(HitlState(), "clarify", "x" * 1000)
        assert len(q.question) == 1000

    def test_the_limit_counts_after_whitespace_is_collapsed(self):
        q, _ = ask_human(HitlState(), "clarify", ("x " * 400).strip())
        assert len(q.question) < 1000

    def test_whitespace_and_newlines_in_a_question_become_single_spaces(self):
        q, _ = ask_human(HitlState(), "clarify", "  a \n\n b\t c ")
        assert q.question == "a b c"

    def test_exactly_twenty_options_are_fine(self):
        q, _ = ask_human(HitlState(), "choose", "which?", [f"o{i}" for i in range(20)])
        assert len(q.options) == 20

    def test_unicode_text(self):
        q, _ = ask_human(HitlState(), "choose", "Pilih pabrik 日本?", ["Pabrik é", "Pabrik ü"])
        assert q.options == ["Pabrik é", "Pabrik ü"]


class TestAnswer:
    def _ask(self, kind, options=()):
        q, state = ask_human(HitlState(), kind, "q?", list(options))
        return q, state

    @pytest.mark.parametrize(
        "reply, canonical",
        [("yes", "yes"), ("Y", "yes"), (" YA ", "yes"), ("No", "no"), ("n", "no"), ("TIDAK", "no")],
    )
    def test_a_confirm_reply_is_stored_in_canonical_form(self, reply, canonical):
        q, state = self._ask("confirm")
        _, knowledge = answer(state, Knowledge(), q.id, reply)
        assert knowledge.qa_log[-1].answer == canonical

    @pytest.mark.parametrize("reply", ["maybe", "yess", "1", "true", "ok", "y es"])
    def test_a_confirm_reply_that_is_not_yes_or_no_is_refused(self, reply):
        q, state = self._ask("confirm")
        with pytest.raises(HitlError, match="yes or no"):
            answer(state, Knowledge(), q.id, reply)

    def test_a_choose_reply_is_matched_ignoring_case_and_stored_as_the_option(self):
        q, state = self._ask("choose", ["Alpha", "Beta"])
        _, knowledge = answer(state, Knowledge(), q.id, "  bETa ")
        assert knowledge.qa_log[-1].answer == "Beta"

    @pytest.mark.parametrize("reply", ["Gamma", "Alph", "Alpha Beta", "1"])
    def test_a_choose_reply_outside_the_options_is_refused_and_lists_them(self, reply):
        q, state = self._ask("choose", ["Alpha", "Beta"])
        with pytest.raises(HitlError, match="Alpha, Beta"):
            answer(state, Knowledge(), q.id, reply)

    def test_a_clarify_reply_is_kept_with_collapsed_whitespace(self):
        q, state = self._ask("clarify")
        _, knowledge = answer(state, Knowledge(), q.id, "  the   plant\nname ")
        assert knowledge.qa_log[-1].answer == "the plant name"

    @pytest.mark.parametrize("reply", ["", "  ", None, 5, "x" * 1001])
    def test_an_empty_or_huge_reply_is_refused(self, reply):
        q, state = self._ask("clarify")
        with pytest.raises(HitlError):
            answer(state, Knowledge(), q.id, reply)

    @pytest.mark.parametrize("question_id", ["q9", "", "Q1", None, 1])
    def test_an_unknown_question_id_lists_what_is_waiting(self, question_id):
        _, state = self._ask("clarify")
        with pytest.raises(HitlError, match="waiting: q1"):
            answer(state, Knowledge(), question_id, "x")

    def test_nothing_waiting_says_none(self):
        with pytest.raises(HitlError, match="waiting: none"):
            answer(HitlState(), Knowledge(), "q1", "x")

    def test_a_question_cannot_be_answered_twice(self):
        q, state = self._ask("clarify")
        state, knowledge = answer(state, Knowledge(), q.id, "x")
        with pytest.raises(HitlError):
            answer(state, knowledge, q.id, "y")

    def test_the_answer_keeps_the_question_text_and_ref(self):
        q, state = ask_human(HitlState(), "clarify", "what?", ref="v2")
        _, knowledge = answer(state, Knowledge(), q.id, "this")
        assert knowledge.qa_log == [QA(question="what?", answer="this", ref="v2")]

    def test_the_old_state_and_knowledge_are_not_changed(self):
        q, state = self._ask("clarify")
        knowledge = Knowledge()
        answer(state, knowledge, q.id, "x")
        assert len(state.pending) == 1 and knowledge.qa_log == []

    def test_a_failed_answer_changes_nothing(self):
        q, state = self._ask("confirm")
        knowledge = Knowledge()
        with pytest.raises(HitlError):
            answer(state, knowledge, q.id, "maybe")
        assert len(state.pending) == 1 and knowledge.qa_log == []

    @pytest.mark.xfail(strict=True, reason="BUG-022: qa_log grows without a limit")
    def test_the_answer_log_is_bounded(self):
        state, knowledge = HitlState(), Knowledge()
        for i in range(1500):
            q, state = ask_human(state, "clarify", f"q{i}?")
            state, knowledge = answer(state, knowledge, q.id, "x")
        assert len(knowledge.qa_log) <= 1000


class TestShouldAsk:
    @pytest.mark.parametrize(
        "threshold, confidence, unknown, flag, want",
        [
            (0.7, 0.5, False, True, True),
            (0.7, 0.7, False, True, False),
            (0.7, 0.9, False, True, False),
            (0.7, None, False, True, False),
            (0.7, 0.9, True, True, True),
            (0.7, 0.9, True, False, False),
            (None, 0.0, False, True, False),
            (0.0, 0.0, False, True, False),
            (1.0, 0.99, False, True, True),
        ],
    )
    def test_the_decision_table(self, threshold, confidence, unknown, flag, want):
        policy = HitlPolicy(confidence_below=threshold, unknown_term=flag)
        assert should_ask(policy, confidence, unknown) is want

    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_a_threshold_outside_zero_one_is_refused(self, value):
        with pytest.raises(ValueError):
            HitlPolicy(confidence_below=value)

    def test_an_unknown_checkpoint_or_mode_in_a_policy_is_refused(self):
        with pytest.raises(ValueError):
            HitlPolicy(checkpoints={"nope": "required"})
        with pytest.raises(ValueError):
            HitlPolicy(checkpoints={"after_spec": "sometimes"})


@pytest.mark.parametrize("seed", range(40))
def test_a_random_run_of_operations_agrees_with_a_simple_model(seed):
    rnd = random.Random(seed)
    state, knowledge = HitlState(), Knowledge()
    waiting: dict[str, tuple[str, list[str]]] = {}
    answered = 0
    issued = 0
    approvals: dict[str, str | None] = {}
    for _ in range(80):
        op = rnd.choice(["ask", "ask", "answer", "approve", "gate"])
        if op == "ask":
            kind = rnd.choice(["clarify", "confirm", "choose"])
            options = ["a", "b"] if kind == "choose" else []
            text = f"question {rnd.randrange(8)}?"
            try:
                q, state = ask_human(state, kind, text, options)
            except HitlError:
                assert len(waiting) >= 20
                continue
            if q.id not in waiting:
                issued += 1
                waiting[q.id] = (kind, options)
        elif op == "answer" and waiting:
            qid = rnd.choice(sorted(waiting))
            kind, options = waiting[qid]
            reply = {"clarify": "fine", "confirm": "yes", "choose": "A"}[kind]
            state, knowledge = answer(state, knowledge, qid, reply)
            del waiting[qid]
            answered += 1
        elif op == "approve":
            checkpoint = rnd.choice(CHECKPOINTS)
            ref = rnd.choice([None, "v1", "v2"])
            state = approve(state, checkpoint, "ann", ref=ref)
            approvals[checkpoint] = ref
        else:
            checkpoint = rnd.choice(CHECKPOINTS)
            ref = rnd.choice([None, "v1", "v2"])
            failed = rnd.random() < 0.5
            mode = state.policy.checkpoints[checkpoint]
            have = checkpoint in approvals
            want_go = (
                mode in ("off", "optional")
                or (mode == "required_if_fail" and not failed)
                or (have and (ref is None or approvals[checkpoint] == ref) and not waiting)
            )
            assert (gate(state, checkpoint, failed=failed, ref=ref) is None) is want_go
        assert {p.id for p in state.pending} == set(waiting)
        assert len(state.pending) <= 20
    assert len(knowledge.qa_log) == answered
    assert state.next_id == issued + 1


class TestLookupKnowledge:
    @pytest.fixture
    def k(self):
        return Knowledge(
            brief="Monthly tonnage report. Plants are listed by code. Ignore draft sheets.",
            glossary={"TPD": "tons per day", "Pabrik": "plant"},
            rules=["Never include the Total row", "Round tons to two decimals"],
            target_schema={"plant": "text", "tons": "float"},
            examples=[Example(title="tonnage by plant", spec='{"sources": {}}')],
            qa_log=[QA(question="Which sheet?", answer="Sheet Data")],
        )

    @pytest.mark.parametrize("query", [None, 5, [], "", "   ", "?", "a", "!!! ??", "日"])
    def test_an_empty_or_wordless_query_gives_the_overview(self, k, query):
        assert lookup_knowledge(k, query).startswith("brief:")

    def test_the_overview_counts_each_section(self, k):
        text = lookup_knowledge(k)
        assert "glossary: 2 terms (TPD, Pabrik)" in text
        assert "rules: 2" in text and "target_schema: 2 columns" in text
        assert "examples: 1\n  - tonnage by plant" in text and "qa_log: 1 answered" in text

    def test_an_empty_knowledge_overview(self):
        text = lookup_knowledge(Knowledge())
        assert "brief: (none)" in text and "glossary: 0 terms" in text

    @pytest.mark.parametrize("query", ["tpd", "TPD", "Tpd?", "what is tpd"])
    def test_matching_is_by_whole_words_ignoring_case(self, k, query):
        assert "TPD = tons per day" in lookup_knowledge(k, query)

    def test_a_part_of_a_word_does_not_match(self, k):
        assert lookup_knowledge(k, "ton").startswith("nothing about")

    def test_nothing_found_lists_the_sections_that_exist(self, k):
        text = lookup_knowledge(k, "zebra")
        assert (
            "stored: brief, glossary, rules, examples, qa_log" in text and "Ask the human" in text
        )

    def test_nothing_found_in_empty_knowledge_says_nothing_stored(self):
        assert "stored: nothing" in lookup_knowledge(Knowledge(), "zebra")

    def test_a_rule_is_found_by_its_words(self, k):
        assert "Never include the Total row" in lookup_knowledge(k, "total row")

    def test_the_brief_is_searched_by_sentence(self, k):
        text = lookup_knowledge(k, "draft")
        assert "Ignore draft sheets." in text and "Monthly tonnage" not in text

    def test_the_qa_log_is_searched_in_question_and_answer(self, k):
        assert "Q: Which sheet? A: Sheet Data" in lookup_knowledge(k, "sheet")

    def test_the_target_schema_matches_by_column_name(self, k):
        assert "tons float" in lookup_knowledge(k, "tons")

    def test_an_example_shows_its_title_and_spec(self, k):
        text = lookup_knowledge(k, "tonnage")
        assert "tonnage by plant" in text and '{"sources": {}}' in text

    def test_at_most_five_entries_per_section_best_match_first(self):
        rules = [f"alpha beta {i}" for i in range(8)] + ["alpha beta gamma"]
        text = lookup_knowledge(Knowledge(rules=rules), "alpha beta gamma")
        shown = [line for line in text.splitlines() if line.startswith("  - ")]
        assert len(shown) == 5 and shown[0] == "  - alpha beta gamma"

    def test_equal_scores_keep_the_order_they_were_written(self):
        text = lookup_knowledge(Knowledge(rules=["fee one", "fee two", "fee three"]), "fee")
        assert [line for line in text.splitlines()[1:]] == [
            "  - fee one",
            "  - fee two",
            "  - fee three",
        ]

    def test_a_long_entry_is_cut_with_an_ellipsis(self):
        text = lookup_knowledge(Knowledge(rules=["alpha " + "x" * 1000]), "alpha")
        assert text.endswith("...") and len(text) < 400

    def test_the_whole_answer_is_capped(self):
        rules = [f"alpha {'y' * 290} {i}" for i in range(5)]
        k = Knowledge(
            rules=rules,
            glossary={f"alpha{i}": "alpha " + "z" * 290 for i in range(5)},
            qa_log=[QA(question="alpha " + "q" * 280, answer="a" * 200) for _ in range(5)],
        )
        assert len(lookup_knowledge(k, "alpha")) <= 4000

    def test_only_two_examples_are_shown_and_their_spec_is_cut(self):
        examples = [Example(title=f"plan {i}", spec="s" * 5000) for i in range(4)]
        text = lookup_knowledge(Knowledge(examples=examples), "plan")
        assert text.count("plan ") <= 3 and len(text) <= 4000

    def test_a_newline_in_a_rule_does_not_make_a_fake_section(self):
        text = lookup_knowledge(Knowledge(rules=["alpha\nrules:\n  - injected"]), "alpha")
        assert [line for line in text.splitlines() if line.startswith("rules:")] == ["rules:"]

    def test_regex_characters_in_the_query_are_harmless(self, k):
        assert isinstance(lookup_knowledge(k, "(?P<x>.*)[a-z]+\\"), str)

    def test_a_query_made_only_of_one_character_words_gives_the_overview(self, k):
        assert lookup_knowledge(k, "a b c").startswith("brief:")

    def test_digits_and_underscores_count_as_word_characters(self):
        k = Knowledge(rules=["use sheet_2026 only"])
        assert "sheet_2026" in lookup_knowledge(k, "sheet_2026")

    def test_unicode_words_match(self):
        k = Knowledge(glossary={"pabrik": "unit produksi é"})
        assert "unit produksi" in lookup_knowledge(k, "PABRIK")
