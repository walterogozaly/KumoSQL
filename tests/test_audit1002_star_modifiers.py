"""Star modifiers (``* EXCEPT``, ``* REPLACE``, ``* RENAME``, ``* ILIKE``) that were ignored, kept as regression cases.

sqlglot 30 renamed Star's ``except`` argument to ``except_``, and ``t.*`` keeps its modifiers on the Star under
the column, so checks reading ``args.get("except")`` off the select item saw nothing: the SMT prover proved
``SELECT * EXCEPT (b) FROM t`` equal to ``SELECT * FROM t``. Controls keep the modelled cases provable.
"""

import pytest
import sqlglot
from sqlglot import exp

pytest.importorskip("z3")

from kumosql import prove_equivalent_smt  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.ast_utils import star_modified, star_modifier  # noqa: E402
from kumosql.smt_equivalence import _star_columns  # noqa: E402

SCHEMA = {"t": ["a", "b"], "u": ["a", "c"]}
JOIN = "FROM t JOIN u ON t.a = u.a"


def smt(left: str, right: str):
    return prove_equivalent_smt(left, right, schema=SCHEMA)


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT * EXCEPT (b) FROM t", "SELECT * FROM t"),  # the audit's witnesses: 1 column against 2
        ("SELECT t.* EXCEPT (b) FROM t", "SELECT t.* FROM t"),
        ("SELECT * REPLACE (a + 1 AS a) FROM t", "SELECT * FROM t"),
        ("SELECT t.* REPLACE (a + 1 AS a) FROM t", "SELECT t.* FROM t"),
        ("SELECT x.b FROM (SELECT * REPLACE (b + 1 AS b) FROM t) AS x", "SELECT b FROM t"),
        ("SELECT * RENAME (a AS c) FROM t", "SELECT * FROM t"),  # output names are compared
        ("SELECT * ILIKE 'a%' FROM t", "SELECT * FROM t"),
        (f"SELECT t.* EXCEPT (a), u.c {JOIN}", f"SELECT t.*, u.c {JOIN}"),
        (f"SELECT t.* REPLACE (u.c AS b), u.c {JOIN}", f"SELECT t.*, u.c {JOIN}"),
        ("WITH c AS (SELECT * EXCEPT (b) FROM t) SELECT * FROM c", "SELECT * FROM t"),
    ],
)
def test_smt_does_not_prove_a_modified_star_equal_to_the_bare_one(left, right):
    for a, b in ((left, right), (right, left)):
        assert not smt(a, b).proven


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT * EXCEPT (b) FROM t", "SELECT a FROM t"),
        ("SELECT t.* EXCEPT (b) FROM t", "SELECT a FROM t"),
        ("SELECT * EXCLUDE (b) FROM t", "SELECT a FROM t"),
        ("SELECT * REPLACE (a + 1 AS a) FROM t", "SELECT a + 1 AS a, b FROM t"),
        ("SELECT t.* REPLACE (a + 1 AS a) FROM t", "SELECT a + 1 AS a, b FROM t"),
        ("SELECT * EXCEPT (b) REPLACE (a * 2 AS a) FROM t", "SELECT a * 2 AS a FROM t"),
        ("SELECT * RENAME (a AS c) FROM t", "SELECT a AS c, b FROM t"),
        ("SELECT * REPLACE (SUM(b) AS b) FROM t GROUP BY a", "SELECT a, SUM(b) AS b FROM t GROUP BY a"),
        (f"SELECT t.* EXCEPT (a), u.c {JOIN}", f"SELECT t.b, u.c {JOIN}"),
        (f"SELECT t.* EXCEPT (a), u.* EXCEPT (a) {JOIN}", f"SELECT t.b, u.c {JOIN}"),
        (f"SELECT * EXCEPT (b) {JOIN}", f"SELECT t.a, u.a, u.c {JOIN}"),
        ("SELECT x.a FROM (SELECT * EXCEPT (b) FROM t) AS x", "SELECT a FROM t"),
        ("WITH c AS (SELECT * EXCEPT (b) FROM t) SELECT * FROM c", "SELECT a FROM t"),
    ],
)
def test_smt_proves_the_columns_a_modified_star_lists(left, right):
    result = smt(left, right)
    assert result.proven, result.reason


@pytest.mark.parametrize(
    "left, right",
    [
        (f"SELECT * EXCEPT (a) {JOIN}", f"SELECT t.b, u.c {JOIN}"),  # ``a`` is in both tables
        ("SELECT * EXCEPT (z) FROM t", "SELECT a, b FROM t"),  # BigQuery rejects a missing column
        ("SELECT * EXCEPT (b, b) FROM t", "SELECT a FROM t"),
        ("SELECT * REPLACE (1 AS a, 2 AS a) FROM t", "SELECT 2 AS a, b FROM t"),
        ("SELECT * EXCEPT (a) REPLACE (1 AS a) FROM t", "SELECT b FROM t"),
        ("SELECT * RENAME (a AS b, b AS a) FROM t", "SELECT b AS a, a AS b FROM t"),  # a swap: declined
        ("SELECT * RENAME (a AS b) FROM t", "SELECT a AS b, b FROM t"),
    ],
)
def test_smt_declines_what_it_cannot_model_exactly(left, right):
    result = smt(left, right)
    assert not result.proven and result.reason.startswith("unsupported"), result.reason


def test_star_columns_reads_the_sqlglot_26_spelling():
    # sqlglot 26 calls the argument ``except``; build that tree by hand on any version.
    columns = [("a", "va"), ("b", "vb")]
    old = exp.Star(**{"except": [exp.column("b")]})
    new = exp.Star(**{"except_": [exp.column("b")]})
    assert _star_columns(old, columns) == _star_columns(new, columns) == [("a", "va")]
    assert star_modifier(old, "except") and star_modifier(new, "except")
    assert star_modified(exp.Column(this=old, table=exp.to_identifier("t")))
    assert not star_modified(exp.Star()) and not star_modified(exp.column("a"))


def test_star_modifier_reads_qualified_stars():
    item = sqlglot.parse_one("SELECT t.* REPLACE (a + 1 AS a) FROM t", read="bigquery").expressions[0]
    assert isinstance(item, exp.Column) and not item.args.get("replace")  # the dead read
    assert [r.alias for r in star_modifier(item, "replace")] == ["a"]


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT * EXCEPT (b) FROM (SELECT a, b FROM t) AS s", "SELECT a, b FROM t"),
        ("SELECT s.* EXCEPT (b) FROM (SELECT a, b FROM t) AS s", "SELECT a, b FROM t"),
        ("SELECT * REPLACE (a + 1 AS a) FROM (SELECT a, b FROM t) AS s", "SELECT a, b FROM t"),
        ("SELECT * EXCEPT (b) FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS s", "SELECT a, b FROM t UNION ALL SELECT a, b FROM u"),
        ("SELECT s.* EXCEPT (b) FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS s", "SELECT a, b FROM t UNION ALL SELECT a, b FROM u"),
    ],
)
def test_algebraic_prover_does_not_unwrap_a_modified_star(left, right):
    schema = {"t": ["a", "b"], "u": ["a", "b"]}
    assert not prove_equivalent_algebraic(left, right, schema=schema).proven
    assert not prove_equivalent_algebraic(right, left, schema=schema).proven


def test_algebraic_prover_still_proves_modified_stars_over_derived_tables():
    schema = {"t": ["a", "b"], "u": ["a", "b"]}
    for left, right in [
        ("SELECT * EXCEPT (b) FROM (SELECT a, b FROM t) AS s", "SELECT a FROM t"),
        ("SELECT * REPLACE (a + 1 AS a) FROM (SELECT a, b FROM t) AS s", "SELECT a + 1 AS a, b FROM t"),
        ("SELECT * EXCEPT (b) FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS s", "SELECT a FROM t UNION ALL SELECT a FROM u"),
    ]:
        result = prove_equivalent_algebraic(left, right, schema=schema)
        assert result.proven, (left, result.reason)


def test_bounded_checker_declines_star_modifiers():
    from kumosql.bounded_equivalence import BColumn, BoundedSchema, BoundedStatus, BTable, check_bounded

    schema = BoundedSchema({"t": BTable("t", [BColumn("a", "INT64"), BColumn("b", "INT64")])})
    for left in ("SELECT * EXCEPT (b) FROM t", "SELECT t.* EXCEPT (b) FROM t", "SELECT * REPLACE (a + 1 AS a) FROM t"):
        result = check_bounded(left, "SELECT * FROM t", schema, rows=2, dialect="bigquery")
        assert result.status is BoundedStatus.UNKNOWN and "SELECT *" in result.reason


def test_set_operation_normalizer_keeps_a_modified_star():
    from kumosql.setop_rules import normalize_set_operations

    union = "(SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS s"
    for sql in (f"SELECT * EXCEPT (b) FROM {union}", f"SELECT s.* EXCEPT (b) FROM {union}", f"SELECT * REPLACE (a + 1 AS a) FROM {union}"):
        tree = normalize_set_operations(sqlglot.parse_one(sql, read="bigquery"))
        assert star_modified(tree.expressions[0]), tree.sql("bigquery")


def test_rewrite_proposals_keep_a_modified_star():
    from kumosql.query_optimizer import Catalog, rewrite_candidates
    from kumosql.sql_simplify import simpler_forms

    catalog = Catalog(columns={"t": ["a", "b"]})
    for sql in ("WITH c AS (SELECT * EXCEPT (b) FROM t) SELECT * FROM c", "SELECT * EXCEPT (b) FROM (SELECT a, b FROM t) AS d"):
        for candidate in rewrite_candidates(sql, catalog, dialect="bigquery"):
            assert "EXCEPT (b)" in candidate.sql, candidate.sql  # never the bare ``SELECT * FROM t``
    for sql in ("WITH c AS (SELECT * EXCEPT (b) FROM m.t) SELECT * FROM c", "SELECT * FROM (SELECT * EXCEPT (b) FROM m.t) AS d"):
        assert not any(" ".join(form.split()).startswith("SELECT * FROM m.t") for form in simpler_forms(sql))


def test_sqlsolver_translation_refuses_star_ilike():
    from kumosql.sqlsolver_backend import TranslationError, translate_query

    if "ilike" not in exp.Star.arg_types:
        pytest.skip("this sqlglot reads * ILIKE as a predicate, not a star modifier")
    with pytest.raises(TranslationError):
        translate_query("SELECT * ILIKE 'a%' FROM t", {"t": [["a", "INT64"], ["b", "INT64"]]})


def test_table_profile_reads_replace_on_a_qualified_star():
    from kumosql import Pipeline, Target, profile_pipeline
    from kumosql.pipeline import Model

    orders = Target("proj", "raw", "orders")
    model = Target("proj", "core", "m")
    pipeline = Pipeline(
        {model.key: Model(model, "table", "SELECT o.* REPLACE (o.amount * 2 AS amount) FROM proj.raw.orders AS o")},
        sources={orders.key: orders},
        source_schema={orders.key: {"id": "INT64", "amount": "FLOAT64"}},
    )
    profile = profile_pipeline(pipeline)[model.key]
    assert profile.attribute("id").meaning == "col:proj.raw.orders.id"
    assert profile.attribute("amount").meaning != "col:proj.raw.orders.amount"  # it is doubled
