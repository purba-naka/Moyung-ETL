from __future__ import annotations

import csv
import json
from datetime import date

import duckdb
import pytest
from conftest import make_xlsx

from excel_etl.engine import PipelineError, run, validate
from excel_etl.pipeline import SpecError, parse_spec
from excel_etl.workbook import Workbook


def _spec(**parts) -> object:
    base = {"sources": {}, "steps": [], "outputs": {}}
    base.update(parts)
    return parse_spec(json.dumps(base))


def _src(sheet: str, anchor: str, role: str = "f", rows: int = 1) -> dict:
    return {
        "file_role": role,
        "sheet": {"name": sheet},
        "region": {"header_anchor": [anchor], "header_rows": rows},
    }


_ORDERS = [
    ["Order", "Item", "Qty"],
    ["o1", "pen", 2],
    ["o2", "ink", 1],
    ["o3", "pen", 5],
    ["o4", "pad", 3],
]
_ITEMS = [
    ["Item", "Group", "Price"],
    ["pen", "write", 1.5],
    ["ink", "write", 4.0],
    ["pad", "paper", 2.0],
]


@pytest.fixture
def book() -> Workbook:
    return Workbook.open(make_xlsx({"Orders": _ORDERS, "Items": _ITEMS}))


def _pipeline() -> object:
    return _spec(
        sources={"orders": _src("Orders", "Order"), "items": _src("Items", "Group")},
        steps=[
            {
                "id": "joined",
                "node": "join",
                "inputs": ["orders", "items"],
                "config": {"on": ["Item"], "how": "left"},
            },
            {
                "id": "valued",
                "node": "derive",
                "inputs": ["joined"],
                "config": {"name": "Value", "expr": "Qty * Price"},
            },
            {
                "id": "by_group",
                "node": "group_by",
                "inputs": ["valued"],
                "config": {
                    "by": ["Group"],
                    "aggs": [{"name": "Value", "fn": "sum", "column": "Value"}],
                },
            },
        ],
        outputs={"detail": {"input": "valued"}, "summary": {"input": "by_group", "format": "csv"}},
    )


# --- validate --------------------------------------------------------------------------------


def test_validate_with_files_gives_every_schema(book) -> None:
    rep = validate(_pipeline(), {"f": book})
    assert rep.ok and not rep.issues
    assert [c.name for c in rep.schemas["valued"]] == [
        "Order",
        "Item",
        "Qty",
        "Group",
        "Price",
        "Value",
    ]
    assert [c.name for c in rep.schemas["by_group"]] == ["Group", "Value"]
    assert rep.text().startswith("validate: ok, 5 tables known")


def test_validate_without_files_checks_only_the_steps_own_settings() -> None:
    rep = validate(_pipeline())
    assert rep.ok and rep.schemas == {}
    assert "no files given" in rep.warnings[0]
    bad = _spec(
        sources={"a": _src("S", "x")},
        steps=[
            {"id": "s1", "node": "nope", "inputs": ["a"]},
            {"id": "s2", "node": "select", "inputs": ["a"], "config": {"colums": ["x"]}},
        ],
    )
    rep = validate(bad)
    assert [i.where for i in rep.issues] == ["steps.s1", "steps.s2"]
    assert "unknown node 'nope'" in rep.issues[0].message


def test_validate_lists_independent_problems_together(book) -> None:
    spec = _spec(
        sources={"orders": _src("Orders", "Order"), "gone": _src("Nope", "x")},
        steps=[
            {"id": "a", "node": "select", "inputs": ["orders"], "config": {"columns": ["zzz"]}},
            {"id": "b", "node": "filter", "inputs": ["orders"], "config": {"where": "Order > 5"}},
            {"id": "c", "node": "select", "inputs": ["gone"], "config": {"columns": ["x"]}},
            {"id": "d", "node": "select", "inputs": ["a"], "config": {"columns": ["x"]}},
        ],
    )
    rep = validate(spec, {"f": book})
    where = [i.where for i in rep.issues]
    assert "sources.gone" in where and "steps.a" in where and "steps.b" in where
    assert "steps.c" not in where and "steps.d" not in where  # follow-ups of a listed failure


def test_validate_file_problems(book) -> None:
    spec = _spec(sources={"a": _src("Orders", "Order", role="other")})
    rep = validate(spec, {"f": book})
    assert "no file for file_role 'other'; files given: f" in rep.issues[0].message
    rep = validate(spec, {"other": b"not an excel file"})
    assert rep.issues[0].where == "sources.a" and "other" in rep.issues[0].message


def test_validate_expect_columns_presence(book) -> None:
    src = _src("Items", "Group")
    src["expect_columns"] = [
        {"match": "GROUP"},
        {"match_regex": "^Missing", "required": False},
        {"match": "Weight"},
        {"match_regex": "^Pri", "type": "float"},
    ]
    rep = validate(_spec(sources={"orders": src}), {"f": book})
    assert [i.message[:34] for i in rep.issues] == ["required column 'Weight' not found"]
    assert any("type is not checked yet" in w for w in rep.warnings)


def test_validate_catches_sandbox_and_unsafe_config(book) -> None:
    spec = _spec(
        sources={"orders": _src("Orders", "Order")},
        steps=[
            {
                "id": "a",
                "node": "filter",
                "inputs": ["orders"],
                "config": {"where": "(SELECT 1) = 1"},
            },
            {
                "id": "b",
                "node": "derive",
                "inputs": ["orders"],
                "config": {"name": "x", "expr": "read_text('f')"},
            },
        ],
    )
    rep = validate(spec, {"f": book})
    assert [i.where for i in rep.issues] == ["steps.a", "steps.b"]


# --- run: files written ----------------------------------------------------------------------


def test_run_writes_parquet_and_csv_and_reports(book, tmp_path) -> None:
    rep = run(_pipeline(), {"f": book}, tmp_path)
    assert rep.status == "ok" and not rep.issues
    assert sorted(p.name for p in tmp_path.iterdir()) == ["detail.parquet", "summary.csv"]
    got = (
        duckdb.connect()
        .execute(f"select * from read_parquet('{(tmp_path / 'detail.parquet').as_posix()}')")
        .fetchall()
    )
    assert got == [
        ("o1", "pen", 2, "write", 1.5, 3.0),
        ("o2", "ink", 1, "write", 4.0, 4.0),
        ("o3", "pen", 5, "write", 1.5, 7.5),
        ("o4", "pad", 3, "paper", 2.0, 6.0),
    ]
    with open(tmp_path / "summary.csv", newline="", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [["Group", "Value"], ["write", "14.5"], ["paper", "6.0"]]
    assert [(n.id, n.rows_in, n.rows_out) for n in rep.nodes] == [
        ("orders", 0, 4),
        ("items", 0, 3),
        ("joined", 7, 4),
        ("valued", 4, 4),
        ("by_group", 4, 2),
    ]
    assert [(o.name, o.rows, o.columns) for o in rep.outputs] == [
        ("detail", 4, 6),
        ("summary", 2, 2),
    ]
    assert "wrote" in rep.text() and "joined|join|7->4 rows" in rep.text()


def test_run_creates_the_output_directory_and_overwrites(book, tmp_path) -> None:
    out = tmp_path / "a" / "b"
    assert run(_pipeline(), {"f": book}, out).status == "ok"
    first = (out / "detail.parquet").stat().st_size
    assert run(_pipeline(), {"f": book}, out).status == "ok"
    assert (out / "detail.parquet").stat().st_size == first
    assert sorted(p.name for p in out.iterdir()) == ["detail.parquet", "summary.csv"]


def test_run_accepts_paths_and_bytes(tmp_path) -> None:
    data = make_xlsx({"Orders": _ORDERS})
    (tmp_path / "in.xlsx").write_bytes(data)
    spec = _spec(sources={"o": _src("Orders", "Order")}, outputs={"o": {"input": "o"}})
    assert run(spec, {"f": tmp_path / "in.xlsx"}, tmp_path / "o1").status == "ok"
    assert run(spec, {"f": str(tmp_path / "in.xlsx")}, tmp_path / "o2").status == "ok"
    assert run(spec, {"f": data}, tmp_path / "o3").status == "ok"


def test_run_text_values_and_odd_names_survive_the_round_trip(tmp_path) -> None:
    rows = [["it's", 'a "q"', "JAN|PKG"], ['é中, "x"\nline', None, 1.5], ["b", "c", None]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    spec = _spec(
        sources={"s": _src("S", "it's")},
        outputs={"p": {"input": "s"}, "c": {"input": "s", "format": "csv"}},
    )
    assert run(spec, {"f": wb}, tmp_path).status == "ok"
    con = duckdb.connect()
    got = con.execute(
        f"select * from read_parquet('{(tmp_path / 'p.parquet').as_posix()}')"
    ).fetchall()
    assert got == [('é中, "x"\nline', None, 1.5), ("b", "c", None)]
    assert [
        d[0]
        for d in con.execute(
            f"select * from read_parquet('{(tmp_path / 'p.parquet').as_posix()}')"
        ).description
    ] == [
        "it's",
        'a "q"',
        "JAN|PKG",
    ]
    with open(tmp_path / "c.csv", newline="", encoding="utf-8") as f:
        assert list(csv.reader(f))[1] == ['é中, "x"\nline', "", "1.5"]


# --- run: failures leave nothing behind ------------------------------------------------------


def test_failed_run_writes_nothing_and_reports_all(book, tmp_path) -> None:
    spec = _spec(
        sources={"orders": _src("Orders", "Order")},
        steps=[
            {"id": "ok", "node": "select", "inputs": ["orders"], "config": {"columns": ["Order"]}},
            {"id": "bad", "node": "select", "inputs": ["orders"], "config": {"columns": ["zzz"]}},
        ],
        outputs={"good": {"input": "ok"}},
    )
    rep = run(spec, {"f": book}, tmp_path)
    assert rep.status == "failed" and rep.outputs == ()
    assert [i.where for i in rep.issues] == ["steps.bad"]
    assert list(tmp_path.iterdir()) == []


def test_a_cast_error_midway_leaves_earlier_outputs_unwritten(tmp_path) -> None:
    wb = Workbook.open(make_xlsx({"S": [["k", "v"], ["a", "1"], ["b", "x"]]}))
    spec = _spec(
        sources={"s": _src("S", "k")},
        steps=[
            {
                "id": "c",
                "node": "cast",
                "inputs": ["s"],
                "config": {"types": {"v": "integer"}, "strict": True},
            }
        ],
        outputs={"first": {"input": "s"}, "second": {"input": "c"}},
    )
    rep = run(spec, {"f": wb}, tmp_path)
    assert rep.status == "failed" and "steps.c" in rep.issues[0].where
    assert list(tmp_path.iterdir()) == []


def test_a_step_that_fails_at_run_lists_no_followup_noise(tmp_path) -> None:
    wb = Workbook.open(make_xlsx({"S": [["k", "v"], ["a", "1"], ["b", "x"]]}))
    spec = _spec(
        sources={"s": _src("S", "k")},
        steps=[
            {
                "id": "c",
                "node": "cast",
                "inputs": ["s"],
                "config": {"types": {"v": "integer"}, "strict": True},
            },
            {
                "id": "d",
                "node": "derive",
                "inputs": ["c"],
                "config": {"name": "w", "expr": "v * 2"},
            },
        ],
        outputs={"o": {"input": "d"}},
    )
    assert validate(spec, {"f": wb}).ok  # the problem only shows up at run time
    rep = run(spec, {"f": wb}, tmp_path)
    assert rep.status == "failed"
    assert [i.where for i in rep.issues] == ["steps.c"]
    assert list(tmp_path.iterdir()) == []


def test_a_failing_write_removes_the_files_already_written(book, tmp_path) -> None:
    spec = _spec(
        sources={"orders": _src("Orders", "Order")},
        outputs={"one": {"input": "orders"}, "two": {"input": "orders"}},
    )
    (tmp_path / "two.parquet.part").mkdir()  # a directory where the temporary file must go
    rep = run(spec, {"f": book}, tmp_path)
    assert rep.status == "failed" and rep.issues[0].where == "outputs.two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["two.parquet.part"]
    assert not (tmp_path / "one.parquet").exists() and not (tmp_path / "one.parquet.part").exists()


def test_a_failed_run_keeps_the_previous_good_output(book, tmp_path) -> None:
    good = _spec(sources={"o": _src("Orders", "Order")}, outputs={"o": {"input": "o"}})
    assert run(good, {"f": book}, tmp_path).status == "ok"
    before = (tmp_path / "o.parquet").read_bytes()
    broken = _spec(
        sources={"o": _src("Orders", "Order")},
        steps=[{"id": "x", "node": "select", "inputs": ["o"], "config": {"columns": ["zzz"]}}],
        outputs={"o": {"input": "o"}},
    )
    assert run(broken, {"f": book}, tmp_path).status == "failed"
    assert (tmp_path / "o.parquet").read_bytes() == before


def test_run_cannot_start_without_files_dict_or_with_a_file_as_out_dir(book, tmp_path) -> None:
    spec = _spec(sources={"o": _src("Orders", "Order")})
    with pytest.raises(PipelineError, match="files must be a dict"):
        run(spec, [book], tmp_path)  # type: ignore[arg-type]
    (tmp_path / "f").write_text("x")
    with pytest.raises(PipelineError, match="cannot use"):
        run(spec, {"f": book}, tmp_path / "f")


# --- the sandbox -----------------------------------------------------------------------------


def test_a_run_can_only_write_inside_out_dir(book, tmp_path) -> None:
    from excel_etl.nodes.base import connect

    out = tmp_path / "out"
    out.mkdir()
    con = connect(out)
    con.execute("create table t as select 1 as a")
    con.execute(f"COPY t TO '{(out / 'inside.csv').as_posix()}' (FORMAT CSV)")
    for outside in (tmp_path / "up.csv", out / ".." / "esc.csv", tmp_path / "out2.csv"):
        with pytest.raises(Exception, match="(?i)permission|disabled|not allowed"):
            con.execute(f"COPY t TO '{outside.as_posix()}' (FORMAT CSV)")
    with pytest.raises(Exception, match="(?i)cannot|disabled"):
        con.execute(f"SET allowed_directories=['{tmp_path.as_posix()}']")
    assert not (tmp_path / "up.csv").exists() and not (tmp_path / "esc.csv").exists()


@pytest.mark.parametrize(
    "name", ["con", "NUL", "com1", "Lpt9", "aux", "prn", "__x", "a b", "a/b", "..", "a.b", ""]
)
def test_output_names_cannot_become_bad_file_names(name: str) -> None:
    with pytest.raises(SpecError):
        _spec(sources={"s": _src("S", "x")}, outputs={name: {"input": "s"}})


def test_names_that_differ_only_in_case_are_refused() -> None:
    with pytest.raises(SpecError, match="differ only in letter case"):
        _spec(sources={"Data": _src("S", "x"), "data": _src("S", "x")})
    with pytest.raises(SpecError, match="would be the same file"):
        _spec(sources={"s": _src("S", "x")}, outputs={"Out": {"input": "s"}, "out": {"input": "s"}})
    with pytest.raises(SpecError, match="differ only in letter case"):
        _spec(
            sources={"data": _src("S", "x")},
            steps=[
                {"id": "DATA", "node": "select", "inputs": ["data"], "config": {"columns": ["x"]}}
            ],
        )


def test_a_spec_cannot_name_a_relation_with_the_helper_prefix() -> None:
    with pytest.raises(SpecError):
        _spec(sources={"__v1": _src("S", "x")})
    with pytest.raises(SpecError):
        _spec(sources={"s": _src("S", "x")}, outputs={"a" * 65: {"input": "s"}})


# --- more than one file, and the order of rows ----------------------------------------------


def test_two_files_one_run(tmp_path) -> None:
    a = Workbook.open(make_xlsx({"S": [["k", "v"], ["a", 1], ["b", 2]]}))
    b = Workbook.open(make_xlsx({"T": [["k", "w"], ["a", 10], ["c", 30]]}))
    spec = _spec(
        sources={"left": _src("S", "k", role="jan"), "right": _src("T", "k", role="feb")},
        steps=[
            {
                "id": "j",
                "node": "join",
                "inputs": ["left", "right"],
                "config": {"on": ["k"], "how": "full"},
            }
        ],
        outputs={"j": {"input": "j", "format": "csv"}},
    )
    rep = run(spec, {"jan": a, "feb": b}, tmp_path)
    assert rep.status == "ok"
    with open(tmp_path / "j.csv", newline="", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [
            ["k", "v", "w"],
            ["a", "1", "10"],
            ["b", "2", ""],
            ["c", "", "30"],
        ]


def test_two_files_with_the_same_layout_are_unioned(tmp_path) -> None:
    jan = make_xlsx({"Data": [["k", "v"], ["a", 1], ["b", 2]]})
    feb = make_xlsx({"Data": [["k", "v"], ["c", 3], ["d", 4]]})
    spec = _spec(
        sources={"m1": _src("Data", "k", role="jan"), "m2": _src("Data", "k", role="feb")},
        steps=[
            {
                "id": "all",
                "node": "union",
                "inputs": ["m1", "m2"],
                "config": {"source_column": "month"},
            }
        ],
        outputs={"all": {"input": "all", "format": "csv"}},
    )
    files = {"jan": jan, "feb": feb}
    assert validate(spec, files).ok
    rep = run(spec, files, tmp_path)
    assert rep.status == "ok"
    with open(tmp_path / "all.csv", newline="", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [
            ["k", "v", "month"],
            ["a", "1", "0"],
            ["b", "2", "0"],
            ["c", "3", "1"],
            ["d", "4", "1"],
        ]
    assert [n.id for n in rep.nodes if n.kind == "source"] == ["m1", "m2"]


def test_row_order_survives_a_whole_pipeline_on_a_large_sheet(tmp_path) -> None:
    n = 20000
    rows = [["id", "grp", "v"], *[[i, i % 7, float(i)] for i in range(n)]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    spec = _spec(
        sources={"s": _src("S", "id")},
        steps=[
            {"id": "f", "node": "filter", "inputs": ["s"], "config": {"where": "v >= 0"}},
            {
                "id": "d",
                "node": "derive",
                "inputs": ["f"],
                "config": {"name": "w", "expr": "v * 2"},
            },
            {"id": "u", "node": "union", "inputs": ["d", "d"], "config": {"source_column": "src"}},
            {"id": "k", "node": "dedupe", "inputs": ["u"], "config": {"keys": ["id"]}},
        ],
        outputs={"k": {"input": "k"}},
    )
    rep = run(spec, {"f": wb}, tmp_path)
    assert rep.status == "ok", rep.text()
    got = (
        duckdb.connect()
        .execute(f"select id from read_parquet('{(tmp_path / 'k.parquet').as_posix()}')")
        .fetchall()
    )
    assert [r[0] for r in got] == list(range(n))


def test_a_date_column_and_empty_cells_reach_the_file(tmp_path) -> None:
    rows = [["day", "n"], [date(2026, 1, 2), 1], [date(2026, 1, 3), None]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    spec = _spec(sources={"s": _src("S", "day")}, outputs={"s": {"input": "s"}})
    assert run(spec, {"f": wb}, tmp_path).status == "ok"
    got = (
        duckdb.connect()
        .execute(f"select * from read_parquet('{(tmp_path / 's.parquet').as_posix()}')")
        .fetchall()
    )
    assert [r[0] for r in got] == [date(2026, 1, 2), date(2026, 1, 3)] and got[1][1] is None


def test_hugeint_to_parquet_gets_a_warning(tmp_path) -> None:
    wb = Workbook.open(make_xlsx({"S": [["k", "v"], ["a", 1]]}))
    spec = _spec(
        sources={"s": _src("S", "k")},
        steps=[
            {
                "id": "p",
                "node": "pivot",
                "inputs": ["s"],
                "config": {"column": "k", "value": "v", "values": ["a"]},
            }
        ],
        outputs={"p": {"input": "p"}},
    )
    rep = run(spec, {"f": wb}, tmp_path)
    assert rep.status == "ok" and any("HUGEINT" in w for w in rep.warnings)


@pytest.mark.sample
def test_sample_pipeline_runs_end_to_end(sample_path, tmp_path) -> None:
    spec = _spec(
        sources={
            "pkg": {
                "file_role": "b",
                "sheet": {"name_like": "(N) PKG"},
                "region": {"header_anchor": ["TANGGAL", "Phonska 1"], "header_rows": 2},
            }
        },
        steps=[
            {
                "id": "days",
                "node": "drop_rows",
                "inputs": ["pkg"],
                "config": {"column": "TANGGAL", "keep_type": "date"},
            }
        ],
        outputs={"days": {"input": "days"}},
    )
    wb = Workbook.open(sample_path)
    assert validate(spec, {"b": wb}).ok
    rep = run(spec, {"b": wb}, tmp_path)
    assert rep.status == "ok" and rep.nodes[-1].rows_out == 730


@pytest.mark.parametrize(
    "head", [["Ton", "Ton", "Ton (2)"], ["Ton", "TON", "ton"], ["a", "A", "a (2)"]]
)
def test_duplicate_headers_in_any_case_load_with_unique_names(head, tmp_path) -> None:
    f = make_xlsx({"S": [head, [1, 2, 3]]})
    spec = _spec(
        sources={"s": _src("S", head[0])},
        outputs={"o": {"input": "s", "format": "csv"}},
    )
    rep = validate(spec, {"f": f})
    assert rep.ok, rep.text()
    assert len({c.casefold() for c in (c.name for c in rep.schemas["s"])}) == 3
    assert run(spec, {"f": f}, tmp_path).status == "ok"


def test_group_by_output_name_clashing_only_in_case_is_caught_by_validate() -> None:
    f = make_xlsx({"S": [["Key", "Ton"], ["a", 1], ["a", 2]]})
    spec = _spec(
        sources={"s": _src("S", "Key")},
        steps=[
            {
                "id": "g",
                "node": "group_by",
                "inputs": ["s"],
                "config": {"by": ["Key"], "aggs": [{"name": "TON", "fn": "sum", "column": "Ton"}]},
            },
        ],
    )
    assert validate(spec, {"f": f}).ok  # TON vs Ton: not a clash, Ton is not an output
    spec2 = _spec(
        sources={"s": _src("S", "Key")},
        steps=[
            {
                "id": "g",
                "node": "group_by",
                "inputs": ["s"],
                "config": {"by": ["Key"], "aggs": [{"name": "key", "fn": "count_rows"}]},
            },
        ],
    )
    assert "twice" in validate(spec2, {"f": f}).text()


def test_wrong_input_count_is_caught_even_without_known_schemas() -> None:
    spec = _spec(
        sources={"s": _src("S", "a")},
        steps=[
            {"id": "j", "node": "join", "inputs": ["s"], "config": {"on": ["a"], "how": "left"}}
        ],
    )
    rep = validate(spec)
    assert not rep.ok
    assert "input" in rep.text()


def test_validate_text_names_the_columns_and_bad_files_become_an_issue() -> None:
    f = make_xlsx({"S": [["Key", "Ton"], ["a", 1]]})
    spec = _spec(sources={"s": _src("S", "Key")})
    assert "s: Key, Ton" in validate(spec, {"f": f}).text()
    rep = validate(spec, ["x"])  # type: ignore[arg-type]
    assert not rep.ok and rep.issues[0].where == "files"
