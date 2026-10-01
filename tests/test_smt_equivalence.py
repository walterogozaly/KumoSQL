import json

import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import SmtStatus, main, prove_equivalent_smt


def _status(left, right, **kwargs):
    return prove_equivalent_smt(left, right, **kwargs).status


EQUIVALENT = [
    pytest.param(
        "SELECT a FROM t WHERE a > 5 AND a > 3",
        "SELECT a FROM t WHERE a > 5",
        id="redundant-range-filter",
    ),
    pytest.param(
        "SELECT x.a, y.b FROM t AS x JOIN u AS y ON x.id = y.id",
        "SELECT x.a, y.b FROM u AS y JOIN t AS x ON y.id = x.id",
        id="join-order-and-symmetric-predicate",
    ),
    pytest.param(
        "SELECT x.a FROM t x, u y WHERE x.id = y.id",
        "SELECT x.a FROM t x JOIN u y ON x.id = y.id",
        id="comma-join-to-inner-join",
    ),
    pytest.param(
        "SELECT id FROM (SELECT id, a FROM t WHERE a = 1) AS s WHERE id > 0",
        "WITH s AS (SELECT id, a FROM t WHERE id > 0) SELECT id FROM s WHERE a = 1",
        id="filter-pushdown-through-cte",
    ),
    pytest.param(
        "SELECT a FROM t WHERE a = 1 OR a IS NULL",
        "SELECT a FROM t WHERE COALESCE(a, 1) = 1",
        id="coalesce-null-handling",
    ),
    pytest.param(
        "SELECT a FROM t WHERE a <> 1 OR a = 1",
        "SELECT a FROM t WHERE a IS NOT NULL",
        id="three-valued-tautology",
    ),
    pytest.param(
        "SELECT a FROM t WHERE NOT (a <> 1)",
        "SELECT a FROM t WHERE a = 1",
        id="negated-comparison",
    ),
    pytest.param(
        "SELECT a FROM t WHERE a IN (1, 2)",
        "SELECT a FROM t WHERE a = 2 OR a = 1",
        id="in-list",
    ),
    pytest.param(
        "SELECT a FROM t WHERE a BETWEEN 1 AND 5",
        "SELECT a FROM t WHERE a >= 1 AND a <= 5",
        id="between",
    ),
    pytest.param(
        "SELECT CASE WHEN a > 0 THEN 'p' ELSE 'n' END AS k FROM t",
        "SELECT IF(a > 0, 'p', 'n') AS k FROM t",
        id="case-to-if",
    ),
    pytest.param(
        "SELECT a + b AS s FROM t",
        "SELECT b + a AS s FROM t",
        id="commutative-addition",
    ),
    pytest.param(
        "SELECT a FROM t WHERE UPPER(s) = 'X'",
        "SELECT a FROM t WHERE 'X' = UPPER(s)",
        id="uninterpreted-function",
    ),
    pytest.param(
        "SELECT a FROM t WHERE d >= '2020-01-01'",
        "SELECT a FROM t WHERE d >= DATE '2020-01-01'",
        id="date-literal-coercion",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM t",
        "SELECT a FROM t GROUP BY a",
        id="distinct-to-group-by",
    ),
    pytest.param(
        "SELECT DISTINCT x.a FROM t AS x JOIN t AS y ON x.a = y.a",
        "SELECT DISTINCT a FROM t WHERE a IS NOT NULL",
        id="self-join-elimination-under-distinct",
    ),
    pytest.param(
        "SELECT a, COUNT(*) AS n FROM t GROUP BY a",
        "SELECT a, COUNT(1) AS n FROM t GROUP BY 1",
        id="count-star-vs-count-constant",
    ),
    pytest.param(
        "SELECT a, SUM(b) AS s FROM t WHERE c > 0 GROUP BY a HAVING SUM(b) > 10",
        "SELECT a, SUM(b) AS s FROM t WHERE 0 < c GROUP BY a HAVING SUM(b) > 10.0",
        id="group-by-having",
    ),
    pytest.param(
        "SELECT a FROM t UNION ALL SELECT b AS a FROM u",
        "SELECT b AS a FROM u UNION ALL SELECT a FROM t",
        id="union-all-branch-order",
    ),
    pytest.param(
        "SELECT a FROM t UNION DISTINCT SELECT a FROM t WHERE a > 1",
        "SELECT DISTINCT a FROM t",
        id="union-distinct-absorbs-subset",
    ),
    pytest.param(
        "SELECT s.n FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS s WHERE s.n > 1",
        "WITH s AS (SELECT a, COUNT(*) AS n FROM t GROUP BY a) SELECT n FROM s WHERE n > 1",
        id="aggregate-subquery-to-cte",
    ),
    pytest.param(
        "SELECT a FROM t WHERE flag",
        "SELECT a FROM t WHERE flag = TRUE",
        id="boolean-column-predicate",
    ),
]


@pytest.mark.parametrize("left,right", EQUIVALENT)
def test_proves_equivalent_rewrites(left, right):
    result = prove_equivalent_smt(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert result.proven
    assert result.assumptions


NOT_PROVEN = [
    pytest.param(
        "SELECT x.a FROM t x JOIN t y ON x.a = y.a",
        "SELECT a FROM t",
        id="self-join-changes-multiplicity",
    ),
    pytest.param("SELECT a FROM t WHERE a = a", "SELECT a FROM t", id="null-self-equality"),
    pytest.param(
        "SELECT a, COUNT(b) AS n FROM t GROUP BY a",
        "SELECT a, COUNT(*) AS n FROM t GROUP BY a",
        id="count-nullable-column",
    ),
    pytest.param("SELECT a FROM t", "SELECT DISTINCT a FROM t", id="distinct-drops-duplicates"),
    pytest.param(
        "SELECT a FROM t UNION ALL SELECT a FROM u",
        "SELECT a FROM t UNION DISTINCT SELECT a FROM u",
        id="union-all-vs-distinct",
    ),
    pytest.param(
        "SELECT s.n FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS s",
        "SELECT s.n FROM (SELECT a, COUNT(*) AS n FROM u GROUP BY a) AS s",
        id="different-opaque-subqueries",
    ),
    pytest.param("SELECT (a + 2) * 3 AS x FROM t", "SELECT 3 * a + 6 AS x FROM t", id="float-arithmetic-is-uninterpreted"),
    pytest.param("SELECT (a + b) - b AS x FROM t", "SELECT a AS x FROM t", id="null-operand-cancels"),
]


@pytest.mark.parametrize("left,right", NOT_PROVEN)
def test_never_proves_inequivalent_queries(left, right):
    assert _status(left, right) is not SmtStatus.PROVEN_EQUIVALENT


def test_exact_arithmetic_is_opt_in():
    left, right = "SELECT (a + 2) * 3 AS x FROM t", "SELECT 3 * a + 6 AS x FROM t"
    assert _status(left, right, exact_arithmetic=True) is SmtStatus.PROVEN_EQUIVALENT


def test_counterexample_for_weaker_filter():
    result = prove_equivalent_smt("SELECT a FROM t WHERE a > 5", "SELECT a FROM t WHERE a > 3")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    example = result.counterexample
    assert example is not None
    (row,) = example.tables["t"]
    assert 3 < row["a"] <= 5
    assert example.left_rows == []
    assert example.right_rows == [(row["a"],)]


def test_counterexample_for_null_semantics():
    result = prove_equivalent_smt("SELECT a FROM t WHERE a <> 1 OR a = 1", "SELECT a FROM t")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample.tables == {"t": [{"a": None}]}


def test_counterexample_on_empty_input_for_global_aggregate():
    result = prove_equivalent_smt(
        "SELECT COUNT(*) AS n FROM t WHERE FALSE", "SELECT SUM(a) AS n FROM t WHERE FALSE"
    )
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample.left_rows == [(0,)]
    assert result.counterexample.right_rows == [(None,)]


def test_counterexample_for_dropped_join():
    result = prove_equivalent_smt(
        "SELECT x.a FROM t x JOIN u y ON x.id = y.id", "SELECT x.a FROM t x"
    )
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample.tables["u"] == []


def test_no_counterexample_when_uninterpreted_functions_are_involved():
    result = prove_equivalent_smt("SELECT UPPER(s) AS s FROM t", "SELECT LOWER(s) AS s FROM t")
    assert result.status is SmtStatus.NOT_PROVEN


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t LIMIT 5",
        "SELECT a FROM t x JOIN u y USING (id)",
        "SELECT RAND() AS r FROM t",
        "SELECT a FROM t WHERE a IN (SELECT a FROM u LIMIT 1)",
        "SELECT ROW_NUMBER() OVER (ORDER BY a) AS r FROM t",
        "SELECT a FROM t WHERE d = '2020-1-1'",
        "SELECT * FROM t",
        "SELECT s.v FROM t, (SELECT t.a AS v, ROW_NUMBER() OVER () AS r FROM u) AS s",
    ],
)
def test_unsupported_features_are_not_proven(sql):
    result = prove_equivalent_smt(sql, sql)
    assert result.status is SmtStatus.NOT_PROVEN
    assert result.reason.startswith("unsupported")


def test_schema_enables_star_and_unqualified_join_columns():
    schema = {"t": ["id", "a"], "u": ["id", "b"]}
    left = "SELECT * FROM t WHERE a > 1"
    right = "SELECT id, a FROM t WHERE 1 < a"
    assert _status(left, right, schema=schema) is SmtStatus.PROVEN_EQUIVALENT
    join_left = "SELECT a, b FROM t JOIN u ON t.id = u.id"
    join_right = "SELECT t.a, u.b FROM u JOIN t ON u.id = t.id"
    assert _status(join_left, join_right, schema=schema) is SmtStatus.PROVEN_EQUIVALENT


def test_output_column_names_must_match():
    result = prove_equivalent_smt("SELECT a AS x FROM t", "SELECT a AS y FROM t")
    assert result.status is SmtStatus.NOT_PROVEN
    assert "named" in result.reason


def test_cli_reports_json(tmp_path, capsys):
    left = tmp_path / "left.sql"
    right = tmp_path / "right.sql"
    left.write_text("SELECT a FROM t WHERE a > 5")
    right.write_text("SELECT a FROM t WHERE a > 3")
    assert main([str(left), str(right)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_equivalent"
    assert payload["counterexample"]["tables"]["t"]


def test_counterexample_falls_back_when_no_integral_one_exists():
    """The bounds differ only for non-integral values, so the plain model must be used, not crash."""

    result = prove_equivalent_smt("SELECT x FROM t WHERE x >= 1", "SELECT x FROM t WHERE x > 0")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample is not None


def test_order_by_limit_compares_cores_and_ordering():
    schema = {"dept": ["deptno", "name"]}
    base = "SELECT deptno, name FROM dept WHERE deptno > 1 ORDER BY deptno, name LIMIT 3"
    same = "SELECT d.deptno, d.name FROM dept d WHERE d.deptno > 1 AND TRUE ORDER BY 1, 2 LIMIT 3"
    assert prove_equivalent_smt(base, same, schema=schema).proven
    for other in (
        base.replace("LIMIT 3", "LIMIT 4"),
        base.replace("deptno, name LIMIT", "deptno DESC, name LIMIT"),
        base.replace("deptno > 1", "deptno > 2"),
        "SELECT deptno, name FROM dept WHERE deptno > 1 ORDER BY deptno, name",
    ):
        assert not prove_equivalent_smt(base, other, schema=schema).proven
    # Ordering by a subset of the columns leaves ties: the proof records that assumption.
    partial = prove_equivalent_smt(
        "SELECT deptno, name FROM dept ORDER BY deptno LIMIT 2",
        "SELECT deptno, name FROM dept d ORDER BY deptno LIMIT 2",
        schema=schema,
    )
    assert partial.proven and any("tied" in a for a in partial.assumptions)
    # Without ORDER BY the rows are arbitrary: nothing is proved.
    assert not prove_equivalent_smt("SELECT deptno FROM dept LIMIT 2", "SELECT deptno FROM dept LIMIT 2", schema=schema).proven


def test_limit_zero_is_empty():
    schema = {"dept": ["deptno", "name"]}
    assert prove_equivalent_smt("SELECT * FROM dept LIMIT 0", "SELECT * FROM dept WHERE 1 = 0", schema=schema).proven
    assert prove_equivalent_smt(
        "(SELECT deptno FROM dept LIMIT 0) UNION ALL (SELECT deptno FROM dept)", "SELECT deptno FROM dept", schema=schema
    ).proven


def test_counterexample_does_not_leave_a_key_column_null():
    """A column only one query reads must not turn NULL in the counterexample (it broke NOT NULL keys)."""
    from kumosql.smt_equivalence import SmtStatus, TableConstraints, prove_equivalent_smt

    constraints = {"t": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
    result = prove_equivalent_smt(
        "SELECT COUNT(DISTINCT id) AS n FROM t",
        "SELECT COUNT(*) AS n FROM t",
        schema={"t": ["id", "x"]},
        constraints=constraints,
    )
    assert result.status is not SmtStatus.NOT_EQUIVALENT
