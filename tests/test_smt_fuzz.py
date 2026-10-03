"""Differential check of the SMT prover against SQLite on random databases.

Every proof must hold on random instances, and every counterexample must
really separate the two queries. The generated subset (integers, NULLs,
joins, filters, GROUP BY) has the same semantics in SQLite and BigQuery.

The 250 pairs are generated up front from their own RNG and pinned by a
sha256, so the prover's answers cannot change which pairs are asked; each
pair's validation databases come from a separate RNG seeded by its index.
A text rewrite often does not apply, leaving two identical queries; those
identity pairs are a sanity check (all must be proven) and the proof floor
counts only pairs whose text changed. Unresolved pairs have no truth label,
so the counts are coverage floors, not accuracy.
"""

from collections import Counter
import hashlib
import random
import sqlite3

import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt


def _atom(rng, cols):
    col = rng.choice(cols)
    roll = rng.random()
    if roll < 0.4:
        return f"{col} {rng.choice(['=', '<>', '<', '<=', '>', '>='])} {rng.randint(0, 3)}"
    if roll < 0.7:
        return f"{col} {rng.choice(['=', '<', '>='])} {rng.choice(cols)}"
    if roll < 0.85:
        return f"{col} IS {rng.choice(['', 'NOT '])}NULL"
    return f"{col} IN ({rng.randint(0, 3)}, {rng.randint(0, 3)})"


def _pred(rng, cols, depth=2):
    if depth == 0 or rng.random() < 0.3:
        return _atom(rng, cols)
    op = rng.choice(["AND", "OR", "NOT"])
    if op == "NOT":
        return f"NOT ({_pred(rng, cols, depth - 1)})"
    return f"({_pred(rng, cols, depth - 1)}) {op} ({_pred(rng, cols, depth - 1)})"


def _query(rng):
    if rng.random() < 0.5:
        cols = ["x.a", "x.b", "y.a", "y.b"]
        source = f"t AS x JOIN {rng.choice(['t', 'u'])} AS y ON {_pred(rng, cols, 1)}"
    else:
        cols = ["a", "b"]
        source = "t"
    key = rng.choice(cols)
    if rng.random() < 0.3:
        aggregate = rng.choice(["COUNT(*)", f"SUM({cols[1]})", f"MIN({cols[0]})", f"COUNT({cols[1]})"])
        return f"SELECT {key} AS k, {aggregate} AS v FROM {source} WHERE {_pred(rng, cols)} GROUP BY {key}"
    distinct = "DISTINCT " if rng.random() < 0.2 else ""
    return f"SELECT {distinct}{key} AS k FROM {source} WHERE {_pred(rng, cols)}"


_REWRITES = [
    ("AND", "OR"),
    (" < ", " <= "),
    ("NOT (", "("),
    ("COUNT(*)", "COUNT(b)"),
    ("x.a", "y.a"),
    (" = ", " >= "),
    ("t AS x JOIN u", "u AS x JOIN t"),
    ("WHERE ", "WHERE TRUE AND "),
]


def _database(rng, rows=None):
    db = sqlite3.connect(":memory:")
    for table in ("t", "u"):
        db.execute(f"CREATE TABLE {table} (a INTEGER, b INTEGER)")
        if rows is None:
            data = [[rng.choice([None, 0, 1, 2, 3]) for _ in range(2)] for _ in range(rng.randint(0, 4))]
        else:
            data = [[row.get("a"), row.get("b")] for row in rows.get(table, [])]
        db.executemany(f"INSERT INTO {table} VALUES (?, ?)", data)
    return db


def _run(db, sql):
    return Counter(db.execute(sql).fetchall())


_SEED = 20260929
_PAIRS = 250
# sha256 of the generated pairs: a change to the generator (or to Python's
# random module) shows up here before it silently moves the floors below.
_PAIRS_SHA256 = "a28ae5bcb54ba73aca3a4c78d22fab872104a165551c15f4cbc29f828c043228"


def _pairs():
    """The frozen pair list: (left, right, family), generated before any proof."""
    rng = random.Random(_SEED)
    pairs = []
    for _ in range(_PAIRS):
        left = _query(rng)
        if rng.random() < 0.7:
            old, new = rng.choice(_REWRITES)
            pairs.append((left, left.replace(old, new, 1), f"{old!r}->{new!r}"))
        else:
            pairs.append((left, _query(rng), "fresh"))
    return pairs


def _digest(pairs):
    return hashlib.sha256("\n".join(f"{left}\t{right}" for left, right, _ in pairs).encode()).hexdigest()


def test_prover_agrees_with_sqlite():
    pairs = _pairs()
    assert _digest(pairs) == _PAIRS_SHA256, "pair generator changed: re-measure the floors and update the digest"
    seen = Counter()
    by_family = Counter()
    for index, (left, right, family) in enumerate(pairs):
        kind = "identity" if left == right else "changed"
        result = prove_equivalent_smt(left, right)
        seen[result.status, kind] += 1
        by_family[family, kind, result.status.name] += 1
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            data_rng = random.Random(_SEED * 1000 + index)
            for _ in range(25):
                db = _database(data_rng)
                assert _run(db, left) == _run(db, right), (left, right)
        elif result.status is SmtStatus.NOT_EQUIVALENT:
            db = _database(None, result.counterexample.tables)
            assert _run(db, left) != _run(db, right), (left, right, result.counterexample)
        if kind == "identity":
            assert result.status is SmtStatus.PROVEN_EQUIVALENT, (family, left, result.status)
    summary = {f"{status.name}/{kind}": n for (status, kind), n in sorted(seen.items(), key=str)}
    summary["by family"] = {"/".join(key): n for key, n in sorted(by_family.items())}
    # Measured on the frozen pairs (2026-10-03): 108 identity pairs (all
    # proven); of 142 changed pairs, 32 proven (23 of them the trivial
    # `WHERE TRUE AND`), 79 refuted, 31 NOT_PROVEN. NOT_PROVEN pairs have no
    # truth label: they are reported in the message, not scored. Floors are
    # the measured counts minus a small margin.
    changed_proofs = seen[SmtStatus.PROVEN_EQUIVALENT, "changed"]
    trivial = by_family["'WHERE '->'WHERE TRUE AND '", "changed", "PROVEN_EQUIVALENT"]
    assert changed_proofs >= 28, summary
    assert changed_proofs - trivial >= 7, summary
    assert seen[SmtStatus.NOT_EQUIVALENT, "changed"] >= 70, summary
