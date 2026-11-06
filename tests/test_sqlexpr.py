from __future__ import annotations

import pytest

from excel_etl.nodes import NodeError
from excel_etl.nodes.base import connect
from excel_etl.nodes.sqlexpr import check_expression


@pytest.mark.parametrize(
    "expr",
    [
        "a + 1",
        '"JAN|PKG" * 2',
        '"it""s" || \'x\'',
        "coalesce(a, 0)",
        "CASE WHEN a > 1 THEN 'hi' WHEN a IS NULL THEN 'none' ELSE 'lo' END",
        "TRY_CAST(a AS DOUBLE)",
        "CAST(a AS DATE)",
        "a BETWEEN 1 AND 2",
        "a IN (1, 2, 3)",
        "a LIKE 'x%' AND b IS NOT NULL OR NOT c",
        "year(d) * 100 + month(d)",
        "d + INTERVAL 1 DAY",
        "DATE '2026-01-01'",
        "round(a / nullif(b, 0), 2)",
        "upper(trim(a))",
        "regexp_replace(a, '[0-9]+', '')",
        "l[1]",
        "a /* note ) */ + 1",
        "'a; b'",
        "'it''s -- not a comment'",
        "  a  ",
        "-a",
        "greatest(a, b)",
        "nullif(a, 0)",
    ],
)
def test_ordinary_expressions_pass(expr: str) -> None:
    assert check_expression(expr) == expr.strip()


@pytest.mark.parametrize(
    ("expr", "needle"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("(SELECT 1)", "subquery"),
        ("EXISTS(SELECT 1)", "subquery"),
        ("a IN (SELECT x FROM t)", "subquery"),
        ("*", r"\* or COLUMNS"),
        ("COLUMNS(*)", r"\* or COLUMNS"),
        ("?", "parameter"),
        ("#1", "#1 reference"),
        ("sum(a) OVER ()", "window"),
        ("list_transform([1], lambda x: x + 1)", "lambda|not allowed"),
        ("1) FROM t WHERE (1=1", "syntax error"),
        ("1) AS x FROM t --", "syntax error|nothing around"),
        ("1) UNION SELECT 2 --", "nothing around"),
        ("1), (2", "nothing around|one expression"),
        ("1) AS a, (2", "nothing around|one expression"),
        ("1); DROP TABLE t; --", "one expression|syntax"),
        ("1; SELECT 2", "one expression|syntax"),
        ("1 --", "syntax error"),
        ("1) AS x FROM (WITH c AS (SELECT 1) SELECT * FROM c", "syntax error|nothing around"),
        ("1) ORDER BY 1 --", "syntax error|nothing around"),
        ("1) LIMIT 1 --", "syntax error|nothing around"),
        ("1) GROUP BY 1 --", "syntax error|nothing around"),
        ("a +", "syntax error"),
        ("((a)", "syntax error"),
        ("a b c", "syntax error"),
    ],
)
def test_shapes_and_forms_that_are_refused(expr: str, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        check_expression(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "read_csv('x.csv')",
        "READ_CSV('x.csv')",
        "read_text('/etc/passwd')",
        "read_parquet('x')",
        "glob('*')",
        "query('select 1')",
        "getvariable('x')",
        "current_setting('enable_external_access')",
        "nextval('s')",
        "repeat('x', 100000000)",
        "len(range(100000000))",
        "random()",
        "now()",
        "uuid()",
        "error('boom')",
        "lpad('x', 100000000, 'y')",
        "a + read_text('f')",
        "coalesce(a, read_blob('f'))",
        "CASE WHEN a THEN read_csv('x') END",
        "upper(repeat('x', 5))",
    ],
)
def test_dangerous_functions_are_refused_wherever_they_hide(expr: str) -> None:
    with pytest.raises(NodeError, match="not allowed"):
        check_expression(expr)


def test_a_quoted_function_name_does_not_get_around_the_list() -> None:
    with pytest.raises(NodeError):
        check_expression("\"read_csv\"('x.csv')")
    with pytest.raises(NodeError):
        check_expression("main.read_csv('x.csv')")


def test_function_clauses_are_refused() -> None:
    for expr in [
        "string_agg(a, ',' ORDER BY b)",
        "sum(a) FILTER (WHERE b > 1)",
        "count(DISTINCT a)",
    ]:
        with pytest.raises(NodeError, match="not allowed"):
            check_expression(expr)


def test_long_and_odd_input() -> None:
    with pytest.raises(NodeError, match="longer than"):
        check_expression("1+" * 3000 + "1")
    with pytest.raises(NodeError, match="NUL"):
        check_expression("a\x00 + 1")
    for bad in (None, 5, ["a"], b"a"):
        with pytest.raises(NodeError, match="must be a string"):
            check_expression(bad)  # type: ignore[arg-type]


def test_error_text_is_short_and_names_the_expression() -> None:
    with pytest.raises(NodeError) as e:
        check_expression("(SELECT " + "x" * 500 + ")")
    assert len(str(e.value)) < 300


def test_the_connection_cannot_read_files_even_if_a_name_slips_through(tmp_path) -> None:
    # layer 3: a function not on the deny list still has no file access
    (tmp_path / "x.csv").write_text("a\n1\n")
    con = connect()
    with pytest.raises(Exception, match="(?i)disabled|permission|not allowed"):
        con.execute(f"SELECT * FROM read_csv('{(tmp_path / 'x.csv').as_posix()}')").fetchall()
    with pytest.raises(Exception, match="(?i)disabled|permission|not allowed"):
        con.execute("SELECT * FROM read_text('x.txt')").fetchall()


def test_star_in_an_aggregate_is_refused_either_way() -> None:
    # `min(COLUMNS(*))` and `count(*)` are both aggregates, caught before the STAR node is
    # reached. `upper(*)` has the same shape as `count(*)` (a call with no argument) but is
    # harmless: it cannot bind to anything.
    for expr, name in [("min(COLUMNS(*))", "min"), ("count(*)", "count_star")]:
        with pytest.raises(NodeError, match=f"aggregate '{name}'"):
            check_expression(expr)
    with pytest.raises(NodeError, match=r"\* or COLUMNS"):
        check_expression("coalesce(COLUMNS(*), 0)")


@pytest.mark.parametrize(
    "expr",
    [
        "sum(a)",
        "SUM(a)",
        "count(a)",
        "max(a)",
        "avg(a)",
        "string_agg(a, ',')",
        "list(a)",
        "first(a)",
        "median(a)",
        "a + sum(b)",
        "coalesce(max(a), 0)",
        "CASE WHEN count(a) > 1 THEN 1 END",
    ],
)
def test_aggregates_are_refused_wherever_they_are(expr: str) -> None:
    # in a plain SELECT they would collapse the whole table to one row instead of per row
    with pytest.raises(NodeError, match="aggregate .* is not allowed here; use the group_by node"):
        check_expression(expr)


def test_every_aggregate_duckdb_knows_is_refused_and_no_scalar_is() -> None:
    con = connect()
    names = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type = 'aggregate' AND function_name ~ '^[a-z_0-9]+$'"
        ).fetchall()
    ]
    assert len(names) > 50
    for name in names:
        try:
            check_expression(f"{name}(a)")
        except NodeError as e:
            assert "aggregate" in str(e) or "not allowed" in str(e), (name, str(e))
    for scalar in ["upper", "round", "abs", "length", "lower", "trim"]:
        assert check_expression(f"{scalar}(a)")
    for call in ["greatest(a, b)", "least(a, b)", "coalesce(a, b)", "nullif(a, b)"]:
        assert check_expression(call)


def test_every_passing_expression_is_one_column_that_cannot_touch_other_data() -> None:
    con = connect()
    con.execute("create table t as select 1 as a, 'x' as c, date '2026-01-01' as d")
    for expr in ["a + 1", "upper(c)", "year(d)", "CASE WHEN a > 0 THEN c END", "greatest(a, 2)"]:
        rel = con.sql(f"select ({check_expression(expr)}) as __e from t")
        assert rel.columns == ["__e"]
        assert len(rel.fetchall()) == 1  # one row in, one row out


def test_parser_behaviour_the_shape_check_leans_on() -> None:
    # pinned so a duckdb upgrade that parses these differently shows up here, not in production
    for escape in ["1) WHERE (1", "1) AS x WHERE (1=1"]:
        with pytest.raises(NodeError, match="syntax error"):
            check_expression(escape)
    # these two do parse; only the shape check stops them
    for escape in ["1) UNION SELECT 2 --", "1) AS x FROM t --"]:
        with pytest.raises(NodeError, match="nothing around"):
            check_expression(escape)
