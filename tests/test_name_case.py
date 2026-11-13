"""duckdb treats `Item` and `ITEM` as one name and quietly renames the second to `ITEM_1`.
A node that would make two such columns must say so when the spec is checked, instead of the
run failing later with "promised ... but produced", which reads like a library bug."""

from __future__ import annotations

import pytest

from excel_etl.nodes import Column, NodeError, build, infer

SCHEMA = (
    Column("Item", "VARCHAR"),
    Column("Qty", "BIGINT"),
    Column("Code", "VARCHAR"),
    Column("Jan|Ton", "BIGINT"),
    Column("Feb|Ton", "BIGINT"),
)
PATTERN = "^(?P<group>.+)[|](?P<measure>[^|]+)$"


@pytest.mark.parametrize(
    ("node", "config"),
    [
        ("derive", {"name": "ITEM", "expr": "upper(Item)"}),
        ("window", {"name": "QTY", "fn": "row_number"}),
        ("split_header_token", {"column": "Code", "into": ["a", "A"], "separator": "."}),
        ("parse_hierarchy", {"column": "Code", "level_column": "x", "parent_column": "X"}),
        ("unpivot", {"keep": ["Item"], "columns": ["Qty"], "name_column": "ITEM"}),
        (
            "unpivot",
            {"keep": ["Item"], "columns": ["Qty"], "name_column": "v", "value_column": "V"},
        ),
        ("unpivot_groups", {"pattern": PATTERN, "keep": ["Item"], "group_column": "ITEM"}),
        ("unpivot_groups", {"pattern": PATTERN, "keep": ["Item"], "group_column": "ton"}),
        ("pivot", {"by": ["Item"], "column": "Code", "value": "Qty", "values": ["x", "X"]}),
        ("pivot", {"by": ["Item"], "column": "Code", "value": "Qty", "values": ["item"]}),
    ],
)
def test_two_names_that_differ_only_in_letter_case_are_refused(node, config):
    with pytest.raises(NodeError, match="letter case"):
        infer(build(node, config), [SCHEMA])


def test_derive_and_window_with_the_exact_name_still_replace_in_place():
    out = infer(build("derive", {"name": "Item", "expr": "upper(Item)"}), [SCHEMA])
    assert [c.name for c in out] == [c.name for c in SCHEMA]
    out = infer(build("window", {"name": "Qty", "fn": "row_number"}), [SCHEMA])
    assert [c.name for c in out] == [c.name for c in SCHEMA]
