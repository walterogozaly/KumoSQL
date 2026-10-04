"""Raw-string values must not change with the quoting delimiter."""

import pytest
import sqlglot

from kumosql.duckdb_load import run_unoptimized
from kumosql.equivalence import prove_equivalent
from kumosql.random_check import _duck
from kumosql.string_literals import canonical_literals


@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
@pytest.mark.parametrize("prefix", ["r", "R"])
def test_raw_escaped_delimiter_preserves_the_backslash(quote, prefix):
    delimiter = quote[0]
    value = "a\\" + delimiter + "b"
    sql = f"SELECT {prefix}{quote}{value}{quote} AS v"
    translated = _duck(sql, "bigquery")
    duckdb = pytest.importorskip("duckdb")
    with duckdb.connect() as db:
        assert db.execute(translated).fetchall() == [(value,)]
        assert run_unoptimized(db, translated) == [[(value,)]]
    assert canonical_literals(canonical_literals(sql)) == canonical_literals(sql)


def test_raw_quote_styles_have_the_same_value_and_proof():
    value = 'a\\"b'
    left = "SELECT r'a\\\"b' AS v"
    right = 'SELECT r"a\\\"b" AS v'
    plain = "SELECT " + sqlglot.exp.Literal.string(value).sql(dialect="bigquery") + " AS v"
    assert _duck(left, "bigquery") == _duck(right, "bigquery") == _duck(plain, "bigquery")
    assert prove_equivalent(left, right).proven
    assert prove_equivalent(right, plain).proven
    assert not prove_equivalent(right, "SELECT 'a\"b' AS v").proven


def test_raw_literals_in_predicates_keep_their_value():
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import prove_equivalent_smt

    left = "SELECT s FROM t WHERE s = r'a\\\"b'"
    right = 'SELECT s FROM t WHERE s = r"a\\\"b"'
    different = "SELECT s FROM t WHERE s = 'a\"b'"
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert prove(left, right, schema={"t": ["s"]}).proven
        assert not prove(right, different, schema={"t": ["s"]}).proven


@pytest.mark.parametrize("sql", ["SELECT r'a\nb'", "SELECT r'a\\'", "SELECT r\"a\\\""])
def test_invalid_raw_literals_are_left_as_written(sql):
    assert canonical_literals(sql) == sql


def test_raw_triple_quote_preserves_newlines_and_escape_text():
    sql = "SELECT r'''a\n\\n''' AS v"
    duckdb = pytest.importorskip("duckdb")
    with duckdb.connect() as db:
        assert run_unoptimized(db, _duck(sql, "bigquery")) == [[("a\n\\n",)]]
