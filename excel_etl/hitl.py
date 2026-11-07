"""Human in the loop: questions the agent cannot answer itself, and approvals before a run.

Nothing here waits. `ask_human` records a question and returns; the host shows it to the
user and later calls `answer`. Every function takes a state and returns a new one, so the
host stores the state wherever it likes (see `state.py`).

A checkpoint is a point in the work where a person may have to say "go". The policy sets, per
checkpoint, whether that is needed. `gate` tells the caller whether it may go on.

ponytail: an approval is checked only by its `ref` text (e.g. a spec version number). Upgrade:
hash the spec itself if hosts forget to pass a ref.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from excel_etl.knowledge import QA, Knowledge

type Checkpoint = Literal[
    "after_workbook_map",
    "after_propose_datasets",
    "after_spec",
    "after_run",
    "on_rebind_drift",
]
type Mode = Literal["off", "optional", "required", "required_if_fail"]
type Kind = Literal["clarify", "confirm", "choose"]

CHECKPOINTS: tuple[str, ...] = (
    "after_workbook_map",
    "after_propose_datasets",
    "after_spec",
    "after_run",
    "on_rebind_drift",
)
_MAX_PENDING = 20
_MAX_TEXT = 1000
_MAX_OPTIONS = 20
_CONFIRM = {"yes": "yes", "y": "yes", "ya": "yes", "no": "no", "n": "no", "tidak": "no"}


class HitlError(ValueError):
    """A call that cannot be done (unknown question, answer not among the options, ...)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HitlPolicy(_Model):
    checkpoints: dict[Checkpoint, Mode] = Field(
        default_factory=lambda: {
            "after_workbook_map": "off",
            "after_propose_datasets": "required",
            "after_spec": "optional",
            "after_run": "required_if_fail",
            "on_rebind_drift": "required",
        }
    )
    confidence_below: float | None = Field(default=0.7, ge=0, le=1)
    unknown_term: bool = True


class PendingQuestion(_Model):
    id: str
    kind: Kind
    question: str
    options: list[str] = []
    ref: str | None = None  # what it is about: "sources.sales", "steps.clean", ...


class Approval(_Model):
    by: str
    at: str
    ref: str | None = None


class HitlState(_Model):
    policy: HitlPolicy = Field(default_factory=HitlPolicy)
    pending: list[PendingQuestion] = []
    approvals: dict[Checkpoint, Approval] = {}
    next_id: int = 1


def should_ask(
    policy: HitlPolicy, confidence: float | None = None, unknown_term: bool = False
) -> bool:
    """Whether the policy wants a person asked: the agent is unsure or met an unknown term."""
    if unknown_term and policy.unknown_term:
        return True
    return (
        confidence is not None
        and policy.confidence_below is not None
        and confidence < policy.confidence_below
    )


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HitlError(f"{what} must be non-empty text")
    value = " ".join(value.split())
    if len(value) > _MAX_TEXT:
        raise HitlError(f"{what} is {len(value)} characters; keep it under {_MAX_TEXT}")
    return value


def ask_human(
    state: HitlState,
    kind: Kind,
    question: str,
    options: list[str] | tuple[str, ...] = (),
    ref: str | None = None,
) -> tuple[PendingQuestion, HitlState]:
    """Record a question for the person. `choose` needs 2+ options; `confirm` is yes/no and
    needs none; `clarify` is free text. The same open question asked twice returns the first.
    """
    if kind not in ("clarify", "confirm", "choose"):
        raise HitlError(f"kind must be clarify, confirm or choose, got {kind!r}")
    q = _text(question, "question")
    if isinstance(options, str) or not isinstance(options, (list, tuple)):
        raise HitlError("options must be a list of texts")
    opts = [_text(o, "option") for o in options]
    if kind == "choose" and len(opts) < 2:
        raise HitlError("a 'choose' question needs at least 2 options")
    if kind == "choose" and len({o.casefold() for o in opts}) != len(opts):
        raise HitlError("options must differ from each other")
    if kind != "choose" and opts:
        raise HitlError(f"a {kind!r} question takes no options")
    if len(opts) > _MAX_OPTIONS:
        raise HitlError(f"{len(opts)} options; at most {_MAX_OPTIONS}")
    if ref is not None:
        ref = _text(ref, "ref")
    for p in state.pending:
        if (p.kind, p.question, p.options, p.ref) == (kind, q, opts, ref):
            return p, state
    if len(state.pending) >= _MAX_PENDING:
        raise HitlError(f"{_MAX_PENDING} questions are already waiting; get them answered first")
    new = PendingQuestion(id=f"q{state.next_id}", kind=kind, question=q, options=opts, ref=ref)
    return new, state.model_copy(
        update={"pending": [*state.pending, new], "next_id": state.next_id + 1}
    )


def answer(
    state: HitlState, knowledge: Knowledge, question_id: str, reply: str
) -> tuple[HitlState, Knowledge]:
    """Give the person's reply to a waiting question. It leaves `pending` and goes to
    `knowledge.qa_log`. A `choose` reply must be one of the options (letter case ignored) and
    a `confirm` reply yes or no; what is stored is the canonical form.
    """
    q = next((p for p in state.pending if p.id == question_id), None)
    if q is None:
        waiting = ", ".join(p.id for p in state.pending) or "none"
        raise HitlError(f"no waiting question {question_id!r} (waiting: {waiting})")
    text = _text(reply, "answer")
    if q.kind == "confirm":
        if text.casefold() not in _CONFIRM:
            raise HitlError("answer yes or no")
        text = _CONFIRM[text.casefold()]
    elif q.kind == "choose":
        hit = [o for o in q.options if o.casefold() == text.casefold()]
        if not hit:
            raise HitlError(f"answer must be one of: {', '.join(q.options)}")
        text = hit[0]
    entry = QA(question=q.question, answer=text, ref=q.ref)
    return (
        state.model_copy(update={"pending": [p for p in state.pending if p.id != q.id]}),
        knowledge.model_copy(update={"qa_log": [*knowledge.qa_log, entry]}),
    )


def approve(
    state: HitlState, checkpoint: Checkpoint, by: str, ref: str | None = None, at: str | None = None
) -> HitlState:
    """Record that a person said go at `checkpoint`. `ref` says what they approved (a spec
    version, say); `gate` called with another `ref` treats the approval as stale.
    """
    if checkpoint not in CHECKPOINTS:
        raise HitlError(f"unknown checkpoint {checkpoint!r}; use one of: {', '.join(CHECKPOINTS)}")
    who = _text(by, "by")
    stamp = at or datetime.now(UTC).isoformat(timespec="seconds")
    ok = Approval(by=who, at=stamp, ref=None if ref is None else _text(ref, "ref"))
    return state.model_copy(update={"approvals": {**state.approvals, checkpoint: ok}})


def gate(
    state: HitlState, checkpoint: Checkpoint, failed: bool = False, ref: str | None = None
) -> str | None:
    """None when the caller may go on, else the reason it may not (text for the agent).

    `required` needs an approval; `required_if_fail` only when `failed`; `off` and `optional`
    never block. An approval made for another `ref` does not count. Questions still waiting
    block a `required` checkpoint too: a person has things left to say.
    """
    if checkpoint not in CHECKPOINTS:
        raise HitlError(f"unknown checkpoint {checkpoint!r}; use one of: {', '.join(CHECKPOINTS)}")
    mode = state.policy.checkpoints.get(checkpoint, "off")
    if mode in ("off", "optional") or (mode == "required_if_fail" and not failed):
        return None
    ok = state.approvals.get(checkpoint)
    if ok is None:
        why = f"{checkpoint} needs a person's approval; none given yet"
    elif ref is not None and ok.ref != ref:
        given = "no version" if ok.ref is None else repr(ok.ref)
        why = f"{checkpoint} was approved for {given}, but this is {ref!r}; ask again"
    elif state.pending:
        ids = ", ".join(p.id for p in state.pending[:5])
        why = f"{len(state.pending)} question(s) still waiting for the person ({ids})"
    else:
        return None
    return why
