import itertools
import random

import pytest
import sqlglot
from sqlglot import exp

from kumosql import (
    VerificationStatus,
    apply_rule,
    apply_rules,
    available_rules,
    prove_equivalent,
)
from kumosql.cleanup import simplify_predicate
from kumosql.equivalence import _normalize_predicate


PROVEN = VerificationStatus.PROVEN
UNCHANGED = VerificationStatus.UNCHANGED


def _body(sql):
    """Whitespace-insensitive text for assertions."""

    return " ".join(sql.split())


def test_cleanup_rules_are_registered():
    assert {
        "remove_trivial_predicates",
        "remove_redundant_parentheses",
        "deduplicate_ctes",
        "remove_unused_ctes",
    } <= set(available_rules())


# --- remove_trivial_predicates -------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        ("SELECT id FROM `p.d.t` WHERE 1 = 1", "SELECT id FROM `p.d.t`"),
        ("SELECT id FROM `p.d.t` WHERE TRUE", "SELECT id FROM `p.d.t`"),
        ("SELECT id FROM `p.d.t` WHERE 1 = 1 AND id > 3", "SELECT id FROM `p.d.t` WHERE id > 3"),
        ("SELECT id FROM `p.d.t` WHERE id > 3 AND TRUE", "SELECT id FROM `p.d.t` WHERE id > 3"),
        ("SELECT id FROM `p.d.t` WHERE id > 3 OR FALSE", "SELECT id FROM `p.d.t` WHERE id > 3"),
        ("SELECT id FROM `p.d.t` WHERE 'a' = 'a' AND x", "SELECT id FROM `p.d.t` WHERE x"),
        ("SELECT id FROM `p.d.t` WHERE NOT (2 < 1) AND x", "SELECT id FROM `p.d.t` WHERE x"),
        (
            "SELECT id FROM `p.d.t` WHERE (TRUE) AND (x OR FALSE) AND 1.5 = 1.5",
            "SELECT id FROM `p.d.t` WHERE x",
        ),
        (
            "SELECT id, COUNT(*) AS c FROM `p.d.t` GROUP BY id HAVING 1 = 1",
            "SELECT id, COUNT(*) AS c FROM `p.d.t` GROUP BY id",
        ),
        (
            "SELECT a.id FROM `p.d.t` AS a JOIN `p.d.u` AS b ON a.id = b.id AND 1 = 1",
            "SELECT a.id FROM `p.d.t` AS a JOIN `p.d.u` AS b ON a.id = b.id",
        ),
    ],
)
def test_trivial_predicates_are_removed_and_proven(source, expected):
    result = apply_rule("remove_trivial_predicates", source)

    assert _body(result.sql) == expected
    assert result.verification.status is PROVEN
    assert result.success


def test_trivial_predicates_in_subqueries_and_ctes_are_removed():
    source = """WITH a AS (SELECT id FROM `p.d.t` WHERE 1 = 1)
SELECT id FROM (SELECT id FROM a WHERE TRUE AND id > 0) AS s WHERE 1 = 1"""

    result = apply_rule("remove_trivial_predicates", source)

    assert "1 = 1" not in result.sql
    assert "TRUE" not in result.sql
    assert "id > 0" in result.sql
    assert result.verification.status is PROVEN


@pytest.mark.parametrize(
    "source",
    [
        # Annihilators would drop a predicate (and any error it raises).
        "SELECT id FROM `p.d.t` WHERE x AND FALSE",
        "SELECT id FROM `p.d.t` WHERE x OR TRUE",
        # x = x is NULL when x is NULL.
        "SELECT id FROM `p.d.t` WHERE x = x",
        # NULL filters every row; it is not TRUE.
        "SELECT id FROM `p.d.t` WHERE NULL",
        "SELECT id FROM `p.d.t` WHERE 1 = NULL",
        # HAVING without GROUP BY can make the query an aggregate.
        "SELECT COUNT(*) AS c FROM `p.d.t` HAVING TRUE",
        # An INNER JOIN needs a condition.
        "SELECT * FROM `p.d.t` AS a JOIN `p.d.u` AS b ON TRUE",
        # INT64 and FLOAT64 literals may be coerced; only exact cases fold.
        "SELECT id FROM `p.d.t` WHERE x OR 9007199254740993 = 9007199254740992.0",
        "SELECT id FROM `p.d.t` WHERE x OR 1 = 1.0",
        # Escaped strings are not compared.
        "SELECT id FROM `p.d.t` WHERE '\\x61' = 'a'",
        # Not a predicate position: rewriting could rename the output column.
        "SELECT x AND TRUE FROM `p.d.t`",
    ],
)
def test_non_trivial_or_unsafe_predicates_are_kept(source):
    result = apply_rule("remove_trivial_predicates", source)

    assert result.sql == source
    assert result.verification.status is UNCHANGED


def test_false_literal_comparison_is_folded_but_kept():
    result = apply_rule("remove_trivial_predicates", "SELECT id FROM `p.d.t` WHERE 1 = 2 OR x")

    assert _body(result.sql) == "SELECT id FROM `p.d.t` WHERE x"
    assert result.verification.status is PROVEN


# --- remove_redundant_parentheses ---------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "SELECT id FROM `p.d.t` WHERE ((a = 1)) AND (b = 2) AND (e AND f)",
            "SELECT id FROM `p.d.t` WHERE a = 1 AND b = 2 AND e AND f",
        ),
        ("SELECT id FROM `p.d.t` WHERE (a OR b)", "SELECT id FROM `p.d.t` WHERE a OR b"),
        (
            "SELECT (a + 1) AS x, (COALESCE(a, 1)) AS y FROM `p.d.t` WHERE (NOT a) OR (b IS NULL)",
            "SELECT a + 1 AS x, COALESCE(a, 1) AS y FROM `p.d.t` WHERE NOT a OR b IS NULL",
        ),
        ("SELECT id FROM `p.d.t` WHERE (a) > ((1))", "SELECT id FROM `p.d.t` WHERE a > 1"),
    ],
)
def test_redundant_parentheses_are_removed_and_proven(source, expected):
    result = apply_rule("remove_redundant_parentheses", source)

    assert _body(result.sql) == expected
    assert result.verification.status is PROVEN


@pytest.mark.parametrize(
    "source",
    [
        "SELECT id FROM `p.d.t` WHERE (a OR b) AND c",
        "SELECT (1 + 2) * 3 AS x FROM `p.d.t`",
        "SELECT a - (b - c) AS x FROM `p.d.t`",
        # BigQuery names an unaliased projection after its expression.
        "SELECT (a) FROM `p.d.t`",
        "SELECT (a).b AS c FROM `p.d.t`",
        "SELECT (x IN UNNEST(arr)) = y AS z FROM `p.d.t`",
        # Kept for readability even though AND binds tighter.
        "SELECT id FROM `p.d.t` WHERE (a AND b) OR c",
    ],
)
def test_meaningful_parentheses_are_kept(source):
    result = apply_rule("remove_redundant_parentheses", source)

    assert result.sql == source


# --- deduplicate_ctes ----------------------------------------------------


def test_duplicate_ctes_are_merged_and_proven():
    source = """WITH a AS (SELECT id FROM `p.d.t`),
b AS (SELECT id FROM `p.d.t`),
c AS (SELECT id FROM a),
d AS (SELECT id FROM b)
SELECT c.id FROM c JOIN d ON c.id = d.id"""

    result = apply_rule("deduplicate_ctes", source)

    assert result.changes == 1
    assert "b AS (" not in result.sql
    assert "FROM a AS b" in result.sql
    assert result.verification.status is PROVEN


def test_merging_cascades_to_ctes_that_become_identical():
    source = """WITH a AS (SELECT id FROM `p.d.t`),
b AS (SELECT id FROM `p.d.t`),
c AS (SELECT x.id FROM a AS x),
d AS (SELECT x.id FROM b AS x)
SELECT c1.id FROM c AS c1 JOIN d ON c1.id = d.id"""

    result = apply_rule("deduplicate_ctes", source)

    assert result.changes == 2
    assert "b AS (" not in result.sql
    assert "d AS (" not in result.sql
    assert result.verification.status is PROVEN


@pytest.mark.parametrize(
    "source",
    [
        "WITH a AS (SELECT RAND() AS r), b AS (SELECT RAND() AS r) SELECT * FROM a CROSS JOIN b",
        "WITH a AS (SELECT id FROM `p.d.t` LIMIT 1), b AS (SELECT id FROM `p.d.t` LIMIT 1) "
        "SELECT * FROM a CROSS JOIN b",
        "WITH a AS (SELECT id FROM `p.d.t`), b AS (SELECT id FROM `p.d.u`) SELECT * FROM a AS x CROSS JOIN b",
        # ``FROM a CROSS JOIN a AS b`` could read as a correlated array path.
        "WITH a AS (SELECT id FROM `p.d.t`), b AS (SELECT id FROM `p.d.t`) SELECT * FROM a CROSS JOIN b",
        # A reference that differs only in case makes resolution unclear.
        "WITH a AS (SELECT id FROM `p.d.t`), b AS (SELECT id FROM `p.d.t`) SELECT * FROM A AS x CROSS JOIN b",
    ],
)
def test_ctes_that_must_not_be_merged_are_kept(source):
    result = apply_rule("deduplicate_ctes", source)

    assert result.sql == source


# --- remove_unused_ctes --------------------------------------------------


def test_unused_ctes_are_removed_transitively_and_proven():
    source = """WITH a AS (SELECT 1 AS x),
b AS (SELECT x FROM a),
c AS (SELECT 2 AS y)
SELECT y FROM c"""

    result = apply_rule("remove_unused_ctes", source)

    assert result.changes == 2
    assert _body(result.sql) == "WITH c AS ( SELECT 2 AS y ) SELECT y FROM c"
    assert result.verification.status is PROVEN


def test_with_clause_is_dropped_when_every_cte_is_unused():
    result = apply_rule("remove_unused_ctes", "WITH a AS (SELECT 1 AS x) SELECT 1 AS z")

    assert _body(result.sql) == "SELECT 1 AS z"
    assert result.verification.status is PROVEN


def test_used_ctes_and_case_mismatches_are_kept():
    for source in (
        "WITH a AS (SELECT 1 AS x) SELECT x FROM a",
        "WITH a AS (SELECT 1 AS x) SELECT (SELECT MAX(x) FROM a) AS m",
        "WITH a AS (SELECT 1 AS x) SELECT x FROM A",
    ):
        assert apply_rule("remove_unused_ctes", source).sql == source


# --- rules together -------------------------------------------------------


def test_cleanup_pipeline_on_a_messy_query():
    source = """WITH unused AS (SELECT 1 AS z),
a AS (SELECT id, amount FROM `p.d.orders` WHERE 1 = 1),
b AS (SELECT id, amount FROM `p.d.orders` WHERE 1 = 1)
SELECT a.id, b.amount
FROM a JOIN (SELECT * FROM b WHERE (amount > 0) AND TRUE) AS bb ON ((a.id = bb.id))
JOIN b ON a.id = b.id
WHERE 1 = 1"""

    result = apply_rules(
        [
            "lift_subqueries",
            "remove_trivial_predicates",
            "remove_redundant_parentheses",
            "deduplicate_ctes",
            "remove_unused_ctes",
        ],
        source,
    )

    assert "1 = 1" not in result.sql
    assert "TRUE" not in result.sql
    assert "unused" not in result.sql
    assert result.verification.trusted
    for step in result.steps:
        assert step.verification.trusted, (step.rule, step.verification.details)


# --- the prover must still refuse wrong rewrites ------------------------


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT id FROM t WHERE x AND FALSE", "SELECT id FROM t WHERE x"),
        ("SELECT id FROM t WHERE x OR TRUE", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE NULL", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE 1 = 2", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE x = x", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE 1 = 1 AND y", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE '\\x61' = 'a'", "SELECT id FROM t"),
        ("SELECT COUNT(*) AS c FROM t HAVING TRUE", "SELECT COUNT(*) AS c FROM t"),
        ("SELECT id FROM t WHERE (a OR b) AND c", "SELECT id FROM t WHERE a OR b AND c"),
        ("SELECT id FROM t WHERE a OR (b AND c)", "SELECT id FROM t WHERE (a OR b) AND c"),
        ("SELECT (1 + 2) * 3 AS x", "SELECT 1 + 2 * 3 AS x"),
        ("SELECT a - (b - c) AS x FROM t", "SELECT a - b - c AS x FROM t"),
        ("SELECT (a) FROM t", "SELECT a FROM t"),
        ("SELECT (a).b AS c FROM t", "SELECT a.b AS c FROM t"),
        ("SELECT id FROM t WHERE x OR 9007199254740993 = 9007199254740992.0", "SELECT id FROM t WHERE x"),
        ("SELECT id FROM t WHERE x OR 1 = 1.0", "SELECT id FROM t WHERE x"),
        (
            "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS x) SELECT * FROM a CROSS JOIN b",
            "WITH a AS (SELECT 1 AS x) SELECT * FROM a CROSS JOIN a AS b",
        ),
        (
            "WITH a AS (SELECT 1 AS x) SELECT x FROM a",
            "SELECT x FROM a",
        ),
        (
            "WITH a AS (SELECT 1 AS x) SELECT x FROM A",
            "SELECT x FROM A",
        ),
    ],
)
def test_prover_rejects_unsound_cleanups(left, right):
    assert not prove_equivalent(left, right).proven


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT id FROM t WHERE 1 = 1", "SELECT id FROM t"),
        ("SELECT id FROM t WHERE a AND (b AND c)", "SELECT id FROM t WHERE (a AND b) AND c"),
        ("SELECT id FROM t WHERE ((a))", "SELECT id FROM t WHERE a"),
        ("SELECT (a) AS a FROM t", "SELECT a AS a FROM t"),
        (
            "WITH a AS (SELECT 1 AS x), b AS (SELECT 1 AS x) SELECT * FROM a AS a1 CROSS JOIN b",
            "WITH a AS (SELECT 1 AS x) SELECT * FROM a AS a1 CROSS JOIN a AS b",
        ),
        ("WITH a AS (SELECT 1 AS x) SELECT 2 AS y", "SELECT 2 AS y"),
    ],
)
def test_prover_accepts_sound_cleanups(left, right):
    result = prove_equivalent(left, right)

    assert result.proven, (result.normalized_left, result.normalized_right)


# --- three-valued logic ---------------------------------------------------


def _eval3(node, env):
    """Evaluate a predicate in SQL three-valued logic (None is NULL)."""

    if isinstance(node, exp.Paren):
        return _eval3(node.this, env)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Column):
        return env[node.name]
    if isinstance(node, exp.Not):
        value = _eval3(node.this, env)
        return None if value is None else not value
    if isinstance(node, exp.And):
        a, b = _eval3(node.this, env), _eval3(node.expression, env)
        if a is False or b is False:
            return False
        return None if a is None or b is None else True
    if isinstance(node, exp.Or):
        a, b = _eval3(node.this, env), _eval3(node.expression, env)
        if a is True or b is True:
            return True
        return None if a is None or b is None else False
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.GT)):
        a, b = int(node.this.this), int(node.expression.this)
        return {exp.EQ: a == b, exp.NEQ: a != b, exp.LT: a < b, exp.GT: a > b}[type(node)]
    raise AssertionError(f"unexpected node {node!r}")


_LEAVES = ["p", "q", "r", "TRUE", "FALSE", "NULL", "1 = 1", "1 = 2", "2 > 1", "1 <> 1"]


def _random_predicate(rng, depth):
    if depth == 0 or rng.random() < 0.25:
        return rng.choice(_LEAVES)
    kind = rng.choice(["AND", "OR", "NOT", "PAREN"])
    if kind == "NOT":
        return f"NOT ({_random_predicate(rng, depth - 1)})"
    if kind == "PAREN":
        return f"({_random_predicate(rng, depth - 1)})"
    return f"({_random_predicate(rng, depth - 1)}) {kind} ({_random_predicate(rng, depth - 1)})"


@pytest.mark.parametrize("simplify", [lambda e: simplify_predicate(e)[0], _normalize_predicate])
def test_predicate_simplification_preserves_three_valued_logic(simplify):
    rng = random.Random(20260929)
    assignments = [
        dict(zip("pqr", values)) for values in itertools.product([True, False, None], repeat=3)
    ]
    for _ in range(400):
        text = _random_predicate(rng, 4)
        original = sqlglot.parse_one(text, read="bigquery")
        simplified = simplify(original.copy())
        for env in assignments:
            assert _eval3(simplified, env) == _eval3(original, env), (
                text,
                simplified.sql("bigquery"),
                env,
            )


def test_sqlx_blocks_and_interpolations_are_kept():
    source = """config { type: "table" }
SELECT id FROM ${ref("orders")} WHERE 1 = 1 AND (id > 0)"""

    result = apply_rules(["remove_trivial_predicates", "remove_redundant_parentheses"], source)

    assert result.sql.startswith('config { type: "table" }')
    assert '${ref("orders")}' in result.sql
    assert "1 = 1" not in result.sql
    assert result.verification.status is PROVEN
