"""MySQL output-domain conversion retains 0/1/NULL, names and cast boundaries."""

from collections import Counter
import duckdb
import pytest
import sqlglot
from sqlglot import exp
from kumosql.mysql_boolean_outputs import canonical_mysql_boolean_outputs
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints


@pytest.mark.parametrize(
    "value",
    [
        "x=1",
        "x>1",
        "x IS NULL",
        "TRUE",
        "FALSE",
        "NOT (x=1)",
        "CASE WHEN x=1 THEN TRUE ELSE NULL END",
        "EXISTS(SELECT 1 FROM b WHERE b.y=x)",
        "NOT EXISTS(SELECT 1 FROM b WHERE b.y=x)",
    ],
)
def test_numeric_boolean_domain_and_alias(value):
    query = f"SELECT {value} AS flag FROM a"
    tree = sqlglot.parse_one(query, read="mysql")
    converted = canonical_mysql_boolean_outputs(tree, "mysql").sql(dialect="duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER)")
    db.execute("CREATE TABLE b(y INTEGER)")
    db.execute("INSERT INTO a VALUES (NULL),(0),(1),(2)")
    db.execute("INSERT INTO b VALUES (1)")
    numeric = f"SELECT CAST(({value}) AS BIGINT) AS flag FROM a"
    expected = db.execute(numeric).fetchall()
    result = db.execute(converted)
    assert result.description[0][0] == "flag"
    assert Counter(result.fetchall()) == Counter(expected)


@pytest.mark.parametrize(
    "value",
    [
        "x",
        "CAST(x AS BOOLEAN)",
        "CAST(x AS SIGNED)",
        "CAST(x=1 AS VARCHAR)",
        "TRY_CAST(x=1 AS SIGNED)",
        "CASE WHEN x=1 THEN 7 ELSE TRUE END",
    ],
)
def test_arbitrary_casts_and_values_not_classified_boolean(value):
    tree = sqlglot.parse_one(f"SELECT {value} AS flag FROM a", read="mysql")
    before = tree.sql()
    assert canonical_mysql_boolean_outputs(tree, "mysql").sql() == before


def test_other_dialect_and_volatile_source_unchanged():
    for query, dialect in [
        ("SELECT x=1 AS flag FROM a", "bigquery"),
        ("SELECT RAND()>0.5 AS flag", "mysql"),
    ]:
        tree = sqlglot.parse_one(query, read=dialect)
        before = tree.sql()
        assert canonical_mysql_boolean_outputs(tree, dialect).sql() == before


def test_explicit_utility_public_names_and_nulls():
    options = dict(
        dialect="mysql",
        schema={"a": ["x"]},
        types={"a": {"x": "INT"}},
        compare_names=True,
    )
    left = "SELECT x=1 AS flag FROM a"
    right = "SELECT CAST(x=1 AS SIGNED) AS flag FROM a"
    # The optional utility is explicit; normalizing every MySQL output globally
    # weakened established projection proofs and is deliberately disabled.
    assert not prove_equivalent_algebraic(left, right, **options).proven
    left = canonical_mysql_boolean_outputs(sqlglot.parse_one(left, read="mysql"), "mysql").sql(dialect="mysql")
    right = canonical_mysql_boolean_outputs(sqlglot.parse_one(right, read="mysql"), "mysql").sql(dialect="mysql")
    assert prove_equivalent_algebraic(left, right, **options).proven
    assert not prove_equivalent_algebraic(
        left, right.replace("AS flag", "AS other"), **options
    ).proven
    assert not prove_equivalent_algebraic(
        left, "SELECT CASE WHEN x=1 THEN 1 ELSE 0 END AS flag FROM a", **options
    ).proven


def test_negated_exists_retains_indicator_join_proof():
    left = "SELECT sal, NOT empno IN (SELECT deptno FROM dept WHERE emp.job=dept.name) FROM emp"
    right = "SELECT emp.sal,t.i IS NULL FROM emp LEFT JOIN (SELECT deptno,TRUE i,name FROM dept) t ON emp.empno=t.deptno AND emp.job=t.name"
    result = prove_equivalent_algebraic(
        left, right, dialect="mysql", compare_names=False, exact_arithmetic=True,
        schema={"emp":["empno","job","sal"],"dept":["deptno","name"]},
        types={"emp":{"empno":"INT","job":"VARCHAR","sal":"INT"},"dept":{"deptno":"INT","name":"VARCHAR"}},
        constraints={"emp":TableConstraints(frozenset({"empno","job","sal"}),(("empno",),)),
                     "dept":TableConstraints(frozenset({"deptno","name"}),(("deptno",),))},
    )
    assert result.proven
