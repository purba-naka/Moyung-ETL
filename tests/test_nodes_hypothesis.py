from __future__ import annotations

from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_nodes_differential import apply, same_rows, table

from moyung.nodes.base import connect

CFG = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
CELL = st.one_of(st.none(), st.integers(-5, 5))
ROWS = st.lists(st.tuples(CELL, CELL), max_size=25)
COLS = [("g", "INTEGER"), ("v", "INTEGER")]


def fresh(*named):
    con = connect()
    for name, columns, rows in named:
        table(con, name, columns, rows)
    return con


@CFG
@given(ROWS, st.integers(-5, 5))
def test_filter_matches_python_and_never_invents_rows(rows, cut):
    con = fresh(("S", COLS, rows))
    try:
        got = apply(con, "filter", {"where": f"v > {cut}"}, "S")
    finally:
        con.close()
    want = [r for r in rows if r[1] is not None and r[1] > cut]
    assert Counter(got) == Counter(want)


@CFG
@given(ROWS)
def test_group_by_sum_and_count_match_python(rows):
    con = fresh(("S", COLS, rows))
    aggs = [
        {"name": "n", "fn": "count_rows"},
        {"name": "s", "fn": "sum", "column": "v"},
    ]
    try:
        got = {r[0]: r[1:] for r in apply(con, "group_by", {"by": ["g"], "aggs": aggs}, "S")}
    finally:
        con.close()
    want = {}
    for g, v in rows:
        n, s = want.get(g, (0, None))
        want[g] = (n + 1, s if v is None else (s or 0) + v)
    assert got == want


@CFG
@given(ROWS, ROWS)
def test_union_keeps_every_row_and_distinct_removes_only_repeats(a, b):
    con = fresh(("A", COLS, a), ("B", COLS, b))
    try:
        plain = apply(con, "union", {}, "A", "B")
        dedup = apply(con, "union", {"distinct": True}, "A", "B")
    finally:
        con.close()
    assert Counter(plain) == Counter(a) + Counter(b)
    assert Counter(dedup) == Counter(set(a) | set(b))


@CFG
@given(ROWS, ROWS)
def test_inner_join_matches_plain_sql(a, b):
    con = fresh(
        ("L", [("k", "INTEGER"), ("a", "INTEGER")], a),
        ("R", [("k", "INTEGER"), ("b", "INTEGER")], b),
    )
    try:
        got = apply(con, "join", {"on": ["k"], "how": "inner"}, "L", "R")
    finally:
        con.close()
    want = [(k, x, y) for k, x in a for k2, y in b if k is not None and k == k2]
    assert same_rows(got, want)


@CFG
@given(ROWS)
def test_dedupe_on_a_key_leaves_one_row_per_key_and_only_original_rows(rows):
    con = fresh(("S", COLS, rows))
    try:
        got = apply(con, "dedupe", {"keys": ["g"]}, "S")
    finally:
        con.close()
    assert {r[0] for r in got} == {r[0] for r in rows}
    assert len({r[0] for r in got}) == len(got)
    assert not Counter(got) - Counter(rows)
