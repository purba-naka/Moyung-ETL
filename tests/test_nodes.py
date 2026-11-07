from __future__ import annotations

import duckdb
import pytest
from pydantic import Field

from excel_etl.nodes import Column, Node, NodeError, build, infer, names, node, quote, schema_of
from excel_etl.nodes.base import NodeConfig, Relation, Schema


def _rel() -> Relation:
    con = duckdb.connect()
    return con.sql("select 1 as a, 'x' as \"JAN|PKG\", 2.5 as c")


def test_schema_of_reads_names_and_types() -> None:
    assert schema_of(_rel()) == (
        Column("a", "INTEGER"),
        Column("JAN|PKG", "VARCHAR"),
        Column("c", "DECIMAL(2,1)"),
    )


def test_quote_survives_odd_headers() -> None:
    con = duckdb.connect()
    for name in ["JAN|PKG", 'a "b"', "x y", "it's", "é"]:
        rel = con.sql(f"select 1 as {quote(name)}")
        assert rel.columns == [name]
        assert rel.project(quote(name)).fetchall() == [(1,)]


def test_registry_lists_and_rejects_unknown() -> None:
    assert "select" in names()
    with pytest.raises(NodeError, match="unknown node 'nope'; nodes: .*select"):
        build("nope")


def test_duplicate_name_is_refused() -> None:
    with pytest.raises(ValueError, match="already registered"):

        @node("select")
        class Again(Node):
            pass


def test_config_errors_say_where() -> None:
    with pytest.raises(NodeError) as e:
        build("select", {"columns": [], "colums": ["a"]})
    msg = str(e.value)
    assert "node 'select' config:" in msg
    assert "columns:" in msg and "colums:" in msg  # empty list and the misspelled key


def test_missing_config_is_an_error() -> None:
    with pytest.raises(NodeError, match="columns"):
        build("select")
    with pytest.raises(NodeError, match="columns"):
        build("select", None)


def test_arity_is_checked() -> None:
    n = build("select", {"columns": ["a"]})
    with pytest.raises(NodeError, match="takes 1 input"):
        infer(n, [])
    with pytest.raises(NodeError, match="takes 1 input"):
        infer(n, [(), ()])


def test_select_infer_matches_run() -> None:
    rel = _rel()
    n = build("select", {"columns": ["c", "JAN|PKG"]})
    inferred = infer(n, [schema_of(rel)])
    out = n.run([rel])
    assert inferred == schema_of(out) == (Column("c", "DECIMAL(2,1)"), Column("JAN|PKG", "VARCHAR"))
    assert out.fetchall() == [(2.5, "x")]


def test_select_names_a_missing_column() -> None:
    n = build("select", {"columns": ["a", "zzz"]})
    with pytest.raises(NodeError, match="no column 'zzz'; columns: a, JAN\\|PKG, c"):
        infer(n, [schema_of(_rel())])


def test_a_new_node_registers_and_runs() -> None:
    @node("test_double")
    class Double(Node):
        class Config(NodeConfig):
            column: str
            times: int = Field(default=2, ge=1)

        def infer_schema(self, inputs: list[Schema]) -> Schema:
            return inputs[0]

        def run(self, inputs: list[Relation]) -> Relation:
            c = quote(self.config.column)
            return inputs[0].project(f"{c} * {self.config.times} as {c}")

    n = build("test_double", {"column": "a", "times": 3})
    assert n.name == "test_double"
    assert n.run([duckdb.connect().sql("select 2 as a")]).fetchall() == [(6,)]


# --- catalog: what an agent reads to write a step ---------------------------------------------


def test_catalog_lists_every_node_with_a_description():
    from excel_etl.nodes import catalog, names

    lines = catalog().splitlines()
    assert [ln.split(":", 1)[0] for ln in lines] == names()
    assert all(ln.split(":", 1)[1].strip() for ln in lines)  # none is blank


def test_catalog_each_node_has_fields_and_required_marks():
    from excel_etl.nodes import catalog, names

    for n in names():
        text = catalog(n)
        assert text.startswith(f"{n} (inputs: ") and "\nconfig: " in text
    gb = catalog("group_by")
    assert "aggs*: [{name*, fn*, column, separator}]" in gb and "by: [string] = []" in gb
    assert "inputs: 2)" in catalog("join") and "how: inner|left|right|full = 'inner'" in catalog(
        "join"
    )
    assert "inputs: 2+)" in catalog("union")


def test_catalog_unknown_node_lists_known_ones():
    import pytest as _pytest

    from excel_etl.nodes import NodeError, catalog

    with _pytest.raises(NodeError, match="unknown node 'merge'; nodes: cast, dedupe"):
        catalog("merge")


def test_config_error_names_the_valid_fields():
    import pytest as _pytest

    from excel_etl.nodes import NodeError, build

    with _pytest.raises(NodeError) as e:
        build("group_by", {"aggs": [{"fn": "sum", "column": "Q", "as": "T"}]})
    assert "aggs.0.name: Field required" in str(e.value)
    assert (
        "fields (* required): by: [string] = []; aggs*: [{name*, fn*, column, separator}]"
        in str(e.value)
    )
