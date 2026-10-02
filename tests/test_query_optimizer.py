"""Proof-gated query rewriting (kumosql.query_optimizer)."""

import pytest

pytest.importorskip("z3")

from kumosql import query_optimizer as qo
from kumosql.algebraic_equivalence import prove_equivalent_algebraic

CATALOG = qo.Catalog(
    columns={"t": ["a", "b"], "u": ["k", "v"], "s": ["x"]},
    not_null={"t": {"a"}, "u": {"k"}, "s": set()},
    keys={"t": [("a",)], "u": [("k",)], "s": []},
)


def rewrite(sql, **options):
    out = qo.optimize(sql, CATALOG, **options)
    assert out.sql is not None, out.reason
    return " ".join(out.sql.split())


def test_star_wrappers_and_cte_chain_are_peeled():
    sql = """
    WITH base AS (SELECT a, b FROM t ORDER BY a LIMIT 5),
         l1 AS (SELECT * FROM (SELECT * FROM base) n1),
         l2 AS (SELECT * FROM l1)
    SELECT * FROM l2
    """
    assert rewrite(sql) == "SELECT a, b FROM t ORDER BY a LIMIT 5"


def test_one_row_aggregate_cross_join_is_removed():
    sql = """
    WITH w1 AS MATERIALIZED (SELECT COUNT(*) AS c FROM s WHERE x IS NULL OR x IS NOT NULL),
         q AS (SELECT a, b FROM t WHERE b > 1)
    SELECT q.* FROM q CROSS JOIN w1 WHERE w1.c >= 0
    """
    out = rewrite(sql)
    assert "COUNT" not in out and "w1" not in out
    assert "b > 1" in out


def test_one_row_join_kept_when_its_value_matters():
    sql = """
    WITH w1 AS (SELECT COUNT(*) AS c FROM s)
    SELECT t.a FROM t CROSS JOIN w1 WHERE w1.c >= 1
    """
    out = qo.optimize(sql, CATALOG)
    assert out.sql is None or "w1" in out.sql


def test_redundant_predicates_and_filtered_subqueries_merge():
    sql = """
    SELECT COUNT(*) FROM (SELECT * FROM t) tt, (SELECT * FROM u WHERE v = 7) uu
    WHERE tt.b = uu.k AND uu.v = 7 AND uu.v IN (1, 1, 7)
    """
    out = rewrite(sql, deletions=False)
    assert "SELECT *" not in out
    assert out.count("v = 7") == 1
    assert "IN (1, 7)" in out


def test_unproven_rewrite_is_never_returned(monkeypatch):
    # A rule that drops a filter is wrong; the prover must refuse it.
    def bad(tree, catalog):
        where = tree.args.get("where")
        if where is None:
            return False
        where.pop()
        return True

    monkeypatch.setattr(qo, "RULES", (("bad", bad),))
    out = qo.optimize("SELECT a FROM t WHERE b > 1", CATALOG)
    assert out.sql is None


def test_every_step_of_a_partial_chain_is_proven(monkeypatch):
    sql = "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT * FROM c) z WHERE a > 0"
    out = qo.optimize(sql, CATALOG)
    assert out.sql is not None
    assert prove_equivalent_algebraic(sql, out.sql, schema=CATALOG.columns, dialect="postgres").proven


@pytest.mark.parametrize(
    "left, right, proven",
    [
        ("SELECT q.a FROM t q CROSS JOIN (SELECT COUNT(*) AS c FROM u) w WHERE w.c >= 0", "SELECT q.a FROM t q", True),
        ("SELECT q.a FROM t q CROSS JOIN (SELECT COUNT(*) AS c FROM u) w WHERE w.c >= 1", "SELECT q.a FROM t q", False),
        ("SELECT q.a FROM t q CROSS JOIN (SELECT MAX(v) AS c FROM u) w WHERE w.c >= 0", "SELECT q.a FROM t q", False),
        ("SELECT q.a, w.c FROM t q CROSS JOIN (SELECT COUNT(*) AS c FROM u) w", "SELECT q.a, 0 AS c FROM t q", False),
        (
            "SELECT q.a, w.c + 1 AS d FROM t q CROSS JOIN (SELECT COUNT(*) AS c FROM u) w",
            "SELECT q.a, 1 + w.c AS d FROM t q, (SELECT COUNT(*) AS c FROM u) w",
            True,
        ),
        (
            "SELECT q.a FROM (SELECT a FROM t ORDER BY a LIMIT 3) q CROSS JOIN (SELECT COUNT(*) AS c FROM u) w WHERE w.c >= 0",
            "SELECT a FROM t ORDER BY a LIMIT 3",
            True,
        ),
        ("SELECT * FROM (SELECT * FROM (SELECT a FROM t ORDER BY a LIMIT 2) x) y", "SELECT a FROM t ORDER BY a LIMIT 2", True),
        ("SELECT * FROM (SELECT a FROM t ORDER BY a LIMIT 2) x WHERE a > 1", "SELECT a FROM t ORDER BY a LIMIT 2", False),
    ],
)
def test_prover_one_row_sources_and_star_wrappers(left, right, proven):
    result = prove_equivalent_algebraic(left, right, schema=CATALOG.columns, dialect="postgres")
    assert result.proven is proven, result.reason


def test_schema_profile_catalog():
    profile = {
        "tables": [
            {
                "name": "Item",
                "primary_key": ["i_item_sk"],
                "columns": [
                    {"column_name": "i_item_sk", "data_type": "integer", "is_nullable": "NO", "ordinal_position": 1},
                    {"column_name": "i_brand", "data_type": "character", "is_nullable": "YES", "ordinal_position": 2},
                ],
            }
        ]
    }
    catalog = qo.Catalog.from_schema_profile(profile)
    assert catalog.columns == {"item": ["i_item_sk", "i_brand"]}
    assert catalog.keys == {"item": [("i_item_sk",)]}
    assert catalog.not_null == {"item": {"i_item_sk"}}


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT DISTINCT a, b FROM t WHERE a > 1", "SELECT a, b FROM t WHERE a > 1"),
        ("SELECT t.a FROM t LEFT JOIN u ON t.b = u.k", "SELECT t.a FROM t"),
        ("SELECT a FROM t GROUP BY a", "SELECT a FROM t"),
        ("SELECT a FROM t WHERE b = 7 AND b IN (1, 7)", "SELECT a FROM t WHERE b = 7"),
    ],
)
def test_proven_deletions(sql, expected):
    assert rewrite(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT b FROM t",  # b is not a key
        "SELECT t.a FROM t LEFT JOIN u ON t.b = u.v",  # u.v is not unique
        "SELECT t.a FROM t JOIN u ON t.b = u.k",  # an inner join filters
    ],
)
def test_unproven_deletions_are_not_made(sql):
    assert qo.optimize(sql, CATALOG).sql is None


def test_group_by_on_a_non_key_becomes_distinct_not_nothing():
    assert rewrite("SELECT b FROM t GROUP BY b") == "SELECT DISTINCT b FROM t"


HITS = qo.Catalog(
    columns={"hits": ["w", "f", "ip", "url"]},
    types={"hits": {"w": "smallint", "f": "double precision", "ip": "integer", "url": "text"}},
    not_null={"hits": {"w", "f", "ip", "url"}},
)


def test_shifted_sums_share_one_sum_and_count():
    out = qo.optimize("SELECT SUM(w), SUM(w + 1), SUM(w + 2) FROM hits", HITS)
    assert out.sql is not None and "shared_sums" in out.steps
    assert " ".join(out.sql.split()).count("SUM(w)") == 3


def test_shifted_sums_of_floats_are_not_proven():
    # SUM(f + 1) and SUM(f) + COUNT(f) round differently for floating point.
    out = qo.optimize("SELECT SUM(f + 1), SUM(f + 2) FROM hits", HITS, deletions=False)
    assert out.sql is None


def test_group_keys_that_depend_on_another_key_are_dropped():
    out = qo.optimize("SELECT ip, ip - 1, COUNT(*) AS c FROM hits GROUP BY ip, ip - 1 ORDER BY c DESC LIMIT 10", HITS)
    assert "GROUP BY ip ORDER" in " ".join(out.sql.split())
    out = qo.optimize("SELECT 1, url, COUNT(*) AS c FROM hits GROUP BY 1, url ORDER BY c DESC LIMIT 10", HITS)
    assert "GROUP BY url ORDER" in " ".join(out.sql.split())


def test_group_key_is_kept_when_dropping_it_would_leave_a_column_ungrouped():
    # Proven equivalent (u.v is determined by the key u.k), but PostgreSQL rejects an ungrouped u.v.
    sql = "SELECT u.k, u.v, COUNT(*) AS c FROM t JOIN u ON t.b = u.k GROUP BY u.k, u.v"
    out = qo.optimize(sql, CATALOG)
    assert out.sql is None or "u.v" in out.sql.split("GROUP BY")[1]


def test_deletion_that_raises_the_estimated_cost_is_not_kept():
    out = qo.optimize("SELECT DISTINCT a, b FROM t", CATALOG, cost=lambda sql: 1.0 if "DISTINCT" in sql else 5.0)
    assert out.sql is None


@pytest.mark.parametrize(
    "left, right",
    [
        # sqlglot 30.21 parses IS NOT NULL as Is(negate=True); reading it as IS NULL made this "always empty".
        ("SELECT MIN(t.a) FROM t, u, s WHERE t.b = u.k AND s.x = u.v AND s.x IS NOT NULL", "SELECT MIN(t.a) FROM t, u, s WHERE s.x = u.v AND s.x IS NOT NULL"),
        ("SELECT t.a FROM t, s WHERE s.x IS NOT NULL", "SELECT t.a FROM t, s WHERE s.x IS NULL"),
        ("SELECT a FROM t WHERE CAST(b AS TEXT) NOT LIKE 'x%'", "SELECT a FROM t WHERE CAST(b AS TEXT) LIKE 'x%'"),
    ],
)
def test_negated_predicates_are_not_read_as_positive(left, right):
    assert not prove_equivalent_algebraic(left, right, schema=CATALOG.columns, dialect="postgres").proven
