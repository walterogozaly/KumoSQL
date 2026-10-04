from collections import Counter

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.lateral_boolean_groups import nullable_lateral_boolean_group

SCHEMA = {"o": ["k"], "d": ["k", "v"]}
QUERY = "SELECT o.k,l.i FROM o LEFT JOIN LATERAL (SELECT (d.v IS NOT NULL) AS i FROM d WHERE d.k=o.k AND d.v>0 GROUP BY (d.v IS NOT NULL)) l ON TRUE"


def rewrite(sql=QUERY, schema=SCHEMA, not_null=None):
    tree = sqlglot.parse_one(sql, read="duckdb")
    from kumosql.ast_utils import canonical_negation
    return nullable_lateral_boolean_group(canonical_negation(tree), schema, not_null)


@pytest.mark.parametrize("outer,inner", [([], []), ([(1,)], []),
    ([(None,), (1,), (1,), (2,)], [(None, 3), (1, None), (1, -1), (1, 2), (1, 2)]),
    ([(1,), (2,)], [(1, 5), (1, 8), (2, None)])])
def test_nullable_indicator_preserves_bags_and_output_names(outer, inner):
    transformed = rewrite()
    assert transformed is not None
    assert transformed.expressions[1].alias_or_name == "i"
    db = duckdb.connect()
    db.execute("CREATE TABLE o(k BIGINT)")
    db.execute("CREATE TABLE d(k BIGINT,v BIGINT)")
    if outer:
        db.executemany("INSERT INTO o VALUES (?)", outer)
    if inner:
        db.executemany("INSERT INTO d VALUES (?,?)", inner)
    db.execute("PRAGMA disable_optimizer")
    assert Counter(db.execute(QUERY).fetchall()) == Counter(db.execute(transformed.sql(dialect="duckdb")).fetchall())
    db.close()


def test_public_api_projection_identity():
    transformed = rewrite()
    result = prove_equivalent_algebraic(QUERY, transformed.sql(dialect="duckdb"), schema=SCHEMA, dialect="duckdb")
    assert result.proven


@pytest.mark.parametrize("predicate", ["NOT l.i", "l.i IS NULL", "l.i OR o.k=2", "(l.i=FALSE) IS NULL"])
def test_parent_three_valued_predicates_keep_null_padding(predicate):
    query = QUERY + " WHERE " + predicate
    transformed = rewrite(query)
    assert transformed is not None
    db = duckdb.connect()
    db.execute("CREATE TABLE o(k BIGINT)")
    db.execute("CREATE TABLE d(k BIGINT,v BIGINT)")
    db.execute("INSERT INTO o VALUES (1),(2),(3)")
    db.execute("INSERT INTO d VALUES (1,7),(1,8),(2,NULL)")
    db.execute("PRAGMA disable_optimizer")
    assert Counter(db.execute(query).fetchall()) == Counter(db.execute(transformed.sql(dialect="duckdb")).fetchall())
    assert prove_equivalent_algebraic(query, transformed.sql(dialect="duckdb"), schema=SCHEMA, dialect="duckdb").proven
    db.close()


@pytest.mark.parametrize("sql", [
    QUERY.replace("d.v>0", "TRUE"),
    QUERY.replace("d.v>0", "d.v IS NULL OR d.v=1"),
    QUERY.replace("LEFT JOIN", "JOIN"),
    QUERY.replace("ON TRUE", "ON FALSE"),
    QUERY.replace("GROUP BY (d.v IS NOT NULL)", "GROUP BY ROLLUP((d.v IS NOT NULL))"),
    QUERY.replace("GROUP BY (d.v IS NOT NULL)", "GROUP BY (d.v IS NOT NULL),d.v"),
    QUERY.replace("GROUP BY (d.v IS NOT NULL)", "GROUP BY (d.v IS NOT NULL) LIMIT 1"),
    QUERY.replace("WHERE d.k=o.k", "WHERE RANDOM()>0 AND d.k=o.k"),
    QUERY.replace("SELECT o.k,l.i", "SELECT o.k,l.*"),
    QUERY.replace("SELECT o.k,l.i", "SELECT o.k,i"),
    QUERY + " RIGHT JOIN o oo ON oo.k=o.k",
    QUERY + " LEFT JOIN LATERAL (SELECT l.i AS i) x ON TRUE",
    QUERY.replace("FROM d WHERE", "FROM d AS d(v,k) WHERE"),
    QUERY.replace(") l ON TRUE", ") l(flag) ON TRUE"),
    QUERY.replace("FROM d WHERE", "FROM d TABLESAMPLE BERNOULLI(50 PERCENT) WHERE"),
    QUERY.replace("(d.v IS NOT NULL)", "(CAST(d.v AS INTEGER) IS NOT NULL)"),
    QUERY.replace("d.v>0", "d.v>0 AND custom_udf(d.v)>0"),
])
def test_boundary_declines(sql):
    assert rewrite(sql) is None


def test_declared_nonnull_source_and_unknown_schema():
    query = QUERY.replace("AND d.v>0", "")
    assert rewrite(query) is None
    assert rewrite(query, not_null={"d": frozenset({"v"})}) is not None
    assert rewrite(schema=None) is None
    assert rewrite(schema={"o": ["k"]}) is None


def test_inherited_cte_cannot_borrow_physical_table_nonnull_fact():
    sql = "WITH d AS (SELECT raw.k,raw.v FROM raw) SELECT x.k,x.i FROM (" + QUERY.replace("AND d.v>0", "") + ") x"
    schema = {**SCHEMA, "raw": ["k", "v"]}
    tree = sqlglot.parse_one(sql, read="duckdb")
    from kumosql.ast_utils import canonical_negation
    tree = canonical_negation(tree)
    for select in list(tree.find_all(sqlglot.exp.Select)):
        assert nullable_lateral_boolean_group(select, schema, {"d": frozenset({"v"})}) is None
    db = duckdb.connect()
    db.execute("CREATE TABLE o(k BIGINT);CREATE TABLE d(k BIGINT,v BIGINT NOT NULL);CREATE TABLE raw(k BIGINT,v BIGINT)")
    db.execute("INSERT INTO o VALUES (1)")
    db.execute("INSERT INTO raw VALUES (1,NULL),(1,7)")
    db.execute("PRAGMA disable_optimizer")
    assert Counter(db.execute(sql).fetchall()) == Counter([(1, True), (1, False)])
    db.close()


def test_null_and_match_groups_would_duplicate_outer_row():
    unsafe = QUERY.replace("d.v>0", "d.v=1 OR d.v IS NULL")
    # A second true disjunct retains both TRUE and FALSE groups. An EXISTS
    # substitution would incorrectly collapse these two rows to one.
    left = unsafe + " WHERE l.i OR TRUE"
    right = "SELECT o.k,TRUE AS i FROM o WHERE EXISTS(SELECT 1 FROM d WHERE d.k=o.k AND (d.v=1 OR d.v IS NULL))"
    db = duckdb.connect()
    db.execute("CREATE TABLE o(k BIGINT)")
    db.execute("CREATE TABLE d(k BIGINT,v BIGINT)")
    db.execute("INSERT INTO o VALUES(1)")
    db.execute("INSERT INTO d VALUES(1,NULL),(1,1)")
    db.execute("PRAGMA disable_optimizer")
    assert Counter(db.execute(left).fetchall()) != Counter(db.execute(right).fetchall())
    assert rewrite(left) is None
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="duckdb", compare_names=False).proven
    db.close()


def test_boolean_alias_shadowing_does_not_capture_local_key():
    sql = QUERY.replace("FROM d WHERE d.", "FROM d AS l WHERE l.").replace("AND d.", "AND l.").replace("(d.v", "(l.v")
    transformed = rewrite(sql)
    assert transformed is not None
    assert "FROM d AS l" in transformed.sql(dialect="duckdb")
