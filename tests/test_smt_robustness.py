"""Inputs that used to crash or stall the provers (determinism and robustness audit, DR-05 to DR-09).

Each prover's contract is a conservative verdict: text it cannot read is ``not_proven``, never an exception, and
work the solver timeout does not cover (building mappings, expanding CTEs, reading literals) is bounded.
"""

import time
import tracemalloc

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import MAX_EXPANDED_READS, drop_case_conflicts, exponent_out_of_range, expanded_reads, parse_statements
from kumosql.bounded_equivalence import BoundedStatus, check_bounded, schema_from_prover
from kumosql.smt_equivalence import SmtStatus, _Compiler, _Prover, _prove, prove_equivalent_smt

PROVERS = (prove_equivalent_smt, prove_equivalent_algebraic)


class _Occurrence:
    def __init__(self, table, uid):
        self.table, self.uid = table, uid


def test_mappings_are_built_lazily_and_in_the_same_order():
    left = [_Occurrence(t, i) for i, t in enumerate("aaabbc")]
    right = [_Occurrence(t, 10 + i) for i, t in enumerate("cbbaaa")]
    found = [[(r.uid, l.uid) for r, l in mapping] for mapping in _Prover._bijections(left, right)]
    # itertools.product over each table's permutations: the last table varies fastest.
    assert len(found) == 6 * 2 and found[0] == [(13, 0), (14, 1), (15, 2), (11, 3), (12, 4), (10, 5)]
    assert found[1] == [(13, 0), (14, 1), (15, 2), (11, 4), (12, 3), (10, 5)]
    many = [_Occurrence("t", i) for i in range(12)]
    tracemalloc.start()
    try:
        next(_Prover._bijections(many, list(reversed(many))))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000  # 12! permutations used to be listed before the first mapping
    assert sum(1 for _ in _Prover._bijections(many, many)) == 5000


def _cte_chain(levels: int) -> str:
    ctes = ["c0 AS (SELECT a FROM t)"] + [
        f"c{i} AS (SELECT x.a FROM c{i - 1} AS x CROSS JOIN c{i - 1} AS y)" for i in range(1, levels + 1)
    ]
    return "WITH " + ", ".join(ctes) + f" SELECT a FROM c{levels}"


def test_expanded_reads_counts_cte_references():
    assert expanded_reads(sqlglot.parse_one("SELECT a FROM t JOIN u ON TRUE")) == 0  # no CTEs: nothing to expand
    assert expanded_reads(sqlglot.parse_one(_cte_chain(3))) == 8
    assert expanded_reads(sqlglot.parse_one(_cte_chain(30))) > MAX_EXPANDED_READS


@pytest.mark.parametrize("prove", PROVERS)
def test_a_doubling_cte_chain_is_declined_quickly(prove):
    sql = _cte_chain(20)
    start = time.monotonic()
    result = prove(sql, sql, timeout_ms=100)
    assert result.status is SmtStatus.NOT_PROVEN and "expand" in result.reason
    assert time.monotonic() - start < 30  # used to run for hours (2^20 table reads)


def test_bounded_checker_declines_a_doubling_cte_chain():
    sql = _cte_chain(20)
    result = check_bounded(sql, sql, schema_from_prover({"t": ["a"]}, types={"t": {"a": "INT64"}}))
    assert result.status is BoundedStatus.UNKNOWN and "expanded" in result.reason


@pytest.mark.parametrize("prove", PROVERS)
@pytest.mark.parametrize("literal", ["1e100000000", "1e-100000000", "9" * 5000], ids=["exponent", "tiny", "digits"])
def test_a_huge_numeric_literal_is_declined(prove, literal):
    start = time.monotonic()
    result = prove(f"SELECT a FROM t WHERE a > {literal}", "SELECT a FROM t WHERE a > 1", timeout_ms=100)
    assert result.status is SmtStatus.NOT_PROVEN
    assert time.monotonic() - start < 30


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '",
        "SELECT 1 /*",
        "SELECT 1 GROUP BY 1.1",
        "SELECT 1 GROUP BY 1e0",
        "SELECT a FROM t ORDER BY 1.5 LIMIT 2",
        "SELECT a FROM t ORDER BY a LIMIT 1.5",
        "SELECT " + "(" * 700 + "1" + ")" * 700,
        "SELECT 1 FROM t WHERE " + " AND ".join(f"a = {i}" for i in range(1800)),
    ],
)
def test_unreadable_input_is_not_proven_never_an_exception(sql):
    result = prove_equivalent_smt(sql, sql)
    assert result.status is SmtStatus.NOT_PROVEN, result.reason
    assert prove_equivalent_algebraic(sql, "SELECT 1").status is SmtStatus.NOT_PROVEN
    bounded = check_bounded(sql, sql, schema_from_prover({"t": ["a"]}, types={"t": {"a": "INT64"}}))
    assert bounded.status is BoundedStatus.UNKNOWN


@pytest.mark.parametrize(
    "options, error",
    [
        ({"timeout_ms": "x"}, ValueError),
        ({"timeout_ms": 0}, ValueError),
        ({"timeout_ms": True}, ValueError),
        ({"schema": {"t": "a"}}, TypeError),
        ({"schema": ["t"]}, TypeError),
        ({"types": {"t": ["INT64"]}}, TypeError),
    ],
)
def test_options_passed_wrongly_raise_a_clear_error(options, error):
    with pytest.raises(error):
        prove_equivalent_smt("SELECT a FROM t", "SELECT a FROM t", **options)


def test_timeout_none_means_the_default():
    assert prove_equivalent_smt("SELECT a FROM t", "SELECT a FROM t", timeout_ms=None).proven


def test_function_identity_does_not_depend_on_argument_insertion_order():
    sql = "SELECT SUBSTR(a, 1, 2) AS x, REGEXP_EXTRACT(a, 'x', 1) AS y FROM t"
    compiler = _Compiler(None, False)
    normal = compiler.compile(sql)
    tree = parse_statements(sql)[0]
    for node in tree.walk():
        node.args = dict(reversed(list(node.args.items())))
    proven, _ = _prove(_Prover(1000), normal, compiler._query(tree, {}))
    assert proven


@pytest.mark.parametrize("prove", PROVERS)
def test_tables_differing_only_in_case_do_not_pick_a_schema_by_insertion_order(prove):
    forward = prove("SELECT * FROM t", "SELECT a FROM t", schema={"t": ["a"], "T": ["b"]})
    backward = prove("SELECT * FROM t", "SELECT a FROM t", schema={"T": ["b"], "t": ["a"]})
    assert forward.status is backward.status is SmtStatus.NOT_PROVEN
    agreeing = prove("SELECT * FROM t", "SELECT a FROM t", schema={"t": ["a"], "T": ["A"]})
    assert agreeing.status is SmtStatus.PROVEN_EQUIVALENT


def test_drop_case_conflicts():
    assert drop_case_conflicts({"t": ["a"], "T": ["b"], "u": ["c"]}) == {"u": ["c"]}
    assert drop_case_conflicts({"t": {"a": "INT64"}, "T": {"A": "INT64"}}) == {"t": {"a": "INT64"}, "T": {"A": "INT64"}}
    assert drop_case_conflicts(None) is None


def test_bounded_schema_drops_case_conflicts_like_the_other_provers():
    conflicting = schema_from_prover({"t": ["a"], "T": ["b"]}, types={"t": {"a": "INT64"}})
    assert not conflicting.tables  # both entries dropped; the checker knows nothing about the table
    result = check_bounded("SELECT a FROM t", "SELECT a FROM t", conflicting)
    assert result.status is BoundedStatus.UNKNOWN
    agreeing = schema_from_prover({"t": ["a"], "T": ["A"]}, types={"t": {"a": "INT64"}})
    assert list(agreeing.tables) == ["t"]
    assert check_bounded("SELECT a FROM t", "SELECT a FROM t", agreeing, rows=2).status is BoundedStatus.BOUNDED_EQUIVALENT


@pytest.mark.parametrize("literal", ["1e100000000", "1e-100000000"])
@pytest.mark.parametrize("prove", PROVERS)
def test_an_extreme_exponent_is_declined_even_beside_an_identical_query(prove, literal):
    # The algebraic prover used to answer "identical scoped queries" for a query against itself when given a schema.
    sql = f"SELECT {literal} AS x"
    for options in ({}, {"schema": {"t": ["a"]}}):
        assert prove(sql, sql, **options).status is SmtStatus.NOT_PROVEN


def test_exponent_out_of_range():
    assert exponent_out_of_range("1e100000000") and exponent_out_of_range("1e-401")
    assert not exponent_out_of_range("1e5") and not exponent_out_of_range("0e100000000") and not exponent_out_of_range("12.5")


def test_bounded_solver_checks_carry_the_work_cap(monkeypatch):
    from kumosql import bounded_equivalence

    created = []
    real = bounded_equivalence.bounded_solver
    monkeypatch.setattr(bounded_equivalence, "bounded_solver", lambda ms: created.append(ms) or real(ms))
    schema = schema_from_prover({"t": ["a"]}, types={"t": {"a": "INT64"}})
    check_bounded("SELECT a FROM t", "SELECT a FROM t WHERE a = a OR a IS NULL", schema, rows=1)
    bounded_equivalence.evaluate("SELECT a FROM t", schema, {"t": [(1,)]})
    assert len(created) >= 2
