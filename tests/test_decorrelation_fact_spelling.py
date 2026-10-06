"""A table's declared facts apply only to the spelling they name."""

from collections import Counter

import pytest
import sqlglot

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.decorrelation_rules import _closed
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import TableConstraints


NOT_NULL = {"t": TableConstraints(not_null=frozenset({"x"}))}
SCHEMA = {"t": ["id", "x"]}

PAIRS = (
    (
        "SELECT t.id FROM other_ds.t AS t WHERE EXISTS "
        "(SELECT 1 FROM other_ds.t AS t2 WHERE t2.x = t.x)",
        "SELECT t.id FROM other_ds.t AS t",
    ),
    (
        "SELECT t.id, g.k FROM other_ds.t AS t JOIN "
        "(SELECT t3.x AS k FROM other_ds.t AS t3 GROUP BY t3.x) AS g ON g.k = t.x",
        "SELECT t.id, t.x AS k FROM other_ds.t AS t",
    ),
)


def prove(left, right, *, facts=NOT_NULL, schema=SCHEMA):
    return prove_equivalent_algebraic(
        left,
        right,
        schema=schema,
        constraints=facts,
        compare_names=False,
        dialect="bigquery",
    )


def test_not_null_facts_for_t_do_not_simplify_decorrelation_on_other_ds_t():
    db = duckdb.connect()
    db.execute("CREATE SCHEMA other_ds")
    db.execute("CREATE TABLE other_ds.t(id BIGINT, x BIGINT)")
    db.execute("INSERT INTO other_ds.t VALUES (1, NULL), (2, 1)")

    for left, right in PAIRS:
        result = prove(left, right)
        assert not result.proven
        assert Counter(run_unoptimized(db, left)[0]) != Counter(run_unoptimized(db, right)[0])
        assert Counter(db.execute(left).fetchall()) != Counter(db.execute(right).fetchall())


def test_not_null_fact_still_applies_to_the_table_spelling_it_names():
    left = "SELECT t.id FROM t AS t WHERE EXISTS (SELECT 1 FROM t AS t2 WHERE t2.x = t.x)"
    right = "SELECT t.id FROM t AS t"

    assert prove_equivalent_algebraic(
        left,
        right,
        schema=SCHEMA,
        constraints=NOT_NULL,
        compare_names=False,
        dialect="bigquery",
    ).proven


def test_facts_apply_when_the_qualified_table_spelling_is_declared():
    facts = {"other_ds.t": TableConstraints(not_null=frozenset({"x"}))}
    schema = {"other_ds.t": ["id", "x"]}

    for left, right in PAIRS:
        assert prove(left, right, facts=facts, schema=schema).proven


def test_closed_scope_does_not_use_schema_for_a_different_table_spelling():
    query = sqlglot.parse_one("SELECT x FROM other_ds.t")

    assert not _closed(query, {"t": ["id", "x"]})
    assert _closed(query, {"other_ds.t": ["id", "x"]})
