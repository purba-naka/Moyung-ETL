"""What the human told us about the job: a brief, terms, rules, past specs, answered questions.

The agent does not get all of it in its prompt; it asks `lookup_knowledge` and gets the few
entries that share words with its question. Matching is by whole words, letter case ignored,
which is enough for short notes written by people.

ponytail: no stemming or fuzzy match (`tonase` does not find `ton`). Upgrade: embeddings
behind the same function if notes grow past a few hundred entries.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_WORD = re.compile(r"\w{2,}")
_PER_SECTION = 5
_ENTRY_CHARS = 300
_SPEC_CHARS = 1500
_TOTAL_CHARS = 4000


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QA(_Model):
    question: str
    answer: str
    ref: str | None = None  # what the question was about: "sources.sales", "steps.clean", ...


class Example(_Model):
    title: str
    spec: str  # a PipelineSpec as JSON text


class Knowledge(_Model):
    brief: str = ""
    glossary: dict[str, str] = {}
    rules: list[str] = []
    target_schema: dict[str, str] | None = None  # column name -> duckdb type, when the host has one
    examples: list[Example] = []
    qa_log: list[QA] = Field(default_factory=list)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _cut(text: str, limit: int = _ENTRY_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _top(want: set[str], entries: list[tuple[str, str]]) -> list[str]:
    """Texts of the entries sharing most words with `want` (key is searched, text is shown)."""
    scored = [(len(want & _words(key)), i, text) for i, (key, text) in enumerate(entries)]
    scored = [s for s in scored if s[0]]
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [text for _, _, text in scored[:_PER_SECTION]]


def _overview(k: Knowledge) -> str:
    lines = [
        f"brief: {_cut(k.brief) if k.brief.strip() else '(none)'}",
        f"glossary: {len(k.glossary)} terms"
        + (f" ({', '.join(list(k.glossary)[:10])})" if k.glossary else ""),
        f"rules: {len(k.rules)}",
        f"target_schema: {len(k.target_schema or {})} columns",
        f"examples: {len(k.examples)}" + "".join(f"\n  - {e.title}" for e in k.examples[:10]),
        f"qa_log: {len(k.qa_log)} answered",
        "Pass a question or keywords to lookup_knowledge to read the matching entries.",
    ]
    return "\n".join(lines)


def lookup_knowledge(k: Knowledge, query: str = "") -> str:
    """The entries of `k` that share words with `query`, as text for the agent.

    An empty query gives an overview of what is stored. Nothing found says so and lists the
    sections that exist, so the agent can ask the human instead of guessing.
    """
    if not isinstance(query, str) or not _words(query):
        return _overview(k)
    want = _words(query)
    sections: list[tuple[str, list[str]]] = [
        (
            "brief",
            _top(want, [(s, s) for s in re.split(r"(?<=[.!?])\s+|\n+", k.brief) if s.strip()]),
        ),
        ("glossary", _top(want, [(f"{t} {d}", f"{t} = {d}") for t, d in k.glossary.items()])),
        ("rules", _top(want, [(r, r) for r in k.rules])),
        (
            "qa_log",
            _top(
                want,
                [(f"{q.question} {q.answer}", f"Q: {q.question} A: {q.answer}") for q in k.qa_log],
            ),
        ),
    ]
    if k.target_schema:
        hit = [f"{n} {t}" for n, t in k.target_schema.items() if want & _words(n)]
        sections.append(("target_schema", hit[: _PER_SECTION * 2]))
    ex = _top(want, [(e.title, f"{e.title}\n{e.spec[:_SPEC_CHARS]}") for e in k.examples])
    sections.append(("examples", ex[:2]))

    out = [
        f"{name}:\n"
        + "\n".join(
            f"  - {_cut(t, _SPEC_CHARS if name == 'examples' else _ENTRY_CHARS)}" for t in found
        )
        for name, found in sections
        if found
    ]
    if not out:
        have = [
            n
            for n, v in (
                ("brief", k.brief.strip()),
                ("glossary", k.glossary),
                ("rules", k.rules),
                ("examples", k.examples),
                ("qa_log", k.qa_log),
            )
            if v
        ]
        stored = ", ".join(have) or "nothing"
        return f"nothing about {query!r} in the notes (stored: {stored}). Ask the human."
    text = "\n".join(out)
    return text if len(text) <= _TOTAL_CHARS else text[: _TOTAL_CHARS - 3] + "..."
