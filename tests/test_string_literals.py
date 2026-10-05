"""BigQuery string literals and quoted names get one spelling before the provers and the execution checks read them."""

import pytest

from kumosql.equivalence import prove_equivalent
from kumosql.string_literals import canonical_literals, undecoded_escape


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


@pytest.mark.parametrize(
    "sql, expected",
    [
        (r"SELECT '\x41'", True),
        (r"SELECT 'a' || '\u0041'", True),
        (r"SELECT '\101', 'z'", True),
        (r'SELECT "\x41"', True),
        (r"SELECT '''x\x41'''", True),
        (r"SELECT 'a\nb', 'x\\y', 'q\'r', 'p\"q'", False),
        (r"SELECT r'\x41', b'\x41', Rb'\x41', rb'\101'", False),
        (r"SELECT `a\x41` FROM t", False),
        ("SELECT 1 -- '\\x41'\nFROM t", False),
        (r"SELECT 'ok' /* '\x41' */", False),
        ("SELECT 'plain'", False),
    ],
)
def test_undecoded_escapes_are_found(sql, expected):
    assert undecoded_escape(sql) is expected


def test_strings_the_prover_cannot_decode_are_not_compared_by_their_text():
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import prove_equivalent_smt

    # BigQuery reads '\x41' as the string 'A'; sqlglot keeps the four characters, so the two were called different
    pair = (r"SELECT t.id FROM t WHERE '\x41' = 'A'", "SELECT t.id FROM t WHERE FALSE")
    assert not prove_equivalent_algebraic(*pair, schema={"t": ["id"]}).proven
    assert not prove_equivalent_smt(*pair, schema={"t": ["id"]}).proven
    plain = ("SELECT t.id FROM t WHERE 'a' = 'A'", "SELECT t.id FROM t WHERE FALSE")
    assert prove_equivalent_algebraic(*plain, schema={"t": ["id"]}).proven
    assert prove_equivalent_smt(*plain, schema={"t": ["id"]}).proven
