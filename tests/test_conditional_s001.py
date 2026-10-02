"""Cases from an outside source (tests/fixtures/conditional): every witness is replayed on DuckDB, and the verdicts are pinned.

The checks do not trust the provers or the case file:

* the counterexample without conditions, every deletion witness and every sufficiency example are replayed on DuckDB;
* a pair the file says has no equivalence at all is never proven or called conditional, and an unconditional control is never
  called conditional;
* every conditional verdict names conditions that the case's own counterexample breaks, holds on random databases repaired to
  meet them, and loses its proof when any one condition is dropped;
* the verdict of each prover on each pair is pinned. Pairs whose expected conditions are outside the catalog (filtered keys,
  CHECK, FD, EXISTS) must stay unproven or refuted.
"""

import functools
import json
import random
import re
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import conditional_equivalence as ce
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import prove_equivalent_smt

CASES = {c["id"]: c for c in json.loads((Path(__file__).parent / "fixtures" / "conditional" / "s001_cases.json").read_text())}

PROVEN, COND, NOT_PROVEN, REFUTED = "proven_equivalent", "proven_conditionally", "not_proven", "not_equivalent"

# (algebraic, smt): the verdict each prover gives, and for a conditional one the conditions it names. A set can be stronger than
# the file's own minimal set (the provers' keys are not filtered, and the SMT prover has no use for FD or EXISTS), never weaker.
EXPECTED = {
    "S001-001": ((NOT_PROVEN,), (NOT_PROVEN,)),  # needs a key that only holds where 1 < itemn < 20
    "S001-002": ((COND, "(a) is unique in R2", "R2.b is NOT NULL"), (COND, "(a) is unique in R2", "R2.b is NOT NULL")),  # the file asks for an FD
    "S001-003": ((COND, "(itemn) is unique in itl", "(itemn) is unique in itp"), (COND, "(itemn) is unique in itl", "(itemn) is unique in itp")),
    "S001-004": ((COND, "(ssno) is unique in payroll"), (COND, "(ssno) is unique in payroll")),
    "S001-005": ((COND, "(dept, mgr) is unique in dept"), (COND, "(dept, mgr) is unique in dept")),  # the file's key holds only where loc = 3
    "S001-006": ((REFUTED,), (REFUTED,)),  # needs a CHECK
    "S001-007": ((COND, "r.b is NOT NULL"), (COND, "r.b is NOT NULL")),
    "S001-008": ((COND, "a.c is NOT NULL"), (COND, "(c) is unique in a", "a.c is NOT NULL")),
    "S001-009": ((COND, "r.a is NOT NULL"), (COND, "r.a is NOT NULL")),
    "S001-010": ((COND, "r.a1 is NOT NULL", "r.a2 is NOT NULL"), (COND, "r.a1 is NOT NULL", "r.a2 is NOT NULL")),
    "S001-011": ((PROVEN,), (PROVEN,)),
    "S001-012": ((PROVEN,), (PROVEN,)),
    "S001-013": ((PROVEN,), (NOT_PROVEN,)),
    "S001-014": ((NOT_PROVEN,), (NOT_PROVEN,)),
    "S001-015": ((COND, "(customer_name) is unique in customer"), (NOT_PROVEN,)),
    "S001-016": ((NOT_PROVEN,), (NOT_PROVEN,)),  # needs CHECK b = 0
    "S001-017": ((NOT_PROVEN,), (NOT_PROVEN,)),  # needs CHECK b = 1
    "S001-018": ((COND, "t.a is NOT NULL"), (COND, "t.a is NOT NULL")),
    "S001-019": ((REFUTED,), (REFUTED,)),  # needs a CHECK
    "S001-020": ((COND, "t.a is NOT NULL", "t.b is NOT NULL"), (COND, "t.a is NOT NULL", "t.b is NOT NULL")),
    "S001-021": ((COND, "t.a is NOT NULL"), (COND, "t.a is NOT NULL")),
    "S001-022": ((COND, "(k) is unique in d", "p(k) references d(k)", "p.k is NOT NULL"), (REFUTED,)),  # the SMT prover has no use for foreign keys
    "S001-023": ((REFUTED,), (REFUTED,)),  # needs a CHECK
    "S001-024": ((NOT_PROVEN,), (NOT_PROVEN,)),
    "S001-025": ((NOT_PROVEN,), (NOT_PROVEN,)),
    "S001-026": ((NOT_PROVEN,), (NOT_PROVEN,)),
    "S001-027": ((REFUTED,), (REFUTED,)),
    "S001-028": ((NOT_PROVEN,), (NOT_PROVEN,)),
    "S001-029": ((NOT_PROVEN,), (NOT_PROVEN,)),
}
UNCONDITIONAL = {"S001-011", "S001-012", "S001-013", "S001-014"}
NO_EQUIVALENCE = {f"S001-{n}" for n in range(24, 30)}


def _columns(case):
    return {m.group(1): [part.strip().split()[0] for part in m.group(2).split(",")] for m in re.finditer(r"CREATE TABLE (\w+) \(([^)]*)\)", case["schema"])}


def _queries(case):
    return case.get("prove_left", case["left"]), case.get("prove_right", case["right"])


def _database(case, tables):
    db = duckdb.connect(":memory:")
    for statement in case["schema"].split(";"):
        if statement.strip():
            db.execute(statement)
    for table, rows in tables.items():
        for row in rows:
            db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in row)})", list(row))
    return db


def _bags(case, tables):
    db = _database(case, tables)
    left, right = _queries(case)
    return Counter(map(tuple, db.execute(left).fetchall())), Counter(map(tuple, db.execute(right).fetchall()))


@functools.cache
def _verdict(case_id, prover):
    case = CASES[case_id]
    left, right = _queries(case)
    prove = (prove_equivalent_algebraic, prove_equivalent_smt)[prover]
    return prove(left, right, conditional=True, schema=_columns(case))


@pytest.mark.parametrize("case_id", CASES)
def test_the_files_witnesses_replay_on_duckdb(case_id):
    case = CASES[case_id]
    if case["counterexample_without_conditions"]:
        left, right = _bags(case, case["counterexample_without_conditions"]["tables"])
        assert left != right
    for witness in case["minimality_witnesses"]:
        left, right = _bags(case, witness["tables"])
        assert left != right, witness["dropped"]
    for example in case["sufficiency_examples"]:
        left, right = _bags(case, example["tables"])
        assert left == right


@pytest.mark.parametrize("prover", [0, 1], ids=["algebraic", "smt"])
@pytest.mark.parametrize("case_id", CASES)
def test_the_verdict_is_the_pinned_one(case_id, prover):
    result = _verdict(case_id, prover)
    expected = EXPECTED[case_id][prover]
    assert result.status.value == expected[0], (result.status, result.reason)
    assert sorted(c.text for c in result.conditions or []) == sorted(expected[1:])
    if case_id in NO_EQUIVALENCE:
        assert result.status.value in {NOT_PROVEN, REFUTED}
    if case_id in UNCONDITIONAL:
        assert result.status.value != COND


def _as_dicts(columns, data):
    return {t: [dict(zip(columns[t], r)) for r in rows] for t, rows in data.items()}


def _repaired(columns, conditions, rng):
    data = {t: [[rng.choice([None, 1, 2, 3]) for _ in cols] for _ in range(rng.randint(0, 5))] for t, cols in columns.items()}
    for _ in range(4):
        at = {t: {name: i for i, name in enumerate(cols)} for t, cols in columns.items()}
        for c in conditions:
            rows = data[c.table]
            if c.kind == "not_null":
                for row in rows:
                    row[at[c.table][c.columns[0]]] = row[at[c.table][c.columns[0]]] or 1
            elif c.kind == "unique":
                seen, kept = set(), []
                for row in rows:
                    key = tuple(row[at[c.table][n]] for n in c.columns)
                    if None in key or key not in seen:
                        seen.add(key)
                        kept.append(row)
                data[c.table] = kept
            else:
                parents = [tuple(r[at[c.parent][n]] for n in c.parent_columns) for r in data[c.parent]]
                kept = []
                for row in rows:
                    key = tuple(row[at[c.table][n]] for n in c.columns)
                    if None in key or key in parents:
                        kept.append(row)
                    elif parents:
                        for n, v in zip(c.columns, rng.choice(parents)):
                            row[at[c.table][n]] = v
                        kept.append(row)
                data[c.table] = kept
        if not any(ce.broken_by(c, _as_dicts(columns, data)) for c in conditions):
            return data
    return None


CONDITIONAL = [(case_id, prover) for case_id, pair in EXPECTED.items() for prover in (0, 1) if pair[prover][0] == COND]


@pytest.mark.parametrize("case_id,prover", CONDITIONAL)
def test_a_conditional_verdict_survives_the_files_data_and_random_databases(case_id, prover):
    case = CASES[case_id]
    conditions = _verdict(case_id, prover).conditions
    columns = _columns(case)
    separating = case["counterexample_without_conditions"]["tables"]
    assert any(ce.broken_by(c, _as_dicts(columns, separating)) for c in conditions), "the file's counterexample must break a named condition"
    for example in case["sufficiency_examples"]:
        left, right = _bags(case, example["tables"])
        assert left == right
    rng, ran = random.Random(case_id), 0
    for _ in range(300):
        data = _repaired(columns, conditions, rng)
        if data is None:
            continue
        ran += 1
        left, right = _bags(case, data)
        assert left == right, data
    assert ran >= 60


@pytest.mark.parametrize("case_id,prover", CONDITIONAL)
def test_each_named_condition_is_needed(case_id, prover):
    case = CASES[case_id]
    left, right = _queries(case)
    conditions = _verdict(case_id, prover).conditions
    prove = (prove_equivalent_algebraic, prove_equivalent_smt)[prover]
    for dropped in conditions:
        rest = [c for c in conditions if c is not dropped]
        assert not prove(left, right, constraints=ce.with_conditions(None, rest) or None, schema=_columns(case)).proven, dropped.text
