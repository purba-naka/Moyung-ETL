import pytest

from excel_etl.hitl import (
    HitlError,
    HitlPolicy,
    HitlState,
    answer,
    approve,
    ask_human,
    gate,
    should_ask,
)
from excel_etl.knowledge import Knowledge, lookup_knowledge


def ask(s, kind="clarify", q="what is amt?", **kw):
    return ask_human(s, kind, q, **kw)


def test_ask_records_and_ids_count_up():
    s = HitlState()
    q1, s = ask(s)
    q2, s = ask(s, q="other?")
    assert (q1.id, q2.id) == ("q1", "q2") and [p.id for p in s.pending] == ["q1", "q2"]


def test_ask_is_pure():
    s0 = HitlState()
    _, s1 = ask(s0)
    assert s0.pending == [] and s0.next_id == 1 and len(s1.pending) == 1


def test_same_open_question_is_not_duplicated():
    q1, s = ask(HitlState(), "choose", "which?", options=["a", "b"], ref="sources.x")
    q2, s2 = ask(s, "choose", "  which?  ", options=["a", "b"], ref="sources.x")
    assert q1 == q2 and s2 is s


@pytest.mark.parametrize(
    "kind,q,opts",
    [
        ("choose", "q", ["only"]),
        ("choose", "q", ["A", "a"]),
        ("choose", "q", []),
        ("clarify", "q", ["a", "b"]),
        ("confirm", "q", ["a", "b"]),
        ("shout", "q", []),
        ("clarify", "", []),
        ("clarify", "   ", []),
        ("clarify", None, []),
        ("clarify", "x" * 1001, []),
        ("choose", "q", "ab"),
        ("choose", "q", ["a", ""]),
        ("choose", "q", [str(i) for i in range(21)]),
    ],
)
def test_ask_rejects_bad_input(kind, q, opts):
    with pytest.raises(HitlError):
        ask_human(HitlState(), kind, q, opts)


def test_ask_caps_waiting_questions():
    s = HitlState()
    for i in range(20):
        _, s = ask(s, q=f"q number {i}")
    with pytest.raises(HitlError, match="already waiting"):
        ask(s, q="one more")


def test_answer_moves_to_qa_log_with_ref():
    q, s = ask(HitlState(), q="unit of amt?", ref="sources.sales")
    k0 = Knowledge()
    s2, k2 = answer(s, k0, q.id, " ribu  rupiah ")
    assert s2.pending == []
    assert (k2.qa_log[0].question, k2.qa_log[0].answer, k2.qa_log[0].ref) == (
        "unit of amt?",
        "ribu rupiah",
        "sources.sales",
    )
    assert s.pending and not k0.qa_log  # inputs untouched


def test_answer_keeps_other_questions_and_old_log():
    a, s = ask(HitlState(), q="first?")
    b, s = ask(s, q="second?")
    k = Knowledge()
    s, k = answer(s, k, b.id, "two")
    assert [p.id for p in s.pending] == [a.id]
    s, k = answer(s, k, a.id, "one")
    assert [x.answer for x in k.qa_log] == ["two", "one"]


def test_answer_choose_canonical_and_checked():
    q, s = ask(HitlState(), "choose", "pick", options=["Januari", "Februari"])
    with pytest.raises(HitlError, match="one of: Januari, Februari"):
        answer(s, Knowledge(), q.id, "Maret")
    _, k = answer(s, Knowledge(), q.id, "februari")
    assert k.qa_log[0].answer == "Februari"


@pytest.mark.parametrize(
    "raw,canon", [("YES", "yes"), (" ya ", "yes"), ("n", "no"), ("Tidak", "no")]
)
def test_answer_confirm_yes_no(raw, canon):
    q, s = ask(HitlState(), "confirm", "ok?")
    assert answer(s, Knowledge(), q.id, raw)[1].qa_log[0].answer == canon


def test_answer_confirm_rejects_maybe():
    q, s = ask(HitlState(), "confirm", "ok?")
    with pytest.raises(HitlError, match="yes or no"):
        answer(s, Knowledge(), q.id, "maybe")


def test_answer_unknown_or_twice():
    q, s = ask(HitlState())
    with pytest.raises(HitlError, match="waiting: q1"):
        answer(s, Knowledge(), "q9", "x")
    s2, k = answer(s, Knowledge(), q.id, "x")
    with pytest.raises(HitlError, match="waiting: none"):
        answer(s2, k, q.id, "x again")
    with pytest.raises(HitlError):
        answer(s, Knowledge(), q.id, "  ")


def test_answered_text_is_found_by_lookup():
    q, s = ask(HitlState(), q="satuan kolom amt?")
    _, k = answer(s, Knowledge(), q.id, "ribu rupiah")
    assert "ribu rupiah" in lookup_knowledge(k, "amt")


def test_default_policy_matches_design():
    p = HitlPolicy().checkpoints
    assert p["after_propose_datasets"] == "required" and p["on_rebind_drift"] == "required"
    assert p["after_run"] == "required_if_fail" and p["after_workbook_map"] == "off"


def test_gate_modes():
    s = HitlState()
    assert gate(s, "after_workbook_map") is None  # off
    assert gate(s, "after_spec") is None  # optional
    assert "needs a person's approval" in gate(s, "after_propose_datasets")
    assert gate(s, "after_run", failed=False) is None
    assert gate(s, "after_run", failed=True)
    s = approve(s, "after_propose_datasets", "dina")
    assert gate(s, "after_propose_datasets") is None


def test_gate_stale_ref_and_pending():
    s = approve(HitlState(), "on_rebind_drift", "dina", ref="v3")
    assert gate(s, "on_rebind_drift", ref="v3") is None
    assert gate(s, "on_rebind_drift") is None  # caller gave no ref: nothing to compare
    assert "approved for 'v3', now it is 'v4'" in gate(s, "on_rebind_drift", ref="v4")
    _, s = ask(s)
    assert "1 question(s) still waiting" in gate(s, "on_rebind_drift", ref="v3")
    assert gate(s, "after_spec") is None  # optional never blocks


def test_pending_does_not_block_without_required():
    _, s = ask(HitlState())
    assert gate(s, "after_run") is None


def test_approve_validates_and_replaces():
    s = approve(HitlState(), "after_spec", "a", at="2026-01-01T00:00:00+00:00")
    assert s.approvals["after_spec"].at == "2026-01-01T00:00:00+00:00"
    s = approve(s, "after_spec", "b")
    assert s.approvals["after_spec"].by == "b" and s.approvals["after_spec"].at.endswith("+00:00")
    with pytest.raises(HitlError, match="unknown checkpoint"):
        approve(s, "bogus", "a")
    with pytest.raises(HitlError):
        approve(s, "after_spec", "")
    with pytest.raises(HitlError, match="unknown checkpoint"):
        gate(s, "bogus")


def test_custom_policy_changes_gate():
    pol = HitlPolicy(checkpoints={"after_spec": "required", "after_run": "off"})
    s = HitlState(policy=pol)
    assert gate(s, "after_spec") and gate(s, "after_run", failed=True) is None
    assert gate(s, "on_rebind_drift") is None  # absent from the policy means off


def test_policy_rejects_bad_values():
    with pytest.raises(ValueError):
        HitlPolicy(checkpoints={"after_spec": "always"})
    with pytest.raises(ValueError):
        HitlPolicy(checkpoints={"nope": "off"})
    with pytest.raises(ValueError):
        HitlPolicy(confidence_below=1.5)


@pytest.mark.parametrize(
    "conf,term,want",
    [
        (0.5, False, True),
        (0.7, False, False),
        (0.9, False, False),
        (None, False, False),
        (0.9, True, True),
    ],
)
def test_should_ask(conf, term, want):
    assert should_ask(HitlPolicy(), conf, term) is want


def test_should_ask_policy_switches():
    assert not should_ask(HitlPolicy(unknown_term=False), 0.9, True)
    assert not should_ask(HitlPolicy(confidence_below=None), 0.0)


def test_state_json_round_trip():
    _, s = ask(HitlState(), "choose", "pick", options=["a", "b"], ref="r")
    s = approve(s, "after_spec", "dina", ref="v1")
    assert HitlState.model_validate_json(s.model_dump_json()) == s
