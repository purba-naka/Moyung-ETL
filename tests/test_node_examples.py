"""The examples in `node_catalog` are shown to an agent, so every one must really work."""

from __future__ import annotations

import json

import pytest

from excel_etl.nodes import build, catalog, infer, names, quote, schema_of
from excel_etl.nodes.base import _REGISTRY, connect

# One table that has every column name the examples use, with NULLs and awkward text.
_TABLE = """select * from (values
    (1, 'A-12', 'PT Maju', 120.0, 2.5, date '2026-01-15', '15/01/2026', 'pen', 'north', 'inv 42'),
    (2, 'B-7',  'CV Jaya', 0.0,   NULL, date '2026-02-10', '10/02/2026', 'pen', 'north', 'none'),
    (3, 'A-3',  NULL,      NULL,  1.0,  date '2027-03-01', '31/02/2026', 'ink', 'south', 'x 7 y 8'),
    (4, ' a-9 ', 'PT Baru', 5.0,  0.0,  NULL,               NULL,         'ink', 'south', NULL)
) v(id, code, text, qty, price, day, date_text, product, area, note)"""

_SHIPPED = [n for n in names() if not n.startswith("test_")]
_CASES = [(n, i) for n in _SHIPPED for i in range(len(_REGISTRY[n].examples))]


def test_filter_derive_and_window_have_examples() -> None:
    for n in ("filter", "derive", "window"):
        assert len(_REGISTRY[n].examples) >= 5, n


@pytest.mark.parametrize(("name", "i"), _CASES, ids=[f"{n}-{i}" for n, i in _CASES])
def test_every_example_builds_runs_and_matches_its_schema(name: str, i: int) -> None:
    config, note = _REGISTRY[name].examples[i]
    assert note.strip()
    rel = connect().sql(_TABLE)
    n = build(name, config)
    out = n.run([rel])
    rows = out.fetchall()  # runs for real, not only binds
    assert infer(n, [schema_of(rel)]) == schema_of(out)
    if name == "window":
        assert len(rows) == 4  # a window step never drops or adds a row


def test_the_catalog_prints_each_example_as_json_with_its_note() -> None:
    for name in _SHIPPED:
        text = catalog(name)
        for config, note in _REGISTRY[name].examples:
            assert f"  {json.dumps(config, ensure_ascii=False)}  # {note}" in text
        assert ("examples:" in text) == bool(_REGISTRY[name].examples)


def test_a_node_without_examples_prints_no_empty_examples_heading() -> None:
    assert "examples:" not in catalog("select")


def test_filter_examples_keep_the_rows_they_say() -> None:
    def keep(where: str) -> list[int]:
        out = build("filter", {"where": where}).run([connect().sql(_TABLE)])
        return [r[0] for r in out.fetchall()]

    by_note = {note: cfg["where"] for cfg, note in _REGISTRY["filter"].examples}
    assert keep(by_note["numbers; a NULL comparison drops the row"]) == [1, 4]
    assert keep(by_note["text starts with (ILIKE ignores letter case)"]) == [1, 3]
    assert keep(by_note["one of several values"]) == [1, 2]
    assert keep(by_note["text matches a regular expression"]) == [1, 4]
    assert keep(by_note["a date range (the end day itself is left out)"]) == [1, 2]
    assert keep(by_note["by part of a date"]) == [1, 2]


def test_derive_examples_give_the_values_they_say() -> None:
    def make(note: str) -> list:
        cfg = next(c for c, n in _REGISTRY["derive"].examples if n.startswith(note))
        out = build("derive", cfg).run([connect().sql(_TABLE)])
        rows = out.order("id").project(quote(cfg["name"])).fetchall()
        return [float(v) if hasattr(v, "as_tuple") else v for (v,) in rows]

    assert make("NULL counted") == [300.0, None, 0.0, 0.0]
    assert make("nullif") == [48.0, None, None, None]
    assert make("text before") == ["A", "B", "A", " a"]
    assert make("trim spaces") == ["A-12", "B-7", "A-3", "A-9"]
    assert make("first number") == [42, None, 7, None]
    assert [d and d.isoformat() for d in make("text in another")] == [
        "2026-01-15", "2026-02-10", None, None,
    ]  # fmt: skip
    assert [d and d.isoformat() for d in make("first day")] == [
        "2026-01-01", "2026-02-01", "2027-03-01", None,
    ]  # fmt: skip
    assert make("date as text") == ["2026-01", "2026-02", "2027-03", None]
    assert make("days between") == [350, 324, -60, None]
    assert make("pick a label") == ["big", "none", "none", "small"]
