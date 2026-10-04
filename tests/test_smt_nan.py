"""NaN in the SMT prover (BigQuery): a declared FLOAT64 column can hold a NaN, and the proof reasons about it.

The rules modelled (see ``smt_values``; the ones not confirmed there are marked unverified in the prover): NaN is not
equal to anything, itself included, and every ordering comparison with it is FALSE, while ``<>`` is TRUE; ``GROUP BY`` and
``DISTINCT`` put all NaNs in one group; ``IEEE_DIVIDE(0, 0)`` and every operation on a NaN is a NaN.
"""

import math
import random

import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["x", "y", "f", "g", "s"], "u": ["k"]}
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "g": "FLOAT64", "s": "STRING"}, "u": {"k": "INT64"}}
NAN_LABEL = "FLOAT64 values are never NaN"


def prove(left, right, types=TYPES):
    return prove_equivalent_smt(left, right, schema=SCHEMA, types=types, timeout_ms=5000)


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT f FROM t WHERE f = f", "SELECT f FROM t WHERE f IS NOT NULL"),
        ("SELECT f FROM t WHERE NOT (f < 1.0)", "SELECT f FROM t WHERE f >= 1.0"),
        ("SELECT f FROM t WHERE f <> 1.0", "SELECT f FROM t WHERE f < 1.0 OR f > 1.0"),
        ("SELECT f FROM t WHERE f <= 1 OR f > 1", "SELECT f FROM t WHERE f IS NOT NULL"),
        ("SELECT f FROM t WHERE f < g OR f >= g", "SELECT f FROM t WHERE f IS NOT NULL AND g IS NOT NULL"),
        ("SELECT CASE WHEN f > 0 THEN 1 WHEN f <= 0 THEN 0 ELSE 2 END AS v FROM t", "SELECT CASE WHEN f > 0 THEN 1 WHEN f IS NULL THEN 2 ELSE 0 END AS v FROM t"),
        ("SELECT f FROM t WHERE COALESCE(f, 0.0) = COALESCE(f, 0.0)", "SELECT f FROM t"),
    ],
)
def test_a_pair_that_differs_only_on_nan_is_refuted_with_a_nan_database(left, right):
    result = prove(left, right)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert any(isinstance(v, float) and math.isnan(v) for rows in result.counterexample.tables.values() for row in rows for v in row.values())
    assert not any(a.startswith(NAN_LABEL) for a in result.assumptions)


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT DISTINCT f FROM t", "SELECT f FROM t GROUP BY f"),
        ("SELECT f FROM t WHERE f BETWEEN 0.0 AND 1.0", "SELECT f FROM t WHERE f >= 0.0 AND f <= 1.0"),
        ("SELECT f FROM t WHERE f IN (1.0, 2.0)", "SELECT f FROM t WHERE f = 1.0 OR f = 2.0"),
        ("SELECT f FROM t WHERE f NOT IN (1.0, 2.0)", "SELECT f FROM t WHERE NOT (f = 1.0 OR f = 2.0)"),
        ("SELECT f FROM t WHERE f <> 1.0", "SELECT f FROM t WHERE NOT (f = 1.0)"),
        ("SELECT f FROM t WHERE f = 1.0 OR f <> 1.0", "SELECT f FROM t WHERE f IS NOT NULL"),
        ("SELECT f FROM t WHERE f < g", "SELECT f FROM t WHERE g > f"),
        ("SELECT f FROM t WHERE IS_NAN(f)", "SELECT f FROM t WHERE f <> f"),
        ("SELECT f FROM t WHERE NOT IS_NAN(f)", "SELECT f FROM t WHERE f = f"),
        ("SELECT a.f FROM t AS a JOIN t AS b ON a.f = b.f", "SELECT a.f FROM t AS a JOIN t AS b ON a.f = b.f AND a.f IS NOT NULL"),
        ("SELECT f + 1 AS v FROM t WHERE f + 1 > 5", "SELECT 1 + f AS v FROM t WHERE 1 + f > 5"),
        ("SELECT f, COUNT(*) AS n FROM t GROUP BY f", "SELECT f, COUNT(*) AS n FROM t GROUP BY f"),
    ],
)
def test_a_pair_that_holds_with_nan_is_still_proved(left, right):
    result = prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert not any(a.startswith(NAN_LABEL) for a in result.assumptions)


def test_an_integer_column_is_never_nan_even_beside_a_float_one():
    result = prove("SELECT x FROM t WHERE x = x AND f = f", "SELECT x FROM t WHERE x IS NOT NULL AND f = f")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    result = prove("SELECT x FROM t WHERE x < 5 OR x >= 5", "SELECT x FROM t WHERE x IS NOT NULL")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    # an INT64 converted to FLOAT64 is a number, so it is ordered
    result = prove("SELECT x FROM t WHERE CAST(x AS FLOAT64) < 5 OR CAST(x AS FLOAT64) >= 5", "SELECT x FROM t WHERE x IS NOT NULL")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT


def test_the_nan_assumption_is_listed_only_where_a_value_is_untyped():
    left, right = "SELECT f FROM t WHERE f < 1.0 OR f >= 1.0 OR IS_NAN(f)", "SELECT f FROM t WHERE f IS NOT NULL OR IS_NAN(f)"
    assert not any(a.startswith(NAN_LABEL) for a in prove(left, right).assumptions)
    # an undeclared column may be FLOAT64 and is read as never NaN: the claim is listed
    result = prove("SELECT f FROM t WHERE f < 1.0 OR f >= 1.0", "SELECT f FROM t WHERE f IS NOT NULL", types=None)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert any(a.startswith(NAN_LABEL) for a in result.assumptions)
    result = prove("SELECT f FROM t WHERE f < 1.0 OR f >= 1.0", "SELECT f FROM t WHERE f IS NOT NULL", types={"t": {"f": "FLOAT64"}})
    assert result.status is SmtStatus.NOT_EQUIVALENT
    # a declared column beside an undeclared one: the undeclared one is still assumed
    result = prove("SELECT u.k FROM u WHERE u.k < 1.0 OR u.k >= 1.0", "SELECT u.k FROM u WHERE u.k IS NOT NULL", types={"t": {"f": "FLOAT64"}})
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert any(a.startswith(NAN_LABEL) for a in result.assumptions)


def test_ieee_divide_of_zero_by_zero_is_a_nan():
    assert prove("SELECT x FROM t WHERE IEEE_DIVIDE(0, 0) = IEEE_DIVIDE(0, 0)", "SELECT x FROM t WHERE FALSE").status is SmtStatus.PROVEN_EQUIVALENT
    # the call is an unknown function for NULL (it is not known never to return one), so only FALSE is proved
    assert prove("SELECT x FROM t WHERE IEEE_DIVIDE(0, 0) < 1 OR IEEE_DIVIDE(0, 0) >= 1", "SELECT x FROM t WHERE FALSE").status is SmtStatus.PROVEN_EQUIVALENT
    # a quotient of unknown integers is a NaN only when both are zero: the pair is not proved
    result = prove("SELECT x FROM t WHERE IEEE_DIVIDE(x, y) < 5 OR IEEE_DIVIDE(x, y) >= 5", "SELECT x FROM t WHERE x IS NOT NULL AND y IS NOT NULL")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_string_cast_to_float64_may_be_a_nan():
    # CAST('NaN' AS FLOAT64) is a NaN; a text column may hold it; a literal number is not one
    assert prove("SELECT x FROM t WHERE CAST('NaN' AS FLOAT64) = CAST('NaN' AS FLOAT64)", "SELECT x FROM t WHERE FALSE").status is not SmtStatus.NOT_EQUIVALENT
    result = prove("SELECT x FROM t WHERE CAST(s AS FLOAT64) < 5 OR CAST(s AS FLOAT64) >= 5", "SELECT x FROM t WHERE s IS NOT NULL")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
    result = prove("SELECT x FROM t WHERE CAST('1.5' AS FLOAT64) < 5 OR CAST('1.5' AS FLOAT64) >= 5", "SELECT x FROM t")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT


def test_a_function_of_a_nan_is_not_assumed_to_be_ordered():
    for left, right in (
        ("SELECT f FROM t WHERE ABS(f) >= 0.0", "SELECT f FROM t WHERE f IS NOT NULL"),
        ("SELECT f FROM t WHERE ROUND(f) < 1 OR ROUND(f) >= 1", "SELECT f FROM t WHERE f IS NOT NULL"),
        ("SELECT f FROM t WHERE GREATEST(f, g) < 1 OR GREATEST(f, g) >= 1", "SELECT f FROM t WHERE f IS NOT NULL AND g IS NOT NULL"),
        ("SELECT SUM(f) AS s FROM t WHERE f > 1.0", "SELECT SUM(f) AS s FROM t WHERE NOT (f <= 1.0)"),
        ("SELECT NULLIF(f, f) AS v FROM t", "SELECT CAST(NULL AS FLOAT64) AS v FROM t"),
        ("SELECT f / g AS q FROM t WHERE f / g < 1 OR f / g >= 1", "SELECT f / g AS q FROM t WHERE f IS NOT NULL AND g IS NOT NULL"),
    ):
        assert prove(left, right).status is not SmtStatus.PROVEN_EQUIVALENT, left


def test_a_set_operation_or_null_safe_comparison_on_a_nan_is_declined():
    # how BigQuery matches a NaN there is not confirmed, so neither is claimed
    result = prove("SELECT f FROM t INTERSECT DISTINCT SELECT g FROM t", "SELECT g FROM t INTERSECT DISTINCT SELECT f FROM t")
    assert result.status is SmtStatus.NOT_PROVEN and "NaN" in result.reason
    result = prove("SELECT f FROM t WHERE f IS NOT DISTINCT FROM g", "SELECT f FROM t WHERE g IS NOT DISTINCT FROM f")
    assert result.status is SmtStatus.NOT_PROVEN and "NaN" in result.reason
    # without a FLOAT64 the same shapes are proved as before
    assert prove("SELECT x FROM t INTERSECT DISTINCT SELECT y FROM t", "SELECT y FROM t INTERSECT DISTINCT SELECT x FROM t").status in (
        SmtStatus.PROVEN_EQUIVALENT, SmtStatus.NOT_PROVEN)


def test_casting_a_nan_to_int64_is_an_error_the_prover_sees():
    result = prove("SELECT CAST(f AS INT64) AS v FROM t", "SELECT SAFE_CAST(f AS INT64) AS v FROM t")
    assert result.errors is not None and result.errors.verdict == "refines"


def test_two_rows_of_one_float_column_may_be_a_nan_and_a_number():
    # the typing fact "every value of a column has one type" must not make a column all-NaN or all-numbers
    left = "SELECT a.f FROM t AS a, t AS b WHERE a.f < b.f"
    right = "SELECT a.f FROM t AS a, t AS b WHERE a.f < b.f AND a.f = a.f AND b.f = b.f"
    assert prove(left, right).status is SmtStatus.PROVEN_EQUIVALENT
    result = prove("SELECT a.f FROM t AS a, t AS b WHERE NOT (a.f < b.f)", "SELECT a.f FROM t AS a, t AS b WHERE a.f >= b.f")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    cells = [v for row in result.counterexample.tables["t"] for v in row.values()]
    assert any(isinstance(v, float) and math.isnan(v) for v in cells)


# -- brute force: the prover against a reference evaluator of three-valued logic with NaN ---------------

NAN = float("nan")
DOMAIN = [None, NAN, -1.0, 0.0, 1.0, 2.0]
ATOMS = ["f", "g", "0.0", "1.0"]


def value(atom, row):
    return {"f": row[0], "g": row[1]}.get(atom, float(atom) if atom[0].isdigit() else None)


def compare(op, a, b):
    if a is None or b is None:
        return None
    if math.isnan(a) or math.isnan(b):
        return op == "<>"
    return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


def not3(p):
    return None if p is None else not p


def and3(a, b):
    return False if a is False or b is False else None if a is None or b is None else True


def or3(a, b):
    return True if a is True or b is True else None if a is None or b is None else False


def evaluate(node, row):
    kind = node[0]
    if kind == "cmp":
        return compare(node[1], value(node[2], row), value(node[3], row))
    if kind == "not":
        return not3(evaluate(node[1], row))
    if kind == "and":
        return and3(evaluate(node[1], row), evaluate(node[2], row))
    if kind == "or":
        return or3(evaluate(node[1], row), evaluate(node[2], row))
    if kind == "isnan":
        v = value(node[1], row)
        return None if v is None else math.isnan(v)
    raise AssertionError(kind)


def sql(node):
    kind = node[0]
    if kind == "cmp":
        return f"({node[2]} {node[1]} {node[3]})"
    if kind == "not":
        return f"(NOT {sql(node[1])})"
    if kind in ("and", "or"):
        return f"({sql(node[1])} {kind.upper()} {sql(node[2])})"
    return f"IS_NAN({node[1]})"


def random_atom_pair(rng):
    a, b = rng.choice(ATOMS[:2]), rng.choice(ATOMS)
    return a, b


def random_predicate(rng, depth=2):
    if depth == 0 or rng.random() < 0.35:
        if rng.random() < 0.15:
            return ("isnan", rng.choice(ATOMS[:2]))
        a, b = random_atom_pair(rng)
        return ("cmp", rng.choice(["=", "<>", "<", ">", "<=", ">="]), a, b)
    kind = rng.choice(["not", "and", "or"])
    if kind == "not":
        return ("not", random_predicate(rng, depth - 1))
    return (kind, random_predicate(rng, depth - 1), random_predicate(rng, depth - 1))


NEGATED = {"=": "<>", "<>": "=", "<": ">=", ">": "<=", "<=": ">", ">=": "<"}


def rewrite(node, rng):
    """One rewrite of ``node`` that is sometimes sound and sometimes only for numbers (a NaN breaks it)."""

    kind = node[0]
    if kind == "cmp":
        _, op, a, b = node
        choices = [
            ("not", ("cmp", NEGATED[op], a, b)),  # NOT (a < b) is a >= b: not with a NaN
            ("cmp", {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "<>": "<>"}[op], b, a),
            ("not", ("not", node)),
        ]
        if op == "<>":
            choices += [("or", ("cmp", "<", a, b), ("cmp", ">", a, b)), ("not", ("cmp", "=", a, b))]
        if op in ("<=", ">="):
            choices.append(("or", ("cmp", "<" if op == "<=" else ">", a, b), ("cmp", "=", a, b)))
        if op == "=":
            choices.append(("and", node, ("cmp", "=", a, a)))
        return rng.choice(choices)
    if kind == "not":
        inner = node[1]
        if inner[0] == "cmp":
            return ("cmp", NEGATED[inner[1]], inner[2], inner[3])
        if inner[0] == "not":
            return inner[1]
        if inner[0] in ("and", "or"):
            return ("or" if inner[0] == "and" else "and", ("not", inner[1]), ("not", inner[2]))
        return node
    if kind in ("and", "or"):
        if rng.random() < 0.5:
            return (kind, rewrite(node[1], rng), node[2])
        return (kind, node[1], rewrite(node[2], rng))
    return ("not", ("not", node))


def keeps(node, row):
    return evaluate(node, row) is True


ROWS = [(a, b) for a in DOMAIN for b in DOMAIN]


def test_the_prover_agrees_with_a_reference_evaluator_on_random_nan_pairs():
    rng = random.Random(484)
    proven = refuted = differing = 0
    for _ in range(70):
        left = random_predicate(rng)
        right = rewrite(left, rng)
        for _ in range(rng.choice([0, 1])):
            right = rewrite(right, rng)
        differ = [row for row in ROWS if keeps(left, row) != keeps(right, row)]
        left_sql = f"SELECT f, g FROM t WHERE {sql(left)}"
        right_sql = f"SELECT f, g FROM t WHERE {sql(right)}"
        result = prove(left_sql, right_sql)
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            proven += 1
            assert not differ, f"proved a pair that differs at {differ[0]}: {left_sql} / {right_sql}"
        elif result.status is SmtStatus.NOT_EQUIVALENT:
            refuted += 1
            assert differ, f"refuted a pair that agrees on every row: {left_sql} / {right_sql}"
            table = result.counterexample.tables["t"]
            rows = [(row.get("f"), row.get("g")) for row in table]
            assert any(keeps(left, r) != keeps(right, r) for r in rows) or len(rows) > 1
        differing += bool(differ)
    assert proven >= 10 and refuted >= 5 and differing >= 10  # the sample has all three kinds


def test_a_nan_counterexample_reaches_the_json_endpoint_as_text():
    import json

    from kumosql import pipeline_equivalence

    result = prove("SELECT f FROM t WHERE f = f", "SELECT f FROM t WHERE f IS NOT NULL")
    cells = [pipeline_equivalence._json_cell(v) for row in result.counterexample.tables["t"] for v in row.values()]
    assert "NaN" in cells
    json.dumps(cells, allow_nan=False)  # a NaN float would be rejected here
