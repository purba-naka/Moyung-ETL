"""`plan_load`: type maps, name rules, the shape of each plan, and that no value escapes quoting.

None of this runs on a warehouse. Where `sqlglot` is installed (it is not a dependency) each
statement is also parsed in the platform's own dialect, as a second opinion on the syntax.
"""

from __future__ import annotations

import re

import pytest

from excel_etl.load import (
    PROFILES,
    LoadError,
    Where,
    check_columns,
    plan_load,
    profile,
)
from excel_etl.nodes import Column

try:
    import sqlglot as _sqlglot
except ImportError:  # the optional check is skipped, the rest runs
    _sqlglot = None

PLATFORMS = ["snowflake", "databricks", "bigquery"]
WHERE = {
    "snowflake": Where(database="DB", schema_name="SC", stage="@DB.SC.STG/in"),
    "databricks": Where(database="cat", schema_name="sc", stage="/Volumes/cat/sc/vol"),
    "bigquery": Where(database="proj", schema_name="ds", stage="gs://bucket/in"),
}
COLS = (
    Column("Plant", "VARCHAR"),
    Column("JAN|Ton", "DECIMAL(18,2)"),
    Column("n", "BIGINT"),
    Column("day", "DATE"),
)


def _plan(platform: str, mode: str = "replace", keys=(), cols=COLS, table: str = "sales", **kw):
    where = kw.pop("where", WHERE[platform])
    existing = kw.pop("existing", None)
    assert not kw, kw  # a misspelt option must fail here, not be ignored
    file = "C:/run/out.parquet"
    return plan_load(platform, "out", file, table, mode, list(keys), cols, where, existing)


def _sql(plan) -> list[str]:
    return [s.sql for s in plan.steps if s.sql]


# --- profiles and type maps --------------------------------------------


def test_profile_lookup_ignores_letter_case_and_names_the_choices() -> None:
    assert profile("BigQuery").name == "bigquery"
    with pytest.raises(LoadError, match="bigquery, databricks, snowflake"):
        profile("oracle")
    with pytest.raises(LoadError, match="unknown platform"):
        profile(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("duck", "snowflake", "databricks", "bigquery"),
    [
        ("VARCHAR", "VARCHAR", "STRING", "STRING"),
        ("BIGINT", "NUMBER(19,0)", "BIGINT", "INT64"),
        ("INTEGER", "NUMBER(10,0)", "INT", "INT64"),
        ("DOUBLE", "FLOAT", "DOUBLE", "FLOAT64"),
        ("BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOL"),
        ("DATE", "DATE", "DATE", "DATE"),
        ("TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_NTZ", "DATETIME"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP_TZ", "TIMESTAMP", "TIMESTAMP"),
        ("DECIMAL(18,2)", "NUMBER(18,2)", "DECIMAL(18,2)", "NUMERIC(18,2)"),
        ("DECIMAL(38,9)", "NUMBER(38,9)", "DECIMAL(38,9)", "NUMERIC(38,9)"),
        ("DECIMAL(38,10)", "NUMBER(38,10)", "DECIMAL(38,10)", "BIGNUMERIC(38,10)"),
        ("BLOB", "BINARY", "BINARY", "BYTES"),
        ("HUGEINT", "NUMBER(38,0)", None, None),
        ("UBIGINT", "NUMBER(20,0)", None, None),
        ("INTERVAL", None, None, None),
        ("UUID", None, None, None),
        ("INTEGER[]", None, None, None),
        ("STRUCT(a INTEGER)", None, None, None),
        ("MAP(VARCHAR, INTEGER)", None, None, None),
        ("JSON", None, None, None),
    ],
)
def test_type_maps(duck: str, snowflake, databricks, bigquery) -> None:
    got = [PROFILES[p].to_type(duck) for p in PLATFORMS]
    assert got == [snowflake, databricks, bigquery]


def test_a_decimal_beyond_a_platform_is_refused_not_cut() -> None:
    assert PROFILES["snowflake"].to_type("DECIMAL(38,38)") is None  # scale max 37
    assert PROFILES["bigquery"].to_type("DECIMAL(38,38)") == "BIGNUMERIC(38,38)"


def test_a_type_nobody_mapped_is_refused_not_guessed() -> None:
    for p in PROFILES.values():
        assert p.to_type("SOME_FUTURE_TYPE") is None
        assert p.to_type("") is None


# --- names --------------------------------------------


def test_every_problem_is_listed_at_once_and_capped() -> None:
    cols = (Column("a", "INTERVAL"), Column("b", "UUID"), Column("c", "HUGEINT"))
    with pytest.raises(LoadError) as e:
        check_columns(PROFILES["databricks"], cols)
    msg = str(e.value)
    assert "'a' is INTERVAL" in msg and "'b' is UUID" in msg and "'c' is HUGEINT" in msg
    many = tuple(Column(f"c{i}", "INTERVAL") for i in range(25))
    with pytest.raises(LoadError, match=r"\(\+15 more\)"):
        check_columns(PROFILES["snowflake"], many)


@pytest.mark.parametrize(
    ("platform", "names", "needle"),
    [
        ("bigquery", ("Column1", "column1"), "one column on bigquery"),
        ("databricks", ("Ton", "TON"), "one column on databricks"),
        ("bigquery", ("_TABLE_x",), "prefix BigQuery keeps"),
        ("bigquery", ("_file_x",), "prefix BigQuery keeps"),
        ("bigquery", ("a.b",), "does not allow"),
        ("bigquery", ("a" * 301,), "longer than 300"),
        ("snowflake", ("a" * 256,), "longer than 255"),
        ("snowflake", ("",), "empty name"),
    ],
)
def test_name_rules_per_platform(platform: str, names: tuple[str, ...], needle: str) -> None:
    with pytest.raises(LoadError, match=needle):
        check_columns(PROFILES[platform], tuple(Column(n, "VARCHAR") for n in names))


def test_snowflake_keeps_case_so_ton_and_TON_are_two_columns() -> None:
    out = check_columns(PROFILES["snowflake"], (Column("Ton", "VARCHAR"), Column("TON", "VARCHAR")))
    assert [c[0] for c in out] == ["Ton", "TON"]


def test_excel_style_names_are_fine_where_the_platform_allows_them() -> None:
    odd = (
        Column("JAN|Ton", "DOUBLE"),
        Column("Net Weight (kg)", "DOUBLE"),
        Column("a=b", "DOUBLE"),
    )
    for p in ("snowflake", "databricks"):
        assert len(check_columns(PROFILES[p], odd)) == 3
    with pytest.raises(LoadError, match="does not allow"):
        check_columns(PROFILES["bigquery"], odd)  # ( ) are not allowed even in flexible names


# --- the shape of each plan --------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("mode", ["replace", "append", "merge"])
def test_a_plan_starts_by_sending_the_file_and_numbers_its_steps(platform: str, mode: str) -> None:
    plan = _plan(platform, mode, ["Plant"] if mode == "merge" else [])
    assert plan.steps[0].kind == "upload" and plan.steps[0].file == "C:/run/out.parquet"
    assert [s.n for s in plan.steps] == list(range(1, len(plan.steps) + 1))
    assert all(s.why for s in plan.steps)
    assert plan.columns[1] == (
        "JAN|Ton",
        "DECIMAL(18,2)",
        PROFILES[platform].to_type("DECIMAL(18,2)"),
    )
    assert plan.text().startswith(f"load out to {platform} table sales ({mode})")


def test_snowflake_replace_is_one_transaction_and_names_the_stage_file() -> None:
    sql = _sql(_plan("snowflake", "replace"))
    joined = "\n".join(sql)
    assert sql[0].startswith("PUT 'file://C:/run/out.parquet' @DB.SC.STG/in ")
    assert "BEGIN" in sql and "COMMIT" in sql
    assert sql.index("BEGIN") < next(i for i, s in enumerate(sql) if s.startswith("TRUNCATE"))
    assert sql.index("COMMIT") > next(i for i, s in enumerate(sql) if s.startswith("COPY INTO"))
    assert 'COPY INTO "DB"."SC"."sales" FROM @DB.SC.STG/in/out.parquet' in joined
    assert "MATCH_BY_COLUMN_NAME = CASE_SENSITIVE" in joined  # columns by name, case as written


def test_databricks_replace_makes_an_empty_table_of_this_shape_then_copies() -> None:
    sql = _sql(_plan("databricks", "replace"))
    assert sql[0].startswith("CREATE OR REPLACE TABLE `cat`.`sc`.`sales` (")
    assert sql[0].endswith(") USING DELTA")
    assert (
        sql[1] == "COPY INTO `cat`.`sc`.`sales` "
        "FROM '/Volumes/cat/sc/vol/out.parquet' FILEFORMAT = PARQUET"
    )


def test_bigquery_replace_and_append_use_load_data() -> None:
    assert _sql(_plan("bigquery", "replace"))[0].startswith(
        "LOAD DATA OVERWRITE `proj`.`ds`.`sales`"
    )
    assert _sql(_plan("bigquery", "append"))[0].startswith("LOAD DATA INTO `proj`.`ds`.`sales`")
    assert "uris = ['gs://bucket/in/out.parquet']" in _sql(_plan("bigquery", "append"))[0]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_merge_uses_a_scratch_table_matches_on_the_keys_and_cleans_up(platform: str) -> None:
    plan = _plan(platform, "merge", ["Plant", "day"])
    kinds = [s.kind for s in plan.steps]
    assert kinds[-2:] == ["merge", "cleanup"]
    merge = next(s.sql for s in plan.steps if s.kind == "merge")
    q = PROFILES[platform].quote
    assert f"t.{q('Plant')} = s.{q('Plant')} AND t.{q('day')} = s.{q('day')}" in merge
    assert f"t.{q('Plant')} = s.{q('Plant')}," not in merge  # a key is never updated
    assert "WHEN MATCHED THEN UPDATE SET" in merge and "WHEN NOT MATCHED THEN INSERT" in merge
    assert "__load" in plan.steps[-1].sql and plan.steps[-1].sql.startswith("DROP TABLE IF EXISTS")


def test_merge_on_every_column_has_nothing_to_update_but_still_inserts() -> None:
    cols = (Column("a", "BIGINT"), Column("b", "VARCHAR"))
    merge = next(
        s.sql for s in _plan("snowflake", "merge", ["a", "b"], cols=cols).steps if s.kind == "merge"
    )
    assert "WHEN MATCHED" not in merge and "WHEN NOT MATCHED THEN INSERT" in merge


def test_databricks_adds_column_mapping_only_when_a_name_needs_it() -> None:
    plain = "\n".join(_sql(_plan("databricks", cols=(Column("a", "BIGINT"),))))
    assert "columnMapping" not in plain
    spaced = _plan("databricks", cols=(Column("Net Weight", "DOUBLE"),))
    assert "TBLPROPERTIES ('delta.columnMapping.mode' = 'name')" in "\n".join(_sql(spaced))
    assert any("column mapping" in w for w in spaced.warnings)
    pipe = "\n".join(_sql(_plan("databricks")))  # `JAN|Ton` has no space or ,;{}()=
    assert "columnMapping" not in pipe


def test_a_missing_where_still_gives_a_plan_with_unqualified_names() -> None:
    plan = _plan("bigquery", where=Where())
    assert "LOAD DATA OVERWRITE `sales`" in plan.steps[1].sql
    assert "uris = ['out.parquet']" in plan.steps[1].sql


# --- refusals --------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_merge_without_keys_or_with_a_missing_key_is_refused(platform: str) -> None:
    with pytest.raises(LoadError, match="merge needs keys"):
        _plan(platform, "merge", [])
    with pytest.raises(LoadError, match="merge key 'nope' is not a column; columns: Plant"):
        _plan(platform, "merge", ["nope"])
    with pytest.raises(LoadError, match="keys are only for merge"):
        _plan(platform, "append", ["Plant"])


def test_an_unloadable_column_stops_the_plan_before_any_sql_exists() -> None:
    with pytest.raises(LoadError, match="'x' is INTERVAL"):
        _plan("snowflake", cols=(*COLS, Column("x", "INTERVAL")))


def test_a_huge_table_is_refused_instead_of_printing_a_giant_plan() -> None:
    cols = tuple(Column(f"column_number_{i}", "VARCHAR") for i in range(800))
    with pytest.raises(LoadError, match="too many columns"):
        _plan("snowflake", cols=cols)


def test_a_bad_stage_path_is_refused_not_pasted() -> None:
    with pytest.raises(LoadError, match="stage path"):
        _plan("snowflake", where=Where(stage="@s; DROP TABLE x"))


# --- nothing a name or path holds can end the statement ------------------------------------------

EVIL = ['x"; DROP TABLE t; --', "x`; DROP TABLE t; --", "x' OR '1'='1", "a\nb", "x\\"]


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("evil", EVIL)
def test_a_hostile_column_name_stays_inside_its_quotes(platform: str, evil: str) -> None:
    p = PROFILES[platform]
    cols = (Column(evil, "VARCHAR"), Column("k", "BIGINT"))
    try:
        plan = _plan(platform, "merge", ["k"], cols=cols)
    except LoadError as e:  # BigQuery refuses these names outright, which is also safe
        assert platform == "bigquery" and "does not allow" in str(e)
        return
    quoted = p.quote(evil)
    for s in plan.steps:
        stripped = s.sql.replace(quoted, "<NAME>")
        assert "DROP TABLE t" not in stripped, (platform, s.sql)
    assert quoted.count(p.quote_char) == 2 + 2 * evil.count(p.quote_char)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("evil", EVIL)
def test_a_hostile_table_name_stays_inside_its_quotes(platform: str, evil: str) -> None:
    p = PROFILES[platform]
    plan = _plan(platform, "replace", table=evil)
    for s in plan.steps:
        assert "DROP TABLE t" not in s.sql.replace(p.quote(evil), "<NAME>")


@pytest.mark.parametrize("platform", ["databricks", "bigquery"])
def test_a_quote_in_a_path_cannot_close_the_string_literal(platform: str) -> None:
    where = Where(database="d", schema_name="s", stage="gs://b/x' ); DROP TABLE t; --")
    plan = _plan(platform, where=where)
    copy = next(s.sql for s in plan.steps if s.kind == "copy")
    assert "x'' ); DROP TABLE t; --" in copy  # the quote is doubled, so it stays text


def test_a_nul_byte_is_refused_everywhere() -> None:
    with pytest.raises(LoadError, match="NUL"):
        _plan("snowflake", cols=(Column("a\x00b", "VARCHAR"),))
    with pytest.raises(LoadError, match="NUL"):
        _plan("databricks", where=Where(stage="a\x00b"))


# --- a second opinion on the syntax --------------------------------------------


@pytest.mark.skipif(_sqlglot is None, reason="sqlglot is not installed (optional, test-only)")
@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("mode", ["replace", "append", "merge"])
def test_every_statement_parses_in_the_platforms_own_dialect(platform: str, mode: str) -> None:
    plan = _plan(platform, mode, ["Plant"] if mode == "merge" else [])
    for s in plan.steps:
        if not s.sql or s.kind == "upload":
            continue  # PUT is a client command, not SQL a parser knows
        tree = _sqlglot.parse_one(s.sql, dialect=platform)
        assert tree is not None, s.sql
    assert re.search(r"\w", plan.text())


# --- the table that is already there --------------------------------------------


def _existing(*pairs: tuple[str, str]):
    return tuple(Column(n, t) for n, t in pairs)


def test_a_plan_says_when_the_target_table_was_not_compared() -> None:
    warned = _plan("snowflake").warnings
    assert any("was not compared" in w for w in warned)
    same = _existing(
        ("Plant", "VARCHAR"), ("JAN|Ton", "NUMBER(18,2)"), ("n", "NUMBER(19,0)"), ("day", "DATE")
    )
    assert not any("was not compared" in w for w in _plan("snowflake", existing=same).warnings)


def test_a_wider_number_or_another_spelling_of_the_same_type_is_not_drift() -> None:
    wide = _existing(
        ("Plant", "TEXT"), ("JAN|Ton", "NUMBER(38,2)"), ("n", "NUMBER(38,0)"), ("day", "DATE")
    )
    assert _plan("snowflake", "append", existing=wide).mode == "append"
    bq = _existing(("Plant", "STRING"), ("JAN|Ton", "NUMERIC"), ("n", "INT64"), ("day", "DATE"))
    assert _plan("bigquery", "append", existing=bq).mode == "append"


def test_drift_lists_new_changed_and_extra_columns() -> None:
    table = _existing(("Plant", "VARCHAR"), ("JAN|Ton", "VARCHAR"), ("old", "DATE"))
    with pytest.raises(LoadError) as e:
        _plan("snowflake", "append", existing=table)
    msg = str(e.value)
    assert "new column 'n'" in msg and "new column 'day'" in msg
    assert "'JAN|Ton' is VARCHAR in the table" in msg
    assert "table column 'old' is not in the file" in msg


def test_names_are_compared_the_way_the_platform_compares_them() -> None:
    table = _existing(("PLANT", "STRING"), ("jan|ton", "NUMERIC"), ("N", "INT64"), ("DAY", "DATE"))
    assert _plan("bigquery", "append", existing=table).mode == "append"  # no case on BigQuery
    with pytest.raises(LoadError, match="new column 'Plant'"):
        _plan("snowflake", "append", existing=_existing(("PLANT", "VARCHAR")))  # case kept


def test_replace_over_a_table_of_another_shape_is_allowed_and_said() -> None:
    plan = _plan("databricks", "replace", existing=_existing(("a", "INT")))
    assert any("the table's shape changes" in w for w in plan.warnings)


def test_a_nul_byte_in_a_name_is_refused_before_it_reaches_sql() -> None:
    with pytest.raises(LoadError, match="NUL"):
        PROFILES["snowflake"].quote("a\x00b")
    with pytest.raises(LoadError, match="NUL"):
        _plan("bigquery", table="a\x00b")
