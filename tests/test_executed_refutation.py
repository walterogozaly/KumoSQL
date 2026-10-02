"""Executed counterexample search behind ``prove_equivalent_algebraic(search_counterexample=True)``."""

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.executed_refutation import search_counterexample  # noqa: E402
from kumosql.result_equivalence import SyntheticDataset, SyntheticTable, compare_outputs, execute_on_dataset  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, TableConstraints  # noqa: E402

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {name: {c: "INT64" for c in cols} for name, cols in SCHEMA.items()}


def _replays(counterexample, left, right, types=TYPES):
    """Run both queries on the counterexample from scratch and compare the bags."""

    typed = {name: dict(cols) for name, cols in types.items()}
    tables = {
        name: SyntheticTable(
            tuple((c, t) for c, t in typed[name].items()),
            tuple(tuple(row.get(c) for c in typed[name]) for row in counterexample.tables.get(name, [])),
        )
        for name in typed
    }
    dataset = SyntheticDataset(0, tables)
    a, _ = execute_on_dataset(left, typed, dataset, run_tag="l")
    b, _ = execute_on_dataset(right, typed, dataset, run_tag="r")
    return not compare_outputs(a, b, check_column_names=False)[0]


def _prove(left, right, **kwargs):
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, types=TYPES, compare_names=False, search_counterexample=True, **kwargs
    )


@pytest.mark.parametrize(
    "left,right",
    [
        # outer join to inner join
        ("SELECT t.b AS p, u.b AS q FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.b AS p, u.b AS q FROM t JOIN u ON t.a = u.a"),
        # NOT IN with a NULL in the subquery
        (
            "SELECT t.a AS k FROM t WHERE t.a NOT IN (SELECT u.b FROM u)",
            "SELECT t.a AS k FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)",
        ),
        # a join that duplicates rows
        ("SELECT t.a AS k FROM t JOIN u ON t.a = u.b", "SELECT t.a AS k FROM t WHERE t.a IN (SELECT u.b FROM u)"),
        # UNION ALL against UNION DISTINCT
        ("SELECT a AS k FROM t UNION ALL SELECT b AS k FROM u", "SELECT a AS k FROM t UNION DISTINCT SELECT b AS k FROM u"),
    ],
)
def test_refutes_with_a_replayable_database(left, right):
    result = _prove(left, right)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample is not None
    assert _replays(result.counterexample, left, right)
    # shrunk: a refutation of these shapes needs at most a few rows
    assert sum(len(rows) for rows in result.counterexample.tables.values()) <= 4


def test_off_by_default():
    left, right = "SELECT t.b AS p FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.b AS p FROM t JOIN u ON t.a = u.a"
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False)
    assert result.status is SmtStatus.NOT_PROVEN


def test_needs_declared_types():
    left, right = "SELECT t.b AS p FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.b AS p FROM t JOIN u ON t.a = u.a"
    assert search_counterexample(left, right, schema=SCHEMA, types=None) is None
    assert search_counterexample(left, right, schema=SCHEMA, types={"t": TYPES["t"]}) is None


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT a FROM t LIMIT 1", "SELECT a FROM t ORDER BY a LIMIT 1"),
        ("SELECT a, ROW_NUMBER() OVER (ORDER BY b) AS r FROM t", "SELECT a, 1 AS r FROM t"),
        ("SELECT CAST(a AS STRING) AS s FROM t", "SELECT '1' AS s FROM t"),
        ("SELECT a FROM t WHERE CAST(a AS STRING) LIKE '1%'", "SELECT a FROM t"),
        ("SELECT a FROM t", "SELECT ANY_VALUE(a) AS a FROM t"),
        ("SELECT a FROM t", "SELECT MOD(a, 2) AS a FROM t"),
    ],
)
def test_declines_constructs_duckdb_may_read_differently(left, right):
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_a_zero_divisor_is_a_bigquery_error_not_a_difference():
    # DuckDB returns infinity for a / 0; BigQuery fails. The pair agrees whenever
    # BigQuery returns a result, so no database may refute it.
    left = "SELECT a / b AS v FROM t"
    right = "SELECT IF(b = 0, NULL, a / b) AS v FROM t"
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_respects_keys_and_foreign_keys():
    schema = {"emp": ["id", "dept"], "dept": ["id", "name"]}
    types = {"emp": {"id": "INT64", "dept": "INT64"}, "dept": {"id": "INT64", "name": "INT64"}}
    constraints = {
        "emp": TableConstraints(keys=(("id",),), foreign_keys=((("dept",), "dept", ("id",)),)),
        "dept": TableConstraints(keys=(("id",),)),
    }
    # equal under the key on emp.id
    assert search_counterexample(
        "SELECT DISTINCT id FROM emp", "SELECT id FROM emp", schema=schema, types=types, constraints=constraints
    ) is None
    # equal under the foreign key: every non-NULL emp.dept has its dept row
    assert search_counterexample(
        "SELECT emp.id FROM emp JOIN dept ON emp.dept = dept.id",
        "SELECT emp.id FROM emp WHERE emp.dept IS NOT NULL",
        schema=schema,
        types=types,
        constraints=constraints,
    ) is None
    # without the constraints both pairs differ
    assert search_counterexample("SELECT DISTINCT id FROM emp", "SELECT id FROM emp", schema=schema, types=types) is not None
    assert search_counterexample(
        "SELECT emp.id FROM emp JOIN dept ON emp.dept = dept.id",
        "SELECT emp.id FROM emp WHERE emp.dept IS NOT NULL",
        schema=schema,
        types=types,
    ) is not None


def test_equivalent_pairs_are_never_refuted():
    pairs = [
        ("SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL", "SELECT COUNT(a) AS v FROM t"),
        ("SELECT t.a FROM t JOIN u ON t.a = u.a", "SELECT t.a FROM u JOIN t ON u.a = t.a"),
        ("SELECT a, b FROM t WHERE a > 1 OR b > 1", "SELECT a, b FROM t WHERE b > 1 UNION ALL SELECT a, b FROM t WHERE a > 1 AND (b <= 1 OR b IS NULL)"),
        ("SELECT AVG(a) AS v FROM t", "SELECT SUM(a) / COUNT(a) AS v FROM t"),
    ]
    for left, right in pairs:
        assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None, (left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        # STRUCT read field by field
        (
            "SELECT y.a FROM (SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)) AS y",
            "SELECT y.a FROM (SELECT s.g AS a FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)) AS y",
        ),
        # day arithmetic on dates
        (
            "SELECT DATE_DIFF(DATE_ADD(DATE '2024-01-01', INTERVAL x.a DAY), DATE '2024-01-01', DAY) AS a FROM t AS x",
            "SELECT x.b AS a FROM t AS x",
        ),
        # CROSS JOIN UNNEST of an array literal
        (
            "SELECT x.a AS a, e AS b FROM t AS x CROSS JOIN UNNEST([x.a, x.b]) AS e WHERE e IS NOT NULL",
            "SELECT x.a AS a, x.a AS b FROM t AS x WHERE x.a IS NOT NULL",
        ),
    ],
)
def test_refutes_struct_date_and_unnest_shapes(left, right):
    counterexample = search_counterexample(left, right, schema=SCHEMA, types=TYPES)
    assert counterexample is not None
    assert _replays(counterexample, left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        # whole structs compare NULL fields differently in DuckDB
        (
            "SELECT s FROM (SELECT STRUCT(a AS f) AS s FROM t) WHERE s = s",
            "SELECT s FROM (SELECT STRUCT(a AS f) AS s FROM t)",
        ),
        # WITH OFFSET counts from 0 in BigQuery and from 1 in DuckDB
        ("SELECT o FROM t CROSS JOIN UNNEST([a]) AS e WITH OFFSET AS o", "SELECT 0 AS o FROM t"),
        # week and month boundaries differ between engines
        ("SELECT DATE_DIFF(DATE '2024-03-01', DATE '2024-01-31', MONTH) AS m FROM t", "SELECT 2 AS m FROM t"),
        ("SELECT a FROM t LEFT JOIN UNNEST([b]) AS e", "SELECT a FROM t"),
    ],
)
def test_declines_struct_date_and_unnest_forms_that_differ(left, right):
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_date_add_compares_as_a_date():
    # DuckDB's date + interval is a timestamp; read as BigQuery's DATE the pair agrees.
    left = "SELECT DATE_ADD(DATE '2024-01-01', INTERVAL 1 DAY) AS d FROM t"
    right = "SELECT DATE '2024-01-02' AS d FROM t"
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None
