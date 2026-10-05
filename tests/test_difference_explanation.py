"""Equivalent except when P: a verified difference predicate (kumosql.difference_explanation, issue #512)."""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import conditional_equivalence as ce
from kumosql import difference_explanation as de
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.difference_explanation import DifferenceExplanation, conditions_of, explain_difference
from kumosql.duckdb_load import insert_rows, run_unoptimized, small_database
from kumosql.smt_equivalence import SmtStatus, TableConstraints, prove_equivalent_smt

TYPES = {"t": {"id": "INT64", "x": "INT64", "a": "INT64", "status": "STRING"}, "u": {"id": "INT64", "y": "INT64"}}
SCHEMA = {table: list(columns) for table, columns in TYPES.items()}

STATUS = ("SELECT id FROM t WHERE status = 'a'", "SELECT id FROM t WHERE status = 'a' OR status IS NULL")
BOUNDARY = ("SELECT id FROM t WHERE x > 5", "SELECT id FROM t WHERE x >= 5")
COALESCED = ("SELECT COALESCE(a, 0) AS a FROM t", "SELECT a FROM t")
JOINED = (
    "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x > 5",
    "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x >= 5",
)
ACROSS = (
    "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x > u.y",
    "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x >= u.y",
)
EQUIVALENT = ("SELECT id FROM t WHERE x > 5", "SELECT id FROM t WHERE 5 < x")


def run(sql, tables):
    """The rows of ``sql`` on ``tables`` (table -> rows as dicts), the optimizer off."""

    db = small_database()
    try:
        for name, rows in tables.items():
            columns = sorted({c for row in rows for c in row})
            kinds = {c: "VARCHAR" if TYPES[name][c] == "STRING" else "BIGINT" for c in columns}
            db.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {kinds[c]}' for c in columns)})")
            insert_rows(db, name, [[r.get(c) for c in columns] for r in rows])
        return sorted(run_unoptimized(db, sql)[0], key=repr)
    finally:
        db.close()


def differs_on(pair, witness):
    left, right = (run(sql, witness) for sql in pair)
    return left != right


@pytest.mark.parametrize("options", [{}, {"schema": SCHEMA, "types": TYPES}], ids=["untyped", "typed"])
def test_a_null_the_second_query_also_accepts(options):
    found = explain_difference(*STATUS, **options)
    assert isinstance(found, DifferenceExplanation)
    assert (found.sql, found.atoms, found.tables, found.exact) == ("status IS NULL", ("status IS NULL",), ("t",), True)
    assert found.to_json() == {"sql": "status IS NULL", "atoms": ["status IS NULL"], "tables": ["t"], "exact": True}


def test_a_boundary_value():
    found = explain_difference(*BOUNDARY)
    assert (found.sql, found.tables, found.exact) == ("x = 5", ("t",), True)


def test_the_value_coalesce_replaces():
    found = explain_difference(*COALESCED)
    assert (found.sql, found.tables) == ("a IS NULL", ("t",))


def test_a_join_whose_difference_sits_on_one_table():
    found = explain_difference(*JOINED)
    assert (found.sql, found.tables) == ("x = 5", ("t",))
    assert set(found.witness) == {"t", "u"}  # the join needs a row on each side


def test_a_difference_only_a_comparison_across_tables_describes_is_unknown():
    assert explain_difference(*ACROSS) is None


def test_equivalent_pairs_have_no_predicate():
    assert explain_difference(*EQUIVALENT) is None
    assert explain_difference("SELECT id FROM t", "SELECT id FROM t") is None


def test_a_declared_fact_that_removes_the_difference_removes_the_predicate():
    not_null = {"t": TableConstraints(not_null=frozenset({"status"}))}
    assert explain_difference(*STATUS, constraints=not_null) is None


@pytest.mark.parametrize(
    "pair",
    [
        ("SELECT COUNT(*) AS n FROM t WHERE x > 5", "SELECT COUNT(*) AS n FROM t WHERE x >= 5"),  # grouped: not row-local
        ("SELECT DISTINCT id FROM t WHERE x > 5", "SELECT DISTINCT id FROM t WHERE x >= 5"),
        ("SELECT p.id FROM t p JOIN t q ON p.id = q.id WHERE p.x > 5", "SELECT p.id FROM t p JOIN t q ON p.id = q.id WHERE p.x >= 5"),  # a table twice
        ("SELECT t.id FROM t LEFT JOIN u ON t.id = u.id", "SELECT t.id FROM t JOIN u ON t.id = u.id"),
        ("SELECT id FROM t WHERE x > 5 LIMIT 3", "SELECT id FROM t WHERE x >= 5 LIMIT 3"),
        ("SELECT id FROM t WHERE x > 5 UNION ALL SELECT id FROM u", "SELECT id FROM t WHERE x >= 5 UNION ALL SELECT id FROM u"),
        ("SELECT id FROM t WHERE id IN (SELECT id FROM u WHERE y > 5)", "SELECT id FROM t WHERE id IN (SELECT id FROM u WHERE y >= 5)"),
        ("SELECT id FROM t", "SELECT id FROM u"),  # other tables
        ("SELECT id AS a FROM t WHERE x > 5", "SELECT id AS b FROM t WHERE x >= 5"),  # other column names
        ("SELECT * FROM t WHERE x > 5", "SELECT * FROM t WHERE x >= 5"),  # no schema for the star
        ("SELECT id FROM t WHERE x >", "SELECT id FROM t"),  # not SQL
    ],
)
def test_what_is_not_row_local_is_unknown(pair):
    assert explain_difference(*pair) is None


def test_a_star_with_a_schema_is_read():
    found = explain_difference("SELECT * FROM t WHERE x > 5", "SELECT * FROM t WHERE x >= 5", schema=SCHEMA, types=TYPES)
    assert found is not None and found.sql == "x = 5"


def test_the_difference_is_exact_on_the_rows_either_query_returns():
    found = explain_difference("SELECT id FROM t WHERE x > 5 AND a = 1", "SELECT id FROM t WHERE x >= 5 AND a = 1")
    assert found is not None and found.sql == "x = 5"


def test_a_predicate_that_every_row_satisfies_explains_nothing():
    assert explain_difference("SELECT x FROM t", "SELECT x + 1 AS x FROM t") is None


def test_a_table_spelled_with_a_dataset_keeps_its_spelling():
    found = explain_difference("SELECT id FROM proj.ds.t WHERE x > 5", "SELECT id FROM proj.ds.t WHERE x >= 5")
    assert found.tables == ("proj.ds.t",)
    (condition,) = conditions_of(found)
    assert condition.check_sql == "SELECT COUNT(*) AS violations FROM `proj`.`ds`.`t` WHERE x = 5"


def test_another_dialect_is_read():
    found = explain_difference(*BOUNDARY, dialect="mysql")
    assert found is not None and found.sql == "x = 5"


def test_a_spent_time_budget_is_unknown():
    assert explain_difference(*BOUNDARY, explain_seconds=0.00001) is None


@pytest.mark.parametrize("pair", [STATUS, BOUNDARY, COALESCED, JOINED])
def test_every_predicate_passes_both_checks(pair):
    found = explain_difference(*pair, schema=SCHEMA, types=TYPES)
    assert found is not None
    # (a) the witness satisfies P in one row of a table and the queries differ on it, run again here on DuckDB
    assert differs_on(pair, found.witness)
    conditions = conditions_of(found)
    assert conditions and any(ce.broken_by(c, found.witness) for c in conditions)
    # (b) filtering each table by NOT P makes the pair provably equivalent, under both provers
    filters = {c.table: (c.predicate, SCHEMA[c.table]) for c in conditions}
    rewritten = [de._rewrite(sql, "bigquery", filters) for sql in pair]
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert prove(*rewritten, schema=SCHEMA, types=TYPES).status is SmtStatus.PROVEN_EQUIVALENT
    # and the same databases without the P rows agree: the check SQL is zero there
    for c in conditions:
        assert c.check_sql.startswith("SELECT COUNT(*) AS violations FROM ")


def test_a_predicate_failing_the_witness_replay_is_never_returned(monkeypatch):
    monkeypatch.setattr(de, "replay", lambda *args, **kwargs: False)
    assert explain_difference(*BOUNDARY) is None


def test_a_predicate_failing_the_proof_is_never_returned(monkeypatch):
    monkeypatch.setattr(de, "_prove_rewritten", lambda *args, **kwargs: False)
    assert explain_difference(*BOUNDARY) is None


def test_a_predicate_the_witness_does_not_satisfy_is_never_returned(monkeypatch):
    monkeypatch.setattr(de, "broken_by", lambda condition, tables: False)
    assert explain_difference(*BOUNDARY) is None


def test_the_replay_counts_a_difference_only_when_duckdb_agrees_with_the_optimizer_off():
    tables = {"t": [{"id": 1, "x": 5}]}
    assert de.replay(*BOUNDARY, tables)
    assert not de.replay(*BOUNDARY, {"t": [{"id": 1, "x": 6}]})  # both return the row
    assert not de.replay(*BOUNDARY, {"t": []})
    assert not de.replay("SELECT id FROM t WHERE x / 0 > 1", "SELECT id FROM t WHERE x / 0 >= 1", tables)  # BigQuery fails here


def test_the_proof_asks_both_provers():
    calls = []

    def prove(name):
        real = {"algebraic": prove_equivalent_algebraic, "smt": prove_equivalent_smt}[name]

        def wrapped(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        return wrapped

    import kumosql.algebraic_equivalence as algebraic
    import kumosql.smt_equivalence as smt

    originals = algebraic.prove_equivalent_algebraic, smt.prove_equivalent_smt
    algebraic.prove_equivalent_algebraic, smt.prove_equivalent_smt = prove("algebraic"), prove("smt")
    try:
        assert explain_difference(*BOUNDARY) is not None
    finally:
        algebraic.prove_equivalent_algebraic, smt.prove_equivalent_smt = originals
    assert calls.count("smt") >= 1 and calls.count("algebraic") >= 2  # the pair first, then the rewritten pair


# ---- the no_rows condition ------------------------------------------------------------------------


def test_the_no_rows_condition():
    c = ce.no_rows(["t"], "status IS NULL", ["status"])
    assert c.kind == "no_rows" and c.text == "no row of t has status IS NULL"
    assert c.check_sql == "SELECT COUNT(*) AS violations FROM `t` WHERE status IS NULL"
    assert c.to_json() == {
        "kind": "no_rows", "table": "t", "columns": ["status"], "text": c.text, "check_sql": c.check_sql, "predicate": "status IS NULL",
    }
    assert c.key != ce.no_rows(["t"], "status = 'a'", ["status"]).key
    assert ce.no_rows(["p", "d", "t"], "x = 5", ["x"]).check_sql == "SELECT COUNT(*) AS violations FROM `p`.`d`.`t` WHERE x = 5"


def test_no_rows_is_broken_by_a_counterexample_that_has_such_a_row():
    c = ce.no_rows(["t"], "x = 5 AND y IS NOT NULL", ["x", "y"])
    assert ce.broken_by(c, {"t": [{"x": 4, "y": 1}, {"x": 5, "y": 1.5}]})
    assert not ce.broken_by(c, {"t": [{"x": 5, "y": None}, {"x": 4, "y": 1}]})  # NULL is not TRUE
    assert not ce.broken_by(c, {"t": [{"x": 5}]})  # a column the rows lack is unconstrained
    assert not ce.broken_by(c, {"u": [{"x": 5, "y": 1}]})  # another table
    assert ce.broken_by(c, {"T": [{"x": 5, "y": 1}]})  # the table is looked up as the provers do
    assert not ce.broken_by(ce.no_rows(["t"], "status IS NULL", ["status"]), {"t": [{"status": "a"}]})
    assert ce.broken_by(ce.no_rows(["t"], "status IS NULL", ["status"]), {"t": [{"status": None}]})  # a column that holds only NULL
    assert not ce.broken_by(ce.no_rows(["t"], "status IS NULL", ["status"]), {"t": []})


def test_no_rows_is_not_a_constraint_the_provers_assume():
    c = ce.no_rows(["t"], "status IS NULL", ["status"])
    assert ce.with_conditions({"t": TableConstraints(not_null=frozenset({"id"}))}, [c]) == {"t": TableConstraints(not_null=frozenset({"id"}))}
    assert ce.with_conditions(None, [c]) == {}


def test_the_conditions_of_an_explanation():
    (c,) = conditions_of(explain_difference(*BOUNDARY))
    assert (c.kind, c.table, c.columns, c.predicate) == ("no_rows", "t", ("x",), "x = 5")
    assert c.text == "no row of t has x = 5"


def test_the_conditions_of_a_predicate_across_tables():
    hand = DifferenceExplanation("t.x IS NULL OR u.y IS NULL", ("t.x IS NULL", "u.y IS NULL"), ("t", "u"), True, {"t": [{"x": None}], "u": [{"y": 1}]})
    by_table = {c.table: c for c in conditions_of(hand)}
    assert set(by_table) == {"t", "u"}
    assert by_table["t"].predicate == "x IS NULL" and by_table["u"].predicate == "y IS NULL"
    assert ce.broken_by(by_table["t"], hand.witness) and not ce.broken_by(by_table["u"], hand.witness)
