from __future__ import annotations

import pytest
from conftest import make_xlsx

from excel_etl.nodes import NodeError, build, infer, schema_of
from excel_etl.nodes.base import connect
from excel_etl.nodes.source import read_region
from excel_etl.pipeline import Source
from excel_etl.workbook import Workbook


def _rel(sql: str):
    return connect().sql(sql)


def _run(name: str, config: dict, rel):
    n = build(name, config)
    out = n.run([rel])
    assert infer(n, [schema_of(rel)]) == schema_of(out)  # the contract, on every case
    return out


def _codes(*codes: str | None):
    rows = ", ".join(f"({i}, {'NULL' if c is None else repr(c)})" for i, c in enumerate(codes))
    return _rel(f"select * from (values {rows}) v(i, code) order by i")


# --- parse_hierarchy -------------------------------------------------------------------------


def test_hierarchy_levels_and_parents() -> None:
    out = _run("parse_hierarchy", {"column": "code"}, _codes("1", "1.1", "1.2", "1.2.1", "2"))
    assert out.columns == ["i", "code", "level", "parent"]
    assert out.fetchall() == [
        (0, "1", 1, None),
        (1, "1.1", 2, "1"),
        (2, "1.2", 2, "1"),
        (3, "1.2.1", 3, "1.2"),
        (4, "2", 1, None),
    ]


@pytest.mark.parametrize(
    ("sep", "codes", "parents"),
    [
        ("-", ["A-3-b", "A-3", "A"], ["A-3", "A", None]),
        (" > ", ["Asia > Japan > Tokyo", "Asia"], ["Asia > Japan", None]),
        ("/", ["a/b/c/d"], ["a/b/c"]),
    ],
)
def test_hierarchy_any_separator(sep: str, codes: list[str], parents: list[str | None]) -> None:
    out = _run("parse_hierarchy", {"column": "code", "separator": sep}, _codes(*codes))
    assert [r[3] for r in out.fetchall()] == parents


def test_hierarchy_bad_codes_are_null_not_guessed() -> None:
    out = _run(
        "parse_hierarchy", {"column": "code"}, _codes("1..2", "1.2.", ".1", "", "  1.2  ", None)
    )
    got = [(r[2], r[3]) for r in out.fetchall()]
    assert got == [(None, None)] * 4 + [(2, "1"), (None, None)]  # trimmed code works


def test_hierarchy_whole_numbers_work_and_decimals_are_refused() -> None:
    ints = _rel("select * from (values (1), (2)) v(code)")
    assert _run("parse_hierarchy", {"column": "code"}, ints).fetchall() == [
        (1, 1, None),
        (2, 1, None),
    ]
    decimals = _rel("select * from (values (1.1), (1.2)) v(code)")
    with pytest.raises(NodeError, match="1.10 becomes 1.1"):
        infer(build("parse_hierarchy", {"column": "code"}), [schema_of(decimals)])


def test_hierarchy_names_and_clashes() -> None:
    out = _run(
        "parse_hierarchy",
        {"column": "code", "level_column": "depth", "parent_column": "up"},
        _codes("1.1"),
    )
    assert out.columns == ["i", "code", "depth", "up"]
    with pytest.raises(NodeError, match="'level' already exists"):
        infer(
            build("parse_hierarchy", {"column": "code"}),
            [schema_of(_rel("select 'a' as code, 1 as level"))],
        )
    with pytest.raises(NodeError, match="no column 'zzz'"):
        infer(build("parse_hierarchy", {"column": "zzz"}), [schema_of(_codes("1"))])


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"column": "c", "separator": ""},
        {"column": "c", "level_column": "x", "parent_column": "x"},
    ],
)
def test_hierarchy_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("parse_hierarchy", config)


# --- split_header_token ----------------------------------------------------------------------


def _tokens(*vals: str | None):
    rows = ", ".join(f"({i}, {'NULL' if v is None else repr(v)})" for i, v in enumerate(vals))
    return _rel(f"select * from (values {rows}) v(i, name) order by i")


def test_split_by_separator() -> None:
    out = _run(
        "split_header_token",
        {"column": "name", "into": ["month", "unit"], "separator": "|"},
        _tokens("JAN|Ton", " FEB | Kg ", "MAR", "APR|", None),
    )
    assert out.columns == ["i", "name", "month", "unit"]
    assert [r[2:] for r in out.fetchall()] == [
        ("JAN", "Ton"),
        ("FEB", "Kg"),
        ("MAR", None),  # fewer parts: the rest is NULL
        ("APR", None),  # empty part is NULL
        (None, None),
    ]


def test_split_last_column_keeps_the_rest() -> None:
    out = _run(
        "split_header_token",
        {"column": "name", "into": ["a", "b"], "separator": "|"},
        _tokens("x|y|z"),
    )
    assert out.fetchall() == [(0, "x|y|z", "x", "y|z")]


def test_split_three_parts_and_multichar_separator() -> None:
    out = _run(
        "split_header_token",
        {"column": "name", "into": ["a", "b", "c"], "separator": " :: "},
        _tokens("1 :: 2 :: 3", "1 :: 2"),
    )
    assert [r[2:] for r in out.fetchall()] == [("1", "2", "3"), ("1", "2", None)]


def test_split_by_pattern_and_no_match() -> None:
    out = _run(
        "split_header_token",
        {"column": "name", "into": ["q", "year"], "pattern": r"^(Q[1-4])\s+(\d{4})$"},
        _tokens("Q1 2026", "Q4  2025", "total", None),
    )
    assert [r[2:] for r in out.fetchall()] == [
        ("Q1", "2026"),
        ("Q4", "2025"),
        (None, None),
        (None, None),
    ]


def test_split_drop_source_and_numeric_column() -> None:
    out = _run(
        "split_header_token",
        {"column": "name", "into": ["a", "b"], "separator": ".", "drop_source": True},
        _rel("select * from (values (1, 12.5)) v(i, name)"),
    )
    assert out.columns == ["i", "a", "b"]
    assert out.fetchall() == [(1, "12", "5")]


def test_split_odd_names_and_quotes() -> None:
    out = _run(
        "split_header_token",
        {"column": "it's", "into": ['a"b', "c d"], "separator": "'"},
        _rel("""select 'x''y' as "it's" """),
    )
    assert out.columns == ["it's", 'a"b', "c d"]
    assert out.fetchall() == [("x'y", "x", "y")]


@pytest.mark.parametrize(
    "config",
    [
        {"column": "c", "into": ["a"], "separator": "|"},
        {"column": "c", "into": ["a", "a"], "separator": "|"},
        {"column": "c", "into": ["a", "b"]},
        {"column": "c", "into": ["a", "b"], "separator": "|", "pattern": "(.)(.)"},
        {"column": "c", "into": ["a", "b"], "pattern": "(["},
        {"column": "c", "into": ["a", "b"], "pattern": "(.)"},
        {"column": "c", "into": ["a", "b"], "separator": ""},
    ],
)
def test_split_config_errors(config: dict) -> None:
    with pytest.raises(NodeError):
        build("split_header_token", config)


def test_split_schema_errors() -> None:
    schema = schema_of(_tokens("a"))
    with pytest.raises(NodeError, match="no column 'zzz'"):
        infer(
            build("split_header_token", {"column": "zzz", "into": ["a", "b"], "separator": "|"}),
            [schema],
        )
    with pytest.raises(NodeError, match="'i' already exists"):
        infer(
            build("split_header_token", {"column": "name", "into": ["i", "b"], "separator": "|"}),
            [schema],
        )
    ok = {"column": "name", "into": ["name", "b"], "separator": "|", "drop_source": True}
    assert [c.name for c in infer(build("split_header_token", ok), [schema])] == ["i", "name", "b"]


# --- the two together, on a shape that is not any particular workbook -------------------------


def test_workbook_unpivot_then_split_header() -> None:
    rows = [
        ["Item", "2025|Units", "2025|Revenue", "2026|Units", "2026|Revenue"],
        ["Pen", 10, 5.5, 12, 6.5],
        ["Ink", 3, 9.0, None, None],
    ]
    wb = Workbook.open(make_xlsx({"S": rows}))
    src = Source.model_validate(
        {"file_role": "f", "sheet": {"name": "S"}, "region": {"header_anchor": ["Item"]}}
    )
    rel = read_region(wb, src, connect(), "raw")
    rel = _run("unpivot", {"keep": ["Item"], "pattern": "[|]", "name_column": "col"}, rel)
    rel = _run(
        "split_header_token",
        {"column": "col", "into": ["year", "measure"], "separator": "|", "drop_source": True},
        rel,
    )
    assert rel.columns == ["Item", "value", "year", "measure"]
    assert sorted(rel.fetchall()) == [
        ("Ink", 3.0, "2025", "Units"),
        ("Ink", 9.0, "2025", "Revenue"),
        ("Pen", 5.5, "2025", "Revenue"),
        ("Pen", 6.5, "2026", "Revenue"),
        ("Pen", 10.0, "2025", "Units"),
        ("Pen", 12.0, "2026", "Units"),
    ]


def test_workbook_outline_codes_in_a_sheet() -> None:
    rows = [
        ["Kode", "Uraian", "Nilai"],
        ["1", "Pendapatan", 100],
        ["1.1", "Penjualan", 70],
        ["1.2", "Lainnya", 30],
        ["2", "Biaya", 40],
        ["2.1", "Gaji", 40],
    ]
    wb = Workbook.open(make_xlsx({"Sheet": rows}))
    src = Source.model_validate(
        {"file_role": "f", "sheet": {"name": "Sheet"}, "region": {"header_anchor": ["Kode"]}}
    )
    rel = read_region(wb, src, connect(), "raw")
    rel = _run("parse_hierarchy", {"column": "Kode"}, rel)
    assert [(r[0], r[3], r[4]) for r in rel.fetchall()] == [
        ("1", 1, None),
        ("1.1", 2, "1"),
        ("1.2", 2, "1"),
        ("2", 1, None),
        ("2.1", 2, "2"),
    ]
