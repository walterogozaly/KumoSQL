"""Counterexample synthesis: databases found by running both queries, and the hooks into the provers."""

import time

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

from kumosql import refutation_synthesis as rs  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.refutation_replay import Judge, Verdict, replay_counterexample  # noqa: E402
from kumosql.smt_equivalence import Counterexample, SmtEquivalenceResult, SmtStatus, TableConstraints  # noqa: E402

SCHEMA = {"t": ["a", "b"], "u": ["a", "b"]}
TYPES = {name: {c: "INT64" for c in cols} for name, cols in SCHEMA.items()}

NOT_IN = "SELECT a FROM t WHERE a NOT IN (SELECT a FROM u)"
NOT_EXISTS = "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a)"
EQUIVALENT = ("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE 1 < a")


def synth(left, right, schema=SCHEMA, types=TYPES, **kwargs):
    kwargs.setdefault("time_limit", 3.0)
    return rs.synthesize(left, right, schema=schema, types=types, **kwargs)


def replays(found, left, right, schema=SCHEMA, types=TYPES, dialect="bigquery", **kwargs):
    """The synthesized database replayed from scratch, the way the evals replay a reported counterexample."""

    counterexample = rs.as_counterexample(found, schema, types)
    typed = {t: dict(types[t]) for t in found.data}
    return replay_counterexample(left, right, counterexample, schema=typed, dialect=dialect, **kwargs)


# --- what it refutes ------------------------------------------------------------------------------------


def test_not_in_with_nulls_is_refuted():
    # NOT IN is UNKNOWN for every row once the subquery returns a NULL; NOT EXISTS keeps the rows
    found = synth(NOT_IN, NOT_EXISTS)
    assert found is not None
    assert sorted(found.left_rows) != sorted(found.right_rows)
    assert any(row[0] is None for rows in found.data.values() for row in rows)  # the witness holds a NULL
    assert replays(found, NOT_IN, NOT_EXISTS)


def test_not_in_equals_not_exists_when_the_columns_are_not_null():
    declared = {"t": TableConstraints(not_null=frozenset({"a"})), "u": TableConstraints(not_null=frozenset({"a"}))}
    assert synth(NOT_IN, NOT_EXISTS, constraints=declared, time_limit=1.5) is None


def test_not_in_stays_refutable_when_only_the_subquery_column_is_not_null():
    # a NULL on the outer side still separates them (NULL NOT IN (1) is unknown, NOT EXISTS is true)
    declared = {"u": TableConstraints(not_null=frozenset({"a"}))}
    found = synth(NOT_IN, NOT_EXISTS, constraints=declared)
    assert found is not None
    assert all(row[0] is not None for row in found.data["u"])  # and the declared column stays NOT NULL
    assert replays(found, NOT_IN, NOT_EXISTS, not_null={"u": ["a"]})


CORRELATED_COUNT = "SELECT a, (SELECT COUNT(*) FROM u WHERE u.a = t.a) AS c FROM t"
DOMAIN_JOIN = "SELECT t.a, COUNT(*) AS c FROM (SELECT DISTINCT a FROM t) AS t JOIN u ON u.a = t.a GROUP BY t.a"
DOMAIN_LEFT_JOIN = (
    "SELECT t.a, COALESCE(d.c, 0) AS c FROM t LEFT JOIN (SELECT a, COUNT(*) AS c FROM u GROUP BY a) AS d ON d.a = t.a"
)


def test_correlated_count_against_its_domain_join_is_refuted():
    # decorrelating COUNT through an inner join loses the keys that have no match (the COUNT bug)
    found = synth(CORRELATED_COUNT, DOMAIN_JOIN)
    assert found is not None
    assert found.rows <= 3
    assert any(row[1] == 0 for row in found.left_rows)  # the correlated form reports a count of 0
    assert replays(found, CORRELATED_COUNT, DOMAIN_JOIN)


def test_correlated_count_against_its_correct_decorrelation_is_not_refuted():
    # the outer join plus COALESCE keeps the zero counts: the pair is equivalent
    assert synth(CORRELATED_COUNT, DOMAIN_LEFT_JOIN, time_limit=1.5) is None


def test_an_equivalent_pair_gives_none():
    assert synth(*EQUIVALENT, time_limit=1.5) is None


def test_identical_queries_give_none():
    assert synth("SELECT a FROM t", "SELECT a FROM t", time_limit=1.0) is None


def test_a_key_removes_the_difference_between_distinct_and_all():
    left, right = "SELECT DISTINCT a FROM t", "SELECT a FROM t"
    found = synth(left, right)
    assert found is not None and replays(found, left, right)
    keyed = {"t": TableConstraints(keys=(("a",),))}
    assert synth(left, right, constraints=keyed, time_limit=1.0) is None


def test_a_foreign_key_keeps_the_parent_rows():
    schema = {"c": ["x", "pid"], "p": ["id"]}
    types = {"c": {"x": "INT64", "pid": "INT64"}, "p": {"id": "INT64"}}
    left, right = "SELECT x FROM c", "SELECT c.x FROM c JOIN p ON c.pid = p.id"
    no_fk = {"c": TableConstraints(not_null=frozenset({"pid"}))}
    found = synth(left, right, schema, types, constraints=no_fk)
    assert found is not None and found.data["p"] == []  # a child row nobody owns
    with_fk = {**no_fk, "c": TableConstraints(not_null=frozenset({"pid"}), foreign_keys=((("pid",), "p", ("id",)),)), "p": TableConstraints(keys=(("id",),))}
    assert synth(left, right, schema, types, constraints=with_fk, time_limit=1.5) is None


def test_many_rows_are_found_by_the_multiplicity_stage():
    left, right = "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 100", "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 101"
    found = synth(left, right, stages=("multiplicity",))
    assert found is not None
    assert found.method == "multiplicity"
    assert found.rows == 101  # shrinking keeps a large witness's copies, and 101 rows is the smallest one
    assert replays(found, left, right)


def test_mysql_pairs_run_through_the_mysql_translation():
    schema = {"t": ["a", "b"]}
    types = {"t": {"a": "INT", "b": "INT"}}
    left, right = "SELECT COUNT(*) FROM t", "SELECT COUNT(a) FROM t"
    found = synth(left, right, schema, types, dialect="mysql")
    assert found is not None
    assert replays(found, left, right, schema, types, dialect="mysql")
    assert synth("SELECT COUNT(*) FROM t", "SELECT COUNT(1) FROM t", schema, types, dialect="mysql", time_limit=1.0) is None


def test_queries_that_read_no_table_are_decided_by_one_evaluation():
    assert synth("SELECT 1 AS x", "SELECT 2 AS x", {}, {}).data == {}
    assert synth("SELECT 1 AS x", "SELECT 1 AS x", {}, {}) is None


def test_each_stage_can_be_run_alone():
    for stage, expected in (("search", "search"), ("bounded", "bounded")):
        found = synth(NOT_IN, NOT_EXISTS, stages=(stage,))
        if found is not None:  # the bounded stage may decline; whatever it returns is confirmed
            assert found.method == expected
            assert replays(found, NOT_IN, NOT_EXISTS)
    assert synth("SELECT a FROM t", "SELECT a FROM t WHERE a > 0", stages=()) is None


# --- the shrunk witness -------------------------------------------------------------------------------


def test_the_witness_is_shrunk_and_replays():
    left, right = "SELECT a FROM t", "SELECT a FROM t WHERE a > 0"
    found = synth(left, right)
    assert found is not None
    assert found.rows == 1
    assert found.data == {"t": [found.data["t"][0]]}
    assert found.tried >= 1 and found.seconds >= 0
    assert replays(found, left, right)


def test_shrink_drops_rows_and_tables_the_difference_does_not_need():
    left, right = "SELECT a FROM t", "SELECT a FROM t WHERE a > 1"
    data = {"t": [(5, 0), (6, 0), (1, 0), (7, 0)], "u": [(1, 1), (2, 2)]}
    with Judge(left, right, {t: TYPES[t] for t in ("t", "u")}) as judge:
        assert judge.verdict(data) is Verdict.DIFFERS
        small = rs.shrink(judge, data, time.monotonic() + 5)
        assert small == {"t": [(1, 0)], "u": []}
        assert judge.verdict(small) is Verdict.DIFFERS


def test_shrink_never_returns_a_database_the_judge_rejects():
    left, right = "SELECT a FROM t", "SELECT a + 1 FROM t"
    keys = {"t": [["a"]]}
    data = {"t": [(1, 1), (2, 2), (3, 3)]}
    with Judge(left, right, {"t": TYPES["t"]}, keys=keys) as judge:
        small = rs.shrink(judge, data, time.monotonic() + 5)
        assert len(small["t"]) == 1
        assert judge.verdict(small) is Verdict.DIFFERS


def test_shrink_without_time_returns_the_database_unchanged():
    left, right = "SELECT a FROM t", "SELECT a FROM t WHERE a > 1"
    data = {"t": [(5, 0), (6, 0)]}
    with Judge(left, right, {"t": TYPES["t"]}) as judge:
        assert rs.shrink(judge, data, time.monotonic() - 1) == data


# --- types and switches ---------------------------------------------------------------------------------

DIFFERENT = ("SELECT a FROM t", "SELECT a FROM t WHERE a > 0")


@pytest.mark.parametrize(
    "types",
    [
        None,
        {},
        {"u": TYPES["u"]},  # the table read has no types
        {"t": {"a": "INT64"}},  # a column of it has none
    ],
)
def test_no_declared_types_means_no_search(types, monkeypatch):
    # a failing judge would show if the search ran at all
    def judged(*args, **kwargs):
        raise AssertionError("the judge ran without declared types")

    monkeypatch.setattr(rs, "Judge", judged)
    assert rs.synthesize(*DIFFERENT, schema=SCHEMA, types=types) is None


def test_no_schema_means_no_search():
    assert rs.synthesize(*DIFFERENT, schema=None, types=TYPES) is None
    assert rs.synthesize(*DIFFERENT, schema={}, types=TYPES) is None


def test_a_query_that_does_not_parse_gives_none():
    assert synth("SELEC FROM", "SELECT a FROM t") is None


def test_the_same_pair_is_refuted_once_types_are_declared():
    assert synth(*DIFFERENT) is not None


def test_typed_schema_adds_foreign_key_parents_and_needs_every_column_type():
    schema = {"c": ["x", "pid"], "p": ["id"]}
    types = {"c": {"x": "INT64", "pid": "INT64"}, "p": {"id": "INT64"}}
    constraints = {"c": TableConstraints(foreign_keys=((("pid",), "p", ("id",)),))}
    typed = rs.typed_schema({"c"}, schema, types, constraints)
    assert typed == {"c": types["c"], "p": types["p"]}
    assert rs.typed_schema({"c"}, schema, types) == {"c": types["c"]}
    assert rs.typed_schema({"c"}, schema, {"c": {"x": "INT64"}}) is None
    assert rs.typed_schema({"missing"}, schema, types) is None
    assert rs.typed_schema({"c"}, {"C": ["X"]}, {"c": {"x": "INT64"}}) == {"C": {"X": "INT64"}}  # names compare in any case


def test_synthesis_is_off_with_the_environment_switch(monkeypatch):
    assert rs.synthesize(*DIFFERENT, schema=SCHEMA, types=TYPES) is not None
    monkeypatch.setenv("KUMOSQL_SYNTHESIS", "0")
    assert rs.enabled() is False
    assert rs.synthesize(*DIFFERENT, schema=SCHEMA, types=TYPES) is None
    unproven = SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unknown")
    assert rs.refute_unproven(*DIFFERENT, unproven, schema=SCHEMA, types=TYPES) is unproven
    monkeypatch.setenv("KUMOSQL_SYNTHESIS", "1")
    assert rs.enabled() is True


# a difference that needs 101 rows: beyond the solver and the cheaper executed search, so only the synthesis refutes it
MANY_ROWS = ("SELECT a FROM t GROUP BY a HAVING COUNT(*) > 100", "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 101")
NOT_IN_PAIR = (NOT_IN, NOT_EXISTS)


def test_the_environment_switch_turns_the_prover_hook_off(monkeypatch):
    def prove():
        return prove_equivalent_algebraic(*MANY_ROWS, schema=SCHEMA, types=TYPES, compare_names=False, search_counterexample=True)

    assert prove().status is SmtStatus.NOT_EQUIVALENT
    monkeypatch.setenv("KUMOSQL_SYNTHESIS", "0")
    assert prove().status is SmtStatus.NOT_PROVEN


# --- the prover hooks -----------------------------------------------------------------------------------


def test_refute_unproven_returns_a_replayable_not_equivalent_result():
    unproven = SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unknown")
    result = rs.refute_unproven(*NOT_IN_PAIR, unproven, schema=SCHEMA, types=TYPES, dialect="bigquery")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.assumptions == (rs.ASSUMPTION,)
    assert "found by" in result.reason
    assert replay_counterexample(*NOT_IN_PAIR, result.counterexample, schema=TYPES, dialect="bigquery")
    # nothing found leaves the result as it was
    assert rs.refute_unproven(*EQUIVALENT, unproven, schema=SCHEMA, types=TYPES) is unproven


BOGUS = Counterexample(tables={"t": [{"a": 1, "b": 1}]}, left_rows=[(1,)], right_rows=[])
GENUINE = Counterexample(tables={"t": [{"a": None, "b": 1}]}, left_rows=[(None,)], right_rows=[])
IS_NOT_NULL = ("SELECT a FROM t", "SELECT a FROM t WHERE a IS NOT NULL")


def claim(counterexample):
    return SmtEquivalenceResult(SmtStatus.NOT_EQUIVALENT, "the solver found a model", counterexample=counterexample)


def test_a_bogus_solver_counterexample_becomes_not_proven():
    # both queries return (1) on the solver's database, so it separates nothing
    checked = rs.check_solver_counterexample(*IS_NOT_NULL, claim(BOGUS), schema=SCHEMA, types=TYPES)
    assert checked.status is SmtStatus.NOT_PROVEN
    assert checked.counterexample is None
    assert "same rows" in checked.reason


def test_a_genuine_solver_counterexample_is_kept():
    result = claim(GENUINE)
    assert rs.check_solver_counterexample(*IS_NOT_NULL, result, schema=SCHEMA, types=TYPES) is result


def test_a_solver_counterexample_that_cannot_be_replayed_is_left_alone():
    result = claim(BOGUS)
    assert rs.check_solver_counterexample(*IS_NOT_NULL, result, schema=SCHEMA, types=None) is result  # no types
    assert rs.check_solver_counterexample(*IS_NOT_NULL, result, schema=SCHEMA, types={"t": {"a": "INT64"}}) is result
    assert rs.check_solver_counterexample("SELEC FROM", IS_NOT_NULL[1], result, schema=SCHEMA, types=TYPES) is result
    assert rs.check_solver_counterexample("SELECT a / b FROM t", "SELECT a FROM t", claim(Counterexample({"t": [{"a": 1, "b": 0}]}, [], [])), schema=SCHEMA, types=TYPES).status is SmtStatus.NOT_EQUIVALENT
    no_database = SmtEquivalenceResult(SmtStatus.NOT_EQUIVALENT, "no model attached")
    assert rs.check_solver_counterexample(*IS_NOT_NULL, no_database, schema=SCHEMA, types=TYPES) is no_database


def test_an_illegal_solver_counterexample_is_not_downgraded():
    # the database breaks a declared key: the judge says illegal, not same, so the solver's answer stands
    two_rows = Counterexample(tables={"t": [{"a": 1, "b": 1}, {"a": 1, "b": 2}]}, left_rows=[], right_rows=[])
    result = claim(two_rows)
    keyed = {"t": TableConstraints(keys=(("a",),))}
    assert rs.check_solver_counterexample(*IS_NOT_NULL, result, schema=SCHEMA, types=TYPES, constraints=keyed) is result


def test_the_environment_switch_leaves_a_solver_counterexample_alone(monkeypatch):
    monkeypatch.setenv("KUMOSQL_SYNTHESIS", "0")
    result = claim(BOGUS)
    assert rs.check_solver_counterexample(*IS_NOT_NULL, result, schema=SCHEMA, types=TYPES) is result


def test_the_prover_downgrades_a_bogus_solver_counterexample(monkeypatch):
    from kumosql import algebraic_equivalence

    # a solver that claims a counterexample for a pair that is equivalent
    monkeypatch.setattr(algebraic_equivalence, "_prove_algebraic", lambda *args, **kwargs: claim(BOGUS))
    kwargs = {"schema": SCHEMA, "types": TYPES, "compare_names": False}
    without = prove_equivalent_algebraic(*EQUIVALENT, **kwargs)
    assert without.status is SmtStatus.NOT_EQUIVALENT  # nothing checks it unless a search is asked for
    checked = prove_equivalent_algebraic(*EQUIVALENT, **kwargs, search_counterexample=True)
    assert checked.status is SmtStatus.NOT_PROVEN
    assert checked.counterexample is None


def test_the_prover_keeps_a_solver_counterexample_that_replays(monkeypatch):
    from kumosql import algebraic_equivalence

    monkeypatch.setattr(algebraic_equivalence, "_prove_algebraic", lambda *args, **kwargs: claim(GENUINE))
    result = prove_equivalent_algebraic(*IS_NOT_NULL, schema=SCHEMA, types=TYPES, compare_names=False, search_counterexample=True)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample is GENUINE


def test_a_pair_the_prover_cannot_decide_is_refuted_with_the_attached_database():
    result = prove_equivalent_algebraic(*MANY_ROWS, schema=SCHEMA, types=TYPES, compare_names=False, search_counterexample=True)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert rs.ASSUMPTION in result.assumptions
    assert len(result.counterexample.tables["t"]) == 101
    assert replay_counterexample(*MANY_ROWS, result.counterexample, schema=TYPES, dialect="bigquery")
    plain = prove_equivalent_algebraic(*MANY_ROWS, schema=SCHEMA, types=TYPES, compare_names=False)
    assert plain.status is SmtStatus.NOT_PROVEN  # without the search it is only unproven


def test_as_counterexample_names_the_columns_and_exports_plain_values():
    from datetime import date
    from decimal import Decimal

    found = rs.Synthesis(
        {"t": [(1, 2)]}, [(Decimal("2"), Decimal("1.5"), date(2020, 1, 2))], [], "search", 0.0,
    )
    out = rs.as_counterexample(found, {"T": ["a", "b"]}, {"t": {"a": "INT64", "b": "INT64"}})
    assert out.tables == {"t": [{"a": 1, "b": 2}]}
    assert out.left_rows == [(2, 1.5, "2020-01-02")]


# --- the time limit -----------------------------------------------------------------------------------------


def test_each_stage_gets_a_share_of_the_time_limit(monkeypatch):
    seen = {}

    def recorder(name):
        def stage(*args, **kwargs):
            deadline = args[-1] if name == "search" else kwargs.get("deadline", args[-1])
            seen[name] = deadline
            return (None, 0) if name == "search" else None

        return stage

    for name, attribute in (("search", "_search"), ("bounded", "_bounded"), ("multiplicity", "_multiplicity")):
        monkeypatch.setattr(rs, attribute, recorder(name))
    began = time.monotonic()
    assert synth(*DIFFERENT, time_limit=10.0) is None
    ended = time.monotonic()
    assert set(seen) == {"search", "bounded", "multiplicity"}
    assert began + 3.9 <= seen["search"] <= ended + 4.0
    assert began + 7.4 <= seen["bounded"] <= ended + 7.5
    assert began + 9.9 <= seen["multiplicity"] <= ended + 10.0


def test_no_stage_runs_once_the_time_is_up(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a stage ran after the deadline")

    for attribute in ("_search", "_bounded", "_multiplicity"):
        monkeypatch.setattr(rs, attribute, forbidden)
    assert synth(*DIFFERENT, time_limit=0) is None


def test_search_stops_at_its_deadline(monkeypatch):
    def slow_candidates(left, right, space):
        for i in range(10_000):
            time.sleep(0.02)
            yield {"t": [(i, i)], "u": []}  # a different database each time, none of them separating the pair

    monkeypatch.setattr(rs, "_candidates", slow_candidates)
    space = rs._Space({"t": TYPES["t"], "u": TYPES["u"]}, {}, "bigquery")
    with Judge(*EQUIVALENT, {"t": TYPES["t"], "u": TYPES["u"]}) as judge:
        began = time.monotonic()
        found, tried = rs._search(judge, *EQUIVALENT, space, began + 0.3)
    assert found is None
    assert 3 <= tried < 40
    assert time.monotonic() - began < 2.0


def test_search_stops_after_its_candidate_limit(monkeypatch):
    def endless(left, right, space):
        i = 0
        while True:
            i += 1
            yield {"t": [(i, i)], "u": []}

    monkeypatch.setattr(rs, "_candidates", endless)
    space = rs._Space({"t": TYPES["t"], "u": TYPES["u"]}, {}, "bigquery")
    with Judge(*EQUIVALENT, {"t": TYPES["t"], "u": TYPES["u"]}) as judge:
        found, tried = rs._search(judge, *EQUIVALENT, space, time.monotonic() + 30, limit=7)
    assert (found, tried) == (None, 7)


def test_a_short_time_limit_ends_an_unrefutable_search_in_time():
    began = time.monotonic()
    assert synth(CORRELATED_COUNT, DOMAIN_LEFT_JOIN, time_limit=0.4) is None
    assert time.monotonic() - began < 3.0  # the limit plus the time one database or solver call may overrun
