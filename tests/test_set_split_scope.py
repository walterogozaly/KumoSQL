"""A false proof from the set-split rules rewriting the wrong occurrence of a test (issue #518).

``x IN (SELECT CASE WHEN c1 THEN a WHEN c2 THEN b END ..)`` is split into one ``IN`` per arm only where just TRUE
counts (a top-level WHERE conjunct): the CASE has no ELSE, so its NULLs turn a FALSE into an unknown, which a
filter cannot tell apart. The rule found the occurrence it had judged by the SQL text of the copy, so an identical
looking ``x IN (...)`` deeper in the query (a nested select where ``x`` is another column, under a ``NOT``) was
split instead: ``NOT (9 IN {5, NULL})`` is unknown, ``NOT (9 IN {5} OR 9 IN {})`` is TRUE.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.set_split_rules import split_distinct_select

SCHEMA = {"t": {"id": "INT64", "x": "INT64"}, "u": {"k": "INT64", "w": "INT64"}}
ROWS = {"t": [(1, 5), (2, 9)], "u": [(1, 5), (2, 3)]}
KEY = "(SELECT CASE WHEN u.k = 1 THEN u.w WHEN u.w = 1 THEN u.k END FROM u)"
SPLIT = "(t.x IN (SELECT u.w FROM u WHERE u.k = 1) OR t.x IN (SELECT u.k FROM u WHERE u.w = 1))"
# the first conjunct sits deeper in the tree than the identical test inside the EXISTS
FILLER = " AND ".join(f"t.id > {-100 - i}" for i in range(9))


def _query(test: str) -> str:
    return f"SELECT t.id FROM t WHERE t.x IN {KEY} AND {FILLER} AND EXISTS (SELECT 1 FROM t WHERE {test})"


def _bag(db, sql: str) -> Counter:
    return Counter(run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _differ(left: str, right: str) -> bool:
    db = duckdb.connect()
    db.execute("CREATE TABLE t(id BIGINT, x BIGINT)")
    db.execute("CREATE TABLE u(k BIGINT, w BIGINT)")
    for table, rows in ROWS.items():
        db.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    return _bag(db, left) != _bag(db, right)


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


def test_a_look_alike_test_in_a_nested_select_is_not_split():
    left, right = _query(f"NOT (t.x IN {KEY})"), _query(f"NOT {SPLIT}")
    assert _differ(left, right), "the database must separate the pair"
    assert not _proven(left, right)


def test_the_rule_splits_only_the_filter_conjunct():
    select = sqlglot.parse_one(_query(f"NOT (t.x IN {KEY})"), read="bigquery")
    split = split_distinct_select(select)
    assert split is not None
    text = split.sql("bigquery")
    assert "NOT (t.x IN (SELECT CASE" in text, "the NOT IN inside the EXISTS keeps its CASE"
    assert text.count("CASE") == 1, "the filter conjunct is the one that was split"


def test_the_same_test_as_a_filter_in_the_nested_select_still_splits():
    left, right = _query(f"t.x IN {KEY}"), _query(SPLIT)
    assert not _differ(left, right)
    assert _proven(left, right)
