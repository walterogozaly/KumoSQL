"""Write the query-containment suite (tests/fixtures/containment/cases.json).

    python tools/make_containment_cases.py            # regenerate and re-validate the labels
    python tools/make_containment_cases.py --check    # validate only

These are purpose-built cases, not a public benchmark. Each case is a pair ``q1``, ``q2`` and two labels,
one per semantics: is every result of ``q1`` also a result of ``q2`` as a set of rows (``set``), and with
at least the same multiplicity (``bag``)?

Labels come from reasoning, then are *checked*, never trusted:

* ``contained`` must survive 400 random databases that respect the schema;
* ``not_contained`` must be witnessed by one of them (so the label is also a checked fact).

Single-column predicate families (``filters``, ``nulls``) are labelled by evaluating both predicates in
three-valued logic over a domain that includes NULL, so the label is the definition applied to every value.

Families are held out whole for the final evaluation: ``joins`` and ``aggregates`` are reserved.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql.random_check import Column, Schema, Table, run_all  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "containment" / "cases.json"
HELD_OUT_FAMILIES = ("joins", "aggregates")

SHOP = Schema(
    [
        Table(
            "orders",
            [Column("id", "int", True), Column("customer_id", "int", True), Column("amount", "int"), Column("status", "text"), Column("region", "int")],
            keys=[("id",)],
        ),
        Table("customers", [Column("id", "int", True), Column("region", "int"), Column("tier", "int")], keys=[("id",)]),
        Table("events", [Column("customer_id", "int"), Column("kind", "text"), Column("amount", "int")]),
    ]
)

# three-valued logic: True, False or None (unknown)
def _not(v):
    return None if v is None else (not v)


def _and(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _or(a, b):
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def _cmp(op):
    def go(a, c):
        if a is None:
            return None
        return {">": a > c, ">=": a >= c, "<": a < c, "<=": a <= c, "=": a == c, "<>": a != c}[op]

    return go


# predicate name -> (sql, function of the value of amount)
FILTERS = {
    "gt10": ("amount > 10", lambda a: _cmp(">")(a, 10)),
    "gt5": ("amount > 5", lambda a: _cmp(">")(a, 5)),
    "ge10": ("amount >= 10", lambda a: _cmp(">=")(a, 10)),
    "lt5": ("amount < 5", lambda a: _cmp("<")(a, 5)),
    "le5": ("amount <= 5", lambda a: _cmp("<=")(a, 5)),
    "eq10": ("amount = 10", lambda a: _cmp("=")(a, 10)),
    "ne10": ("amount <> 10", lambda a: _cmp("<>")(a, 10)),
    "between": ("amount BETWEEN 5 AND 10", lambda a: _and(_cmp(">=")(a, 5), _cmp("<=")(a, 10))),
    "in": ("amount IN (5, 10)", lambda a: _or(_cmp("=")(a, 5), _cmp("=")(a, 10))),
    "range": ("amount > 5 AND amount < 20", lambda a: _and(_cmp(">")(a, 5), _cmp("<")(a, 20))),
    "outside": ("amount < 5 OR amount > 10", lambda a: _or(_cmp("<")(a, 5), _cmp(">")(a, 10))),
    "not_le10": ("NOT (amount <= 10)", lambda a: _not(_cmp("<=")(a, 10))),
}

NULLS = {
    "is_null": ("amount IS NULL", lambda a: a is None),
    "not_null": ("amount IS NOT NULL", lambda a: a is not None),
    "gt10_or_null": ("amount > 10 OR amount IS NULL", lambda a: _or(_cmp(">")(a, 10), a is None)),
    "split": ("amount <= 10 OR amount > 10", lambda a: _or(_cmp("<=")(a, 10), _cmp(">")(a, 10))),
    "le10": ("amount <= 10", lambda a: _cmp("<=")(a, 10)),
    "not_gt10": ("NOT (amount > 10)", lambda a: _not(_cmp(">")(a, 10))),
    "coalesce": ("COALESCE(amount, 0) <= 10", lambda a: (0 if a is None else a) <= 10),
    "self_eq": ("amount = amount", lambda a: _cmp("=")(a, a) if a is not None else None),
    "true": ("1 = 1", lambda a: True),
    "not_in": ("amount NOT IN (1, 2)", lambda a: None if a is None else (a not in (1, 2))),
    "not_in_null": ("amount NOT IN (1, 2, NULL)", lambda a: False if a in (1, 2) else None),
    "is_null_gt10": ("(amount > 10) IS NULL", lambda a: a is None),
}

DOMAIN = [None, *range(-3, 26)]


def predicate_label(f, g) -> str:
    """contained iff every value of amount where f is true also makes g true."""

    return "contained" if all(g(v) is True for v in DOMAIN if f(v) is True) else "not_contained"


def _select(where: str) -> str:
    return f"SELECT id, amount FROM orders WHERE {where}"


def predicate_cases(prefix: str, family: str, pool: dict, limit: int | None = None) -> list[dict]:
    cases = []
    for (a, (sa, fa)), (b, (sb, fb)) in itertools.permutations(pool.items(), 2):
        label = predicate_label(fa, fb)
        cases.append(
            {
                "id": f"{prefix}.{a}-in-{b}",
                "family": family,
                "q1": _select(sa),
                "q2": _select(sb),
                "set": label,
                "bag": label,
                "note": "same table and columns; label by three-valued evaluation over every value of amount including NULL",
            }
        )
    return cases[:limit] if limit else cases


def hand(case_id, family, q1, q2, set_label, bag_label, note) -> dict:
    return {"id": case_id, "family": family, "q1": q1, "q2": q2, "set": set_label, "bag": bag_label, "note": note}


C, N = "contained", "not_contained"


def handwritten() -> list[dict]:
    cases = []
    add = lambda *a: cases.append(hand(*a))  # noqa: E731
    # --- set versus bag: projection and DISTINCT
    add("dup.plain-in-distinct", "duplicates", "SELECT region FROM orders", "SELECT DISTINCT region FROM orders", C, N, "set-equal; the plain query repeats rows the DISTINCT one collapses")
    add("dup.distinct-in-plain", "duplicates", "SELECT DISTINCT region FROM orders", "SELECT region FROM orders", C, C, "DISTINCT only removes duplicates")
    add("dup.distinct-filter-in-plain", "duplicates", "SELECT DISTINCT region FROM orders WHERE amount > 10", "SELECT region FROM orders", C, C, "a filtered DISTINCT is still within the plain rows")
    add("dup.filter-in-distinct", "duplicates", "SELECT region FROM orders WHERE amount > 10", "SELECT DISTINCT region FROM orders", C, N, "set-contained; bag-wise the filtered rows can repeat")
    add("dup.distinct-wider-in-narrower", "duplicates", "SELECT DISTINCT region FROM orders", "SELECT DISTINCT region FROM orders WHERE amount > 10", N, N, "the unfiltered regions are not all in the filtered ones")
    add("dup.same-distinct", "duplicates", "SELECT DISTINCT region FROM orders WHERE amount > 10", "SELECT DISTINCT region FROM orders WHERE amount > 5", C, C, "both DISTINCT, stronger filter on the left")
    add("dup.status-in-all", "duplicates", "SELECT DISTINCT status FROM orders WHERE region = 1", "SELECT DISTINCT status FROM orders", C, C, "both DISTINCT")
    add("dup.status-all-in-region", "duplicates", "SELECT DISTINCT status FROM orders", "SELECT DISTINCT status FROM orders WHERE region = 1", N, N, "a status can occur only outside region 1")
    add("dup.key-distinct-noop", "duplicates", "SELECT id FROM orders", "SELECT DISTINCT id FROM orders", C, C, "id is a key, so DISTINCT removes nothing")
    add("dup.key-distinct-noop-rev", "duplicates", "SELECT DISTINCT id FROM orders WHERE amount > 5", "SELECT id FROM orders", C, C, "id is a key")
    add("dup.nonkey-column-order", "duplicates", "SELECT status, region FROM orders", "SELECT region, status FROM orders", N, N, "same columns in a different order are different rows")
    # --- constants and expressions
    add("expr.computed-in-computed", "filters-expr", "SELECT id, amount + 1 FROM orders WHERE amount > 10", "SELECT id, amount + 1 FROM orders WHERE amount > 5", C, C, "same projection, weaker filter on the right")
    add("expr.computed-different", "filters-expr", "SELECT id, amount + 1 FROM orders WHERE amount > 10", "SELECT id, amount FROM orders WHERE amount > 5", N, N, "different second column")
    add("expr.constant-column", "filters-expr", "SELECT id, 1 FROM orders WHERE amount > 10", "SELECT id, 1 FROM orders", C, C, "constant column unchanged")
    add("expr.region-eq-vs-in", "filters-expr", "SELECT id, region FROM orders WHERE region = 3", "SELECT id, region FROM orders WHERE region IN (1, 3)", C, C, "equality inside an IN list")
    add("expr.region-in-vs-eq", "filters-expr", "SELECT id, region FROM orders WHERE region IN (1, 3)", "SELECT id, region FROM orders WHERE region = 3", N, N, "region 1 rows are missing from the right")
    add("expr.status-text", "filters-expr", "SELECT id FROM orders WHERE status = 'paid' AND amount > 0", "SELECT id FROM orders WHERE status = 'paid'", C, C, "extra conjunct on the left")
    add("expr.status-or", "filters-expr", "SELECT id FROM orders WHERE status = 'paid' OR amount > 0", "SELECT id FROM orders WHERE status = 'paid'", N, N, "extra disjunct widens the left")
    add("expr.two-column", "filters-expr", "SELECT id FROM orders WHERE amount > 10 AND region = 1", "SELECT id FROM orders WHERE amount > 5 OR region = 2", C, C, "left conjunct implies a right disjunct")
    add("expr.two-column-neg", "filters-expr", "SELECT id FROM orders WHERE amount > 10 OR region = 1", "SELECT id FROM orders WHERE amount > 10 AND region = 1", N, N, "disjunction is wider than conjunction")
    # --- set operations
    add("union.left-in-union-all", "setops", "SELECT id FROM orders WHERE amount > 10", "SELECT id FROM orders WHERE amount > 10 UNION ALL SELECT id FROM orders WHERE region = 1", C, C, "one branch of a UNION ALL")
    add("union.union-all-in-left", "setops", "SELECT id FROM orders WHERE amount > 10 UNION ALL SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE amount > 10", N, N, "the second branch is missing on the right")
    add("union.union-in-union-all", "setops", "SELECT id FROM orders WHERE amount > 10 UNION SELECT id FROM orders WHERE amount > 10", "SELECT id FROM orders WHERE amount > 10 UNION ALL SELECT id FROM orders WHERE amount > 10", C, C, "UNION collapses what UNION ALL doubles")
    add("union.union-all-in-union", "setops", "SELECT id FROM orders WHERE amount > 10 UNION ALL SELECT id FROM orders WHERE amount > 10", "SELECT id FROM orders WHERE amount > 10 UNION SELECT id FROM orders WHERE amount > 10", C, N, "set-equal; the UNION ALL repeats every row")
    add("union.nested-filter", "setops", "SELECT id FROM orders WHERE amount > 10 AND region = 1 UNION ALL SELECT id FROM orders WHERE region = 2", "SELECT id FROM orders WHERE amount > 5 UNION ALL SELECT id FROM orders WHERE region IN (1, 2)", C, C, "each left branch is inside a right branch, so every row appears at least as often on the right")
    add("union.except", "setops", "SELECT id FROM orders WHERE amount > 10 EXCEPT SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE amount > 10", C, C, "EXCEPT removes rows")
    add("union.intersect", "setops", "SELECT id FROM orders WHERE amount > 10 INTERSECT SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE region = 1", C, C, "INTERSECT is within each side")
    # --- joins (held out)
    add("join.inner-in-base", "joins", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", "SELECT id FROM orders", C, C, "customers.id is a key, so the join cannot duplicate an order")
    add("join.base-in-inner", "joins", "SELECT id FROM orders", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", N, N, "orders without a customer are lost by the join")
    add("join.nonkey-duplicates", "joins", "SELECT o.id FROM orders o JOIN events e ON o.customer_id = e.customer_id", "SELECT id FROM orders", C, N, "events has no key, so a matching order repeats once per event")
    add("join.nonkey-distinct", "joins", "SELECT DISTINCT o.id FROM orders o JOIN events e ON o.customer_id = e.customer_id", "SELECT id FROM orders", C, C, "DISTINCT removes the join's repeats")
    add("join.swap-order", "joins", "SELECT o.id, c.tier FROM orders o JOIN customers c ON o.customer_id = c.id", "SELECT o.id, c.tier FROM customers c JOIN orders o ON c.id = o.customer_id", C, C, "same join written the other way round")
    add("join.extra-filter", "joins", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.tier = 1", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", C, C, "filter on the joined table")
    add("join.extra-filter-rev", "joins", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.tier = 1", N, N, "the unfiltered join has other tiers")
    add("join.semi-in-join", "joins", "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE tier = 1)", "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", C, C, "customers.id is a key, so IN and JOIN return the same rows; the filter only narrows")
    add("join.left-in-inner", "joins", "SELECT o.id, c.tier FROM orders o LEFT JOIN customers c ON o.customer_id = c.id", "SELECT o.id, c.tier FROM orders o JOIN customers c ON o.customer_id = c.id", N, N, "the LEFT JOIN keeps unmatched orders with a NULL tier")
    add("join.inner-in-left", "joins", "SELECT o.id, c.tier FROM orders o JOIN customers c ON o.customer_id = c.id", "SELECT o.id, c.tier FROM orders o LEFT JOIN customers c ON o.customer_id = c.id", C, C, "an inner join is a subset of the left join")
    add("join.self-cross", "joins", "SELECT a.id FROM orders a JOIN orders b ON a.id = b.id", "SELECT id FROM orders", C, C, "self-join on a key returns each row once")
    add("join.region-join", "joins", "SELECT o.id FROM orders o JOIN customers c ON o.region = c.region", "SELECT id FROM orders", C, N, "region is not a key of customers: matches can repeat")
    # --- aggregation (held out)
    add("agg.filter-on-group", "aggregates", "SELECT region, SUM(amount) FROM orders WHERE region IN (1, 2) GROUP BY region", "SELECT region, SUM(amount) FROM orders WHERE region > 0 GROUP BY region", C, C, "the filter is on the grouping column, so whole groups are kept")
    add("agg.filter-on-measure", "aggregates", "SELECT region, SUM(amount) FROM orders WHERE amount > 10 GROUP BY region", "SELECT region, SUM(amount) FROM orders WHERE amount > 0 GROUP BY region", N, N, "different rows are summed, so the totals differ")
    add("agg.count-filter-measure", "aggregates", "SELECT region, COUNT(*) FROM orders WHERE amount > 10 GROUP BY region", "SELECT region, COUNT(*) FROM orders GROUP BY region", N, N, "the counts differ")
    add("agg.group-in-group", "aggregates", "SELECT region, MAX(amount) FROM orders WHERE region = 1 GROUP BY region", "SELECT region, MAX(amount) FROM orders GROUP BY region", C, C, "the region-1 group is a group of the unfiltered query")
    add("agg.having", "aggregates", "SELECT region, COUNT(*) FROM orders GROUP BY region HAVING COUNT(*) > 2", "SELECT region, COUNT(*) FROM orders GROUP BY region", C, C, "HAVING removes whole groups")
    add("agg.having-rev", "aggregates", "SELECT region, COUNT(*) FROM orders GROUP BY region", "SELECT region, COUNT(*) FROM orders GROUP BY region HAVING COUNT(*) > 2", N, N, "small groups are missing on the right")
    add("agg.having-stronger", "aggregates", "SELECT region, COUNT(*) FROM orders GROUP BY region HAVING COUNT(*) > 5", "SELECT region, COUNT(*) FROM orders GROUP BY region HAVING COUNT(*) > 2", C, C, "a stronger HAVING")
    add("agg.global-count", "aggregates", "SELECT COUNT(*) FROM orders WHERE amount > 10", "SELECT COUNT(*) FROM orders", N, N, "a global aggregate always returns one row; the counts differ")
    add("agg.global-sum-same", "aggregates", "SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders", C, C, "identical")
    add("agg.sum-vs-count", "aggregates", "SELECT region, SUM(amount) FROM orders GROUP BY region", "SELECT region, COUNT(amount) FROM orders GROUP BY region", N, N, "different measure")
    add("agg.distinct-count", "aggregates", "SELECT region, COUNT(DISTINCT customer_id) FROM orders WHERE region = 1 GROUP BY region", "SELECT region, COUNT(DISTINCT customer_id) FROM orders GROUP BY region", C, C, "whole-group filter on the grouping column")
    add("agg.group-superset", "aggregates", "SELECT region, status, COUNT(*) FROM orders GROUP BY region, status", "SELECT region, COUNT(*) FROM orders GROUP BY region", N, N, "different columns")
    add("agg.group-key-distinct", "aggregates", "SELECT DISTINCT region FROM orders", "SELECT region FROM orders GROUP BY region", C, C, "DISTINCT and GROUP BY give the same rows")
    add("agg.group-key-distinct-rev", "aggregates", "SELECT region FROM orders GROUP BY region", "SELECT DISTINCT region FROM orders", C, C, "DISTINCT and GROUP BY give the same rows")
    add("agg.max-weaker-filter", "aggregates", "SELECT region, MAX(amount) FROM orders WHERE amount > 10 GROUP BY region", "SELECT region, MAX(amount) FROM orders WHERE amount > 5 GROUP BY region", C, C, "not contained: the max over amount > 5 can be larger than the max over amount > 10 only if no value >10 exists, in which case the left group is absent; if present the maxima agree")
    return [c for c in cases if c]


def build() -> list[dict]:
    cases = predicate_cases("filters", "filters", FILTERS) + predicate_cases("nulls", "nulls", NULLS) + handwritten()
    return cases


def validate(cases: list[dict], trials: int = 150) -> list[str]:
    problems = []
    for case in cases:
        found = run_all(SHOP, [case["q1"], case["q2"]], range(trials), modes=("subset", "subbag"))
        for semantics, mode in (("set", "subset"), ("bag", "subbag")):
            witness = found[mode]
            if case[semantics] == "contained" and witness is not None:
                problems.append(f"{case['id']} [{semantics}]: labelled contained but database {witness.seed} separates the queries")
            if case[semantics] == "not_contained" and witness is None:
                problems.append(f"{case['id']} [{semantics}]: labelled not contained but no database in {trials} separates the queries")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cases = build()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    problems = validate(cases)
    for line in problems:
        print("LABEL PROBLEM", line)
    if problems:
        return 1
    if "--check" not in argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"held_out_families": list(HELD_OUT_FAMILIES), "schema": "shop", "cases": cases}, indent=1) + "\n", encoding="utf-8")
        print(f"{len(cases)} cases written to {OUT}")
    else:
        print(f"{len(cases)} cases, labels consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
