import math

import pytest

from excel_etl.nodes.base import NodeError, check_fits, sql_text, sql_value


@pytest.mark.parametrize(
    ("value", "lit"),
    [(True, "TRUE"), (3, "3"), (1.5, "1.5"), ("it's", "'it''s'"), ("a\b", "'a\b'")],
)
def test_sql_value(value, lit):
    assert sql_value(value) == lit


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "a\x00b"])
def test_sql_value_rejects(bad):
    with pytest.raises(NodeError):
        sql_value(bad)


def test_check_fits():
    check_fits("'5'", "INTEGER", "x")
    with pytest.raises(NodeError, match="nope"):
        check_fits(sql_text("abc"), "INTEGER", "nope")
