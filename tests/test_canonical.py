"""Canonical SELECT text: copies in different clothes share a fingerprint, different queries never do."""

import pytest
import sqlglot

from kumosql import load_compiled_graph
from kumosql.pipeline_duplicates import _fingerprint

ORDERS = "`proj.raw.orders`"
CUSTOMERS = "`proj.raw.customers`"


def fp(sql):
    return _fingerprint(sqlglot.parse_one(sql, read="bigquery"))[0]


BASE = (
    f"SELECT o.id, o.amount, c.name FROM {ORDERS} AS o JOIN {CUSTOMERS} AS c ON o.customer_id = c.id "
    "WHERE o.amount > 5 AND o.status = 'paid' AND c.tier IN ('gold', 'silver')"
)

SAME = {
    "aliases": BASE.replace("o.", "x.").replace(" o ", " x ").replace("AS o", "AS x").replace("c.", "k.").replace("AS c", "AS k"),
    "conjunct order": BASE.replace("o.amount > 5 AND o.status = 'paid'", "o.status = 'paid' AND o.amount > 5"),
    "flipped comparison": BASE.replace("o.amount > 5", "5 < o.amount"),
    "flipped equality": BASE.replace("o.status = 'paid'", "'paid' = o.status"),
    "in list order": BASE.replace("('gold', 'silver')", "('silver', 'gold')"),
    "inner join": BASE.replace(" JOIN ", " INNER JOIN "),
    "or of equalities": BASE.replace("c.tier IN ('gold', 'silver')", "(c.tier = 'gold' OR c.tier = 'silver')"),
}

DIFFERENT = {
    "boundary": BASE.replace("o.amount > 5", "o.amount >= 5"),
    "literal": BASE.replace("'paid'", "'open'"),
    "or instead of and": BASE.replace("o.amount > 5 AND o.status", "o.amount > 5 OR o.status"),
    "left join": BASE.replace(" JOIN ", " LEFT JOIN "),
    "other output name": BASE.replace("SELECT o.id, o.amount,", "SELECT o.id, o.amount AS total,"),
    "swapped sources": BASE.replace("o.customer_id = c.id", "o.id = c.id"),
    "negated": BASE.replace("o.amount > 5", "NOT o.amount > 5"),
}


@pytest.mark.parametrize("name", SAME)
def test_rewrites_that_keep_the_query_share_a_fingerprint(name):
    assert fp(SAME[name]) == fp(BASE)


@pytest.mark.parametrize("name", DIFFERENT)
def test_changes_that_alter_the_query_get_another_fingerprint(name):
    assert fp(DIFFERENT[name]) != fp(BASE)


def test_between_ifnull_and_in_agree_with_their_spelled_out_forms():
    a = f"SELECT IFNULL(o.amount, 0) AS a, o.id FROM {ORDERS} AS o WHERE o.amount BETWEEN 3 AND 9 AND o.region IN ('eu', 'us') AND o.qty IS NOT NULL"
    b = f"SELECT COALESCE(o.amount, 0) AS a, o.id FROM {ORDERS} AS o WHERE (o.region = 'us' OR o.region = 'eu') AND o.amount >= 3 AND o.amount <= 9 AND o.qty IS NOT NULL"
    assert fp(a) == fp(b)


def test_correlated_alias_inside_a_nested_query_follows_the_rename():
    a = f"SELECT o.id FROM {ORDERS} AS o WHERE EXISTS (SELECT 1 FROM {CUSTOMERS} AS c WHERE c.id = o.customer_id AND c.tier = 'gold')"
    b = f"SELECT x.id FROM {ORDERS} AS x WHERE EXISTS (SELECT 1 FROM {CUSTOMERS} AS k WHERE k.id = x.customer_id AND k.tier = 'gold')"
    wrong = f"SELECT x.id FROM {ORDERS} AS x WHERE EXISTS (SELECT 1 FROM {CUSTOMERS} AS k WHERE k.id = x.id AND k.tier = 'gold')"
    assert fp(a) == fp(b)
    assert fp(a) != fp(wrong)


def test_the_same_text_over_different_ctes_is_not_a_duplicate():
    def model(name, base_filter):
        return {
            "target": {"database": "proj", "schema": "mart", "name": name},
            "type": "table",
            "fileName": f"definitions/{name}.sqlx",
            "query": (
                f"WITH base AS (SELECT o.id, o.amount, o.status FROM {ORDERS} AS o WHERE {base_filter}), "
                "agg AS (SELECT b.id, b.amount FROM base AS b WHERE b.amount > 5 AND b.status = 'paid' AND b.id > 0) "
                "SELECT id FROM agg"
            ),
        }

    graph = {
        "tables": [model("a", "o.amount > 0"), model("b", "o.amount > 0"), model("c", "o.amount > 100")],
        "declarations": [{"target": {"database": "proj", "schema": "raw", "name": "orders"}}],
    }
    groups = load_compiled_graph(graph).duplicate_selects(min_nodes=8)

    models = [{o.model for o in group.occurrences} for group in groups]
    assert {"proj.mart.a", "proj.mart.b"} in models
    assert not any("proj.mart.c" in group for group in models)
