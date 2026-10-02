import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.limit_rules import limit_rule

SCHEMA = {"t": ["a", "b"], "u": ["a", "b"], "dept": ["deptno", "name"]}


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def _rule(sql):
    tree = sqlglot.parse_one(sql, read="mysql")
    out = limit_rule(tree)
    return out.sql(dialect="mysql") if out is not None else None


def test_top_k_over_union_all_equals_pre_limited_branches_when_the_order_covers_the_output():
    union = "SELECT a FROM t UNION ALL SELECT a FROM u"
    left = f"SELECT d.a FROM ({union}) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    right = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 9)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, right)
    # A branch cut shorter than LIMIT + OFFSET can lose rows the outer cut keeps.
    short = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 3) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, short)
    # A branch with its own OFFSET drops rows.
    skipped = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4 OFFSET 1) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, skipped)
    # The other direction pushes the cut the wrong way.
    descending = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a DESC LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, descending)


def test_union_pushdown_needs_the_order_to_cover_every_output_column():
    # Ordered by a alone, a tie on a can keep either b: the branch cut and the outer cut may break it differently.
    left = "SELECT d.a, d.b FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS d ORDER BY d.a LIMIT 1"
    right = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a LIMIT 1)) AS d ORDER BY d.a LIMIT 1"
    assert not _proven(left, right)
    # Ordered by both columns, ties are equal rows.
    left_total = left.replace("ORDER BY d.a LIMIT", "ORDER BY d.a, d.b LIMIT")
    right_total = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a, b LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a, b LIMIT 1)) AS d ORDER BY d.a, d.b LIMIT 1"
    assert _proven(left_total, right_total)


def test_nested_cuts_on_the_same_order_merge():
    left = "SELECT d.a FROM (SELECT a, b FROM t ORDER BY a LIMIT 10 OFFSET 2) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 3")
    assert not _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 1")
    # The inner cut runs out first: rows 3..11 of t, then 2 skipped, so at most 8 survive.
    short = "SELECT d.a FROM (SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 2) AS d ORDER BY d.a LIMIT 20 OFFSET 1"
    assert _proven(short, "SELECT a FROM t ORDER BY a LIMIT 8 OFFSET 3")
    assert not _proven(short, "SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 3")


def test_nested_cuts_do_not_merge_on_other_orders_or_hidden_ties():
    other = "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(other, "SELECT a FROM t ORDER BY a LIMIT 3")
    # The outer output b is not fixed by the key a, so which tied row each cut keeps matters.
    ties = "SELECT d.b FROM (SELECT a, b FROM t ORDER BY a LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(ties, "SELECT b FROM t ORDER BY a LIMIT 3")


def test_offset_without_limit_is_an_unbounded_cut():
    assert _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT x.a FROM t AS x ORDER BY x.a OFFSET 2")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a OFFSET 1")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a DESC OFFSET 2")
    # OFFSET without ORDER BY skips arbitrary rows.
    assert not _proven("SELECT a FROM t OFFSET 2", "SELECT a FROM t OFFSET 2")


def test_cut_lifts_out_of_nested_projections():
    left = "SELECT e.c FROM (SELECT d.a + 1 AS c FROM (SELECT a, b FROM t ORDER BY b OFFSET 1) AS d) AS e"
    assert _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 1")
    assert not _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 2")
    # DISTINCT before the cut is not a projection of it.
    distinct = "SELECT d.a FROM (SELECT DISTINCT a, b FROM t ORDER BY a, b LIMIT 2) AS d"
    assert _rule(distinct) is None


def test_repeated_order_key_and_unread_order_are_dropped():
    assert _rule("SELECT a FROM t ORDER BY a, b DESC, a DESC LIMIT 2") == "SELECT a FROM t ORDER BY a, b DESC LIMIT 2"
    assert _proven("SELECT d.a FROM (SELECT a FROM t ORDER BY b) AS d", "SELECT a FROM t")


def test_order_read_by_a_cut_without_order_or_an_array_is_kept():
    # LIMIT without ORDER BY may follow the order of its input, so the inner ORDER BY stays.
    sql = "SELECT d.a FROM (SELECT a FROM t ORDER BY a) AS d LIMIT 1"
    tree = sqlglot.parse_one(sql, read="mysql")
    inner = tree.find(sqlglot.exp.Subquery).this
    assert limit_rule(inner) is None
    assert not _proven(sql, "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC) AS d LIMIT 1")
    array = sqlglot.parse_one("SELECT ARRAY(SELECT a FROM t ORDER BY a) AS x", read="bigquery")
    inner = [s for s in array.find_all(sqlglot.exp.Select) if s is not array]
    assert inner and all(limit_rule(s) is None for s in inner)


def test_union_branch_cut_reads_as_a_derived_table():
    branch = "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY a LIMIT 1)"
    assert _proven(f"({branch}) ORDER BY a", branch)
    assert not _proven(branch, "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY b LIMIT 1)")
