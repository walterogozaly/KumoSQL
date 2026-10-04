"""The fourth verdict: equivalent under stated conditions (kumosql.conditional_equivalence)."""

import json
import subprocess
import sys

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import conditional_equivalence as ce
from kumosql import pipeline_equivalence
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus, TableConstraints, prove_equivalent_smt

NOT_IN = "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT customer_id FROM customers)"
ANTI = "SELECT o.id FROM orders o LEFT JOIN customers c ON o.id = c.customer_id WHERE c.customer_id IS NULL"
SELF_JOIN = "SELECT x.id, x.name FROM users x JOIN users y ON x.id = y.id"
PLAIN_USERS = "SELECT id, name FROM users"
FK_JOIN = "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id"
FK_PLAIN = "SELECT id FROM orders"


def texts(result):
    return {c.text for c in result.conditions}


@pytest.mark.parametrize("prove", [prove_equivalent_algebraic, prove_equivalent_smt])
def test_not_in_against_an_anti_join_needs_the_columns_to_be_non_null(prove):
    assert not prove(NOT_IN, ANTI).proven
    result = prove(NOT_IN, ANTI, conditional=True)
    assert result.status is SmtStatus.PROVEN_CONDITIONALLY
    assert not result.proven and result.conditionally_proven
    assert texts(result) == {"orders.id is NOT NULL", "customers.customer_id is NOT NULL"}
    assert result.reason.startswith("equivalent whenever: ")
    # a database that separates them without the conditions, when the prover found one, breaks one of them
    if result.counterexample is not None:
        assert any(ce.broken_by(c, result.counterexample.tables) for c in result.conditions)


def test_a_self_join_on_a_key_is_the_table_when_the_key_is_a_primary_key():
    result = prove_equivalent_algebraic(SELF_JOIN, PLAIN_USERS, conditional=True)
    assert result.status is SmtStatus.PROVEN_CONDITIONALLY
    assert texts(result) == {"(id) is unique in users", "users.id is NOT NULL"}
    assert result.reason == "equivalent whenever: users(id) is a primary key (unique, never NULL)"


def test_a_foreign_key_join_is_redundant_under_three_conditions():
    result = prove_equivalent_algebraic(FK_JOIN, FK_PLAIN, conditional=True)
    assert result.status is SmtStatus.PROVEN_CONDITIONALLY
    assert texts(result) == {"(id) is unique in customers", "orders(cid) references customers(id)", "orders.cid is NOT NULL"}


def test_the_conditions_are_minimal():
    for left, right in ((NOT_IN, ANTI), (SELF_JOIN, PLAIN_USERS), (FK_JOIN, FK_PLAIN)):
        result = prove_equivalent_algebraic(left, right, conditional=True)
        assert result.conditionally_proven
        for dropped in result.conditions:
            rest = [c for c in result.conditions if c is not dropped]
            again = prove_equivalent_algebraic(left, right, constraints=ce.with_conditions(None, rest) or None)
            assert not again.proven, f"{dropped.text} was not needed"
        everything = prove_equivalent_algebraic(left, right, constraints=ce.with_conditions(None, result.conditions))
        assert everything.proven


def test_declared_facts_are_not_listed_as_conditions():
    declared = {"users": TableConstraints(not_null=frozenset({"id"}))}
    result = prove_equivalent_algebraic(SELF_JOIN, PLAIN_USERS, conditional=True, constraints=declared)
    assert texts(result) == {"(id) is unique in users"}
    both = {"users": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
    outright = prove_equivalent_algebraic(SELF_JOIN, PLAIN_USERS, conditional=True, constraints=both)
    assert outright.status is SmtStatus.PROVEN_EQUIVALENT and not outright.conditions


def test_off_by_default():
    assert prove_equivalent_algebraic(SELF_JOIN, PLAIN_USERS).status is not SmtStatus.PROVEN_CONDITIONALLY
    assert prove_equivalent_smt(SELF_JOIN, PLAIN_USERS).status is not SmtStatus.PROVEN_CONDITIONALLY


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a > 2"),
        ("SELECT a FROM t", "SELECT b FROM t"),
        ("SELECT a FROM t WHERE a IS NULL", "SELECT a FROM t WHERE a IS NOT NULL"),
    ],
)
def test_pairs_no_condition_settles_stay_unconditional(left, right):
    assert prove_equivalent_algebraic(left, right, conditional=True).status in (SmtStatus.NOT_EQUIVALENT, SmtStatus.NOT_PROVEN)


def test_conditions_that_make_both_queries_constant_are_not_offered():
    # with (id) unique a row cannot differ from itself, so the join returns nothing: the pair is proven, but for the wrong reason
    left = "SELECT a.id FROM u AS a JOIN u AS b ON a.id = b.id WHERE a.name <> b.name"
    right = "SELECT a.id FROM u AS a JOIN u AS b ON a.id = b.id WHERE a.name > b.name"
    unique = ce.with_conditions(None, [ce.Condition("unique", "u", ("id",))])
    assert prove_equivalent_algebraic(left, right, constraints=unique).proven
    assert ce.data_independent(left, right, unique) is True
    assert prove_equivalent_algebraic(left, right, conditional=True).status is not SmtStatus.PROVEN_CONDITIONALLY
    assert ce.data_independent(NOT_IN, ANTI, ce.with_conditions(None, [ce.Condition("not_null", "orders", ("id",))])) is False


def test_a_refutation_that_meets_every_condition_withholds_the_verdict():
    result = SmtEquivalenceResult(
        SmtStatus.NOT_EQUIVALENT, "differ",
        counterexample=__import__("kumosql.smt_equivalence", fromlist=["Counterexample"]).Counterexample({"t": [{"a": 1}]}, [], []),
    )
    proving = lambda constraints: SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, "ok")  # noqa: E731
    out = ce.add_conditions("SELECT a FROM t", "SELECT a FROM t WHERE a IS NOT NULL", result, proving)
    assert out is result  # the database holds a=1: every candidate holds there, so nothing is conditional


def test_broken_by_reads_what_the_counterexample_shows():
    nn = ce.Condition("not_null", "t", ("a",))
    assert ce.broken_by(nn, {"t": [{"a": None}]}) and not ce.broken_by(nn, {"t": [{"a": 1}]})
    assert not ce.broken_by(nn, {"t": [{"b": None}]})  # a column the database does not show is unconstrained
    unique = ce.Condition("unique", "p.d.t", ("a", "b"))
    assert ce.broken_by(unique, {"t": [{"a": 1, "b": 2}, {"a": 1, "b": 2}]})
    assert not ce.broken_by(unique, {"t": [{"a": 1, "b": None}, {"a": 1, "b": None}]})  # NULL keys may repeat
    fk = ce.Condition("foreign_key", "c", ("p",), "par", ("id",))
    assert ce.broken_by(fk, {"c": [{"p": 5}], "par": [{"id": 1}]})
    assert not ce.broken_by(fk, {"c": [{"p": 1}, {"p": None}], "par": [{"id": 1}]})


def test_minimal_conditions_drops_in_chunks_and_keeps_what_is_needed():
    cands = [ce.Condition("not_null", "t", (f"c{i}",)) for i in range(30)]
    need = {cands[3].key, cands[22].key}
    calls = []

    def proves(chosen):
        calls.append(len(chosen))
        return need <= {c.key for c in chosen}

    kept, minimal = ce.minimal_conditions(cands, proves)
    assert {c.key for c in kept} == need and minimal
    assert len(calls) < 30  # fewer prover calls than candidates
    kept, minimal = ce.minimal_conditions(cands, proves, deadline=0.0)
    assert not minimal and len(kept) == 30


def test_each_condition_comes_with_a_check_that_counts_the_rows_breaking_it():
    cands = ce.candidate_conditions(FK_JOIN, FK_PLAIN, dialect="duckdb")
    by_text = {c.text: c for c in cands}
    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE orders (id INT, cid INT)")
    db.execute("CREATE TABLE customers (id INT)")
    db.execute("INSERT INTO customers VALUES (1), (1), (NULL), (NULL)")
    db.execute("INSERT INTO orders VALUES (1, 1), (2, 7), (3, NULL)")
    violations = lambda text: db.execute(by_text[text].check_sql).fetchone()[0]  # noqa: E731
    assert violations("orders.cid is NOT NULL") == 1
    assert violations("(id) is unique in customers") == 1  # the two 1s; NULL keys may repeat
    assert violations("orders(cid) references customers(id)") == 1  # cid 7 has no parent; the NULL is fine
    db.execute("DELETE FROM customers")
    db.execute("INSERT INTO customers VALUES (1), (7)")
    db.execute("DELETE FROM orders WHERE cid IS NULL")
    assert [violations(t) for t in by_text] == [0] * len(by_text)


def test_candidates_come_from_the_queries_and_skip_declared_facts():
    cands = ce.candidate_conditions(FK_JOIN, FK_PLAIN)
    assert {c.text for c in cands} >= {"(id) is unique in customers", "orders(cid) references customers(id)", "orders.cid is NOT NULL"}
    declared = {"customers": TableConstraints(keys=(("id",),))}
    assert "(id) is unique in customers" not in {c.text for c in ce.candidate_conditions(FK_JOIN, FK_PLAIN, constraints=declared)}
    assert ce.candidate_conditions("SELECT 1", "SELECT 2") == []
    assert len(ce.candidate_conditions(FK_JOIN, FK_PLAIN)) <= ce.MAX_CANDIDATES


def test_unqualified_columns_are_attributed_by_schema():
    left = "SELECT name FROM a JOIN b ON a.k = b.k WHERE name IS NOT NULL"
    cands = ce.candidate_conditions(left, left, schema={"a": ["k", "name"], "b": ["k"]})
    assert "a.name is NOT NULL" in {c.text for c in cands}
    assert "b.name is NOT NULL" not in {c.text for c in cands}


def test_a_column_the_schema_does_not_list_is_never_a_candidate():
    sql = "SELECT a.k FROM a JOIN b ON a.k = b.k WHERE a.ghost IS NOT NULL ORDER BY a.k"
    cands = ce.candidate_conditions(sql, sql, schema={"a": ["k"], "b": ["k"]})
    assert cands and all("ghost" not in c.text for c in cands)
    assert any("ghost" in c.text for c in ce.candidate_conditions(sql, sql))


def test_api_reports_the_conditions_and_their_checks():
    result = pipeline_equivalence.prove_queries(SELF_JOIN, PLAIN_USERS)
    assert result["status"] == "proven_conditionally"
    assert {c["text"] for c in result["conditions"]} == {"(id) is unique in users", "users.id is NOT NULL"}
    assert all(c["check_sql"].startswith("SELECT COUNT(*) AS violations") for c in result["conditions"])
    assert "bounded" not in result
    assert result["counterexample"]["tables"]  # what goes wrong without them


def test_the_app_still_calls_different_queries_different():
    result = pipeline_equivalence.prove_queries("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a > 2")
    assert result["status"] == "not_equivalent" and "conditions" not in result


def test_comparing_tables_reports_a_conditional_verdict(tmp_path):
    from kumosql.pipeline_loading import load_sqlx_project

    (tmp_path / "definitions").mkdir()
    (tmp_path / "definitions" / "a.sqlx").write_text('config { type: "table" }\nSELECT x.id, x.name FROM users x JOIN users y ON x.id = y.id\n')
    (tmp_path / "definitions" / "b.sqlx").write_text('config { type: "table" }\nSELECT id, name FROM users\n')
    pipeline = load_sqlx_project(str(tmp_path))
    assert pipeline_equivalence.prove_models(pipeline, "a", "b").status == "unknown"
    result = pipeline_equivalence.prove_models(pipeline, "a", "b", conditional=True)
    assert result.status == "conditional" and not result.proven
    assert {c["text"] for c in result.conditions} == {"(id) is unique in users", "users.id is NOT NULL"}
    assert result.to_json()["conditions"]


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "kumosql", *args], capture_output=True, text=True)


@pytest.mark.parametrize("command", ["prove-sql-equivalent", "prove-sql-smt"])
def test_both_commands_report_conditions_and_exit_3(command, tmp_path):
    (tmp_path / "a.sql").write_text(SELF_JOIN)
    (tmp_path / "b.sql").write_text(PLAIN_USERS)
    plain = _cli(command, str(tmp_path / "a.sql"), str(tmp_path / "b.sql"))
    assert plain.returncode in (1, 2) and "proven_conditionally" not in plain.stdout
    result = _cli(command, str(tmp_path / "a.sql"), str(tmp_path / "b.sql"), "--conditional")
    assert result.returncode == 3, result.stderr
    assert "proven_conditionally" in result.stdout
    assert "users.id is NOT NULL" in result.stdout and "SELECT COUNT(*) AS violations" in result.stdout
    if command == "prove-sql-smt":
        payload = json.loads(result.stdout)
        assert payload["status"] == "proven_conditionally" and len(payload["conditions"]) == 2


def test_a_proof_outright_is_still_exit_0(tmp_path):
    (tmp_path / "a.sql").write_text("SELECT a FROM t WHERE a > 1 AND TRUE")
    (tmp_path / "b.sql").write_text("SELECT a FROM t WHERE a > 1")
    assert _cli("prove-sql-smt", str(tmp_path / "a.sql"), str(tmp_path / "b.sql"), "--conditional").returncode == 0
    assert _cli("prove-sql-equivalent", str(tmp_path / "a.sql"), str(tmp_path / "b.sql"), "--conditional").returncode == 0


def test_describe_merges_a_unique_non_null_column_into_a_primary_key():
    parts = [ce.Condition("unique", "t", ("a",)), ce.Condition("not_null", "t", ("a",)), ce.Condition("not_null", "t", ("b",), text="t.b is NOT NULL")]
    assert ce.describe(parts) == "t(a) is a primary key (unique, never NULL); t.b is NOT NULL"
    assert ce.describe([ce.Condition("unique", "t", ("a",), text="(a) is unique in t")]) == "(a) is unique in t"


# ---- a prover that proves some sets of conditions and abstains on others (not monotone) ----------------------


def partial_prover(proving_sets, assumptions=None):
    """``prove(constraints, pair=None)`` that proves exactly when the NOT NULL columns it is given are one of ``proving_sets``.

    ``assumptions`` maps a set of columns to the assumptions the proof under it reports. The control query
    (``pair`` given) is never proved, so no set looks vacuous.
    """

    calls = []

    def prove(constraints, pair=None):
        if pair is not None:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "control")
        facts = constraints.get("t")
        columns = frozenset(facts.not_null) if facts else frozenset()
        calls.append(columns)
        if columns in proving_sets:
            return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, "ok", assumptions=tuple((assumptions or {}).get(columns, ())))
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "no")

    prove.calls = calls
    return prove


XY = ("SELECT x, y FROM t", "SELECT x, y FROM t WHERE x IS NOT NULL")
NOT_PROVEN = SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "not proven")


def not_nulls(*columns):
    return [ce.Condition("not_null", "t", (c,)) for c in columns]


def test_the_last_condition_is_tried_too():
    cands = not_nulls("x", "y")

    def proves(chosen):  # proves under {x, y}, {x} and the empty set, abstains under {y}
        return {c.columns[0] for c in chosen} in ({"x", "y"}, {"x"}, set())

    kept, minimal = ce.minimal_conditions(cands, proves)
    assert kept == [] and minimal  # a singleton {x} would be reported as minimal although deleting x still proves


def test_a_set_the_prover_can_shrink_to_nothing_is_not_reported_as_conditional():
    prove = partial_prover({frozenset({"x", "y"}), frozenset({"x"}), frozenset()})
    out = ce.add_conditions(*XY, NOT_PROVEN, prove, dialect="duckdb")
    assert out.status is SmtStatus.NOT_PROVEN and not out.conditions


def test_every_returned_condition_was_tried_against_the_final_set():
    prove = partial_prover({frozenset({"x", "y"}), frozenset({"x"})})  # {y} and {} are not proved
    out = ce.add_conditions(*XY, NOT_PROVEN, prove, dialect="duckdb")
    assert out.status is SmtStatus.PROVEN_CONDITIONALLY and texts(out) == {"t.x is NOT NULL"}
    assert frozenset() in prove.calls  # deleting the last condition was tried and failed


def test_the_assumptions_are_those_of_the_proof_under_the_returned_conditions():
    full, final = frozenset({"x", "y"}), frozenset({"x"})
    prove = partial_prover({full, final}, {full: ("full set premise",), final: ("t.y is read as NOT NULL by this proof",)})
    out = ce.add_conditions(*XY, NOT_PROVEN, prove, dialect="duckdb")
    assert out.status is SmtStatus.PROVEN_CONDITIONALLY and texts(out) == {"t.x is NOT NULL"}
    assert out.assumptions == ("t.y is read as NOT NULL by this proof",)


def test_a_check_that_failed_does_not_clear_the_conditions(monkeypatch):
    left, right = SELF_JOIN, PLAIN_USERS
    assert prove_equivalent_algebraic(left, right, conditional=True).status is SmtStatus.PROVEN_CONDITIONALLY
    monkeypatch.setattr(ce, "_independence", lambda *args, **kwargs: "error")
    assert prove_equivalent_algebraic(left, right, conditional=True).status is not SmtStatus.PROVEN_CONDITIONALLY


def test_queries_the_engine_rejects_keep_the_verdict_and_say_it_was_not_checked(monkeypatch):
    left, right = SELF_JOIN, PLAIN_USERS
    monkeypatch.setattr(ce, "_independence", lambda *args, **kwargs: "not_run")
    out = prove_equivalent_algebraic(left, right, conditional=True)
    assert out.status is SmtStatus.PROVEN_CONDITIONALLY
    assert "not checked for conditions that make both queries constant" in out.reason
    monkeypatch.undo()
    assert "not checked" not in prove_equivalent_algebraic(left, right, conditional=True).reason


def test_a_declared_varchar_column_does_not_stop_the_constant_check_from_running():
    types = {"users": {"id": "INT", "name": "VARCHAR", "at": "TIME"}}
    schema = {"users": ["id", "name", "at"]}
    unique = ce.with_conditions(None, [ce.Condition("unique", "users", ("id",))])
    out = ce._independence("SELECT id FROM users", "SELECT id FROM users", unique, schema=schema, types=types, dialect="mysql")
    assert out == "dependent"
