import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import SmtStatus, TableConstraints, prove_equivalent_smt

SCHEMA = {"t": ["id", "a"], "u": ["id", "b"]}
NOT_NULL = {"t": TableConstraints(not_null=frozenset({"id", "a"}), keys=(("id",),))}


def status(left, right, **kwargs):
    return prove_equivalent_smt(left, right, schema=SCHEMA, **kwargs).status


def test_not_null_makes_is_not_null_redundant():
    left, right = "SELECT a FROM t WHERE a IS NOT NULL", "SELECT a FROM t"
    assert status(left, right) is not SmtStatus.PROVEN_EQUIVALENT
    assert status(left, right, constraints=NOT_NULL) is SmtStatus.PROVEN_EQUIVALENT


def test_primary_key_makes_a_repeated_join_redundant():
    left = "SELECT x.a FROM t AS x JOIN t AS y ON x.id = y.id"
    right = "SELECT a FROM t"
    assert status(left, right) is not SmtStatus.PROVEN_EQUIVALENT
    assert status(left, right, constraints=NOT_NULL) is SmtStatus.PROVEN_EQUIVALENT


def test_a_non_key_self_join_is_not_redundant():
    left = "SELECT x.id FROM t AS x JOIN t AS y ON x.a = y.a"
    assert status(left, "SELECT id FROM t", constraints=NOT_NULL) is not SmtStatus.PROVEN_EQUIVALENT


def test_counterexamples_respect_the_key():
    result = prove_equivalent_smt(
        "SELECT id FROM t", "SELECT id FROM t WHERE a > 0", schema=SCHEMA, constraints=NOT_NULL
    )
    assert result.status is SmtStatus.NOT_EQUIVALENT
    rows = result.counterexample.tables["t"]
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(row["a"] is not None for row in rows)


def test_contradictory_filters_equal_an_empty_query():
    assert status("SELECT id FROM t WHERE a = 1 AND a = 2", "SELECT id FROM t WHERE 1 = 0") is SmtStatus.PROVEN_EQUIVALENT
    assert status("SELECT id FROM t WHERE a = 1", "SELECT id FROM t WHERE 1 = 0") is not SmtStatus.PROVEN_EQUIVALENT


def test_global_aggregate_over_nothing_is_one_constant_row():
    left = "SELECT COUNT(*) AS n, SUM(a) AS s FROM t WHERE 1 = 0"
    assert status(left, "SELECT 0 AS n, CAST(NULL AS INT64) AS s") is SmtStatus.PROVEN_EQUIVALENT
    assert status(left, "SELECT 0 AS n, 0 AS s") is not SmtStatus.PROVEN_EQUIVALENT
    assert status("SELECT COUNT(*) AS n FROM t WHERE 1 = 0", "SELECT id FROM t WHERE 1 = 0") is not SmtStatus.PROVEN_EQUIVALENT


def test_output_names_can_be_ignored():
    left, right = "SELECT a AS x FROM t", "SELECT a AS y FROM t"
    assert status(left, right) is SmtStatus.NOT_PROVEN
    assert status(left, right, compare_names=False) is SmtStatus.PROVEN_EQUIVALENT


def test_input_dialect_is_honoured():
    result = prove_equivalent_smt("SELECT `a` FROM `t`", "SELECT a FROM t", schema=SCHEMA, dialect="mysql")
    assert result.proven
