"""False proofs from an independent review of merged soundness PRs (S016), each with a near-miss that still proves."""

import pytest

from kumosql import rewrite
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.equivalence import prove_equivalent
from kumosql.layout_equivalence import layout_only_change, restore_function_case
from kumosql.set_operation_types import mixed_types
from kumosql.set_operations import positional_sql_pair
from kumosql.smt_equivalence import prove_equivalent_smt


def _algebraic(left, right, **kwargs):
    return prove_equivalent_algebraic(left, right, compare_names=False, timeout_ms=3000, **kwargs).proven


# BY NAME alignment must not reorder the select list under an ``ORDER BY 1`` (PR #277). On t=(10,20) and
# u=(1,9),(2,0) the left query is (10,20),(9,1) and the right one (10,20),(0,2).
BY_NAME = "SELECT a,b FROM (SELECT x AS a,y AS b FROM t UNION ALL BY NAME (SELECT x AS b,y AS a FROM u ORDER BY 1 LIMIT 1)) d"
POSITIONAL = "SELECT a,b FROM (SELECT x AS a,y AS b FROM t UNION ALL (SELECT y AS a,x AS b FROM u ORDER BY 1 LIMIT 1)) d"


def test_by_name_keeps_an_ordinal_order_by_on_the_branch_as_written():
    assert not _algebraic(BY_NAME, POSITIONAL)
    assert not prove_equivalent_smt(BY_NAME, POSITIONAL, compare_names=False, timeout_ms=3000).proven
    left, _, problem = positional_sql_pair(BY_NAME, "SELECT 1")
    assert problem is None and "ORDER BY 1" in left and "SELECT x AS b, y AS a FROM u" in left


def test_by_name_wraps_a_branch_grouped_by_ordinal():
    left, _, _ = positional_sql_pair("SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT y AS b, x AS a FROM u GROUP BY 1, 2", "SELECT 1")
    assert "SELECT y AS b, x AS a FROM u GROUP BY 1, 2" in left


def test_by_name_without_ordinals_still_proves():
    left = "SELECT a, b FROM (SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT x AS b, y AS a FROM u) d"
    right = "SELECT a, b FROM (SELECT x AS a, y AS b FROM t UNION ALL SELECT y AS a, x AS b FROM u) d"
    assert _algebraic(left, right)


# A filter or projection moved into the branches of a set operation runs before the branches are converted
# to the common type (PR #366). Over a(x BIGINT)=1 and b(x VARCHAR)='01' DuckDB keeps one row on the left
# (the union is VARCHAR, so 1 becomes '1') and two on the right (1 = '01' compares as integers).
SCHEMA = {"a": ["x"], "b": ["x"]}
MIXED = {"a": {"x": "BIGINT"}, "b": {"x": "VARCHAR"}}


@pytest.mark.parametrize("operation", ["UNION ALL", "UNION DISTINCT", "INTERSECT DISTINCT", "EXCEPT DISTINCT"])
@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
def test_filter_is_not_moved_before_a_set_operation_converts_types(operation, dialect):
    left = f"SELECT x FROM (SELECT x FROM a {operation} SELECT x FROM b) d WHERE x = '01'"
    right = f"SELECT x FROM a WHERE x = '01' {operation} SELECT x FROM b WHERE x = '01'"
    assert not _algebraic(left, right, schema=SCHEMA, types=MIXED, dialect=dialect)
    assert not _algebraic(left, right, schema=SCHEMA, types={"a": {"x": "INT64"}, "b": {"x": "FLOAT64"}}, dialect=dialect)
    assert _algebraic(left, right, schema=SCHEMA, types={"a": {"x": "INT64"}, "b": {"x": "INT64"}}, dialect=dialect)


def test_mixed_types_are_followed_through_ctes_and_derived_tables():
    types = {"t": {"p": "FLOAT64", "k": "INT64", "s": "STRING"}, "u": {"n": "NUMERIC"}}
    assert mixed_types("WITH c AS (SELECT p AS z FROM t) SELECT z FROM c UNION ALL SELECT k FROM t", types)
    assert mixed_types("SELECT z FROM (SELECT k AS z FROM t UNION ALL SELECT NULL) d UNION ALL SELECT s FROM t", types)
    assert mixed_types("SELECT n FROM u UNION ALL SELECT p FROM t", types)
    assert mixed_types("SELECT s FROM t UNION ALL SELECT 0", types)
    # the same type, a NULL, a small integer literal next to any number, an explicit cast, or an unknown table
    assert mixed_types("SELECT p FROM t UNION ALL SELECT 0", types) is None
    assert mixed_types("SELECT n FROM u UNION ALL SELECT 1", types) is None
    assert mixed_types("SELECT NULL AS z UNION ALL SELECT s FROM t", types) is None
    assert mixed_types("SELECT CAST(k AS FLOAT64) FROM t UNION ALL SELECT p FROM t", types) is None
    assert mixed_types("SELECT x FROM unknown UNION ALL SELECT s FROM t", types) is None
    assert mixed_types("SELECT s FROM t UNION ALL SELECT k FROM t", None) is None


# Temporary function names are case sensitive (PR #338): a script may create both `abs` and `ABS`.
def _scripts(declaration):
    before = f"CREATE TEMP FUNCTION {declaration}(x INT64) AS (x+1); SELECT abs(1) AS v;"
    return before, before.replace("SELECT abs(1)", "SELECT ABS(1)")


@pytest.mark.parametrize("declaration", ["`abs`", "/* note */ abs", "-- note\nabs", "IF NOT EXISTS `abs`", "`ABS`"])
def test_a_created_function_keeps_its_case_whatever_its_declaration_looks_like(declaration):
    before, after = _scripts(declaration)
    assert not layout_only_change(before, after)
    assert restore_function_case(before, after) == before
    assert rewrite.verify_rewrite(before, after).status is not rewrite.VerificationStatus.PROVEN


def test_two_created_functions_differing_in_case_are_not_one_function():
    before = "CREATE TEMP FUNCTION `abs`(x INT64) AS (x+1); CREATE TEMP FUNCTION /* other */ `ABS`(x INT64) AS (x+2); SELECT abs(1) AS v;"
    after = before.replace("SELECT abs(1)", "SELECT ABS(1)")
    assert not layout_only_change(before, after)
    assert rewrite.verify_rewrite(before, after).status is not rewrite.VerificationStatus.PROVEN


def test_a_script_with_a_created_function_still_proves_other_changes():
    before = "CREATE TEMP FUNCTION f(x INT64) AS (x+1); SELECT f(a) AS v FROM t WHERE 1=1;"
    assert rewrite.verify_rewrite(before, before.replace(" WHERE 1=1", "")).status is rewrite.VerificationStatus.PROVEN
    assert layout_only_change("select count(x) from t", "SELECT COUNT(x) FROM t")


# Adjacent string or bytes literals need whitespace or a comment between them in GoogleSQL (PR #338).
@pytest.mark.parametrize(
    "before,after",
    [("SELECT 'a' 'b'", "SELECT 'a''b'"), ("SELECT r'a' r'b'", "SELECT r'a'r'b'"), ("SELECT b'a' b'b'", "SELECT b'a'b'b'"), ("SELECT 'a'\n'b'", "SELECT 'a'\"b\"")],
)
def test_literal_chunks_must_stay_separated(before, after):
    assert not layout_only_change(before, after)
    assert rewrite.verify_rewrite(before, after).status is not rewrite.VerificationStatus.PROVEN


def test_literal_chunks_may_change_their_separator():
    assert layout_only_change("SELECT 'a'  'b'", "SELECT 'a'\n'b'")
    assert layout_only_change("SELECT 'a' /*c*/'b'", "SELECT 'a'/*c*/ 'b'")


# A single-quoted bytes literal holding a line break is not valid GoogleSQL; canonicalizing it must not make it
# equal to the valid ``b'a\x0Ab'`` (PR #401).
@pytest.mark.parametrize("prefix,quote", [("b", "'"), ("b", '"'), ("rb", "'"), ("br", '"')])
def test_a_bytes_literal_with_a_line_break_is_not_canonicalized(prefix, quote):
    left = f"SELECT {prefix}{quote}a\nb{quote} AS v, '\\n' AS marker FROM t"
    right = f"SELECT {prefix}{quote}a\\x0Ab{quote} AS v, '\\n' AS marker FROM t"
    assert not prove_equivalent(left, right).proven
    assert not _algebraic(left, right)


def test_triple_quoted_bytes_may_hold_a_line_break():
    assert prove_equivalent("SELECT b'''a\nb''' AS v, '\\n' AS m FROM t", "SELECT b'a\\x0Ab' AS v, '\\n' AS m FROM t").proven
