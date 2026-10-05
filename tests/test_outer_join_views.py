"""Outer-join view matching (Larson and Zhou): terms of a join block, subsumption, null-rejecting
predicates, presence tests, and the traps the matching must leave alone.

``rewrite_over_model`` only returns what the prover proves, so the unit tests of ``compensate`` check the
proposal and the end-to-end tests check the proof; each rewrite is also run against the original on random
DuckDB data.
"""

import random
from collections import Counter

import duckdb
import pytest

pytest.importorskip("z3")

import sqlglot  # noqa: E402

from kumosql.containment import check_containment  # noqa: E402
from kumosql.model_reuse import _block, _prepare, rewrite_over_model  # noqa: E402
from kumosql.outer_join_views import Unreadable, compensate, read_shape, terms  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"a": ["id", "k", "x"], "b": ["id", "k", "y"], "c": ["id", "k", "z"]}


def shape_of(sql):
    return read_shape(_prepare(sql, SCHEMA, "postgres"))[0]


def term_names(sql):
    return {tuple(sorted(s)) for s in terms(shape_of(sql))}


def test_left_join_has_the_joined_and_the_unmatched_term():
    assert term_names("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k") == {("a", "b"), ("a",)}


def test_full_join_adds_the_other_side_alone():
    assert term_names("SELECT a.id FROM a FULL JOIN b ON a.k = b.k") == {("a", "b"), ("a",), ("b",)}


def test_right_join_terms():
    assert term_names("SELECT a.id FROM a RIGHT JOIN b ON a.k = b.k") == {("a", "b"), ("b",)}


def test_null_rejecting_where_removes_the_unmatched_term():
    found = terms(shape_of("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.y > 3"))
    assert [t for t, term in found.items() if term.where is not None] == [frozenset({"a", "b"})]


def test_is_null_where_keeps_the_unmatched_term():
    found = terms(shape_of("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.y IS NULL"))
    assert {t for t, term in found.items() if term.where is not None} == {frozenset({"a", "b"}), frozenset({"a"})}


def test_chain_where_on_the_middle_table():
    found = terms(shape_of("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.k = c.k WHERE b.y > 3"))
    assert {t for t, term in found.items() if term.where is not None} == {frozenset({"a", "b"}), frozenset({"a", "b", "c"})}


def test_filtering_derived_table_acts_like_an_on_condition():
    found = terms(shape_of("SELECT a.id FROM a LEFT JOIN (SELECT k, y FROM b WHERE y > 3) t ON a.k = t.k"))
    assert {tuple(sorted(s)) for s in found} == {("a", "t"), ("a",)}
    assert any("> 3" in c.sql() for c in found[frozenset({"a", "t"})].join)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.id FROM a LEFT JOIN (SELECT k, SUM(y) AS y FROM b GROUP BY k) t ON a.k = t.k",
        "SELECT a.id FROM a LEFT JOIN (SELECT b.k FROM b JOIN c ON b.k = c.k) t ON a.k = t.k",
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k AND a.x = b.y LEFT JOIN c ON c.k = b.k WHERE COALESCE(c.z, 1) = 1 + b.y",
    ],
)
def test_shapes_outside_the_reading_are_refused_or_read_without_guessing(sql):
    try:
        shape = shape_of(sql)
        terms(shape)
    except Unreadable:
        return
    assert shape.leaves  # read: nothing to refuse


def match(query, view, outputs=None):
    q, v = shape_of(query), shape_of(view)
    names = outputs or {"a": {"id", "k"}, "b": {"id", "k", "y"}, "c": {"id", "k", "z"}}
    return compensate(q, v, key=lambda c: c.sql(), view_outputs=names, not_null={})


def test_same_join_needs_no_selection():
    got = match("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k")
    assert got is not None and got.predicate == []


def test_where_residual_is_the_predicate():
    got = match("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.y > 3", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k")
    assert got is not None
    assert {s.sql() for s in got.predicate} >= {"b.y > 3"} or any("b.y > 3" in s.sql() for s in got.predicate)
    assert got.terms == [frozenset({"a", "b"})]


def test_presence_column_selects_the_matched_term():
    got = match("SELECT a.id FROM a JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k")
    assert got is not None and any("IS NULL" in s.sql() for s in got.predicate)


def test_no_presence_column_means_no_proposal():
    assert match("SELECT a.id FROM a JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", outputs={"a": {"id"}, "b": set()}) is None


@pytest.mark.parametrize(
    "query, view",
    [
        # the view lacks rows the query needs (an inner join cannot give the unmatched ones)
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a JOIN b ON a.k = b.k"),
        # the view preserves the other side
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM b LEFT JOIN a ON a.k = b.k"),
        # a residual in ON (not WHERE) changes which rows are unmatched
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k AND b.y > 3", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k"),
        # the view filtered the preserved side
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.x > 3"),
        # a full join read from a left join
        ("SELECT a.id FROM a FULL JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k"),
        # different tables
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN c ON a.k = c.k"),
    ],
)
def test_traps_get_no_proposal(query, view):
    assert match(query, view) is None


CASES = [
    # (query, view, expected status)
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k WHERE b.y > 3", "SELECT a.id, b.k AS bk, b.y FROM a LEFT JOIN b ON a.k = b.k", "rewritten"),
    ("SELECT a.id, b.y FROM a JOIN b ON a.k = b.k", "SELECT a.id, b.k AS bk, b.y FROM a LEFT JOIN b ON a.k = b.k", "rewritten"),
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id, b.k AS bk, b.y FROM a LEFT JOIN b ON a.k = b.k", "rewritten"),
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k WHERE b.y IS NULL", "SELECT a.id, b.k AS bk, b.y FROM a LEFT JOIN b ON a.k = b.k", "rewritten"),
    ("SELECT a.id, t.y FROM a LEFT JOIN (SELECT k, y FROM b WHERE y > 3) t ON a.k = t.k WHERE t.y = 5", "SELECT a.id, t.k AS tk, t.y FROM a LEFT JOIN (SELECT k, y FROM b WHERE y > 3) t ON a.k = t.k", "rewritten"),
    ("SELECT a.id, c.z FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.k = c.k WHERE c.z > 1", "SELECT a.id, b.k AS bk, c.k AS ck, c.z FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.k = c.k", "rewritten"),
    ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.k = c.k WHERE b.y > 1", "SELECT a.id, b.k AS bk, b.y, c.k AS ck FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.k = c.k", "rewritten"),
    # traps
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id, b.k AS bk, b.y FROM a JOIN b ON a.k = b.k", "no"),
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k AND b.y > 3", "SELECT a.id, b.k AS bk, b.y FROM a LEFT JOIN b ON a.k = b.k", "no"),
    ("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id, b.k AS bk, b.y FROM b LEFT JOIN a ON a.k = b.k", "no"),
    # the view lacks the residual's column
    ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.y > 3", "SELECT a.id, b.k AS bk FROM a LEFT JOIN b ON a.k = b.k", "no"),
]


def random_database(seed):
    rng = random.Random(seed)
    con = duckdb.connect()
    for table, third in (("a", "x"), ("b", "y"), ("c", "z")):
        con.execute(f"CREATE TABLE {table} (id INTEGER, k INTEGER, {third} INTEGER)")
        for i in range(rng.randint(0, 8)):
            row = [i] + [rng.choice([None, 1, 2, 3, 4, 5, 6]) for _ in range(2)]
            con.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
    return con


def bag(con, sql):
    return Counter(con.execute(sql).fetchall())


@pytest.mark.parametrize("query, view, expected", CASES)
def test_end_to_end_rewrites_are_proven_and_traps_stay_unrewritten(query, view, expected):
    result = rewrite_over_model(query, view, schema=SCHEMA, timeout_ms=4000)
    if expected == "no":
        assert not result.rewritten, result.sql
        return
    assert result.rewritten, result.reason
    for seed in range(12):
        con = random_database(seed)
        con.execute(f"CREATE TABLE mv0 AS {sqlglot.transpile(view, read='postgres', write='duckdb')[0]}")
        assert bag(con, query) == bag(con, result.sql), (seed, result.sql)


def test_not_null_presence_column_is_read_from_a_declared_constraint():
    constraints = {"b": TableConstraints(not_null=("id",))}
    query = "SELECT a.id FROM a JOIN b ON a.k = b.k"
    view = "SELECT a.id, b.id AS bid FROM a LEFT JOIN b ON a.k = b.k"
    result = rewrite_over_model(query, view, schema=SCHEMA, constraints=constraints, timeout_ms=4000)
    assert result.rewritten and "bid" in result.sql


def test_containment_refuses_outer_blocks():
    prepared = _prepare("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", SCHEMA, "postgres")
    assert _block(prepared, shapes=True).shape is not None
    with pytest.raises(Exception):
        _block(prepared)  # the plain reading still refuses an outer join
    result = check_containment("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.x > 1", schema=SCHEMA)
    assert not result.contained
