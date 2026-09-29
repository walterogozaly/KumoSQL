from kumosql import VerificationStatus, apply_rule, get_rule


RULE = "inline_single_use_ctes"


def _apply(sql):
    return apply_rule(RULE, sql)


def test_inlines_single_use_cte_and_keeps_reference_alias():
    result = _apply(
        "WITH o AS (SELECT customer_id FROM `p.d.orders`)\n"
        "SELECT x.customer_id FROM o AS x"
    )

    assert result.changes == 1
    assert "WITH" not in result.sql
    assert ") AS x" in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_unaliased_reference_gets_the_cte_name_as_alias():
    result = _apply("WITH o AS (SELECT id FROM `p.d.orders`) SELECT o.id FROM o")

    assert ") AS o" in result.sql
    assert "o.id" in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_multi_use_cte_is_kept_and_single_use_neighbour_is_inlined():
    source = """WITH shared AS (SELECT id FROM `p.d.a`),
once AS (SELECT id FROM `p.d.b`)
SELECT * FROM shared AS s1 JOIN shared AS s2 USING (id) JOIN once USING (id)"""

    result = _apply(source)

    assert result.changes == 1
    assert "shared AS (" in result.sql
    assert "once AS (" not in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_chained_single_use_ctes_inline_in_dependency_order():
    source = """WITH a AS (SELECT id FROM `p.d.t`),
b AS (SELECT id FROM a WHERE id > 1)
SELECT * FROM b"""

    result = _apply(source)

    assert result.changes == 2
    assert "WITH" not in result.sql
    assert result.sql.index("p.d.t") < result.sql.index(") AS a") < result.sql.index("id > 1")
    assert result.verification.status is VerificationStatus.PROVEN


def test_cte_used_from_another_cte_is_inlined_there():
    source = """WITH a AS (SELECT id FROM `p.d.t`),
b AS (SELECT id FROM a),
c AS (SELECT id FROM `p.d.u`)
SELECT * FROM b AS b1 JOIN b AS b2 USING (id) JOIN c USING (id)"""

    result = _apply(source)

    assert result.changes == 2
    assert "b AS (" in result.sql
    assert "a AS (" not in result.sql
    assert "c AS (" not in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_create_table_as_is_inlined_and_verified():
    result = _apply(
        "CREATE OR REPLACE TABLE `p.d.out` AS\n"
        "WITH a AS (SELECT id FROM `p.d.t`) SELECT id FROM a"
    )

    assert result.changes == 1
    assert "CREATE OR REPLACE TABLE `p.d.out` AS" in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_sqlx_interpolations_are_preserved_and_verified():
    source = '''config { type: "table" }

WITH o AS (SELECT customer_id FROM ${ref("orders")})
SELECT c.id
FROM ${ref("customers")} AS c
JOIN o ON o.customer_id = c.id'''

    result = _apply(source)

    assert result.changes == 1
    assert result.sql.startswith('config { type: "table" }\n\n')
    assert '${ref("orders")}' in result.sql
    assert '${ref("customers")}' in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_nondeterministic_body_is_rewritten_but_flagged_unproven():
    result = _apply("WITH a AS (SELECT RAND() AS r) SELECT r FROM a")

    assert result.changes == 1
    assert result.rule_success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert any("RAND" in detail for detail in result.verification.details)
    assert not result.success


def test_conservative_skips_leave_sql_byte_for_byte():
    skipped = [
        # referenced twice
        "WITH a AS (SELECT 1 AS x) SELECT * FROM a JOIN a AS b ON TRUE",
        # referenced twice; sqlglot 26 parses the second reference as UNNEST(a)
        "WITH a AS (SELECT 1 AS id) SELECT * FROM a JOIN a AS a2 USING (id)",
        # unused
        "WITH a AS (SELECT 1 AS x) SELECT 2",
        # reference differs in case from the CTE name
        "WITH a AS (SELECT 1 AS x) SELECT * FROM A",
        # column aliases on the CTE
        "WITH a(y) AS (SELECT 1 AS x) SELECT y FROM a",
        # recursive WITH
        "WITH RECURSIVE a AS (SELECT 1 AS x UNION ALL SELECT x + 1 FROM a WHERE x < 3) SELECT * FROM a",
        # nested WITH scope could shadow the name
        "WITH a AS (SELECT 1 AS x) SELECT * FROM (WITH a AS (SELECT 2 AS x) SELECT * FROM a)",
        # time travel on the reference
        "WITH a AS (SELECT 1 AS x) SELECT * FROM a FOR SYSTEM_TIME AS OF TIMESTAMP '2024-01-01'",
        # qualified table that merely shares the CTE name
        "WITH t AS (SELECT 1 AS x) SELECT * FROM `p.d.t` JOIN t ON TRUE JOIN t AS t2 ON TRUE",
    ]

    for sql in skipped:
        result = _apply(sql)
        assert result.changes == 0, sql
        assert result.sql == sql, sql
        assert result.verification.status is VerificationStatus.UNCHANGED, sql


def test_qualified_table_with_same_name_is_not_counted_as_a_reference():
    result = _apply("WITH t AS (SELECT 1 AS x) SELECT * FROM `p.d.t` JOIN t ON TRUE")

    assert result.changes == 1
    assert "`p.d.t`" in result.sql
    assert result.verification.status is VerificationStatus.PROVEN


def test_rule_is_idempotent():
    source = "WITH a AS (SELECT id FROM `p.d.t`) SELECT * FROM a"
    rule = get_rule(RULE)

    once = rule.apply(source).sql
    twice = rule.apply(once)

    assert twice.changes == 0
    assert twice.sql == once
