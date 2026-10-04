"""The independent check of column qualification (``proof_qualify``).

The rule picks the FROM item that owns a bare column and the prover resolves columns with a similar decision, so a
mistake in either could be shared. The checker re-derives each added qualifier from the step's SQL (and the
columns of the physical tables, supplied by the acceptance layer) and imports no rule or prover code. Fault
injection corrupts the rule and the prover's verdict the same way and requires the checker to refuse the result.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from kumosql import apply_rule, apply_rules, qualify_columns, rewrite
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.proof_qualify import QUALIFY_ASSUMPTIONS, QUALIFY_FAMILY, check_qualify_transition
from kumosql.proof_steps import RewriteStep
from kumosql.prover_context import use_columns
from kumosql.rewrite import INDEPENDENT_CHECK, VerificationStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TABLES = {
    "p.d.orders": ["id", "total", "cid"],
    "p.d.customers": ["name", "cid", "region"],
    "a": ["x", "k", "arr", "s"],
    "b": ["y", "k", "f"],
    "c": ["z", "w"],
}


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


def step_of(before: str, after: str, tables: dict | None = None) -> RewriteStep:
    known = tuple(sorted((name, tuple(columns)) for name, columns in (TABLES if tables is None else tables).items()))
    return RewriteStep("qualify_columns", QUALIFY_FAMILY, 0, before, after, QUALIFY_ASSUMPTIONS, known_columns=known)


def check(before: str, after: str, tables: dict | None = None):
    return check_qualify_transition(step_of(before, after, tables), parse(before), parse(after))


def independent(result) -> list:
    return [record for record in result.verification.checks if record.kind == INDEPENDENT_CHECK]


# --- valid qualifications are accepted --------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    (
        "SELECT id, name FROM `p.d.orders` AS o JOIN `p.d.customers` ON o.cid = customers.cid WHERE total > 1",
        "SELECT o.id, customers.name FROM `p.d.orders` AS o JOIN `p.d.customers` ON o.cid = customers.cid WHERE o.total > 1",
    ),
    ("SELECT k, x, y FROM a JOIN b USING (k)", "SELECT k, a.x, b.y FROM a JOIN b USING (k)"),
    (  # a name that is not a select alias in WHERE: aliases are not visible there
        "SELECT x AS y FROM a JOIN b ON a.k = b.k WHERE y > 1",
        "SELECT a.x AS y FROM a JOIN b ON a.k = b.k WHERE b.y > 1",
    ),
    (  # a bare column of the select list that is also grouped on: the same column, so it may be qualified everywhere
        "SELECT x, y FROM a JOIN b ON a.k = b.k GROUP BY x, y",
        "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k GROUP BY a.x, b.y",
    ),
    (  # a window clause or ORDER BY naming a column that is not an output name
        "SELECT x, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY x ORDER BY y",
        "SELECT a.x, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY a.x ORDER BY b.y",
    ),
    (  # the ON of a join reads the sources up to and including its own join
        "SELECT 1 FROM a JOIN b ON x = y JOIN c ON z = w",
        "SELECT 1 FROM a JOIN b ON a.x = b.y JOIN c ON c.z = c.w",
    ),
    (  # an UNNEST argument reads the sources before it
        "SELECT x FROM a JOIN b ON a.k = b.k CROSS JOIN UNNEST(arr) AS e",
        "SELECT a.x FROM a JOIN b ON a.k = b.k CROSS JOIN UNNEST(a.arr) AS e",
    ),
    (  # an UNNEST value and offset stay bare
        "SELECT x, v, o FROM a CROSS JOIN UNNEST(a.arr) AS v WITH OFFSET AS o",
        "SELECT a.x, v, o FROM a CROSS JOIN UNNEST(a.arr) AS v WITH OFFSET AS o",
    ),
    (  # CTEs and derived tables list their own columns
        "WITH s AS (SELECT 1 AS p, 2 AS k), t AS (SELECT 3 AS q, 2 AS k) SELECT p, q FROM s JOIN t ON s.k = t.k",
        "WITH s AS (SELECT 1 AS p, 2 AS k), t AS (SELECT 3 AS q, 2 AS k) SELECT s.p, t.q FROM s JOIN t ON s.k = t.k",
    ),
    (
        "SELECT p, q FROM (SELECT 1 AS p, 2 AS k) AS s JOIN (SELECT 3 AS q, 2 AS k) AS t ON s.k = t.k",
        "SELECT s.p, t.q FROM (SELECT 1 AS p, 2 AS k) AS s JOIN (SELECT 3 AS q, 2 AS k) AS t ON s.k = t.k",
    ),
    (  # a union takes its column names from its first query
        "SELECT p, q FROM (SELECT 1 AS p UNION ALL SELECT 2) AS s JOIN (SELECT 3 AS q) AS t ON TRUE",
        "SELECT s.p, t.q FROM (SELECT 1 AS p UNION ALL SELECT 2) AS s JOIN (SELECT 3 AS q) AS t ON TRUE",
    ),
    (  # each select is judged on its own sources: the inner select qualifies only what it owns
        "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` AS o ON b.k = o.cid WHERE y = x)",
        "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` AS o ON b.k = o.cid WHERE b.y = x)",
    ),
    (  # a CTE shadows a physical table of the same name
        "WITH a AS (SELECT 1 AS only_here) SELECT only_here, y FROM a JOIN b ON TRUE",
        "WITH a AS (SELECT 1 AS only_here) SELECT a.only_here, b.y FROM a JOIN b ON TRUE",
    ),
    (  # an alias is case-insensitive
        "SELECT x, y FROM a AS Left_Side JOIN b AS right_side ON TRUE",
        "SELECT left_side.x, RIGHT_SIDE.y FROM a AS Left_Side JOIN b AS right_side ON TRUE",
    ),
])
def test_valid_qualifications_are_accepted(before, after):
    result = check(before, after)
    assert result.accepted, result.reason
    assert result.cases_checked >= 1


# --- unsound qualifications are refused ---------------------------------------------------------------------

@pytest.mark.parametrize("before,after,reason", [
    # the wrong table
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT b.x FROM a JOIN b ON a.k = b.k", "its source is 'a'"),
    # the table's own name where an alias hides it
    ("SELECT x FROM a AS q JOIN b ON TRUE", "SELECT a.x FROM a AS q JOIN b ON TRUE", "its source is 'q'"),
    # a source that is not in the select at all
    ("SELECT x FROM a JOIN b ON TRUE", "SELECT zzz.x FROM a JOIN b ON TRUE", "its source is 'a'"),
    # a select alias, qualified as if it were a table column (GROUP BY reads the alias first)
    ("SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y",
     "SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY b.y", "names an output"),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k ORDER BY y", "SELECT x AS y FROM a JOIN b ON a.k = b.k ORDER BY b.y", "names an output"),
    ("SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY x HAVING y > 0",
     "SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY x HAVING b.y > 0", "names an output"),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k QUALIFY ROW_NUMBER() OVER (ORDER BY y) = 1",
     "SELECT x AS y FROM a JOIN b ON a.k = b.k QUALIFY ROW_NUMBER() OVER (ORDER BY b.y) = 1", "names an output"),
    # a * REPLACE name is an output name too
    ("SELECT * REPLACE (x + 1 AS y) FROM a JOIN b ON TRUE ORDER BY y", "SELECT * REPLACE (x + 1 AS y) FROM a JOIN b ON TRUE ORDER BY b.y", "names an output"),
    # a struct field output: ORDER BY f reads the output f (a.s.f), not the column b.f
    ("SELECT s.f FROM a JOIN b ON a.k = b.k ORDER BY f", "SELECT s.f FROM a JOIN b ON a.k = b.k ORDER BY b.f", "names an output"),
    # two sources have the column
    ("SELECT k FROM a JOIN b ON a.x = b.y", "SELECT a.k FROM a JOIN b ON a.x = b.y", "more than one source"),
    # a table with no known columns hides a second candidate
    ("SELECT x FROM a JOIN mystery ON TRUE", "SELECT a.x FROM a JOIN mystery ON TRUE", "not known"),
    # a struct field, not a column of any source
    ("SELECT fld FROM a JOIN b ON a.k = b.k", "SELECT a.fld FROM a JOIN b ON a.k = b.k", "not a column of any source"),
    # a correlated reference to an outer select: z belongs to the outer `a`... here `w` only to the outer c
    ("SELECT 1 FROM c WHERE EXISTS (SELECT 1 FROM a JOIN b ON a.k = b.k WHERE w = 1)",
     "SELECT 1 FROM c WHERE EXISTS (SELECT 1 FROM a JOIN b ON a.k = b.k WHERE c.w = 1)", "not a column of any source"),
    # a later source's column in an earlier ON is an outer column there
    ("SELECT 1 FROM a JOIN b ON a.k = z JOIN c ON TRUE", "SELECT 1 FROM a JOIN b ON a.k = c.z JOIN c ON TRUE", "not a column of any source it can read"),
    # an UNNEST argument cannot read a later source
    ("SELECT 1 FROM UNNEST(arr) AS e CROSS JOIN a", "SELECT 1 FROM UNNEST(a.arr) AS e CROSS JOIN a", "reads no source"),
    ("SELECT 1 FROM b CROSS JOIN UNNEST(arr) AS e CROSS JOIN a", "SELECT 1 FROM b CROSS JOIN UNNEST(a.arr) AS e CROSS JOIN a", "not a column of any source it can read"),
    # a value table has no named columns
    ("SELECT x FROM (SELECT AS VALUE 1 AS x) AS s JOIN b ON TRUE", "SELECT s.x FROM (SELECT AS VALUE 1 AS x) AS s JOIN b ON TRUE", "not all named"),
    ("SELECT g FROM (SELECT AS STRUCT 1 AS g) AS s JOIN b ON TRUE", "SELECT s.g FROM (SELECT AS STRUCT 1 AS g) AS s JOIN b ON TRUE", "not all named"),
    # a star or an unnamed expression hides columns
    ("SELECT p FROM (SELECT * FROM a) AS s JOIN b ON TRUE", "SELECT s.p FROM (SELECT * FROM a) AS s JOIN b ON TRUE", "not all named"),
    ("SELECT p FROM (SELECT 1 + 1, 2 AS p) AS s JOIN b ON TRUE", "SELECT s.p FROM (SELECT 1 + 1, 2 AS p) AS s JOIN b ON TRUE", "not all named"),
    # USING merges the column
    ("SELECT k FROM a LEFT JOIN b USING (k)", "SELECT a.k FROM a LEFT JOIN b USING (k)", "USING"),
    # a NATURAL join, a SEMI join, a pivot, a table function, a duplicate source name
    ("SELECT x FROM a NATURAL JOIN b", "SELECT a.x FROM a NATURAL JOIN b", "NATURAL"),
    ("SELECT x FROM a JOIN a ON TRUE", "SELECT a.x FROM a JOIN a ON TRUE", "share a name"),
    ("SELECT x FROM a CROSS JOIN GENERATE_ARRAY(1, 2) AS g", "SELECT a.x FROM a CROSS JOIN GENERATE_ARRAY(1, 2) AS g", ""),
    # the name of a source, an UNNEST value, an offset
    ("SELECT a FROM a JOIN b ON TRUE", "SELECT b.a FROM a JOIN b ON TRUE", "name of a source"),
    ("SELECT v FROM a CROSS JOIN UNNEST(a.arr) AS v", "SELECT a.v FROM a CROSS JOIN UNNEST(a.arr) AS v", "name of a source"),
    ("SELECT o FROM a CROSS JOIN UNNEST(a.arr) AS v WITH OFFSET AS o", "SELECT a.o FROM a CROSS JOIN UNNEST(a.arr) AS v WITH OFFSET AS o", "UNNEST element or offset"),
    # a star's EXCEPT names, a set operation's ORDER BY, a date part argument
    ("SELECT * EXCEPT (x) FROM a JOIN b ON a.k = b.k", "SELECT * EXCEPT (a.x) FROM a JOIN b ON a.k = b.k", "star"),
    ("SELECT x FROM a JOIN b ON TRUE UNION ALL SELECT y FROM b JOIN a ON TRUE ORDER BY x",
     "SELECT x FROM a JOIN b ON TRUE UNION ALL SELECT y FROM b JOIN a ON TRUE ORDER BY a.x", "set operation"),
    ("SELECT FOO(x, MONTH) FROM a JOIN b ON TRUE", "SELECT FOO(a.x, b.MONTH) FROM a JOIN b ON TRUE", "date part"),
    # a recursive WITH
    ("WITH RECURSIVE r AS (SELECT 1 AS p UNION ALL SELECT p + 1 FROM r) SELECT p FROM r JOIN b ON TRUE",
     "WITH RECURSIVE r AS (SELECT 1 AS p UNION ALL SELECT p + 1 FROM r) SELECT r.p FROM r JOIN b ON TRUE", "recursive"),
    # a DML statement has no select scope
    ("UPDATE a SET x = 1 FROM b WHERE a.k = b.k AND y = 2", "UPDATE a SET x = 1 FROM b WHERE a.k = b.k AND b.y = 2", "SELECT"),
])
def test_unsound_qualifications_are_refused(before, after, reason):
    result = check(before, after)
    assert not result.accepted
    assert reason in result.reason, result.reason


@pytest.mark.parametrize("before,after", [
    # an extra change hiding inside the step
    ("SELECT x FROM a JOIN b ON a.k = b.k WHERE y > 1", "SELECT a.x FROM a JOIN b ON a.k = b.k WHERE b.y > 2"),
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x AS x2 FROM a JOIN b ON a.k = b.k"),
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a LEFT JOIN b ON a.k = b.k"),
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k LIMIT 1"),
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT DISTINCT a.x FROM a JOIN b ON a.k = b.k"),
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON b.k = a.k"),
    # an already-qualified column changed to another qualifier
    ("SELECT a.x FROM a JOIN b ON a.k = b.k", "SELECT b.x FROM a JOIN b ON a.k = b.k"),
    # a qualifier removed
    ("SELECT a.x FROM a JOIN b ON a.k = b.k", "SELECT x FROM a JOIN b ON a.k = b.k"),
    # a different column under a gained qualifier
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.k FROM a JOIN b ON a.k = b.k"),
    # a database part added as well
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT d.a.x FROM a JOIN b ON a.k = b.k"),
    # nothing added
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT x FROM a JOIN b ON a.k = b.k"),
    # a column added or dropped
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k"),
    # a literal changed
    ("SELECT x FROM a JOIN b ON a.k = b.k WHERE a.k = 1", "SELECT a.x FROM a JOIN b ON a.k = b.k WHERE a.k = 2"),
])
def test_a_change_other_than_added_qualifiers_is_refused(before, after):
    assert not check(before, after).accepted


def test_one_unjustified_qualifier_refuses_the_whole_step():
    result = check("SELECT x, y FROM a JOIN b ON a.k = b.k", "SELECT a.x, a.y FROM a JOIN b ON a.k = b.k")
    assert not result.accepted and "'y'" in result.reason


def test_plain_tables_need_the_columns_the_acceptance_layer_supplies():
    sql = ("SELECT x, y FROM a JOIN b ON a.k = b.k", "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k")
    assert check(*sql).accepted
    assert not check(*sql, tables={}).accepted
    assert not check(*sql, tables={"a": ["x", "k"]}).accepted  # b unknown
    assert not check(*sql, tables={"a": ["x", "k"], "b": ["k"]}).accepted  # y has no owner
    assert not check(*sql, tables={"a": ["x", "y", "k"], "b": ["y", "k"]}).accepted  # two owners
    assert check(*sql, tables={"a": ["X", "K"], "b": ["Y", "K"]}).accepted  # names are case-insensitive


def test_wrong_family_or_assumptions_are_refused():
    before, after = "SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k"
    record = step_of(before, after)
    assert check_qualify_transition(record, parse(before), parse(after)).accepted
    assert not check_qualify_transition(replace(record, family="unregistered"), parse(before), parse(after)).accepted
    for assumptions in ((), QUALIFY_ASSUMPTIONS[:-1], QUALIFY_ASSUMPTIONS + ("x_is_not_null",)):
        assert not check_qualify_transition(replace(record, assumptions=assumptions), parse(before), parse(after)).accepted


def test_the_check_reads_the_text_not_the_callers_tree():
    before = parse("SELECT x FROM a JOIN b ON a.k = b.k")
    wrong = before.copy()
    for column in wrong.find_all(exp.Column):
        if not column.table:
            column.set("table", exp.to_identifier("b"))
    honest = parse("SELECT a.x FROM a JOIN b ON a.k = b.k")
    record = step_of(before.sql(dialect="bigquery"), wrong.sql(dialect="bigquery"))
    assert not check_qualify_transition(record, honest, honest).accepted  # the trees look fine, the text is wrong
    ok = step_of(before.sql(dialect="bigquery"), honest.sql(dialect="bigquery"))
    assert check_qualify_transition(ok, wrong, wrong).accepted  # and the other way round


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    from kumosql import proof_qualify

    def broken(*args, **kwargs):
        raise RuntimeError("scope analysis unavailable")

    monkeypatch.setattr(proof_qualify, "_sources", broken)
    result = check("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k")
    assert not result.accepted and "unavailable" in result.reason
    monkeypatch.undo()
    monkeypatch.setattr(proof_qualify, "_reparse", broken)
    assert not check("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k").accepted


def test_the_inputs_are_not_changed():
    before = parse("SELECT x FROM a JOIN b ON a.k = b.k")
    after = parse("SELECT a.x FROM a JOIN b ON a.k = b.k")
    text = before.sql(dialect="bigquery"), after.sql(dialect="bigquery")
    assert check_qualify_transition(step_of(*text), before, after).accepted
    assert (before.sql(dialect="bigquery"), after.sql(dialect="bigquery")) == text


def test_the_steps_record_what_they_were_given():
    record = step_of("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k")
    data = record.to_json()
    assert data["known_columns"]["a"] == ["x", "k", "arr", "s"]
    assert "known_columns" not in RewriteStep("r", "f", 0, "SELECT 1", "SELECT 1").to_json()


# --- the rule: accepted by the acceptance layer, with the check recorded --------------------------------------

def run(sql: str, tables: dict | None = None, rule: str = "qualify_columns"):
    with use_columns(TABLES if tables is None else tables):
        return apply_rule(rule, sql)


def test_rule_results_record_the_independent_check_and_the_columns_it_used():
    result = run("SELECT id, name FROM `p.d.orders` o JOIN `p.d.customers` c ON o.cid = c.cid WHERE total > 1")
    assert result.changes == 3
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details
    assert [record.outcome for record in independent(result)] == ["passed"]
    (check_record,) = result.verification.proof_checks
    assert check_record.step.family == QUALIFY_FAMILY and check_record.accepted
    assert dict(check_record.step.known_columns) == {
        "p.d.customers": ("name", "cid", "region"), "p.d.orders": ("id", "total", "cid"),
    }  # only the tables the statement reads


def test_the_rule_no_longer_qualifies_a_column_a_later_source_owns_in_an_earlier_on():
    # BigQuery reads z in the first ON as the outer select's column (checked on BigQuery); c is not readable there.
    sql = "SELECT 1 FROM a JOIN b ON a.k = z JOIN c ON TRUE"
    result = run(sql)
    assert result.changes == 0 and result.sql == sql
    sql = "SELECT 1 FROM UNNEST(arr) AS e CROSS JOIN a"
    assert run(sql).changes == 0


def test_the_rule_keeps_a_value_table_s_columns_unknown():
    sql = "SELECT x FROM (SELECT AS VALUE 1 AS x) AS s JOIN b ON TRUE"
    assert run(sql).changes == 0


def test_the_rule_leaves_a_date_part_argument_alone():
    tables = {"a": ["month", "x"], "b": ["y"]}
    result = run("SELECT FOO(x, MONTH) FROM a JOIN b ON TRUE", tables)
    assert " ".join(result.sql.split()) == "SELECT FOO(a.x, MONTH) FROM a JOIN b ON TRUE"


def test_the_rule_leaves_a_name_that_a_struct_field_output_takes_alone():
    result = run("SELECT s.f FROM a JOIN b ON a.k = b.k ORDER BY f")
    assert "ORDER BY f" in " ".join(result.sql.split())


# --- the rule and the prover share a wrong decision: the checker still refuses --------------------------------

class _Proved:
    proven = True
    diagnostics = ()
    reason = ""
    proof_checks = ()


def _always_proved(monkeypatch):
    # The prover's normalization shares the rule's wrong decision, so it approves whatever the rule wrote.
    monkeypatch.setattr(rewrite, "prove_equivalent", lambda old, new, **kwargs: _Proved())


def _qualify_with_the_last_source(select, columns_of, final):
    sources = qualify_columns._sources_of(select)
    if sources is None or len(sources) < 2:
        return 0
    changed = 0
    for column in columns_of.get(id(select), []):
        if column.name.lower() in {source.name for source in sources}:
            continue
        if any(column.name.lower() in (source.columns or []) for source in sources):
            column.set("table", sources[-1].qualifier.copy())
            changed += 1
    return changed


def test_a_rule_that_picks_the_wrong_table_cannot_be_certified(monkeypatch):
    sql = "SELECT x, y FROM a JOIN b ON a.k = b.k"
    monkeypatch.setattr(qualify_columns, "_qualify_select", _qualify_with_the_last_source)
    _always_proved(monkeypatch)
    result = run(sql)
    assert result.rule_success and result.sql != sql
    assert "b.x" in " ".join(result.sql.split())
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]
    assert "its source is 'a'" in independent(result)[0].detail


@pytest.mark.parametrize("fault,sql,bad", [
    # the rule takes a select alias for a source column in GROUP BY
    ("_blocked_names", "SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y", "GROUP BY b.y"),
    # the rule forgets which sources an ON clause can read
    ("_place", "SELECT 1 FROM a JOIN b ON a.k = z JOIN c ON TRUE", "ON a.k = c.z"),
    # the rule claims the value table's columns
    ("_output_names", "SELECT x FROM (SELECT AS VALUE 1 AS x) AS s JOIN b ON TRUE", "SELECT s.x"),
])
def test_a_rule_with_one_wrong_scoping_decision_cannot_be_certified(monkeypatch, fault, sql, bad):
    if fault == "_blocked_names":
        monkeypatch.setattr(qualify_columns, "_blocked_names", lambda select: set())
    elif fault == "_place":
        def place(column, select, count):
            below, top = None, column
            while top.parent is not None and top.parent is not select:
                below, top = top, top.parent
            return top.arg_key, count
        monkeypatch.setattr(qualify_columns, "_place", place)
    else:
        real = qualify_columns._output_names

        def names(query):
            if isinstance(query, exp.Select) and query.args.get("kind") == "VALUE":
                query = query.copy()
                query.set("kind", None)
            return real(query)

        monkeypatch.setattr(qualify_columns, "_output_names", names)
    _always_proved(monkeypatch)
    result = run(sql)
    assert result.rule_success and bad in " ".join(result.sql.split()), result.sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_rule_that_thinks_a_column_has_one_owner_cannot_be_certified(monkeypatch):
    # The rule's catalog reading loses b's column k, so it sees a unique owner; the checker is given the real catalog.
    real = qualify_columns._schema_columns
    monkeypatch.setattr(qualify_columns, "_schema_columns", lambda: {**real(), "b": ["y"]})
    _always_proved(monkeypatch)
    result = run("SELECT k FROM a JOIN b ON a.x = b.y")
    assert "a.k" in " ".join(result.sql.split())
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "more than one source" in independent(result)[0].detail


def test_the_real_prover_alone_does_not_decide_a_corrupted_rule(monkeypatch):
    # Without the stub: the prover or the checker (or both) refuse; the checker's refusal is recorded either way.
    monkeypatch.setattr(qualify_columns, "_qualify_select", _qualify_with_the_last_source)
    result = run("SELECT x, y FROM a JOIN b ON a.k = b.k")
    assert result.verification.status is not VerificationStatus.PROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]


class _QualifiesWrongly(RewriteRule):
    name = "qualify_columns"
    summary = "Fault injection: qualifies with the wrong table without going through the shared driver"

    def apply(self, sql):
        return RuleOutput("SELECT b.x FROM a JOIN b ON a.k = b.k", 1, 1, 1, 0, ())


def test_an_override_cannot_opt_out_of_the_qualification_check():
    with use_columns(TABLES):
        result = apply_rule(
            "qualify_columns", "SELECT x FROM a JOIN b ON a.k = b.k", overrides={"qualify_columns": _QualifiesWrongly()},
        )
    assert not result.success
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_later_step_cannot_rescue_a_refused_qualification_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("SELECT x FROM a JOIN b ON a.k = b.k", 1, 1, 1, 0, ())

    with use_columns(TABLES):
        result = apply_rules(
            ["qualify_columns", "restore"], "SELECT x FROM a JOIN b ON a.k = b.k",
            overrides={"qualify_columns": _QualifiesWrongly(), "restore": Restore()},
        )
    assert result.sql == result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason


def test_a_pipeline_with_a_correct_qualification_step_is_proven():
    with use_columns(TABLES):
        result = apply_rules(["qualify_columns"], "SELECT x, y FROM a JOIN b ON a.k = b.k")
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.reason


# --- a corpus: every change the rule makes is accepted, and every wrong qualifier is refused ------------------

def _key_of(table: exp.Table) -> str:
    parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]
    return ".".join(parts)


def _invent_schema(tree: exp.Expression, variant: int) -> dict[str, list[str]]:
    """Columns for the tables a query reads: every column it qualifies, and each bare column given to one table."""

    columns: dict[str, set[str]] = {}
    for select in tree.find_all(exp.Select):
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            continue
        relations = [from_.this] + [join.this for join in select.args.get("joins") or []]
        tables = [r for r in relations if isinstance(r, exp.Table) and isinstance(r.this, exp.Identifier)]
        if not tables:
            continue
        by_alias = {(t.alias or t.name).lower(): t for t in tables}
        for column in select.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            if column.table:
                owner = by_alias.get(column.table.lower())
                if owner is not None:
                    columns.setdefault(_key_of(owner), set()).add(column.name.lower())
            else:
                digest = int(hashlib.md5(f"{variant}:{column.name.lower()}".encode()).hexdigest(), 16)
                columns.setdefault(_key_of(tables[digest % len(tables)]), set()).add(column.name.lower())
    return {name: sorted(names) for name, names in columns.items()}


def _corpus() -> list[str]:
    queries = [json.loads(line)["sql"] for line in (FIXTURES / "qualify_columns" / "queries.jsonl").read_text().splitlines()]
    for pattern in ("bq_corpora/**/*.sql", "bq_syntax/sql/query/*.sql", "jaffle_shop/**/*.sql"):
        for path in sorted(FIXTURES.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "${" not in text:
                queries.append(text)
    return queries


def _gained(before: exp.Expression, after: exp.Expression) -> list[exp.Column]:
    old, new = list(before.find_all(exp.Column)), list(after.find_all(exp.Column))
    assert len(old) == len(new)
    return [n for o, n in zip(old, new) if not o.table and n.table]


@pytest.mark.parametrize("variant", [0, 1, 2])
def test_every_change_the_rule_makes_on_the_corpus_is_accepted_and_every_wrong_qualifier_is_refused(variant):
    changed = qualifiers = refused_mutations = 0
    for sql in _corpus():
        try:
            tree = parse(sql)
        except Exception:  # noqa: BLE001 - a corpus statement sqlglot cannot read is not a qualification case
            continue
        schema = _invent_schema(tree, variant)
        with use_columns(schema):
            result = apply_rule("qualify_columns", sql)
        if not result.changes:
            continue
        changed += 1
        records = independent(result)
        assert records and all(record.outcome == "passed" for record in records), (sql, result.sql, [r.detail for r in records])
        qualifiers += result.changes
        before_text = tree.sql(dialect="bigquery")
        after = parse(result.sql)
        known = tuple(sorted((name, tuple(columns)) for name, columns in schema.items()))
        # Re-qualify one added qualifier with another source's name: that must never be accepted.
        names = {t.alias_or_name.lower() for t in after.find_all(exp.Table)}
        for column in _gained(tree, after)[:3]:
            wrong = sorted(n for n in names | {"no_such_source"} if n != column.table.lower())[0]
            original = column.args["table"]
            column.set("table", exp.to_identifier(wrong))
            mutated = RewriteStep("qualify_columns", QUALIFY_FAMILY, 0, before_text, after.sql(dialect="bigquery"), QUALIFY_ASSUMPTIONS, known_columns=known)
            assert not check_qualify_transition(mutated, tree, after).accepted, (sql, wrong)
            refused_mutations += 1
            column.set("table", original)
    assert changed >= 25 and qualifiers >= 50 and refused_mutations >= 50, (changed, qualifiers, refused_mutations)


def test_the_rule_never_qualifies_what_the_checker_would_refuse_on_hand_written_queries():
    # the queries of tests/test_qualify_columns.py, run with its tables
    queries = [
        "SELECT id, name FROM `p.d.orders` AS o JOIN `p.d.customers` ON o.cid = customers.cid WHERE total > 1",
        "SELECT k, x, y FROM a JOIN b USING (k)",
        "SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y HAVING n > 0",
        "SELECT x AS y FROM a JOIN b ON a.k = b.k WHERE y > 1",
        "SELECT * FROM (SELECT x FROM a JOIN b ON a.k = b.k ORDER BY y LIMIT 3)",
        "SELECT x, v, o FROM a, UNNEST(a.arr) AS v WITH OFFSET AS o",
        "SELECT x, y FROM a JOIN b ON a.k = b.k WHERE x IN (SELECT total FROM `p.d.orders`)",
        "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` o ON b.k = o.cid WHERE y = x)",
        "SELECT x FROM a JOIN b ON a.k = b.k UNION ALL SELECT y FROM b JOIN a ON a.k = b.k ORDER BY x",
        "SELECT z, x FROM c, a, b WHERE w = y",
    ]
    changed = 0
    for sql in queries:
        result = run(sql)
        if result.changes:
            changed += 1
            assert [record.outcome for record in independent(result)] == ["passed"], (sql, result.sql)
    assert changed >= 8
