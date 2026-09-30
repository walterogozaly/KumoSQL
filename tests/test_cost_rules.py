import pytest
from sqlglot import exp

from kumosql import VerificationStatus, apply_rule, available_rules
from kumosql.cost_rules import CATALOG, NOT_IMPLEMENTED, SHIPPED, rule_catalog
from kumosql.distinct_safety import distinct_is_redundant
from kumosql.equivalence import prove_equivalent

RULE = "remove_redundant_distinct"

REDUNDANT = {
    "one_key": (
        "SELECT DISTINCT region, COUNT(*) AS n FROM `p.d.customers` GROUP BY region",
        "SELECT region, COUNT(*) AS n FROM `p.d.customers` GROUP BY region",
    ),
    "two_keys_reordered_and_aliased": (
        "SELECT DISTINCT SUM(amount) AS total, customer_id AS cid, order_id "
        "FROM `p.d.orders` GROUP BY order_id, customer_id",
        "SELECT SUM(amount) AS total, customer_id AS cid, order_id "
        "FROM `p.d.orders` GROUP BY order_id, customer_id",
    ),
    "inside_subquery": (
        "SELECT s.region FROM (SELECT DISTINCT region FROM `p.d.customers` GROUP BY region) AS s",
        "SELECT s.region FROM (SELECT region FROM `p.d.customers` GROUP BY region) AS s",
    ),
}

KEPT = {
    "no_group_by": "SELECT DISTINCT region FROM `p.d.customers`",
    "key_not_projected": "SELECT DISTINCT COUNT(*) AS n FROM `p.d.customers` GROUP BY region",
    "expression_key": "SELECT DISTINCT region || 'x' AS r FROM `p.d.customers` GROUP BY region || 'x'",
    "ordinal_key": "SELECT DISTINCT region FROM `p.d.customers` GROUP BY 1",
    "rollup": "SELECT DISTINCT region, COUNT(*) AS n FROM `p.d.customers` GROUP BY ROLLUP(region)",
    "alias_shadows_key": (
        "SELECT DISTINCT LOWER(name) AS region, region AS other "
        "FROM `p.d.customers` GROUP BY region"
    ),
    "projected_only_as_expression": (
        "SELECT DISTINCT UPPER(region) AS r FROM `p.d.customers` GROUP BY region"
    ),
}


@pytest.mark.parametrize("name", sorted(REDUNDANT))
def test_redundant_distinct_is_removed_and_proven(name):
    source, expected = REDUNDANT[name]

    result = apply_rule(RULE, source)

    assert " ".join(result.sql.split()) == expected
    assert result.verification.status is VerificationStatus.PROVEN
    assert result.success


@pytest.mark.parametrize("name", sorted(KEPT))
def test_distinct_is_kept_when_not_provably_redundant(name):
    source = KEPT[name]

    result = apply_rule(RULE, source)

    assert result.sql == source
    assert result.changes == 0


def test_rule_is_idempotent():
    once = apply_rule(RULE, REDUNDANT["one_key"][0])
    twice = apply_rule(RULE, once.sql)

    assert twice.sql == once.sql
    assert twice.changes == 0


def test_dml_is_left_alone():
    source = (
        "UPDATE `p.d.customers` SET region = 'x' WHERE id IN "
        "(SELECT DISTINCT id FROM `p.d.orders` GROUP BY id)"
    )

    assert apply_rule(RULE, source).changes == 0


def test_prover_does_not_equate_distinct_that_removes_rows():
    result = prove_equivalent(
        "SELECT DISTINCT region FROM `p.d.customers`",
        "SELECT region FROM `p.d.customers`",
    )

    assert result.status.value == "not_proven"


def test_condition_helper_reads_the_select():
    select = exp.maybe_parse("SELECT DISTINCT a FROM t GROUP BY a", dialect="bigquery")
    assert distinct_is_redundant(select)


def test_catalog_shape_and_states():
    rules = rule_catalog()

    assert {"id", "name", "state", "safe_when", "requires", "outcome", "measured_outcome"} <= set(rules[0])
    assert len({rule["id"] for rule in rules}) == len(rules)
    assert [rule["id"] for rule in rules if rule["state"] == SHIPPED] == [RULE]
    assert all(rule["state"] in {SHIPPED, NOT_IMPLEMENTED} for rule in rules)


def test_measured_outcome_is_unknown_until_measured():
    for rule in rule_catalog():
        assert rule["measured_outcome"] is None
        assert rule["outcome"] == "Not yet measured"


def test_every_shipped_catalog_rule_is_registered_with_conditions():
    registered = available_rules()
    for spec in CATALOG:
        assert spec.safe_when and spec.requires
        if spec.state == SHIPPED:
            assert spec.id in registered
        else:
            assert spec.id not in registered


def test_removed_distinct_returns_identical_rows_on_synthetic_data():
    pytest.importorskip("duckdb")
    from kumosql.result_equivalence import assert_result_equivalent

    schema = {
        "p.d.customers": {"id": "INT64", "name": "STRING", "region": "STRING", "active": "BOOL"},
        "p.d.orders": {"order_id": "INT64", "customer_id": "INT64", "amount": "FLOAT64"},
    }
    for source, _ in REDUNDANT.values():
        result = apply_rule(RULE, source)
        assert_result_equivalent(source, result.sql, schema, seeds=range(8))
