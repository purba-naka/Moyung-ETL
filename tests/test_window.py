"""The `window` node, checked against a plain-Python reference on several data shapes."""

from __future__ import annotations

import random
import time
from functools import cmp_to_key

import pytest

from excel_etl.nodes import NodeError, build, infer, schema_of
from excel_etl.nodes.base import connect

_COLS = "grp VARCHAR, sub VARCHAR, seq INTEGER, qty INTEGER, price INTEGER"
_NAMES = ["grp", "sub", "seq", "qty", "price"]


def _rel(rows: list[tuple], cols: str = _COLS):
    con = connect()
    con.execute(f"CREATE TABLE t ({cols})")
    if rows:
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' * len(rows[0]))})", rows)
    return con.sql("SELECT * FROM t")


def _run(config: dict, rel):
    n = build("window", config)
    out = n.run([rel])
    assert infer(n, [schema_of(rel)]) == schema_of(out)  # the contract, on every case
    return out


def _last(config: dict, rows: list[tuple]) -> list:
    return [r[-1] for r in _run(config, _rel(rows)).fetchall()]


def _err(config: dict, rel) -> str:
    with pytest.raises(NodeError) as e:
        infer(build("window", config), [schema_of(rel)])
    return str(e.value)


# --- the reference: the slow, obvious way ------------------------------------------------------


def _cmp(a, b):
    if a is None or b is None:
        return (a is None) - (b is None)  # NULL last
    return (a > b) - (a < b)


def _reference(rows, cfg):
    at = {n: i for i, n in enumerate(_NAMES)}
    parts, by = cfg.get("partition_by", []), cfg.get("order_by", [])
    fn, col = cfg["fn"], cfg.get("column")
    keys = [(at[b.lstrip("-")], b.startswith("-")) for b in by]

    def compare(i, j):
        for c, desc in keys:
            a, b = rows[i][c], rows[j][c]
            r = _cmp(a, b)
            if r:
                return -r if desc and a is not None and b is not None else r
        return 0

    groups: dict[tuple, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(tuple(row[at[p]] for p in parts), []).append(i)
    out: list = [None] * len(rows)
    for idx in groups.values():
        ordered = sorted(idx, key=cmp_to_key(lambda i, j: compare(i, j) or (i - j)))
        vals = [rows[i][at[col]] if col else None for i in ordered]
        whole = [v for v in vals if v is not None]
        for k, i in enumerate(ordered):
            seen = [v for v in vals[: k + 1] if v is not None] if by else whole
            if fn == "sum":
                out[i] = sum(seen) if seen else None
            elif fn == "count":
                out[i] = len(seen)
            elif fn == "min":
                out[i] = min(seen) if seen else None
            elif fn == "max":
                out[i] = max(seen) if seen else None
            elif fn == "avg":
                out[i] = sum(seen) / len(seen) if seen else None
            elif fn == "row_number":
                out[i] = k + 1
            elif fn in ("rank", "dense_rank"):
                before = [j for j in ordered if compare(j, i) < 0]
                distinct = {tuple(rows[j][c] for c, _ in keys) for j in before}
                out[i] = len(before) + 1 if fn == "rank" else len(distinct) + 1
            elif fn in ("lag", "lead"):
                step = cfg.get("offset", 1)
                p = k - step if fn == "lag" else k + step
                out[i] = vals[p] if 0 <= p < len(vals) else None
            elif fn == "first_value":
                out[i] = whole[0] if whole else None
            elif fn == "last_value":
                out[i] = whole[-1] if whole else None
            elif fn == "share":
                v, total = vals[k], sum(whole)
                out[i] = None if v is None or total == 0 else v / total
    return out


def _data(seed: int, n: int, kind: str) -> list[tuple]:
    """Rows shaped by `kind`: many small groups, one group, NULL group keys, or many ties."""
    r = random.Random(seed)
    rows = []
    for _ in range(n):
        if kind == "many_small_groups":
            grp = r.choice("abcdefgh") + str(r.randrange(20))
        elif kind == "one_group":
            grp = "only"
        elif kind == "null_groups":
            grp = r.choice([None, None, "x", "y"])
        else:
            grp = r.choice(["x", "y"])
        seq = r.randrange(5) if kind == "ties" else r.randrange(1000)
        qty = (
            r.randrange(100)
            if kind == "one_group"
            else r.choice([None, None, r.randrange(-50, 100)])
        )
        rows.append((grp, r.choice([None, "p", "q"]), seq, qty, r.randrange(1, 9)))
    return rows


_KINDS = ["many_small_groups", "one_group", "null_groups", "ties"]
_CONFIGS = [
    {"fn": "sum", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "sum", "column": "qty", "partition_by": ["grp"]},
    {"fn": "sum", "column": "qty", "order_by": ["-seq"]},
    {"fn": "count", "column": "qty", "partition_by": ["grp", "sub"], "order_by": ["seq"]},
    {"fn": "min", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "max", "column": "qty", "partition_by": ["grp"]},
    {"fn": "avg", "column": "qty", "partition_by": ["grp"], "order_by": ["seq", "-price"]},
    {"fn": "row_number", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "row_number"},
    {"fn": "rank", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "rank", "order_by": ["-qty"]},
    {"fn": "dense_rank", "partition_by": ["grp"], "order_by": ["-seq", "qty"]},
    {"fn": "lag", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "lead", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"], "offset": 2},
    {"fn": "lag", "column": "sub", "partition_by": ["grp"], "order_by": ["-seq"], "offset": 3},
    {"fn": "first_value", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "last_value", "column": "qty", "partition_by": ["grp"], "order_by": ["seq"]},
    {"fn": "share", "column": "qty", "partition_by": ["grp"]},
    {"fn": "share", "column": "price"},
]


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("cfg", _CONFIGS, ids=lambda c: "-".join(f"{k}={v}" for k, v in c.items()))
def test_matches_the_reference_on_every_data_shape(kind: str, cfg: dict) -> None:
    rows = _data(7, 400, kind)
    got = _run({"name": "w", **cfg}, _rel(rows)).fetchall()
    assert [r[:5] for r in got] == rows  # every row, the same order, old columns untouched
    for g, w in zip([r[5] for r in got], _reference(rows, cfg), strict=True):
        assert g == pytest.approx(w) if isinstance(w, float) and g is not None else g == w


# --- edges --------------------------------------------------------------------------------------


def test_empty_table_keeps_its_columns() -> None:
    out = _run({"name": "w", "fn": "sum", "column": "qty", "order_by": ["seq"]}, _rel([]))
    assert out.columns == [*_NAMES, "w"]
    assert out.fetchall() == []


def test_a_group_of_only_nulls_gives_null_sums_and_zero_counts() -> None:
    rows = [("a", None, 1, None, 1), ("a", None, 2, None, 1)]
    assert (
        _last({"name": "w", "fn": "sum", "column": "qty", "order_by": ["seq"]}, rows) == [None] * 2
    )
    assert (
        _last({"name": "w", "fn": "count", "column": "qty", "order_by": ["seq"]}, rows) == [0] * 2
    )
    assert _last({"name": "w", "fn": "last_value", "column": "qty"}, rows) == [None] * 2


def test_share_is_null_when_the_group_adds_to_zero_never_infinite() -> None:
    rows = [("a", None, 1, 5, 1), ("a", None, 2, -5, 1), ("b", None, 3, 4, 1)]
    cfg = {"name": "w", "fn": "share", "column": "qty", "partition_by": ["grp"]}
    assert _last(cfg, rows) == [None, None, 1.0]


def test_ties_follow_table_order_for_running_values_and_share_a_rank() -> None:
    rows = [("a", None, 1, 10, 1), ("a", None, 1, 20, 1), ("a", None, 0, 5, 1)]
    assert _last({"name": "w", "fn": "sum", "column": "qty", "order_by": ["seq"]}, rows) == [
        15,
        35,
        5,
    ]
    assert _last({"name": "w", "fn": "rank", "order_by": ["seq"]}, rows) == [2, 2, 1]
    assert _last({"name": "w", "fn": "row_number", "order_by": ["seq"]}, rows) == [2, 3, 1]


def test_descending_puts_nulls_last_too() -> None:
    rows = [("a", None, None, 1, 1), ("a", None, 3, 1, 1), ("a", None, 7, 1, 1)]
    assert _last({"name": "w", "fn": "row_number", "order_by": ["-seq"]}, rows) == [3, 2, 1]


def test_an_existing_column_is_replaced_in_place() -> None:
    rows = [("a", "p", 1, 3, 2), ("a", "q", 2, 4, 2)]
    out = _run({"name": "qty", "fn": "sum", "column": "qty", "order_by": ["seq"]}, _rel(rows))
    assert out.columns == _NAMES
    assert [r[3] for r in out.fetchall()] == [3, 7]


def test_awkward_column_names_work_and_a_real_dash_column_beats_the_marker() -> None:
    con = connect()
    con.execute('CREATE TABLE s ("JAN|Ton" INTEGER, "my ""g""" VARCHAR, "-x" INTEGER)')
    con.execute("INSERT INTO s VALUES (1,'a',9), (2,'a',8), (3,'b',7)")
    cfg = {
        "name": "a b",
        "fn": "sum",
        "column": "JAN|Ton",
        "partition_by": ['my "g"'],
        "order_by": ["-x"],  # a real column, so ascending: 8 comes before 9
    }
    out = _run(cfg, con.sql("SELECT * FROM s"))
    assert out.columns[-1] == "a b"
    assert [r[3] for r in out.fetchall()] == [3, 2, 3]


@pytest.mark.parametrize(
    ("cfg", "col_type", "out_type"),
    [
        ({"fn": "sum", "column": "v"}, "INTEGER", "BIGINT"),
        ({"fn": "sum", "column": "v"}, "DECIMAL(10,2)", "DECIMAL(38,2)"),
        ({"fn": "sum", "column": "v"}, "DOUBLE", "DOUBLE"),
        ({"fn": "avg", "column": "v"}, "INTEGER", "DOUBLE"),
        ({"fn": "min", "column": "v"}, "DATE", "DATE"),
        ({"fn": "max", "column": "v"}, "VARCHAR", "VARCHAR"),
        ({"fn": "lag", "column": "v"}, "DATE", "DATE"),
        ({"fn": "share", "column": "v"}, "DECIMAL(10,2)", "DOUBLE"),
        ({"fn": "count", "column": "v"}, "VARCHAR", "BIGINT"),
        ({"fn": "row_number"}, "INTEGER", "BIGINT"),
    ],
)
def test_output_type(cfg: dict, col_type: str, out_type: str) -> None:
    rel = _rel([], f"v {col_type}")
    out = _run({"name": "w", **cfg}, rel)
    assert str(out.types[-1]) == out_type


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "needle"),
    [
        ({"fn": "rank"}, "needs order_by"),
        ({"fn": "dense_rank", "column": "qty", "order_by": ["seq"]}, "takes no column"),
        ({"fn": "row_number", "column": "qty"}, "takes no column"),
        ({"fn": "sum"}, "needs a column"),
        ({"fn": "lag", "column": "qty", "offset": 0}, "greater than or equal to 1"),
        ({"fn": "sum", "column": "qty", "offset": 1}, "only for lag and lead"),
        ({"fn": "median", "column": "qty"}, "fn"),
        ({"fn": "sum", "column": "qty", "partition_by": ["grp", "GRP"]}, "twice"),
        ({"fn": "sum", "column": "qty", "bogus": 1}, "not a setting"),
        ({"fn": "sum", "column": "qty", "name": "__x"}, "reserved"),
    ],
)
def test_bad_settings_say_what_to_change(cfg: dict, needle: str) -> None:
    with pytest.raises(NodeError, match=needle):
        build("window", {"name": "w", **cfg})


@pytest.mark.parametrize(
    ("cfg", "needle"),
    [
        ({"fn": "sum", "column": "nope"}, "no column 'nope'"),
        ({"fn": "sum", "column": "qty", "partition_by": ["nope"]}, "no column 'nope'"),
        ({"fn": "row_number", "order_by": ["nope"]}, "no column 'nope'"),
        ({"fn": "row_number", "order_by": ["-nope"]}, "no column 'nope'"),
        ({"fn": "sum", "column": "grp"}, "needs a number column"),
        ({"fn": "avg", "column": "sub"}, "needs a number column"),
        ({"fn": "share", "column": "grp"}, "needs a number column"),
    ],
)
def test_checks_against_the_columns_without_reading_data(cfg: dict, needle: str) -> None:
    assert needle in _err({"name": "w", **cfg}, _rel([]))


def test_a_helper_named_column_in_the_input_is_refused() -> None:
    rel = connect().sql("SELECT 1 AS __pos, 2 AS v")
    assert "reserved" in _err({"name": "w", "fn": "sum", "column": "v"}, rel)


def test_200k_rows_keep_their_order_and_stay_fast() -> None:
    con = connect()
    con.execute(
        "CREATE TABLE big AS SELECT i AS id, i % 97 AS g, (i * 7919) % 1000 AS seq, "
        "CASE WHEN i % 11 = 0 THEN NULL ELSE i % 50 END AS qty FROM range(200000) r(i)"
    )
    rel = con.sql("SELECT * FROM big")
    t0 = time.perf_counter()
    for cfg in (
        {"fn": "sum", "column": "qty", "partition_by": ["g"], "order_by": ["seq"]},
        {"fn": "rank", "partition_by": ["g"], "order_by": ["-seq"]},
        {"fn": "lag", "column": "qty", "partition_by": ["g"], "order_by": ["seq"]},
        {"fn": "share", "column": "qty", "partition_by": ["g"]},
    ):
        out = _run({"name": "w", **cfg}, rel).fetchall()
        assert [r[0] for r in out] == list(range(200000))
    assert time.perf_counter() - t0 < 30  # only catches a blow-up, not a slow machine
