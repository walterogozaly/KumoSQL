"""A SUM over a group as an error site of the SMT prover (``smt_group_sums``).

A ``SUM`` of INT64 values overflows on the rows of one group, so two queries are compared group by group: the
same rows with the same values in each group fail alike, a group only one of them computes can fail only there,
and a regrouped sum is not related at all. Every verdict is also checked against DuckDB with BigQuery's guards
(``SUM`` past INT64 is an error) on every small database over a few extreme values.
"""

import itertools

import pytest

pytest.importorskip("z3")

from kumosql import smt_errors
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

SCHEMA = {"t": ["x", "y", "f", "n", "c"], "u": ["x", "y"]}
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "n": "NUMERIC", "c": "BOOL"}, "u": {"x": "INT64", "y": "INT64"}}
MAX = 2**63 - 1


def _errors(left, right):
    result = prove_equivalent_smt(left, right, schema=SCHEMA, types=TYPES, timeout_ms=5000)
    return result.errors.verdict if result.errors is not None else None


def test_having_on_a_key_moved_into_where_refines_the_original():
    assert _errors(
        "SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0", "SELECT y, SUM(x) AS s FROM t WHERE y > 0 GROUP BY y"
    ) == smt_errors.REFINES


def test_where_on_a_key_moved_into_having_introduces_an_error():
    assert _errors(
        "SELECT y, SUM(x) AS s FROM t WHERE y > 0 GROUP BY y", "SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0"
    ) == smt_errors.INTRODUCES


def test_a_having_on_the_aggregate_changes_no_group():
    assert _errors(
        "SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING SUM(x) > 0", "SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING NOT (SUM(x) <= 0)"
    ) == smt_errors.SAME


def test_a_filter_spelled_another_way_is_the_same_exposure():
    assert _errors(
        "SELECT c, SUM(x) AS s FROM t WHERE y > 1 GROUP BY c", "SELECT c, SUM(x) AS s FROM t WHERE y >= 2 GROUP BY c"
    ) == smt_errors.SAME


def test_the_order_of_the_grouping_keys_does_not_matter():
    assert _errors("SELECT y, c, SUM(x) AS s FROM t GROUP BY y, c", "SELECT y, c, SUM(x) AS s FROM t GROUP BY c, y") == smt_errors.SAME


def test_a_distinct_sum_is_related_to_a_distinct_sum_only():
    assert _errors(
        "SELECT y, SUM(DISTINCT x) AS s FROM t GROUP BY y HAVING y <> 0",
        "SELECT y, SUM(DISTINCT x) AS s FROM t WHERE y <> 0 GROUP BY y",
    ) == smt_errors.REFINES
    assert _errors(
        "SELECT y, SUM(DISTINCT x) AS s FROM t WHERE y <> 0 GROUP BY y",
        "SELECT y, SUM(DISTINCT x) AS s FROM t GROUP BY y HAVING y <> 0",
    ) == smt_errors.INTRODUCES


def test_a_join_keeps_the_group_a_join_row_at_a_time():
    left = "SELECT a.y, SUM(a.x) AS s FROM t AS a JOIN t AS b ON a.y = b.y GROUP BY a.y HAVING a.y > 0"
    right = "SELECT a.y, SUM(a.x) AS s FROM t AS a JOIN t AS b ON a.y = b.y WHERE a.y > 0 GROUP BY a.y"
    assert _errors(left, right) == smt_errors.REFINES
    # a witness database is only built over one table occurrence, so a join stays unknown in this direction
    assert _errors(right, left) == smt_errors.UNKNOWN
    assert _errors(left.replace("a.y = b.y", "b.y = a.y"), left) == smt_errors.SAME


def test_a_filter_on_the_summed_value_is_the_same_group_in_both_spellings():
    # SUM skips NULL: the rows with a NULL x are in no group's sum, so a filter that drops them changes nothing
    assert _errors("SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0", "SELECT y, SUM(x) AS s FROM t WHERE x IS NOT NULL OR x IS NULL GROUP BY y HAVING y > 0") == smt_errors.SAME


def test_a_sum_over_another_type_or_an_average_is_never_shown_to_overflow():
    for column in ("f", "n"):
        verdict = _errors(
            f"SELECT y, SUM({column}) AS s FROM t WHERE y > 0 GROUP BY y", f"SELECT y, SUM({column}) AS s FROM t GROUP BY y HAVING y > 0"
        )
        assert verdict == smt_errors.UNKNOWN
    # whether AVG of INT64 can raise an overflow error is not known
    assert _errors("SELECT y, AVG(x) AS s FROM t WHERE y > 0 GROUP BY y", "SELECT y, AVG(x) AS s FROM t GROUP BY y HAVING y > 0") == smt_errors.UNKNOWN


def test_a_regrouped_sum_is_unknown_not_the_same():
    # the same groups under a redundant key: the prover does not see through the key, so it says nothing
    verdict = _errors("SELECT y, SUM(x) AS s FROM t GROUP BY y", "SELECT y, SUM(x) AS s FROM t GROUP BY y, y + 0")
    assert verdict == smt_errors.UNKNOWN


def test_a_sum_in_a_window_relation_is_the_same_only_in_the_same_relation():
    relation = "(SELECT y, SUM(x) OVER (PARTITION BY y) AS s FROM t) AS {}"
    result = prove_equivalent_smt(
        f"SELECT y FROM {relation.format('kqw1')} WHERE y > 0",
        f"SELECT y FROM {relation.format('kqw2')} WHERE y > 0 AND TRUE",
        schema=SCHEMA,
        types=TYPES,
        timeout_ms=5000,
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert result.errors.verdict == smt_errors.SAME
    assert any("SUM" in s for s in result.errors.sites)


def test_a_witness_needs_rows_to_overflow_and_the_original_not_to():
    # the rewrite's groups are the original's, and a filter on the summed value never moves a row between groups
    left = "SELECT y, SUM(x) AS s FROM t WHERE x > 0 GROUP BY y"
    assert _errors(left, "SELECT y, SUM(x) AS s FROM t WHERE x >= 1 GROUP BY y") == smt_errors.SAME


# -- every verdict against DuckDB -------------------------------------------------------------------

duckdb = pytest.importorskip("duckdb")
import sqlglot  # noqa: E402

from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful, is_bigquery_failure  # noqa: E402

PAIRS = [
    ("SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0", "SELECT y, SUM(x) AS s FROM t WHERE y > 0 GROUP BY y"),
    ("SELECT y, SUM(x) AS s FROM t WHERE y > 0 GROUP BY y", "SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0"),
    ("SELECT y, c, SUM(x) AS s FROM t WHERE c GROUP BY y, c", "SELECT y, c, SUM(x) AS s FROM t GROUP BY y, c HAVING c"),
    ("SELECT y, c, SUM(x) AS s FROM t GROUP BY y, c HAVING c", "SELECT y, c, SUM(x) AS s FROM t WHERE c GROUP BY y, c"),
    ("SELECT y, SUM(DISTINCT x) AS s FROM t GROUP BY y HAVING y <> 0", "SELECT y, SUM(DISTINCT x) AS s FROM t WHERE y <> 0 GROUP BY y"),
    ("SELECT y, SUM(DISTINCT x) AS s FROM t WHERE y <> 0 GROUP BY y", "SELECT y, SUM(DISTINCT x) AS s FROM t GROUP BY y HAVING y <> 0"),
    ("SELECT c, SUM(x) AS s FROM t WHERE y > 1 GROUP BY c", "SELECT c, SUM(x) AS s FROM t WHERE y >= 2 GROUP BY c"),
    ("SELECT y, SUM(x) AS s FROM t GROUP BY y", "SELECT y, SUM(x) AS s FROM t GROUP BY y, y + 0"),
    ("SELECT y, SUM(x) AS s FROM t GROUP BY y HAVING y > 0 AND SUM(x) > 0", "SELECT y, SUM(x) AS s FROM t WHERE y > 0 GROUP BY y HAVING SUM(x) > 0"),
    (
        "SELECT a.y, SUM(a.x) AS s FROM t AS a JOIN t AS b ON a.y = b.y GROUP BY a.y HAVING a.y > 0",
        "SELECT a.y, SUM(a.x) AS s FROM t AS a JOIN t AS b ON a.y = b.y WHERE a.y > 0 GROUP BY a.y",
    ),
    ("SELECT SUM(x) AS s FROM t WHERE y > 0", "SELECT SUM(IF(y > 0, x, NULL)) AS s FROM t"),
]
ROWS = [(x, y, c) for x in (MAX, 1) for y in (0, 1) for c in (True, False)]


def _fails(db, sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    # BigQuery adds up every group before HAVING drops it; DuckDB filters a HAVING on a key first, so the check
    # runs the query without its HAVING (the sums are what is being compared, not the rows).
    for select in tree.find_all(sqlglot.exp.Select):
        select.set("having", None)
    tree = faithful(tree)
    try:
        db.execute(tree.sql(dialect="duckdb")).fetchall()
    except Exception as error:  # noqa: BLE001
        if is_bigquery_failure(error):
            return True
        raise
    return False


@pytest.mark.parametrize("left,right", PAIRS)
def test_the_verdict_holds_on_every_small_database(left, right):
    result = prove_equivalent_smt(left, right, schema=SCHEMA, types=TYPES, timeout_ms=5000)
    if result.errors is None:
        pytest.skip("the rows are not proven equal")
    verdict = result.errors.verdict
    db = duckdb.connect()
    configure(db)
    db.execute("CREATE TABLE t (x BIGINT, y BIGINT, f DOUBLE, n DECIMAL(38, 9), c BOOLEAN)")
    seen = set()
    for size in (1, 2, 3):
        for rows in itertools.combinations_with_replacement(ROWS, size):
            db.execute("DELETE FROM t")
            for x, y, c in rows:
                db.execute("INSERT INTO t VALUES (?, ?, NULL, NULL, ?)", [x, y, c])
            seen.add((_fails(db, left), _fails(db, right)))
    db.close()
    both, only_left, only_right = (True, True) in seen, (True, False) in seen, (False, True) in seen
    if verdict == smt_errors.SAME:
        assert not only_left and not only_right
    elif verdict == smt_errors.REFINES:
        assert not only_right
    elif verdict == smt_errors.INTRODUCES:
        assert only_right
    del both
