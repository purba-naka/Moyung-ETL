from __future__ import annotations

import itertools
import re

import duckdb
import pytest

from moyung.load import (
    PROFILES,
    LoadError,
    Where,
    check_columns,
    compare_target,
    file_columns,
    plan_load,
    profile,
)
from moyung.nodes.base import Column

PLATFORMS = ["snowflake", "databricks", "bigquery"]
IDENT_QUOTE = {"snowflake": '"', "databricks": "`", "bigquery": "`"}
IDENT_ESCAPES = {"snowflake": False, "databricks": False, "bigquery": True}
BENIGN_STAGE = {
    "snowflake": "@db.sch.stage",
    "databricks": "/Volumes/c/s/v",
    "bigquery": "gs://bucket/path",
}
PAYLOADS = [
    "x'; DROP TABLE t; --",
    'x"; DROP TABLE t; --',
    "x`; DROP TABLE t; --",
    "x\\",
    "\\",
    "x\\'; DROP TABLE t; --",
    "x\\`; DROP TABLE t; --",
    "p\\`.`; DROP TABLE t; --",
    "x\nDROP TABLE t",
    "x*/ DROP /*",
    "a b",
    "a;b",
    "a--b",
    "%s{0}$$?",
    "é日本語",
    "'",
    '"',
    "`",
    "''",
    "``",
    '""',
]


def outside_quotes(sql: str, platform: str) -> str:
    quote_char = IDENT_QUOTE[platform]
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", quote_char):
            escapes = True if c == "'" else IDENT_ESCAPES[platform]
            i += 1
            while i < n:
                if escapes and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            out.append("<Q>")
            continue
        out.append(c)
        i += 1
    text = "".join(out).replace(" TBLPROPERTIES (<Q> = <Q>)", "")
    return re.sub(r"@[A-Za-z0-9_$./~%-]*", "@STAGE", text)


def plan_sql(
    platform,
    mode,
    *,
    table="t",
    database="",
    schema="",
    stage="",
    file="o.parquet",
    names=("a", "b"),
):
    columns = tuple(Column(n, "BIGINT" if i == 0 else "VARCHAR") for i, n in enumerate(names))
    keys = [names[0]] if mode == "merge" else []
    where = Where(database=database, schema_name=schema, stage=stage)
    plan = plan_load(platform, "o", file, table, mode, keys, columns, where)
    return "\n;\n".join(s.sql for s in plan.steps)


def skeleton(platform, mode, **slots):
    return outside_quotes(plan_sql(platform, mode, **slots), platform)


def backslash_in_a_bigquery_name(platform, slot, payload):
    return platform == "bigquery" and slot in ("table", "database", "schema") and "\\" in payload


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("mode", ["replace", "append", "merge"])
@pytest.mark.parametrize("slot", ["table", "database", "schema", "file", "column", "key_column"])
@pytest.mark.parametrize("payload", PAYLOADS)
def test_hostile_text_never_leaves_its_quotes(platform, mode, slot, payload):
    if backslash_in_a_bigquery_name(platform, slot, payload):
        pytest.xfail("BUG-023: BigQuery quoted identifiers keep backslash escapes")
    benign = {
        "table": "t",
        "database": "db",
        "schema": "sc",
        "file": "o.parquet",
    }
    if slot in ("table", "database", "schema", "file"):
        base = {**benign, "database": "db", "schema": "sc"}
        hostile = {**base, slot: payload}
        want = skeleton(platform, mode, **base)
        try:
            got = skeleton(platform, mode, **hostile)
        except LoadError:
            return
    else:
        names = ("a", "b")
        hostile_names = (payload, "b") if slot == "key_column" else ("a", payload)
        want = skeleton(platform, mode, names=names)
        try:
            got = skeleton(platform, mode, names=hostile_names)
        except LoadError:
            return
    assert got == want


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("payload", ["x\\", "\\", "x\\`; DROP TABLE t; --"])
def test_a_backslash_in_a_database_name_changes_the_shape_of_a_bigquery_statement(
    platform, payload
):
    sql = plan_sql(platform, "replace", database=payload, schema="sc")
    shape = outside_quotes(sql, platform)
    if platform == "bigquery":
        assert shape != outside_quotes(
            plan_sql(platform, "replace", database="db", schema="sc"), platform
        )
    else:
        assert shape == outside_quotes(
            plan_sql(platform, "replace", database="db", schema="sc"), platform
        )


@pytest.mark.xfail(strict=True, reason="BUG-023: BigQuery identifiers need backslash escaped too")
def test_bigquery_quote_escapes_the_backslash_like_a_string():
    quoted = profile("bigquery").quote("a\\")
    assert quoted == "`a\\\\`"


class TestQuoting:
    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_the_quote_character_is_doubled(self, platform):
        p = profile(platform)
        q = p.quote_char
        assert p.quote(f"a{q}b") == f"{q}a{q}{q}b{q}"

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_nul_is_refused(self, platform):
        with pytest.raises(LoadError, match="NUL"):
            profile(platform).quote("a\x00b")

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_path_joins_quoted_parts_and_skips_empty_ones(self, platform):
        p = profile(platform)
        q = p.quote_char
        assert p.path("d", "", "t") == f"{q}d{q}.{q}t{q}"

    @pytest.mark.parametrize("name", ["snowflake", "SNOWFLAKE", "Databricks", "bigQuery"])
    def test_platform_names_ignore_case(self, name):
        assert profile(name).name == name.lower()

    @pytest.mark.parametrize("name", ["", "oracle", None, 5, "snow flake"])
    def test_an_unknown_platform_lists_the_known_ones(self, name):
        with pytest.raises(LoadError, match="bigquery, databricks, snowflake"):
            profile(name)


class TestTypes:
    DUCKDB_TYPES = [
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER",
        "UBIGINT", "HUGEINT", "UHUGEINT", "FLOAT", "DOUBLE", "VARCHAR", "BLOB", "BOOLEAN",
        "DATE", "TIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "DECIMAL(10,2)", "UUID",
        "INTERVAL", "INTEGER[]", "STRUCT(a INTEGER)", "MAP(VARCHAR, INTEGER)",
    ]  # fmt: skip

    @pytest.mark.parametrize("platform", PLATFORMS)
    @pytest.mark.parametrize("kind", DUCKDB_TYPES)
    def test_every_type_is_mapped_or_refused_with_the_column_named(self, platform, kind):
        try:
            typed = check_columns(profile(platform), (Column("c", kind),))
        except LoadError as e:
            assert "'c'" in str(e) and kind in str(e)
        else:
            assert typed[0][:2] == ("c", kind) and typed[0][2]

    @pytest.mark.parametrize(
        "platform, kind, ok",
        [
            ("snowflake", "DECIMAL(38,37)", True),
            ("snowflake", "DECIMAL(38,38)", False),
            ("snowflake", "DECIMAL(39,0)", False),
            ("databricks", "DECIMAL(38,38)", True),
            ("databricks", "DECIMAL(39,2)", False),
            ("bigquery", "DECIMAL(38,9)", True),
            ("bigquery", "DECIMAL(38,10)", True),
            ("bigquery", "DECIMAL(76,38)", True),
            ("bigquery", "DECIMAL(76,39)", False),
            ("bigquery", "DECIMAL(77,0)", False),
        ],
    )
    def test_decimal_limits_per_platform(self, platform, kind, ok):
        try:
            check_columns(profile(platform), (Column("c", kind),))
        except LoadError:
            assert not ok
        else:
            assert ok

    def test_a_decimal_picks_the_narrowest_bigquery_type(self):
        assert (
            check_columns(profile("bigquery"), (Column("c", "DECIMAL(20,5)"),))[0][2]
            == "NUMERIC(20,5)"
        )
        assert (
            check_columns(profile("bigquery"), (Column("c", "DECIMAL(40,5)"),))[0][2]
            == "BIGNUMERIC(40,5)"
        )

    def test_the_timezone_types_differ_per_platform(self):
        kind = "TIMESTAMP WITH TIME ZONE"
        assert check_columns(profile("snowflake"), (Column("c", kind),))[0][2] == "TIMESTAMP_TZ"
        assert check_columns(profile("databricks"), (Column("c", kind),))[0][2] == "TIMESTAMP"
        assert check_columns(profile("bigquery"), (Column("c", kind),))[0][2] == "TIMESTAMP"

    def test_unsigned_big_integers_are_refused_where_nothing_holds_them(self):
        for platform in ("databricks", "bigquery"):
            with pytest.raises(LoadError):
                check_columns(profile(platform), (Column("c", "UBIGINT"),))
        assert (
            check_columns(profile("snowflake"), (Column("c", "UBIGINT"),))[0][2] == "NUMBER(20,0)"
        )


class TestColumnNames:
    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_an_empty_name_is_refused(self, platform):
        with pytest.raises(LoadError, match="empty name"):
            check_columns(profile(platform), (Column("", "BIGINT"),))

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_name_at_the_limit_is_fine_and_one_more_is_not(self, platform):
        p = profile(platform)
        check_columns(p, (Column("a" * p.max_name, "BIGINT"),))
        with pytest.raises(LoadError, match="longer than"):
            check_columns(p, (Column("a" * (p.max_name + 1), "BIGINT"),))

    def test_case_variants_are_one_column_where_the_platform_folds_case(self):
        columns = (Column("Qty", "BIGINT"), Column("qty", "BIGINT"))
        check_columns(profile("snowflake"), columns)
        for platform in ("databricks",):
            with pytest.raises(LoadError, match="one column"):
                check_columns(profile(platform), columns)

    @pytest.mark.parametrize(
        "name",
        [
            "_TABLE_x",
            "_file_y",
            "__ROOT__z",
            "_PARTITIONTIME",
            "a.b",
            "a/b",
            "a$b",
            "a,b",
            "a\\b",
            "a`b",
        ],
    )
    def test_names_bigquery_refuses(self, name):
        with pytest.raises(LoadError):
            check_columns(profile("bigquery"), (Column(name, "BIGINT"),))

    @pytest.mark.parametrize("name", ["a b", "Kolom é", "日本語", "a-b", "_x", "1st"])
    def test_names_bigquery_accepts(self, name):
        check_columns(profile("bigquery"), (Column(name, "BIGINT"),))

    def test_every_problem_is_listed_at_once_and_capped_at_ten(self):
        columns = tuple(Column(f"_TABLE_{i}", "BIGINT") for i in range(14))
        with pytest.raises(LoadError) as e:
            check_columns(profile("bigquery"), columns)
        text = str(e.value)
        assert text.count("prefix BigQuery keeps") == 10 and "(+4 more)" in text

    def test_names_are_clipped_in_messages(self):
        with pytest.raises(LoadError) as e:
            check_columns(profile("bigquery"), (Column("_TABLE_" + "x" * 500, "BIGINT"),))
        assert len(str(e.value)) < 300


class TestPlanRules:
    COLS = (Column("id", "BIGINT"), Column("v", "VARCHAR"))
    WHERE = Where()

    def plan(self, platform="snowflake", mode="replace", keys=None, existing=None, columns=None):
        return plan_load(
            platform,
            "o",
            "o.parquet",
            "t",
            mode,
            keys or [],
            columns or self.COLS,
            self.WHERE,
            existing,
        )

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_steps_are_numbered_from_one_in_order(self, platform):
        plan = self.plan(platform)
        assert [s.n for s in plan.steps] == list(range(1, len(plan.steps) + 1))

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_merge_needs_keys_and_other_modes_refuse_them(self, platform):
        with pytest.raises(LoadError, match="merge needs keys"):
            self.plan(platform, "merge")
        for mode in ("replace", "append"):
            with pytest.raises(LoadError, match="keys are only for merge"):
                self.plan(platform, mode, ["id"])

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_merge_key_that_is_not_a_column_is_named(self, platform):
        with pytest.raises(LoadError, match="'zz' is not a column"):
            self.plan(platform, "merge", ["zz"])

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_merge_on_every_column_has_no_update_clause(self, platform):
        plan = self.plan(platform, "merge", ["id", "v"])
        assert not any("WHEN MATCHED" in s.sql for s in plan.steps)

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_merge_cleans_up_its_scratch_table(self, platform):
        kinds = [s.kind for s in self.plan(platform, "merge", ["id"]).steps]
        assert kinds[-1] == "cleanup" and "merge" in kinds

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_the_table_was_not_compared_note_appears_only_without_describe(self, platform):
        assert any("not compared" in w for w in self.plan(platform).warnings)
        same = tuple(Column(n, {"id": "NUMBER(19,0)", "v": "VARCHAR"}[n]) for n in ("id", "v"))
        shaped = {
            "snowflake": same,
            "databricks": (Column("id", "BIGINT"), Column("v", "STRING")),
            "bigquery": (Column("id", "INT64"), Column("v", "STRING")),
        }[platform]
        assert not any("not compared" in w for w in self.plan(platform, existing=shaped).warnings)

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_a_table_of_another_shape_is_refused_for_append_and_merge_but_not_replace(
        self, platform
    ):
        existing = (Column("other", "BIGINT"),)
        self.plan(platform, "replace", existing=existing)
        with pytest.raises(LoadError, match="does not fit the file"):
            self.plan(platform, "append", existing=existing)
        with pytest.raises(LoadError, match="does not fit the file"):
            self.plan(platform, "merge", ["id"], existing=existing)

    def test_a_replace_over_another_shape_says_the_shape_changes(self):
        plan = self.plan("snowflake", "replace", existing=(Column("other", "NUMBER(10,0)"),))
        assert any("shape changes" in w for w in plan.warnings)

    def test_a_wider_number_in_the_table_is_not_drift(self):
        p = profile("snowflake")
        planned = check_columns(p, (Column("id", "BIGINT"),))
        assert compare_target(p, planned, (Column("id", "NUMBER(38,0)"),)) == []

    @pytest.mark.parametrize(
        "table_type, planned_type, drift",
        [
            ("VARCHAR(100)", "VARCHAR", False),
            ("TEXT", "VARCHAR", False),
            ("FLOAT", "VARCHAR", True),
            ("TIMESTAMP_NTZ(9)", "TIMESTAMP", False),
            ("DATE", "TIMESTAMP", True),
            ("BOOLEAN", "BOOLEAN", False),
        ],
    )
    def test_type_families_decide_drift(self, table_type, planned_type, drift):
        p = profile("snowflake")
        planned = check_columns(p, (Column("c", planned_type),))
        assert bool(compare_target(p, planned, (Column("c", table_type),))) is drift

    def test_drift_names_new_missing_and_changed_columns(self):
        p = profile("snowflake")
        planned = check_columns(p, (Column("a", "BIGINT"), Column("b", "VARCHAR")))
        lines = compare_target(p, planned, (Column("a", "VARCHAR"), Column("gone", "DATE")))
        assert any("'a' is VARCHAR in the table" in x for x in lines)
        assert any("new column 'b'" in x for x in lines)
        assert any("table column 'gone' is not in the file" in x for x in lines)

    def test_drift_matches_names_ignoring_case_only_where_the_platform_folds(self):
        sf, db = profile("snowflake"), profile("databricks")
        planned_sf = check_columns(sf, (Column("Qty", "BIGINT"),))
        planned_db = check_columns(db, (Column("Qty", "BIGINT"),))
        assert compare_target(sf, planned_sf, (Column("qty", "NUMBER(38,0)"),))
        assert compare_target(db, planned_db, (Column("qty", "BIGINT"),)) == []

    @pytest.mark.parametrize("platform", ["snowflake", "databricks"])
    def test_a_huge_table_is_refused_by_size(self, platform):
        columns = tuple(Column(f"column_number_{i}_" + "x" * 60, "BIGINT") for i in range(400))
        with pytest.raises(LoadError, match="over 20000 characters"):
            self.plan(platform, columns=columns)

    def test_databricks_warns_about_column_mapping_for_awkward_names(self):
        plan = self.plan("databricks", columns=(Column("a b", "BIGINT"), Column("v", "VARCHAR")))
        assert any("column mapping" in w for w in plan.warnings)
        plain = self.plan("databricks")
        assert not any("column mapping" in w for w in plain.warnings)

    def test_the_plan_text_lists_every_step(self):
        text = self.plan("snowflake", "merge", ["id"]).text()
        assert text.startswith("load o to snowflake table t (merge)")
        assert text.count("\n  ") >= 4

    def test_a_plan_is_immutable(self):
        plan = self.plan()
        with pytest.raises(ValueError):
            plan.table = "other"

    @pytest.mark.parametrize("mode", ["upsert", "", None, "REPLACE"])
    def test_an_unknown_mode_is_refused(self, mode):
        with pytest.raises((LoadError, KeyError, ValueError)):
            self.plan("snowflake", mode)


class TestSnowflakeStage:
    def plan(self, stage):
        return plan_load(
            "snowflake",
            "o",
            "o.parquet",
            "t",
            "replace",
            [],
            (Column("a", "BIGINT"),),
            Where(stage=stage),
        )

    @pytest.mark.parametrize(
        "stage", ["@db.sch.stage", "@~", "@%t", "@db.sch.stage/dir/sub", "@db.sch.stage/"]
    )
    def test_good_stage_references(self, stage):
        assert stage.rstrip("/") in self.plan(stage).steps[0].sql

    @pytest.mark.parametrize(
        "stage",
        [
            "@s; DROP TABLE t",
            "@s'",
            '@s"',
            "@s/../x",
            "@s//x",
            "@s--x",
            "@s x",
            "@s\n",
            "@s`",
            "@s\\",
            "@s(x)",
        ],
    )
    def test_a_stage_that_could_carry_sql_is_refused(self, stage):
        with pytest.raises(LoadError, match="stage path"):
            self.plan(stage)

    def test_no_stage_uses_the_user_stage(self):
        assert "@~" in self.plan("").steps[0].sql


class TestFileColumns:
    def test_the_footer_gives_the_real_columns_and_the_row_count(self, tmp_path):
        path = tmp_path / "f.parquet"
        duckdb.sql(
            "COPY (SELECT i::BIGINT AS id, 'x' AS v, 1.5::DOUBLE AS d FROM range(7) t(i)) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
        columns, rows = file_columns(str(path))
        assert [(c.name, c.type) for c in columns] == [
            ("id", "BIGINT"),
            ("v", "VARCHAR"),
            ("d", "DOUBLE"),
        ]
        assert rows == 7

    def test_a_missing_file_is_a_load_error_not_a_duckdb_error(self, tmp_path):
        with pytest.raises(LoadError, match="cannot read"):
            file_columns(str(tmp_path / "nope.parquet"))

    def test_a_file_that_is_not_parquet(self, tmp_path):
        path = tmp_path / "f.parquet"
        path.write_text("a,b\n1,2\n")
        with pytest.raises(LoadError, match="cannot read"):
            file_columns(str(path))

    def test_a_path_with_a_quote_is_read_safely(self, tmp_path):
        folder = tmp_path / "o'brien"
        folder.mkdir()
        path = folder / "f.parquet"
        target = path.as_posix().replace("'", "''")
        duckdb.sql(f"COPY (SELECT 1 AS a) TO '{target}' (FORMAT PARQUET)")
        assert file_columns(str(path))[1] == 1

    def test_an_empty_parquet_has_columns_and_no_rows(self, tmp_path):
        path = tmp_path / "e.parquet"
        duckdb.sql(f"COPY (SELECT 1 AS a WHERE false) TO '{path.as_posix()}' (FORMAT PARQUET)")
        assert file_columns(str(path)) == ((Column("a", "INTEGER"),), 0)


def test_the_profiles_table_holds_exactly_the_three_platforms():
    assert sorted(PROFILES) == ["bigquery", "databricks", "snowflake"]
    assert all(set(itertools.chain.from_iterable([[p.name]])) for p in PROFILES.values())
