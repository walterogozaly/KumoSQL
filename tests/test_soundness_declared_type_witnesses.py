"""A counterexample row must be a value the declared column type can hold.

The model gives a column any value of its kind, so an ``INT64`` column could be handed ``1.5`` and a
witness that separated the queries only through that value was reported as ``not_equivalent``. No real
table holds the row: replayed with the declared type it rounds to the next integer and the two queries
agree again, so the pair is ``not_proven`` instead. Witnesses a declared type can hold are unaffected,
and so is every pair proved without declared types.
"""

from fractions import Fraction

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, _breaks_declared_type, prove_equivalent_smt

PROVERS = (prove_equivalent_smt, prove_equivalent_algebraic)
DUCK = {"INT64": "BIGINT", "INTEGER": "BIGINT", "BIGINT": "BIGINT", "NUMERIC": "DECIMAL(38, 9)", "FLOAT64": "DOUBLE"}

# (left, right, declared type): on master the witness was a fraction or a decimal an integer column cannot hold
UNDECIDED = [
    ("SELECT t.a FROM t WHERE t.a = 1.5", "SELECT t.a FROM t WHERE FALSE", "INT64"),
    ("SELECT t.a FROM t WHERE t.a > 1.5", "SELECT t.a FROM t WHERE t.a >= 2", "INT64"),
    ("SELECT t.a FROM t WHERE t.a IN (1.5, 2.5)", "SELECT t.a FROM t WHERE FALSE", "INT64"),
    ("SELECT t.a FROM t WHERE t.a BETWEEN 1.5 AND 9.5", "SELECT t.a FROM t WHERE t.a > 1 AND t.a < 10", "INT64"),
    ("SELECT t.a FROM t WHERE t.a = -1.5", "SELECT t.a FROM t WHERE FALSE", "INT64"),
    ("SELECT t.a FROM t WHERE t.a = 1.5", "SELECT t.a FROM t WHERE FALSE", "BIGINT"),
]

# a whole number still separates these, so the witness stands and the pair is refuted
REFUTED = [
    ("SELECT t.a FROM t WHERE t.a > 1.5", "SELECT t.a FROM t WHERE FALSE", "INT64"),
    ("SELECT t.a FROM t WHERE t.a = 3", "SELECT t.a FROM t WHERE FALSE", "INT64"),
    ("SELECT t.a FROM t WHERE t.a <> 1.5", "SELECT t.a FROM t WHERE t.a <= 1.5", "INT64"),
    ("SELECT t.a FROM t WHERE t.a > 1.5", "SELECT t.a FROM t WHERE t.a < 2.5", "INT64"),
    ("SELECT t.a FROM t WHERE t.a = 1.5", "SELECT t.a FROM t WHERE FALSE", "NUMERIC"),
    ("SELECT t.a FROM t WHERE t.a > 1.5", "SELECT t.a FROM t WHERE FALSE", "FLOAT64"),
]


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right,declared", UNDECIDED)
def test_no_counterexample_outside_the_declared_integer_type(prover, left, right, declared):
    result = prover(left, right, schema={"t": ["a"]}, types={"t": {"a": declared}})
    assert result.status is SmtStatus.NOT_PROVEN, (left, result.status, result.counterexample)


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right,declared", REFUTED)
def test_a_legal_witness_still_refutes(prover, left, right, declared):
    result = prover(left, right, schema={"t": ["a"]}, types={"t": {"a": declared}})
    assert result.status is SmtStatus.NOT_EQUIVALENT, (left, result.reason)
    assert result.counterexample is not None


@pytest.mark.parametrize("prover", PROVERS)
def test_without_declared_types_the_witness_is_kept(prover):
    """An undeclared column may hold any number, so ``a = 1.5`` really can be true."""

    result = prover(
        "SELECT t.a FROM t WHERE t.a = 1.5", "SELECT t.a FROM t WHERE FALSE", schema={"t": ["a"]}
    )
    assert result.status is SmtStatus.NOT_EQUIVALENT, result.reason


@pytest.mark.parametrize("prover", PROVERS)
def test_same_kind_comparisons_stay_proven(prover):
    assert prover(
        "SELECT t.a FROM t WHERE t.a = 3.0", "SELECT t.a FROM t WHERE t.a = 3",
        schema={"t": ["a"]}, types={"t": {"a": "INT64"}},
    ).status is SmtStatus.PROVEN_EQUIVALENT
    assert prover(
        "SELECT t.a FROM t WHERE t.a = 2", "SELECT t.a FROM t WHERE t.a = 2.0",
        schema={"t": ["a"]}, types={"t": {"a": "INT64"}},
    ).status is SmtStatus.PROVEN_EQUIVALENT
    assert prover(
        "SELECT t.a FROM t WHERE t.a = 1 AND t.a = 2", "SELECT t.a FROM t WHERE FALSE",
        schema={"t": ["a"]}, types={"t": {"a": "INT64"}},
    ).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT t.a FROM t WHERE t.a = 1.5", "SELECT t.a FROM t WHERE FALSE"),
        ("SELECT t.a FROM t WHERE t.a > 1.5", "SELECT t.a FROM t WHERE t.a >= 2"),
    ],
)
def test_the_witness_did_not_separate_the_queries_on_duckdb(left, right):
    """The refutation master reported is not one: with the declared type both sides return the same rows."""

    duckdb = pytest.importorskip("duckdb")

    from kumosql.bigquery_on_duckdb import configure
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    configure(db)
    db.execute(f'CREATE TABLE t ("a" {DUCK["INT64"]})')
    db.execute("INSERT INTO t VALUES (1), (2), (3)")
    rows = run_unoptimized(db, left, right)
    assert rows[0] == rows[1], rows


def test_declared_type_check_reads_one_column():
    types = {"t": {"a": "INT64"}}
    assert not _breaks_declared_type(types, "T", {"a": Fraction(2)})
    assert not _breaks_declared_type(types, "t", {"a": 2, "b": None})
    assert _breaks_declared_type(types, "t", {"a": Fraction(5, 2)})
    assert _breaks_declared_type(types, "t", {"a": "x"})
    assert _breaks_declared_type(types, "t", {"a": True})
    # a column with no declared type, and a value the declared type can hold
    assert not _breaks_declared_type(types, "t", {"b": Fraction(5, 2)})
    assert not _breaks_declared_type({"t": {"a": "NUMERIC(10, 2)"}}, "t", {"a": Fraction(5, 2)})
    assert not _breaks_declared_type({}, "t", {"a": Fraction(5, 2)})
