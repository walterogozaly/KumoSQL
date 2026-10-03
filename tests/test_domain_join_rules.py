"""Calcite's decorrelated ANY/ALL aggregates (grouped joins and domain joins), checked on NULL-bearing DuckDB data."""

import itertools
import json
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")
import sqlglot
from sqlglot import exp

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.domain_join_rules import lateral_aggregates
from kumosql.random_check import Column, Schema, Table, find_difference, prover_constraints

# o.j is NOT NULL: a domain join may correlate on it. o.k and i.k may be NULL.
SCHEMA = Schema(
    [
        Table("o", [Column("x"), Column("k"), Column("j", not_null=True)]),
        Table("i", [Column("y"), Column("k")]),
    ]
)
NOT_NULL = {"o": {"j"}}


def _prove(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA.columns, constraints=prover_constraints(SCHEMA), dialect="postgres", compare_names=False)


def _rewritten(sql: str) -> tuple[str, int]:
    tree = sqlglot.parse_one(sql, read="postgres")
    made = sum(len(lateral_aggregates(s, NOT_NULL)) for s in list(tree.find_all(exp.Select)))
    return tree.sql(dialect="postgres"), made


# Calcite's plans project every outer column above the join, so the correlation stays readable.
# Calcite's three-valued readings; s.e is TRUE for an empty subquery (no group, or a zero count).
_ANY = "CASE WHEN {e} THEN FALSE WHEN (s.x {op} s.m) IS TRUE THEN TRUE WHEN s.c > s.ck THEN NULL ELSE (s.x {op} s.m) END"
_ALL = "CASE WHEN {e} THEN TRUE WHEN (s.x {op} s.m) IS FALSE THEN FALSE WHEN s.c > s.ck THEN NULL ELSE (s.x {op} s.m) END"
_GROUPS = "SELECT i.k, {agg}(i.y) AS m, COUNT(*) AS c, COUNT(i.y) AS ck FROM i {where}GROUP BY i.k"


def _grouped(test: str, op: str, agg: str, where: str, side: str, key: str, form: str) -> tuple[str, str, bool]:
    """``o JOIN (groups of i by k) ON o.<key> = g.k``, as QED's Calcite plans write it."""

    quantified = {"any": "ANY", "all": "ALL"}[test]
    filter_ = f" AND {where}" if where else ""
    groups = _GROUPS.format(agg=agg, where=f"WHERE {where} " if where else "")
    g = f"(SELECT p.m, p.c, p.ck, TRUE AS i, p.k FROM ({groups}) AS p) AS g"
    reading = (_ANY if test == "any" else _ALL).format(e="s.i IS NULL OR s.c = 0", op=op)
    sub = f"SELECT i.y FROM i WHERE i.k = o.{key}{filter_}"
    if side == "LEFT":
        original = f"SELECT o.x, (o.x {op} {quantified} ({sub})) AS v FROM o"
        expansion = f"SELECT s.x, ({reading}) AS v FROM (SELECT o.x, o.{key}, g.m, g.c, g.ck, g.i FROM o LEFT JOIN {g} ON o.{key} = g.k) AS s"
    else:
        original = f"SELECT o.x FROM o WHERE o.x {op} {quantified} ({sub})"
        condition = reading.replace("s.x", "o.x").replace("s.", "g.")
        expansion = f"SELECT o.x FROM o INNER JOIN {g} ON o.{key} = g.k AND ({condition})"
    faithful = test == "any" and agg == {">": "MIN", "<": "MAX"}[op] and form == "plain"
    return original, expansion, faithful


def _domain(test: str, op: str, agg: str, where: str, side: str, domain: str) -> tuple[str, str, bool]:
    """The top-down decorrelator's domain join, as the mined Calcite plans write it."""

    quantified = {"any": "ANY", "all": "ALL"}[test]
    filter_ = f" AND {where}" if where else ""
    groups = _GROUPS.format(agg=agg, where=f"WHERE {where} " if where else "").replace("COUNT(i.y) AS ck", "COUNT(i.y) AS ck, TRUE AS i")
    domains = {
        "o": "SELECT o2.j FROM o AS o2 GROUP BY o2.j",
        "distinct": "SELECT DISTINCT o2.j FROM o AS o2",
        "filtered": "SELECT o2.j FROM o AS o2 WHERE o2.x > 1 GROUP BY o2.j",
        "other": "SELECT i2.k AS j FROM i AS i2 GROUP BY i2.k",
    }
    joined = f"SELECT d.j AS dj, p.m, p.c, p.ck, p.i FROM ({domains[domain]}) AS d LEFT JOIN ({groups}) AS p ON d.j IS NOT DISTINCT FROM p.k"
    g = f"(SELECT t.m, CASE WHEN t.c IS NOT NULL THEN t.c ELSE 0 END AS c, CASE WHEN t.ck IS NOT NULL THEN t.ck ELSE 0 END AS ck, t.i, t.dj FROM ({joined}) AS t) AS g"
    reading = (_ANY if test == "any" else _ALL).format(e="s.i IS NULL OR s.c = 0", op=op)
    sub = f"SELECT i.y FROM i WHERE i.k = o.j{filter_}"
    if side == "LEFT":
        original = f"SELECT o.x, (o.x {op} {quantified} ({sub})) AS v FROM o"
        expansion = f"SELECT s.x, ({reading}) AS v FROM (SELECT o.x, o.j, g.m, g.c, g.ck, g.i FROM o LEFT JOIN {g} ON o.j = g.dj) AS s"
    else:
        original = f"SELECT o.x FROM o WHERE o.x {op} {quantified} ({sub})"
        condition = reading.replace("s.x", "o.x").replace("s.", "g.")
        expansion = f"SELECT o.x FROM o INNER JOIN {g} ON o.j = g.dj AND ({condition})"
    bound = {">": "MIN", "<": "MAX"}[op] if test == "any" else {">": "MAX", "<": "MIN"}[op]
    faithful = agg == bound and domain in ("o", "distinct")
    return original, expansion, faithful


def _cases():
    cases = []
    for op, agg, where, side in itertools.product([">", "<"], ["MIN", "MAX"], ["", "i.y > 1"], ["LEFT", ""]):
        for key in ("k", "j"):
            cases.append(_grouped("any", op, agg, where, side, key, "plain"))
        for domain in ("o", "distinct", "filtered", "other"):
            cases.append(_domain("any", op, agg, where, side, domain))
        cases.append(_domain("all", op, agg, where, side, "o"))
    mutated = []
    for original, expansion, faithful in cases:
        if not faithful:
            continue
        for a, b in [("THEN FALSE WHEN", "THEN NULL WHEN"), ("s.c > s.ck", "s.c >= s.ck"), ("g.c > g.ck", "g.c >= g.ck"), ("OR s.c = 0", "OR s.c = 1"), ("OR g.c = 0", "OR g.c = 1"), ("LEFT JOIN (SELECT p.m", "INNER JOIN (SELECT p.m"), ("ON o.j = g.dj", "ON o.k = g.dj"), ("IS TRUE THEN TRUE", "IS NOT FALSE THEN TRUE")]:
            if a in expansion:
                mutated.append((original, expansion.replace(a, b, 1), False))
    return cases + mutated


_CASES = _cases()


@pytest.mark.parametrize("block", range(4))
def test_decorrelated_aggregates_fold_only_when_they_mean_the_quantified_test(block):
    """Faithful plans are proved; every proof and every rewrite agrees with DuckDB on random databases."""

    for index in range(block, len(_CASES), 4):
        original, expansion, faithful = _CASES[index]
        rewritten, made = _rewritten(expansion)
        if made:
            assert find_difference(SCHEMA, expansion, rewritten, trials=40) is None, (expansion, rewritten)
        proof = _prove(original, expansion)
        if faithful:
            assert proof.proven, (original, expansion, proof.reason)
        if proof.proven:
            assert find_difference(SCHEMA, original, expansion, trials=40) is None, (original, expansion)


def test_a_domain_that_may_miss_the_outer_key_is_not_rewritten():
    for domain in ("filtered", "other"):
        _original, expansion, _faithful = _domain("any", ">", "MIN", "", "LEFT", domain)
        assert _rewritten(expansion)[1] == 0
    # o.k may be NULL: no domain row matches it, so the join to the domain is not one row per outer row.
    _original, expansion, _faithful = _domain("any", ">", "MIN", "", "LEFT", "o")
    assert _rewritten(expansion.replace("ON o.j = g.dj", "ON o.k = g.dj"))[1] == 0


# Pairs from outside research on correlated ANY/IN (projected booleans keep UNKNOWN), each
# checked on DuckDB here: equivalent pairs show no difference, the others show one.
_RESEARCH = [
    ("SELECT o.x, o.x IN (SELECT i.y FROM i) AS v FROM o", "SELECT o.x, o.x = ANY (SELECT i.y FROM i) AS v FROM o", True),
    ("SELECT o.x, o.x IN (SELECT i.y FROM i) AS v FROM o", "SELECT o.x, EXISTS (SELECT 1 FROM i WHERE i.y = o.x) AS v FROM o", False),
    ("SELECT o.x FROM o WHERE o.x IN (SELECT i.y FROM i)", "SELECT o.x FROM o WHERE EXISTS (SELECT 1 FROM i WHERE i.y = o.x)", True),
    ("SELECT o.x FROM o WHERE o.x NOT IN (SELECT i.y FROM i)", "SELECT o.x FROM o WHERE NOT EXISTS (SELECT 1 FROM i WHERE i.y = o.x)", False),
    (
        "SELECT o.x, o.x > ANY (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o",
        "SELECT o.x, CASE WHEN (SELECT COUNT(*) FROM i WHERE i.k = o.k) = 0 THEN FALSE WHEN (o.x > (SELECT MIN(i.y) FROM i WHERE i.k = o.k)) IS TRUE THEN TRUE"
        " WHEN (SELECT COUNT(*) FROM i WHERE i.k = o.k) > (SELECT COUNT(i.y) FROM i WHERE i.k = o.k) THEN NULL ELSE o.x > (SELECT MIN(i.y) FROM i WHERE i.k = o.k) END AS v FROM o",
        True,
    ),
    ("SELECT o.x, o.x > ANY (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o", "SELECT o.x, o.x > (SELECT MAX(i.y) FROM i WHERE i.k = o.k) AS v FROM o", False),
    ("SELECT o.x, o.x > ANY (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o", "SELECT o.x, o.x > (SELECT MIN(i.y) FROM i WHERE i.k = o.k) AS v FROM o", False),
    (
        "SELECT o.x, o.x > ALL (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o",
        "SELECT o.x, CASE WHEN (SELECT COUNT(*) FROM i WHERE i.k = o.k) = 0 THEN TRUE WHEN (o.x > (SELECT MAX(i.y) FROM i WHERE i.k = o.k)) IS FALSE THEN FALSE"
        " WHEN (SELECT COUNT(*) FROM i WHERE i.k = o.k) > (SELECT COUNT(i.y) FROM i WHERE i.k = o.k) THEN NULL ELSE o.x > (SELECT MAX(i.y) FROM i WHERE i.k = o.k) END AS v FROM o",
        True,
    ),
    ("SELECT o.x, o.x > ALL (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o", "SELECT o.x, o.x > (SELECT MAX(i.y) FROM i WHERE i.k = o.k) AS v FROM o", False),
    ("SELECT o.x, o.x > ALL (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o", "SELECT o.x, o.x > (SELECT MIN(i.y) FROM i WHERE i.k = o.k) AS v FROM o", False),
    ("SELECT o.x, o.x > ANY (SELECT i.y FROM i WHERE i.k = o.k) AS v FROM o", "SELECT o.x, EXISTS (SELECT 1 FROM i WHERE i.k = o.k AND o.x > i.y) AS v FROM o", False),
    (
        "SELECT o.x FROM o WHERE o.x > ANY (SELECT i.y FROM i WHERE i.k = o.k) AND o.k = 1",
        "SELECT o.x FROM o WHERE EXISTS (SELECT 1 FROM i WHERE i.k = o.k AND o.x > i.y) AND o.k = 1",
        True,
    ),
    (
        "SELECT o.x FROM o WHERE o.x IN (SELECT i.y FROM i WHERE i.k = o.k)",
        "SELECT o.x FROM o WHERE EXISTS (SELECT 1 FROM (SELECT DISTINCT o2.k FROM o AS o2) AS d JOIN i ON i.k = d.k WHERE d.k IS NOT DISTINCT FROM o.k AND i.y = o.x)",
        True,
    ),
    (
        "SELECT o.x FROM o WHERE o.x IN (SELECT i.y FROM i WHERE i.k = o.k)",
        "SELECT o.x FROM o WHERE o.x IN (SELECT i.y FROM i WHERE i.k IS NOT DISTINCT FROM o.k)",
        False,
    ),
    ("SELECT o.x, o.x IN (SELECT i.y FROM i) AS v FROM o", "SELECT o.x, NOT EXISTS (SELECT 1 FROM i WHERE i.y <> o.x) AS v FROM o", False),
]


@pytest.mark.parametrize("left, right, equivalent", _RESEARCH)
def test_research_pairs(left, right, equivalent):
    witness = find_difference(SCHEMA, left, right, trials=200)
    assert (witness is None) == equivalent, witness
    if not equivalent:
        assert not _prove(left, right).proven


_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "correlated_quantified" / "pairs.json").read_text())
_PLAIN = Schema([Table("o", [Column("x"), Column("k")]), Table("i", [Column("y"), Column("k")])])
_PROVED = {"R006-026", "R006-027"}


@pytest.mark.parametrize("pair", _FIXTURE["pairs"], ids=lambda p: p["id"])
def test_hand_written_pairs(pair):
    """Each label holds on DuckDB; a non-equivalent pair is never proved, and the proofs we have stay."""

    witness = find_difference(_PLAIN, pair["left"], pair["right"], trials=300)
    assert (witness is None) == pair["equivalent"], witness
    proof = prove_equivalent_algebraic(pair["left"], pair["right"], schema=_PLAIN.columns, constraints=prover_constraints(_PLAIN), dialect="postgres", compare_names=False)
    if not pair["equivalent"]:
        assert not proof.proven
    if pair["id"] in _PROVED:
        assert proof.proven, proof.reason
