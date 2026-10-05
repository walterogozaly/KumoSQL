import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.literal_fold_rules import distribute_over_constant_union, fold_string_literals


def _fold(sql):
    return fold_string_literals(sqlglot.parse_one(sql, read="mysql")).sql(dialect="mysql")


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema={"t": ["a"]}, dialect="mysql", compare_names=False).proven


def test_string_functions_over_literals_are_evaluated():
    assert _fold("SELECT UPPER('table'), LOWER('ViEw')") == "SELECT 'TABLE', 'view'"
    assert _fold("SELECT SUBSTRING('table' FROM 1 FOR 2), SUBSTRING('table', 3)") == "SELECT 'ta', 'ble'"
    assert _fold("SELECT CONCAT('ta', 'ble')") == "SELECT 'table'"
    assert _fold("SELECT 'a' = 'a', 'a' = 'A', 'a' <> 'A'") == "SELECT TRUE, FALSE, TRUE"


def test_cases_that_dialects_read_differently_are_left_alone():
    assert _fold("SELECT SUBSTRING('table', 0, 2)") == "SELECT SUBSTRING('table', 0, 2)"  # a start before 1 differs by dialect
    assert _fold("SELECT SUBSTRING('table', 9)") == "SELECT SUBSTRING('table', 9)"  # an empty result is left as written
    assert _fold("SELECT UPPER('straße')") == "SELECT UPPER('straße')"  # non-ASCII case mapping differs
    assert _fold("SELECT CONCAT('a', NULL), CONCAT('a', x)") == "SELECT CONCAT('a', NULL), CONCAT('a', x)"
    assert _fold("SELECT UPPER(x)") == "SELECT UPPER(x)"


def test_a_select_over_a_union_of_constant_rows_is_the_union_of_its_rows():
    union = "SELECT UPPER(x) AS u FROM (SELECT 'a' AS x UNION SELECT 'b') AS d WHERE x <> 'b'"
    assert _proven(union, "SELECT 'A'")
    assert not _proven(union, "SELECT 'B'")
    assert _proven("SELECT d.x FROM (SELECT 1 AS x UNION ALL SELECT 1) AS d", "SELECT 1 UNION ALL SELECT 1")


def test_a_union_that_collapses_equal_rows_is_not_distributed():
    for source in ("SELECT 'a' AS x UNION SELECT 'a'", "SELECT 1 AS x UNION SELECT 1.0", "SELECT 1 AS x UNION SELECT 2 UNION SELECT 1"):
        select = sqlglot.parse_one(f"SELECT x FROM ({source}) AS d", read="mysql")
        assert distribute_over_constant_union(select) is None
    # the collapsed rows still count once
    assert not _proven("SELECT d.x FROM (SELECT 'a' AS x UNION SELECT 'a') AS d", "SELECT 'a' UNION ALL SELECT 'a'")


def _bigquery_select(source):
    return sqlglot.parse_one(f"SELECT x FROM ({source}) AS d", read="bigquery")


def test_a_union_whose_numbers_are_one_double_is_not_distributed():
    # BigQuery reads a decimal or exponent literal as a FLOAT64, so these pairs are one row of a UNION
    for source in (
        "SELECT 0.1 AS x UNION DISTINCT SELECT 0.10000000000000000555",
        "SELECT 1e0 AS x UNION DISTINCT SELECT 1.0000000000000001e0",
        "SELECT 'k' AS s, 0.1 AS x UNION DISTINCT SELECT 'k', 0.10000000000000000555",
    ):
        assert distribute_over_constant_union(_bigquery_select(source)) is None


def test_a_union_of_distinct_doubles_and_integers_is_still_distributed():
    for source in (
        "SELECT 0.1 AS x UNION DISTINCT SELECT 0.2",
        "SELECT 9007199254740993 AS x UNION DISTINCT SELECT 9007199254740992",  # INT64 literals stay exact
    ):
        assert distribute_over_constant_union(_bigquery_select(source)) is not None


def test_the_prover_keeps_two_literals_of_one_double_as_one_row():
    one_row = "SELECT d.x FROM (SELECT 0.1 AS x UNION DISTINCT SELECT 0.10000000000000000555) AS d"
    assert not prove_equivalent_algebraic(one_row, "SELECT 0.1 AS x UNION ALL SELECT 0.10000000000000000555", dialect="bigquery", compare_names=False).proven
    assert prove_equivalent_algebraic(
        "SELECT d.x FROM (SELECT 0.1 AS x UNION DISTINCT SELECT 0.2) AS d", "SELECT 0.1 AS x UNION ALL SELECT 0.2", dialect="bigquery", compare_names=False
    ).proven
