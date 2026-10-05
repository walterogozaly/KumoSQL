"""BigQuery string literals and quoted names get one spelling before the provers and the execution checks read them."""

import pytest

from kumosql.equivalence import prove_equivalent
from kumosql.string_literals import canonical_literals, invalid_literal


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
        (r"SELECT '\x41', '\101', '\u0041', '\U00000041', 'A'", "SELECT 'A', 'A', 'A', 'A', 'A'"),
        (r"SELECT '\a\b\f\v', '\u00e9'", r"SELECT '\a\b\f\v', 'é'"),
        # an escape of uncertain value (a byte above 127?) is left as written
        (r"SELECT '\xE9', '\377', '\q'", r"SELECT '\xE9', '\377', '\q'"),
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


def test_a_string_broken_over_two_lines_is_not_proven_equal_to_its_escaped_form():
    # BigQuery rejects 'a<line break>b' (unclosed string literal); only a triple-quoted string may span lines.
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    broken, escaped = "SELECT 'a\nb' AS v FROM t", r"SELECT 'a\nb' AS v FROM t"
    assert not prove_equivalent(broken, escaped).proven
    assert not prove_equivalent_algebraic(broken, escaped, schema={"t": ["s"]}).proven
    assert prove_equivalent("SELECT '''a\nb''' AS v FROM t", escaped).proven


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


@pytest.mark.parametrize("escaped, plain", [(r"'\x41'", "'A'"), (r"'\101'", "'A'"), (r"'\U00000041'", "'A'"), (r"'a\x22b'", "'a\"b'")])
def test_a_hex_octal_or_unicode_escape_is_the_character_it_names(escaped, plain):
    assert prove_equivalent(f"SELECT {escaped} AS x FROM t", f"SELECT {plain} AS x FROM t").proven


@pytest.mark.parametrize("escaped, other", [(r"'\x41'", r"'\\x41'"), (r"'\u0041'", r"'\\u0041'"), (r"'\U00000041'", r"'\\U00000041'"), (r"'\101'", r"'\\101'")])
def test_an_escape_is_not_proved_equal_to_a_backslash_and_the_same_text(escaped, other):
    # sqlglot reads both as the characters backslash, x, 4, 1; BigQuery reads the first as the one letter A.
    assert not prove_equivalent(f"SELECT {escaped} AS x FROM t", f"SELECT {other} AS x FROM t").proven


@pytest.mark.parametrize("escape", [r"'\xE9'", r"'\377'", r"'\q'", r"'a\xzz'"])
def test_an_escape_of_uncertain_value_is_never_proved(escape):
    sql = f"SELECT {escape} AS x FROM t"
    assert not prove_equivalent(sql, sql).proven
    assert invalid_literal(sql)


def test_raw_and_bytes_literals_keep_their_escapes_without_being_declined():
    assert not invalid_literal(r"SELECT r'\xE9', b'\xE9', rb'\q', B'\377'")
    assert invalid_literal(r"SELECT 'x', '\xE9'")
    assert not invalid_literal(r"SELECT 1 AS \q -- '\xE9'")
