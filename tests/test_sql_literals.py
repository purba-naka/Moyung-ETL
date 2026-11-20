import math

import pytest

from moyung.nodes.base import NodeError, check_fits, sql_value


@pytest.mark.parametrize(
    ("value", "lit"),
    [(True, "TRUE"), (3, "3"), (1.5, "1.5"), ("it's", "'it''s'"), (r"a\b", r"'a\b'")],
)
def test_sql_value(value, lit):
    assert sql_value(value) == lit


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "a\x00b"])
def test_sql_value_rejects(bad):
    with pytest.raises(NodeError):
        sql_value(bad)


@pytest.mark.parametrize(("value", "kind"), [(5, "INTEGER"), ("5", "INTEGER"), (1.5, "DOUBLE")])
def test_check_fits_keeps_value(value, kind):
    assert check_fits(value, kind, "x")


@pytest.mark.parametrize(
    ("value", "kind"),
    [("abc", "INTEGER"), (1.7, "INTEGER"), (10**400, "BIGINT"), (math.nan, "DOUBLE")],
)
def test_check_fits_rejects_changed_value(value, kind):
    with pytest.raises(NodeError, match="nope"):
        check_fits(value, kind, "nope")
