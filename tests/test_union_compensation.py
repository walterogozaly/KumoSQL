"""Union compensation: a model that covers part of the query's rows, plus the rest read from the base tables.

``rewrite_over_model(.., union_compensation=True)`` proposes ``sigma_residual(V) UNION ALL sigma_(P_Q AND (P_V) IS
NOT TRUE)(base)`` (re-aggregated for a grouped model); the algebraic prover proves it, and each case here is
also run on a small database with NULLs and duplicates, where ``NOT (P_V)`` would lose rows.
"""

import duckdb
import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402

SCHEMA = {"t": ["id", "k", "a", "b", "v"], "d": ["k", "name"]}
SETUP = [
    "CREATE TABLE t (id INT, k INT, a INT, b INT, v INT)",
    "INSERT INTO t VALUES (1, 1, 1, 1, 10), (2, 1, 2, NULL, 20), (3, 2, 3, 5, NULL), (4, 2, NULL, 6, 40), (5, 3, 5, NULL, NULL),"
    " (6, 3, 7, 8, 5), (7, NULL, 1, 9, 3), (8, 4, 9, 1, 7), (9, 4, 9, 1, 7), (10, NULL, NULL, NULL, NULL)",
    "CREATE TABLE d (k INT, name TEXT)",
    "INSERT INTO d VALUES (1, 'x'), (2, 'y'), (3, 'z'), (4, 'w'), (4, 'w2')",
]


def _rows(sql):
    con = duckdb.connect()
    for statement in SETUP:
        con.execute(statement)
    return sorted(map(repr, con.execute(sql).fetchall()))


def _union(query, model, **options):
    reuse = rewrite_over_model(query, model, schema=SCHEMA, union_compensation=True, **options)
    assert reuse.rewritten, reuse.reason
    assert _rows(query) == _rows(reuse.inlined_sql), reuse.sql
    return reuse


def test_off_by_default():
    """The replacement reads base tables, so a caller that needs the model to stand alone never gets one."""

    assert not rewrite_over_model("SELECT id FROM t WHERE id < 6", "SELECT id FROM t WHERE id < 3", schema=SCHEMA).rewritten


def test_wider_range_reads_the_rest_from_the_base_table():
    reuse = _union("SELECT id, a FROM t WHERE id < 6", "SELECT id, a FROM t WHERE id < 3")
    assert reuse.strategy == "union-rows"
    assert "UNION ALL" in reuse.sql and "IS TRUE" in reuse.sql and "mv0" in reuse.sql


def test_plain_reuse_is_still_preferred():
    reuse = rewrite_over_model("SELECT id, a FROM t WHERE id < 2", "SELECT id, a FROM t WHERE id < 3", schema=SCHEMA, union_compensation=True)
    assert reuse.rewritten and reuse.strategy.startswith("spj") and "UNION" not in reuse.sql


def test_complement_keeps_the_rows_where_the_model_predicate_is_null():
    """``b < 6`` is NULL on rows 2, 5 and 10; ``NOT (b < 6)`` would drop them from the base-table branch."""

    reuse = _union("SELECT id FROM t WHERE a < 10", "SELECT id, a FROM t WHERE b < 6")
    assert "IS TRUE" in reuse.sql
    assert _rows("SELECT id FROM t WHERE a < 10 AND NOT (b < 6)") != _rows("SELECT id FROM t WHERE a < 10 AND (b < 6) IS NOT TRUE")


def test_extra_filter_on_the_model_part_and_the_base_part():
    _union("SELECT id, v FROM t WHERE id < 8 AND k = 1 OR id < 8 AND k = 2", "SELECT id, v, k FROM t WHERE id < 4")  # an OR is one conjunct
    reuse = _union("SELECT id, v FROM t WHERE id < 8 AND a > 1", "SELECT id, v, a FROM t WHERE id < 4")
    assert reuse.sql.count("a > 1") + reuse.sql.count("1 < ") >= 1


def test_join_range_with_duplicates():
    _union(
        "SELECT t.id, d.name FROM t JOIN d ON t.k = d.k WHERE t.id < 9",
        "SELECT t.id, d.name FROM t JOIN d ON t.k = d.k WHERE t.id < 5",
    )


def test_query_that_runs_on_top_of_the_union_of_rows():
    reuse = _union("SELECT DISTINCT k FROM t WHERE id < 9", "SELECT id, k FROM t WHERE id < 4")
    assert reuse.strategy == "union-rows-on-top"
    reuse = _union("SELECT k, SUM(v) AS s, COUNT(*) AS c FROM t WHERE id < 9 GROUP BY k", "SELECT id, k, v FROM t WHERE id < 4")
    assert reuse.strategy == "union-rows-regrouped"
    _union("SELECT id FROM t WHERE id < 9 ORDER BY id", "SELECT id FROM t WHERE id < 4")


def test_contained_mode_takes_only_a_model_inside_the_query():
    """Calcite's union rewriting: the model's rows are read as they are, so they must all be rows of the query."""

    inside = rewrite_over_model("SELECT id, a FROM t WHERE id > 2", "SELECT id, a FROM t WHERE id > 6", schema=SCHEMA, union_compensation="contained")
    assert inside.rewritten and "UNION ALL" in inside.sql and _rows("SELECT id, a FROM t WHERE id > 2") == _rows(inside.inlined_sql)
    overlapping = ("SELECT id, a FROM t WHERE id < 8", "SELECT id, a FROM t WHERE id > 3")
    assert not rewrite_over_model(*overlapping, schema=SCHEMA, union_compensation="contained").rewritten
    assert rewrite_over_model(*overlapping, schema=SCHEMA, union_compensation=True).rewritten


def test_model_with_no_row_of_the_query_is_not_rewritten_in_contained_mode():
    assert not rewrite_over_model("SELECT id FROM t WHERE id < 3", "SELECT id FROM t WHERE id > 6", schema=SCHEMA, union_compensation="contained").rewritten


def test_missing_column_is_not_rewritten():
    assert not rewrite_over_model("SELECT id, b FROM t WHERE id < 6", "SELECT id, a FROM t WHERE id < 3", schema=SCHEMA, union_compensation=True).rewritten


def test_model_with_an_unreadable_residual_that_it_does_not_imply_is_not_rewritten():
    """``a > 1`` cannot be read from the model and does not follow from ``id < 3``."""

    assert not rewrite_over_model("SELECT id FROM t WHERE id < 6 AND a > 1", "SELECT id FROM t WHERE id < 3", schema=SCHEMA, union_compensation=True).rewritten


def test_other_table_than_the_model_reads_is_not_rewritten():
    assert not rewrite_over_model("SELECT t.id FROM t JOIN d ON t.k = d.k WHERE t.id < 6", "SELECT id FROM t WHERE id < 3", schema=SCHEMA, union_compensation=True).rewritten


# -- grouped models ---------------------------------------------------------------------------------


def test_sum_and_count_of_a_grouped_model_are_combined():
    reuse = _union(
        "SELECT k, SUM(v) AS s, COUNT(v) AS c FROM t WHERE id < 9 GROUP BY k",
        "SELECT k, SUM(v) AS s, COUNT(v) AS c FROM t WHERE id < 5 GROUP BY k",
    )
    assert reuse.strategy == "union-aggregate" and "SUM(u.p0)" in reuse.sql


def test_groups_the_model_split_across_the_range_are_added_up():
    """The filter is on a column that is not a group key, so a group has rows on both sides of it."""

    reuse = _union("SELECT k, SUM(v) AS s FROM t GROUP BY k", "SELECT k, SUM(v) AS s FROM t WHERE id < 6 GROUP BY k")
    assert reuse.strategy == "union-aggregate"


def test_filter_on_a_group_key_of_the_model():
    _union("SELECT k, SUM(v) AS s FROM t WHERE k < 4 GROUP BY k", "SELECT k, SUM(v) AS s FROM t WHERE k < 3 GROUP BY k")


def test_coarser_grouping_and_minimum_maximum():
    _union(
        "SELECT k, MIN(v) AS lo, MAX(v) AS hi FROM t WHERE id < 9 GROUP BY k",
        "SELECT k, a, MIN(v) AS lo, MAX(v) AS hi FROM t WHERE id < 5 GROUP BY k, a",
    )


def test_global_query_over_a_grouped_model():
    _union("SELECT SUM(v) AS s, COUNT(v) AS c FROM t WHERE id < 9", "SELECT k, SUM(v) AS s, COUNT(v) AS c FROM t WHERE id < 5 GROUP BY k")
    # the model's groups hold no row of this query: the count is still 0 once the base rows are added
    _union("SELECT COUNT(v) AS c FROM t WHERE id < 9 AND id > 4", "SELECT k, COUNT(v) AS c FROM t WHERE id > 4 AND id < 5 GROUP BY k")


def test_average_from_a_sum_and_a_count():
    _union(
        "SELECT k, AVG(v) AS m FROM t WHERE id < 9 GROUP BY k",
        "SELECT k, SUM(v) AS s, COUNT(v) AS c FROM t WHERE id < 5 GROUP BY k",
    )


def test_average_without_its_count_is_not_rewritten():
    """An average of the model's averages weights groups wrongly, and the base rows add a part the model cannot weigh."""

    query, model = "SELECT k, AVG(v) AS m FROM t WHERE id < 9 GROUP BY k", "SELECT k, AVG(v) AS m FROM t WHERE id < 5 GROUP BY k"
    assert not rewrite_over_model(query, model, schema=SCHEMA, union_compensation=True).rewritten


def test_distinct_aggregate_is_not_rewritten():
    query, model = "SELECT k, COUNT(DISTINCT v) AS c FROM t WHERE id < 9 GROUP BY k", "SELECT k, COUNT(DISTINCT v) AS c FROM t WHERE id < 5 GROUP BY k"
    assert not rewrite_over_model(query, model, schema=SCHEMA, union_compensation=True).rewritten


def test_model_that_drops_groups_is_not_rewritten():
    query, model = "SELECT k, SUM(v) AS s FROM t WHERE id < 9 GROUP BY k", "SELECT k, SUM(v) AS s FROM t WHERE id < 5 GROUP BY k HAVING SUM(v) > 20"
    assert not rewrite_over_model(query, model, schema=SCHEMA, union_compensation=True).rewritten


# -- the prover rule behind the grouped cases -------------------------------------------------------


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="postgres", compare_names=False, timeout_ms=5000)


GROUPED_SPLIT = (
    "SELECT u.k, SUM(u.s) AS s FROM (SELECT k, SUM(v) AS s FROM t WHERE {first} GROUP BY k UNION ALL "
    "SELECT k, SUM(v) AS s FROM t WHERE {second} GROUP BY k) AS u GROUP BY u.k"
)


def test_recombining_a_partitioned_grouped_union():
    whole = "SELECT k, SUM(v) AS s FROM t GROUP BY k"
    assert _prove(whole, GROUPED_SPLIT.format(first="id < 5", second="(id < 5) IS NOT TRUE")).proven


def test_recombining_refuses_branches_that_do_not_partition():
    whole = "SELECT k, SUM(v) AS s FROM t GROUP BY k"
    assert not _prove(whole, GROUPED_SPLIT.format(first="id < 5", second="id < 7")).proven  # overlap
    assert not _prove(whole, GROUPED_SPLIT.format(first="a < 5", second="NOT (a < 5)")).proven  # a NULL row is in neither


def test_recombining_counts_needs_a_row_even_when_no_group_exists():
    """Without an outer GROUP BY the sum of the branches' counts is NULL when no group has a row; the count is 0."""

    grouped = (
        "SELECT SUM(u.c) AS c FROM (SELECT COUNT(v) AS c FROM t WHERE {w} GROUP BY k UNION ALL "
        "SELECT COUNT(v) AS c FROM t WHERE NOT ({w}) GROUP BY k) AS u"
    )
    assert not _prove("SELECT COUNT(v) AS c FROM t WHERE id < 0", grouped.format(w="id < 5")).proven
