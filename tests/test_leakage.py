from __future__ import annotations

import duckdb

from excel_etl import leakage
from excel_etl.nodes.base import schema_of


def _run(rows, columns, *, target, features, ids=(), by=None, strategy="random", parts=None):
    con = duckdb.connect()
    cols = ", ".join(f"{n} {t}" for n, t in columns)
    con.execute(f"CREATE TABLE base (__rn BIGINT, {cols})")
    for i, row in enumerate(rows):
        con.execute(f"INSERT INTO base VALUES ({i + 1}, {', '.join('?' for _ in row)})", list(row))
    con.execute("CREATE TABLE part (__rn BIGINT, __ml_p VARCHAR)")
    for i in range(len(rows)):
        label = (parts or (lambda n: "train" if n % 2 else "test"))(i)
        con.execute("INSERT INTO part VALUES (?, ?)", [i + 1, label])
    by_name = {c.name: c for c in schema_of(con.table("base"))}
    return leakage.check(
        con,
        base="base",
        part="part",
        row="__rn",
        columns=[c for c in by_name.values() if c.name != "__rn"],
        target=by_name[target],
        features=[by_name[f] for f in features],
        ids=[by_name[i] for i in ids],
        by=by_name[by] if by else None,
        strategy=strategy,
    )


def test_a_feature_that_fixes_the_target_is_flagged():
    rows = [(i % 3, "yes" if i % 3 else "no", i * 7 % 5) for i in range(30)]
    found = _run(
        rows,
        [("a", "INTEGER"), ("y", "VARCHAR"), ("b", "INTEGER")],
        target="y",
        features=["a", "b"],
    )
    assert any("'a'" in w and "determine" in w for w in found)
    assert not any("'b'" in w and "determine" in w for w in found)


def test_a_copy_of_the_target_is_flagged():
    rows = [(i, i) for i in range(10)]
    found = _run(rows, [("a", "INTEGER"), ("y", "INTEGER")], target="y", features=["a"])
    assert any("'a'" in w for w in found)


def test_an_id_like_feature_is_flagged_but_a_float_is_not():
    rows = [(i, i * 0.37, i % 2) for i in range(40)]
    found = _run(
        rows,
        [("k", "INTEGER"), ("x", "DOUBLE"), ("y", "INTEGER")],
        target="y",
        features=["k", "x"],
    )
    assert any("'k'" in w and "identifier" in w for w in found)
    assert not any("'x'" in w for w in found)


def test_a_date_feature_after_a_date_target_is_flagged():
    rows = [(f"2024-01-{d:02d}", f"2024-02-{d:02d}") for d in range(1, 21)]
    found = _run(rows, [("t", "DATE"), ("y", "DATE")], target="t", features=["y"])
    assert any("after the target" in w for w in found)


def test_a_date_feature_after_the_time_split_column_is_flagged():
    rows = [(f"2024-01-{d:02d}", f"2024-02-{d:02d}", d % 2) for d in range(1, 21)]
    found = _run(
        rows,
        [("t", "DATE"), ("later", "DATE"), ("y", "INTEGER")],
        target="y",
        features=["later"],
        by="t",
        strategy="time",
    )
    assert any("split column" in w for w in found)


def test_shared_ids_and_duplicate_rows_across_partitions_are_flagged():
    rows = [(1, 5, 0), (1, 5, 0), (2, 6, 1), (3, 7, 1)]
    found = _run(
        rows,
        [("e", "INTEGER"), ("x", "INTEGER"), ("y", "INTEGER")],
        target="y",
        features=["x"],
        ids=["e"],
        parts=lambda n: "train" if n in (0, 2) else "test",
    )
    assert any("same entity" in w and "2 row" in w for w in found)
    assert any("identical in every column" in w for w in found)


def test_a_group_split_does_not_warn_about_spread_and_clean_data_is_quiet():
    rows = [(i % 7, (i * 3) % 11, i % 2) for i in range(60)]
    cols = [("x", "INTEGER"), ("z", "INTEGER"), ("y", "INTEGER")]
    found = _run(rows, cols, target="y", features=["x", "z"], strategy="group", by="x")
    assert not any("another partition" in w for w in found)


def test_a_column_filled_only_for_one_outcome_is_flagged_by_its_emptiness():
    rows = [
        ("price" if i % 3 == 0 else None, i % 7, "yes" if i % 3 == 0 else "no") for i in range(60)
    ]
    found = _run(
        rows,
        [("reason", "VARCHAR"), ("z", "INTEGER"), ("y", "VARCHAR")],
        target="y",
        features=["reason", "z"],
    )
    assert any("are empty almost fixes" in w and "'reason'" in w for w in found)
    assert not any("'z'" in w for w in found)


def test_low_cardinality_data_and_empty_ids_are_not_flagged_as_duplicates_or_sharing():
    rows = [(None, i % 5, i % 2) for i in range(200)]
    found = _run(
        rows,
        [("e", "INTEGER"), ("x", "INTEGER"), ("y", "INTEGER")],
        target="y",
        features=["x"],
        ids=["e"],
    )
    assert not any("share their ids" in w for w in found)


def test_a_tiny_sample_does_not_claim_a_feature_determines_the_target():
    rows = [(i % 3, i % 2) for i in range(8)]
    found = _run(rows, [("a", "INTEGER"), ("y", "INTEGER")], target="y", features=["a"])
    assert not any("determine" in w for w in found)
