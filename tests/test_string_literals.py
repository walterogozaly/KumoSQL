"""BigQuery string literals and quoted names get one spelling before the provers and the execution checks read them."""

import pytest

from kumosql.equivalence import prove_equivalent
from kumosql.string_literals import canonical_literals


@pytest.mark.parametrize(
    "sql, expected",
    [
        (r"SELECT 'a\"b'", "SELECT 'a\"b'"),
        ('SELECT "a\\"b", "x"', "SELECT 'a\"b', 'x'"),
        (r"SELECT '''x\'y\"z'''", r"SELECT 'x\'y" + '"z' + "'"),
        (r"SELECT 'a\\b', 'c\nd'", r"SELECT 'a\\b', 'c\nd'"),
        ("SELECT `col``col` FROM t", "SELECT `col` `col` FROM t"),
        # left as written: raw strings, escapes this module does not decode, comments
        (r"SELECT r'a\"b'", r"SELECT r'a\"b'"),
        # bytes: printable ASCII as is, every other byte (quote and backslash too) as \xHH
        (r"SELECT b'x\n', Rb'q\"', B'\x41', b'\101', b'it\'s', b'é'", r"SELECT b'x\x0A', b'q\x5C" + '"' + r"', b'A', b'A', b'it\x27s', b'\xC3\xA9'"),
        (r"SELECT b'\\x41', b'\u0041'", r"SELECT b'\x5Cx41', b'\u0041'"),
        (r"SELECT '\x41', 'A'", r"SELECT '\x41', 'A'"),
        ("SELECT 1 -- it's \\\nFROM t", "SELECT 1 -- it's \\\nFROM t"),
        ("SELECT 'unterminated\\'", "SELECT 'unterminated\\'"),
    ],
)
def test_canonical_literals(sql, expected):
    assert canonical_literals(sql) == expected
    assert canonical_literals(canonical_literals(sql)) == canonical_literals(sql)  # idempotent


def test_equal_strings_spelled_differently_are_proved_equal():
    assert prove_equivalent(r"SELECT 'a\"b' AS x FROM t", "SELECT 'a\"b' AS x FROM t").proven
    assert prove_equivalent(r"SELECT 1 FROM t WHERE s = 'it\'s'", 'SELECT 1 FROM t WHERE s = "it\'s"').proven


def test_different_strings_are_not_proved_equal():
    assert not prove_equivalent(r"SELECT 'a\\b' AS x FROM t", "SELECT 'a\\b' AS x FROM t").proven
    assert not prove_equivalent(r"SELECT r'a\"b' AS x FROM t", "SELECT 'a\"b' AS x FROM t").proven  # raw keeps the backslash


def test_bytes_are_compared_by_value():
    # BigQuery: b'\\x41' is the four bytes \x41, b'\x41' is the one byte A, rb'a\d' = b'a\\d'
    assert not prove_equivalent(r"SELECT b'\\x41' AS v FROM t", r"SELECT b'\x41' AS v FROM t").proven
    assert not prove_equivalent(r"SELECT 1 FROM t WHERE b = b'\\n'", r"SELECT 1 FROM t WHERE b = b'\n'").proven
    assert prove_equivalent(r"SELECT b'\x41' AS v FROM t", "SELECT B'A' AS v FROM t").proven
    assert prove_equivalent(r"SELECT rb'a\d' AS v FROM t", r"SELECT b'a\\d' AS v FROM t").proven


def test_the_prover_agrees_with_bigquery_on_escaped_strings():
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    schema = {"t": ["s"]}
    assert prove_equivalent_algebraic(r"SELECT s FROM t WHERE s = 'a\"b'", "SELECT s FROM t WHERE s = 'a\"b'", schema=schema).proven
    assert not prove_equivalent_algebraic(r"SELECT s FROM t WHERE s = 'a\\b'", "SELECT s FROM t WHERE s = 'a\\\\\\\\b'", schema=schema).proven


def test_execution_check_reads_escapes_like_bigquery():
    pytest.importorskip("duckdb")
    from kumosql.random_check import _duck

    assert _duck(r"SELECT 'a\"b'", "bigquery") == _duck("SELECT 'a\"b'", "bigquery")
    assert _duck("SELECT `a``b` FROM t", "bigquery") == _duck("SELECT `a` `b` FROM t", "bigquery")
