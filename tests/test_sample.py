from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import make_xlsx

from excel_etl.inspect.encode import sample_data
from excel_etl.nodes.base import NodeError, connect
from excel_etl.sample import MAX_ROWS, STRATEGIES, sample_table
from excel_etl.workbook import Workbook, WorkbookError


def _rows() -> list[list]:
    out = [["id", "grp", "amt", "note"]]
    for i in range(1, 41):
        out.append([i, "A" if i <= 30 else "B", float(i), None if i % 4 else "n"])
    out.append([40, "B", 40.0, "n"])
    return out


def _wb() -> Workbook:
    return Workbook.open(make_xlsx({"S": _rows()}))


def _con():
    con = connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1,'A',1.0,NULL),(2,'A',2.0,'x'),"
        "(3,'B',3.0,NULL),(3,'B',3.0,NULL),(4,'B',NULL,'y')) v(id, grp, amt, note)"
    )
    return con


def _ids(res: dict) -> list[int]:
    return [r[0] for r in res["rows"]]


def test_head_tail_head_tail_keep_table_order():
    con = _con()
    try:
        assert _ids(sample_table(con, "t", "head", n=2)) == [1, 2]
        assert _ids(sample_table(con, "t", "tail", n=2)) == [3, 4]
        assert _ids(sample_table(con, "t", "head_tail", n=4)) == [1, 2, 3, 4]
        assert _ids(sample_table(con, "t", "head_tail", n=2)) == [1, 4]
    finally:
        con.close()


def test_random_is_reproducible_for_a_seed_and_differs_between_seeds():
    con = _con()
    try:
        a = sample_table(con, "t", "random", n=3, seed=7)
        assert a == sample_table(con, "t", "random", n=3, seed=7)
        assert len(a["rows"]) == 3 and a["seed"] == 7
        picks = {tuple(_ids(sample_table(con, "t", "random", n=2, seed=s))) for s in range(12)}
        assert len(picks) > 1
    finally:
        con.close()


def test_random_keeps_table_order_and_never_repeats_a_row():
    con = _con()
    try:
        for seed in range(10):
            got = sample_table(con, "t", "random", n=4, seed=seed)
            assert got["positions"] == sorted(set(got["positions"]))
    finally:
        con.close()


def test_n_larger_than_table_returns_all_rows_once():
    con = _con()
    try:
        for strategy in ("head", "tail", "head_tail", "random"):
            assert len(sample_table(con, "t", strategy, n=50, seed=1)["rows"]) == 5, strategy
    finally:
        con.close()


def test_stratified_takes_up_to_n_per_group_and_reports_group_sizes():
    con = _con()
    try:
        res = sample_table(con, "t", "stratified", column="grp", n=1, seed=3)
        assert sorted(r[1] for r in res["rows"]) == ["A", "B"]
        assert res["groups"] == {"A": 2, "B": 3}
        assert res == sample_table(con, "t", "stratified", column="grp", n=1, seed=3)
    finally:
        con.close()


def test_stratified_never_returns_more_than_the_row_cap_and_says_so():
    con = connect()
    try:
        con.execute(
            "CREATE TABLE big AS SELECT range AS id, 'g' || (range % 600) AS g FROM range(6000)"
        )
        res = sample_table(con, "big", "stratified", column="g", n=5, seed=2)
        assert len(res["rows"]) == MAX_ROWS
        assert (
            res["rows"] == sample_table(con, "big", "stratified", column="g", n=5, seed=2)["rows"]
        )
        assert len(res["groups"]) == 600
        assert "capped at 1000" in res["basis"]
        small = sample_table(con, "big", "stratified", column="g", n=1, seed=2)
        assert len(small["rows"]) == 600 and "capped" not in small["basis"]
    finally:
        con.close()


def test_stratified_null_group_is_its_own_group():
    con = _con()
    try:
        res = sample_table(con, "t", "stratified", column="note", n=1, seed=1)
        assert res["groups"] == {"<null>": 3, "x": 1, "y": 1}
    finally:
        con.close()


def test_where_uses_the_filter_expression_rules():
    con = _con()
    try:
        assert _ids(sample_table(con, "t", "where", where="amt >= 3", n=10)) == [3, 3]
        assert sample_table(con, "t", "where", where="amt > 99", n=10)["rows"] == []
        for bad in ("(SELECT 1) = 1", "count(*) > 1", "amt", "no_such > 1", "1; DROP TABLE t"):
            with pytest.raises(NodeError):
                sample_table(con, "t", "where", where=bad, n=5)
        assert con.execute("SELECT count(*) FROM t").fetchall()[0][0] == 5
    finally:
        con.close()


def test_nulls_and_duplicates_find_the_rows_to_look_at():
    con = _con()
    try:
        assert _ids(sample_table(con, "t", "nulls", column="amt", n=10)) == [4]
        assert _ids(sample_table(con, "t", "duplicates", n=10)) == [3, 3]
    finally:
        con.close()


def test_every_result_says_how_many_rows_matched_and_the_basis():
    con = _con()
    try:
        res = sample_table(con, "t", "nulls", column="note", n=1)
        assert res["matched"] == 3 and len(res["rows"]) == 1
        assert res["basis"] == "nulls(note), 3 rows matched, 1 shown"
        assert res["columns"] == ["id", "grp", "amt", "note"]
        json.dumps(res)
    finally:
        con.close()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"strategy": "median"}, "strategy"),
        ({"strategy": "head", "n": 0}, "n must"),
        ({"strategy": "head", "n": True}, "n must"),
        ({"strategy": "head", "n": 10**9}, "n must"),
        ({"strategy": "random", "seed": "x"}, "seed"),
        ({"strategy": "stratified"}, "column"),
        ({"strategy": "stratified", "column": "nope"}, "nope"),
        ({"strategy": "nulls"}, "column"),
        ({"strategy": "where"}, "where"),
        ({"strategy": "head", "where": "id > 1"}, "where"),
        ({"strategy": "head", "column": "id"}, "column"),
    ],
)
def test_bad_arguments_are_refused_with_the_reason(kwargs, match):
    con = _con()
    try:
        with pytest.raises(NodeError, match=match):
            sample_table(con, "t", **{"n": 2, **kwargs})
    finally:
        con.close()


def test_strategies_are_listed_once():
    assert STRATEGIES == (
        "head", "tail", "head_tail", "random", "stratified", "where", "nulls", "duplicates",
    )  # fmt: skip


def test_hostile_column_names_work():
    con = connect()
    try:
        con.execute('CREATE TABLE t ("a|b" INTEGER, "x""y" VARCHAR)')
        con.execute("INSERT INTO t VALUES (1, 'q'), (2, NULL)")
        assert sample_table(con, "t", "nulls", column='x"y', n=5)["rows"] == [[2, None]]
        assert sample_table(con, "t", "stratified", column="a|b", n=1, seed=1)["groups"] == {
            "1": 1,
            "2": 1,
        }
    finally:
        con.close()


def test_sample_data_on_a_region_shows_basis_provenance_and_rows():
    out = sample_data(_wb(), "F1", "F1.S1.R1", strategy="where", where="grp = 'B'", n=3)
    lines = out["text"].splitlines()
    assert lines[0].startswith("F1.S1.R1 S A1:D42: where(grp = 'B'), 11 rows matched, 3 shown")
    assert "sha " in lines[0] and "at 20" in lines[0] and lines[0].endswith("Z")
    assert lines[1] == "id|grp|amt|note"
    assert lines[2].startswith("31|B|31")
    assert len(lines) == 5


def test_sample_data_random_names_seed_in_the_basis():
    out = sample_data(_wb(), "F1", "F1.S1.R1", strategy="random", n=2, seed=5)
    assert "random(seed=5)" in out["text"].splitlines()[0]
    again = sample_data(_wb(), "F1", "F1.S1.R1", strategy="random", n=2, seed=5)
    assert out["text"].splitlines()[1:] == again["text"].splitlines()[1:]


def test_sample_data_values_are_text_safe_and_dates_iso():
    rows = [["d", "t"], [date(2026, 1, 2), "a|b\nc"], [date(2026, 1, 3), None]]
    wb = Workbook.open(make_xlsx({"S": rows}))
    lines = sample_data(wb, "F1", "F1.S1.R1", n=5)["text"].splitlines()
    assert lines[2] == "2026-01-02|a/b c"
    assert lines[3] == "2026-01-03|"


def test_sample_data_budget_cuts_rows_and_offset_continues():
    first = sample_data(_wb(), "F1", "F1.S1.R1", n=40, budget=300)
    assert first["truncated"] is True and first["next_offset"]
    rest = sample_data(_wb(), "F1", "F1.S1.R1", n=40, budget=300, offset=first["next_offset"])
    assert rest["text"].splitlines()[2] != first["text"].splitlines()[2]


def test_sample_data_errors_are_workbook_errors():
    wb = _wb()
    with pytest.raises(WorkbookError, match="strategy"):
        sample_data(wb, "F1", "F1.S1.R1", strategy="median")
    with pytest.raises(WorkbookError, match="subquery"):
        sample_data(wb, "F1", "F1.S1.R1", strategy="where", where="(SELECT 1)")
    with pytest.raises(WorkbookError, match="region id"):
        sample_data(wb, "F1", "S")


def test_random_gives_the_same_rows_in_another_process():
    import subprocess
    import sys

    code = (
        "from excel_etl.nodes.base import connect\n"
        "from excel_etl.sample import sample_table\n"
        "con = connect()\n"
        'con.execute("CREATE TABLE t AS SELECT range AS id FROM range(5000)")\n'
        "print(sample_table(con, 't', 'random', n=8, seed=11)['positions'])\n"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout
        for _ in range(2)
    }
    con = connect()
    try:
        con.execute("CREATE TABLE t AS SELECT range AS id FROM range(5000)")
        here = str(sample_table(con, "t", "random", n=8, seed=11)["positions"]) + "\n"
    finally:
        con.close()
    assert runs == {here}
