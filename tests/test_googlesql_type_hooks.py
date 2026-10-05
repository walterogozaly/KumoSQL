"""The GoogleSQL type checker inside schema-change assessment and the set-operation type assumption.

``kumosql.googlesql_types`` answers only where it is certain: these tests pin a type it corrects (sqlglot wrong or
unknown), a case where it says unknown and the old answer stays, and the set-operation assumption it removes.
"""

import pytest

from kumosql import schema_change
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target
from kumosql.set_operation_types import ASSUMPTION as SET_TYPES, mixed_types, unchecked_types


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _pipeline(schema=None, **sql):
    models = {f"p.d.{n}": Model(Target("p", "d", n), "table", q) for n, q in sql.items()}
    raw = schema or {"a": "INT64", "b": "STRING", "c": "INT64"}
    return Pipeline(models, {"p.d.raw": Target("p", "d", "raw")}, {"p.d.raw": raw})


def _retyped(pipeline, column, new_type):
    result = pipeline.assess_schema_change("retype_column", "p.d.raw", column, new_type=new_type)
    assert not result.breaks and not result.unknown
    return {e.model.split(".")[-1]: e.retyped for e in result.output_changes}


@pytest.fixture
def sqlglot_types_only(monkeypatch):
    """The answers schema-change gave before the type checker: sqlglot's types as they are."""

    monkeypatch.setattr(schema_change, "_checked_types", lambda query, tables, columns: tuple(columns))


# --- schema change ----------------------------------------------------------------------------------------------


def test_union_supertype_follows_a_retyped_arm():
    # sqlglot types a UNION by its first arm, so FLOAT64 on the second arm never reached the output
    p = _pipeline(m="SELECT a AS x FROM `p.d.raw` UNION ALL SELECT c AS x FROM `p.d.raw`")
    assert _retyped(p, "c", "FLOAT64") == {"m": ("x",)}
    assert _retyped(p, "c", "NUMERIC") == {"m": ("x",)}  # INT64 and NUMERIC have the supertype NUMERIC
    assert _retyped(p, "a", "FLOAT64") == {"m": ("x",)}


def test_union_supertype_that_does_not_change_is_not_reported():
    # FLOAT64 and NUMERIC have the supertype FLOAT64, so retyping the NUMERIC arm to FLOAT64 changes nothing
    p = _pipeline({"a": "FLOAT64", "c": "NUMERIC"}, m="SELECT a AS x FROM `p.d.raw` UNION ALL SELECT c AS x FROM `p.d.raw`")
    assert _retyped(p, "c", "FLOAT64") == {}


def test_before_the_checker_a_retyped_union_arm_was_missed(sqlglot_types_only):
    p = _pipeline(m="SELECT a AS x FROM `p.d.raw` UNION ALL SELECT c AS x FROM `p.d.raw`")
    assert _retyped(p, "c", "FLOAT64") == {}  # the miss the checker fixes: sqlglot kept INT64


def test_a_type_sqlglot_leaves_unknown_is_given():
    # TRUNC of an INT64 is FLOAT64, of a NUMERIC NUMERIC; sqlglot leaves both UNKNOWN, so the change went unseen
    p = _pipeline(m="SELECT TRUNC(a) AS t FROM `p.d.raw`")
    assert _retyped(p, "a", "NUMERIC") == {"m": ("t",)}


def test_before_the_checker_an_unknown_type_hid_the_change(sqlglot_types_only):
    p = _pipeline(m="SELECT TRUNC(a) AS t FROM `p.d.raw`")
    assert _retyped(p, "a", "NUMERIC") == {}


def test_a_type_sqlglot_got_wrong_is_corrected():
    # DATE - DATE is an INTERVAL in GoogleSQL; sqlglot says DATE
    p = _pipeline({"d": "DATE", "e": "DATE"}, m="SELECT d - e AS gap FROM `p.d.raw`")
    columns = schema_change._resolve(p, "p.d.m", {"p.d.raw": (("d", "DATE"), ("e", "DATE"))})
    assert columns == (("gap", "INTERVAL"),)


def test_where_the_checker_is_unknown_sqlglot_answers_as_before():
    # MAX_BY is not typed by the checker (unknown); sqlglot's STRING stays, so a retype of the value column is seen
    p = _pipeline(m="SELECT MAX_BY(b, a) AS v FROM `p.d.raw`")
    columns = schema_change._resolve(p, "p.d.m", {"p.d.raw": (("a", "INT64"), ("b", "STRING"), ("c", "INT64"))})
    assert columns == (("v", "STRING"),)
    assert _retyped(p, "b", "BYTES") == {"m": ("v",)}


def test_a_failure_of_the_checker_keeps_sqlglots_answer(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the checker is not available")

    monkeypatch.setattr(schema_change, "infer", broken)
    p = _pipeline(m="SELECT a + 1 AS x, b FROM `p.d.raw`")
    assert _retyped(p, "a", "FLOAT64") == {"m": ("x",)}


def test_the_checker_never_changes_a_type_both_agree_on():
    p = _pipeline(m="SELECT a, b, a + 1 AS x, CAST(c AS NUMERIC) AS n FROM `p.d.raw`")
    tables = {"p.d.raw": (("a", "INT64"), ("b", "STRING"), ("c", "INT64"))}
    with_checker = schema_change._resolve(p, "p.d.m", tables)
    assert with_checker == (("a", "INT64"), ("b", "STRING"), ("x", "INT64"), ("n", "NUMERIC"))


# --- set operations ----------------------------------------------------------------------------------------------

TYPES = {"a": {"x": "INT64"}, "b": {"x": "INT64"}}
SCHEMA = {"a": ["x", "k"], "b": ["x", "k"]}
LEFT = "SELECT y FROM (SELECT ABS(x) AS y FROM a UNION ALL SELECT x AS y FROM b) d WHERE y = 3"
RIGHT = "SELECT ABS(x) AS y FROM a WHERE ABS(x) = 3 UNION ALL SELECT x AS y FROM b WHERE x = 3"


def _prove(types, left=LEFT, right=RIGHT):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types, compare_names=False, timeout_ms=3000)


def test_assumption_is_dropped_when_every_branch_type_is_known_and_equal():
    # ABS(x) is not a column, cast or literal, so the declared-type reader left it unknown; the checker types it INT64
    assert unchecked_types(LEFT, TYPES) is False
    result = _prove(TYPES)
    assert result.proven and SET_TYPES not in result.assumptions


def test_assumption_stays_when_a_branch_type_is_unknown():
    assert unchecked_types(LEFT, {"a": {"x": "INT64"}}) is True  # table b is not declared
    assert unchecked_types(LEFT, None) is True
    result = _prove({"a": {"x": "INT64"}})
    assert result.proven and SET_TYPES in result.assumptions


def test_assumption_stays_when_the_known_branch_types_differ():
    types = {"a": {"x": "INT64"}, "b": {"x": "FLOAT64"}}
    assert unchecked_types(LEFT, types) is True
    assert mixed_types(LEFT, types) is None  # not a decline: the old reading never knew the first branch's type
    result = _prove(types)
    assert result.proven and SET_TYPES in result.assumptions


def test_assumption_stays_outside_bigquery():
    # the checker types GoogleSQL only
    assert unchecked_types(LEFT, TYPES, "duckdb") is True


def test_star_branches_over_known_tables_need_no_assumption():
    types = {"a": {"x": "INT64", "k": "STRING"}, "b": {"x": "INT64", "k": "STRING"}}
    assert unchecked_types("SELECT * FROM a UNION ALL SELECT * FROM b", types) is False
    assert unchecked_types("SELECT * FROM a UNION ALL SELECT * FROM b", {**types, "b": {"x": "INT64", "k": "INT64"}}) is True
    assert unchecked_types("SELECT * FROM a UNION ALL SELECT * FROM b", {"a": types["a"]}) is True
    assert unchecked_types("SELECT * EXCEPT (k) FROM a UNION ALL SELECT * FROM b", types) is True


def test_a_checker_failure_keeps_the_assumption(monkeypatch):
    from kumosql import set_operation_types

    def broken(*args, **kwargs):
        raise RuntimeError("the checker is not available")

    monkeypatch.setattr(set_operation_types, "infer", broken)
    assert unchecked_types(LEFT, TYPES) is True


def test_a_null_branch_fits_any_known_type():
    types = {"a": {"x": "INT64"}}
    assert unchecked_types("SELECT ABS(x) AS y FROM a UNION ALL SELECT NULL", types) is False
    assert unchecked_types("SELECT ABS(x) AS y FROM a UNION ALL SELECT ABS(x) FROM a UNION ALL SELECT NULL", types) is False
