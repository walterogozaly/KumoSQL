import sqlglot

from kumosql import (
    EquivalenceStatus,
    build_bag_verifier_sql,
    prove_equivalent,
)


def test_proves_inline_query_equivalent_to_named_cte_under_bag_semantics():
    left = "SELECT id FROM (SELECT id FROM `p.d.customers`) AS c"
    right = "WITH customer_rows AS (SELECT id FROM `p.d.customers`) SELECT id FROM customer_rows AS c"

    result = prove_equivalent(left, right)

    assert result.status is EquivalenceStatus.PROVEN_EQUIVALENT
    assert result.left_fingerprint == result.right_fingerprint
    assert "FULL OUTER JOIN" in result.verifier_sql
    assert "COUNT(*)" in result.verifier_sql


def test_ignores_output_order_when_no_row_selection_is_involved():
    left = "SELECT id FROM `p.d.customers` ORDER BY RAND()"
    right = "SELECT id FROM `p.d.customers`"

    result = prove_equivalent(left, right)

    assert result.proven
    assert not result.diagnostics


def test_unchanged_value_nondeterminism_can_be_proven():
    for call in ("RAND()", "CURRENT_TIMESTAMP()", "GENERATE_UUID()", "rand()"):
        result = prove_equivalent(
            f"SELECT id, {call} AS v FROM `p.d.t` WHERE a = 1 AND TRUE",
            f"SELECT id, {call.upper()} AS v FROM `p.d.t` WHERE a = 1",
        )

        assert result.proven, call
        assert any("left unchanged" in d for d in result.diagnostics)


def test_changed_value_nondeterminism_is_not_proven():
    base = "SELECT id, RAND() AS v FROM `p.d.t`"
    variants = [
        "SELECT id, RAND() + 1 AS v FROM `p.d.t`",
        "SELECT id, RAND() AS v, RAND() AS w FROM `p.d.t`",
        "SELECT id, 0.5 AS v FROM `p.d.t`",
        "SELECT id, CURRENT_DATE() AS v FROM `p.d.t`",
    ]
    for other in variants:
        for left, right in ((base, other), (other, base)):
            result = prove_equivalent(left, right)
            assert not result.proven, other
    dropped = prove_equivalent(base, "SELECT id, 1 AS v FROM `p.d.t`")
    assert dropped.status is EquivalenceStatus.NOT_PROVEN


def test_merged_random_ctes_are_not_proven():
    two = "WITH a AS (SELECT RAND() AS r), b AS (SELECT RAND() AS r) SELECT a.r, b.r FROM a, b"
    one = "WITH a AS (SELECT RAND() AS r) SELECT a.r, a.r FROM a"
    assert not prove_equivalent(two, one).proven
    assert not prove_equivalent(one, two).proven


def test_unchanged_window_stays_unproven():
    sql = "SELECT id, RAND() AS v, ROW_NUMBER() OVER (ORDER BY id) AS n FROM `p.d.t`"
    assert not prove_equivalent(sql, sql).proven


def test_does_not_prove_limit_without_schema_constraints():
    result = prove_equivalent(
        "SELECT id FROM `p.d.customers` LIMIT 10",
        "SELECT id FROM `p.d.customers` LIMIT 10",
    )

    assert result.status is EquivalenceStatus.NOT_PROVEN
    assert "LIMIT/OFFSET" in result.reason


def test_structural_difference_is_not_called_inequivalent():
    result = prove_equivalent(
        "SELECT id FROM `p.d.customers`",
        "SELECT customer_id FROM `p.d.customers`",
    )

    assert result.status is EquivalenceStatus.NOT_PROVEN
    assert result.reason == "normalized query structures differ"


def test_exact_order_mode_requires_ordering():
    result = prove_equivalent(
        "SELECT id FROM `p.d.customers`",
        "SELECT id FROM `p.d.customers`",
        ignore_row_order=False,
    )

    assert result.status is EquivalenceStatus.NOT_PROVEN
    assert "explicit ordering" in result.reason


def test_safety_guards_prefer_not_proven_for_nondeterministic_constructs():
    samples = [
        "SELECT * FROM `p.d.t` TABLESAMPLE SYSTEM (10 PERCENT)",
        "SELECT ROW_NUMBER() OVER (ORDER BY id) AS n FROM `p.d.t`",
        "SELECT ARRAY_AGG(id) FROM `p.d.t`",
        "SELECT id FROM `p.d.t` ORDER BY RAND() LIMIT 10",
    ]

    for sql in samples:
        result = prove_equivalent(sql, sql)
        assert not result.proven, sql


def test_cte_column_aliases_are_not_discarded_during_canonicalization():
    result = prove_equivalent(
        "WITH a(x) AS (SELECT 1) SELECT * FROM a",
        "WITH a(y) AS (SELECT 1) SELECT * FROM a",
    )

    assert result.status is EquivalenceStatus.NOT_PROVEN


def test_bag_verifier_is_deterministic_text():
    verifier = build_bag_verifier_sql("SELECT 1 AS x", "SELECT 1 AS x")

    assert verifier == build_bag_verifier_sql("SELECT 1 AS x", "SELECT 1 AS x")
    assert "TO_JSON_STRING(left_row)" in verifier
    assert isinstance(sqlglot.parse_one(verifier, read="bigquery"), sqlglot.exp.Select)


def test_cte_order_is_normalized_when_dependencies_allow_it():
    result = prove_equivalent(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT * FROM b JOIN a ON TRUE",
        "WITH b AS (SELECT 2 AS y), a AS (SELECT 1 AS x) SELECT * FROM b JOIN a ON TRUE",
    )

    assert result.proven


def test_cte_reorder_does_not_hide_swapped_bodies():
    result = prove_equivalent(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS x) SELECT * FROM a",
        "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS x) SELECT * FROM b",
    )

    assert not result.proven


def test_implicit_cte_alias_matters_for_equivalence():
    result = prove_equivalent(
        "WITH a AS (SELECT 1 AS x) SELECT a FROM a",
        "WITH b AS (SELECT 1 AS x) SELECT a FROM b",
    )

    assert not result.proven


def test_forward_cte_reference_blocks_reordering():
    result = prove_equivalent(
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a",
        "WITH b AS (SELECT 1 AS x), a AS (SELECT * FROM b) SELECT * FROM a",
    )

    assert not result.proven
