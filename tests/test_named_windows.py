"""Named windows (``WINDOW w AS (...)``) are spelled out before proving (cluster 20)."""

import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.equivalence import prove_equivalent
from kumosql.named_windows import inline_named_windows

SCHEMA = {"t": ["a", "b", "x"]}


def _spelled(sql: str) -> str:
    return inline_named_windows(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def test_references_take_the_named_specification():
    assert _spelled("SELECT SUM(x) OVER w, RANK() OVER (w ORDER BY b) FROM t WINDOW w AS (PARTITION BY a)") == (
        "SELECT SUM(x) OVER (PARTITION BY a), RANK() OVER (PARTITION BY a ORDER BY b) FROM t"
    )
    # a named window may build on an earlier one
    assert _spelled("SELECT SUM(x) OVER v FROM t WINDOW w AS (PARTITION BY a), v AS (w ORDER BY b)") == (
        "SELECT SUM(x) OVER (PARTITION BY a ORDER BY b) FROM t"
    )
    # a subquery's own WINDOW clause is its own
    assert _spelled(
        "SELECT (SELECT MAX(x) OVER w FROM t WINDOW w AS (ORDER BY b) LIMIT 1), SUM(x) OVER w FROM t WINDOW w AS (PARTITION BY a)"
    ) == "SELECT (SELECT MAX(x) OVER (ORDER BY b) FROM t LIMIT 1), SUM(x) OVER (PARTITION BY a) FROM t"


def test_conflicting_or_unknown_references_are_left_alone():
    for sql in (
        "SELECT SUM(x) OVER (w ORDER BY b) FROM t WINDOW w AS (ORDER BY a)",  # ORDER BY twice
        "SELECT SUM(x) OVER (w PARTITION BY b) FROM t WINDOW w AS (ORDER BY a)",  # a reference cannot partition
        "SELECT SUM(x) OVER (w ROWS UNBOUNDED PRECEDING) FROM t WINDOW w AS (ORDER BY a ROWS 1 PRECEDING)",
        "SELECT SUM(x) OVER z FROM t WINDOW w AS (PARTITION BY a)",
        "SELECT SUM(x) OVER w FROM t WINDOW w AS (v), v AS (w)",
    ):
        assert "WINDOW" in _spelled(sql), sql


def test_a_named_window_proves_like_its_spelling():
    named = "SELECT a, SUM(x) OVER w AS s FROM t WINDOW w AS (PARTITION BY a)"
    spelled = "SELECT a, SUM(x) OVER (PARTITION BY a) AS s FROM t"
    assert prove_equivalent_algebraic(named, spelled, schema=SCHEMA, dialect="bigquery").proven
    assert not prove_equivalent_algebraic(named, spelled.replace("PARTITION BY a", "PARTITION BY b"), schema=SCHEMA, dialect="bigquery").proven
    assert prove_equivalent(named, spelled).proven


def test_a_tie_sensitive_named_window_stays_unproven():
    named = "SELECT a, ROW_NUMBER() OVER w AS n FROM t WINDOW w AS (PARTITION BY a ORDER BY b)"
    assert not prove_equivalent(named, named).proven
    stable = "SELECT a, RANK() OVER w AS n FROM t WINDOW w AS (PARTITION BY a ORDER BY b)"
    assert prove_equivalent(stable, "SELECT a, RANK() OVER (PARTITION BY a ORDER BY b) AS n FROM t").proven
