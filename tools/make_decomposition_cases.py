"""Write the aggregate-decomposition suite (tests/fixtures/decomposition/cases.json).

    python tools/make_decomposition_cases.py            # regenerate and re-validate
    python tools/make_decomposition_cases.py --check

Purpose-built cases, not a public benchmark. A case gives a *summary* ``S`` (a finer-grained aggregate that already
exists) and a *target* ``Q`` (a coarser aggregate). Task: answer ``Q`` from ``S`` alone, or say it cannot be done.

* ``expect: "rewrite"``: ``Q`` can be rebuilt from ``S`` (``SUM`` of sums, ``AVG`` as a ratio of ``SUM`` and
  non-NULL ``COUNT``, ...). The replacement is proven by the prover and re-run on random databases.
* ``expect: "none"``: no function of ``S`` gives ``Q``. This is *checked*: the generator finds two databases
  with identical ``S`` and different ``Q`` and stores them in the case (``witness``), so the label is replayable.
* ``traps``: tempting replacements that are wrong (averaging averages, ``SUM`` of distinct counts, a global
  ``COUNT`` that returns NULL on empty input). Each must be refuted by a database; the generator verifies that
  too and stores the refuting database.

Families are held out whole for the final evaluation: ``distinct`` and ``empty-null``.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql.random_check import Column, Schema, Table, _connect, _domains, _load, _norm, _duck, find_difference, random_tables  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "decomposition" / "cases.json"
HELD_OUT_FAMILIES = ("distinct", "empty-null")

SALES = Schema(
    [
        Table(
            "sales",
            [
                Column("id", "int", True),
                Column("region", "int", True),
                Column("product", "int", True),
                Column("customer", "int"),
                Column("day", "int", True),
                Column("amount", "int", True),
                Column("qty", "int", True),
                Column("discount", "int"),
            ],
            keys=[("id",)],
        )
    ]
)

FINE = "SELECT region, product, day, {aggs} FROM sales GROUP BY region, product, day"


def case(case_id, family, summary_aggs, query, expect, note, traps=(), summary=None):
    return {
        "id": case_id,
        "family": family,
        "summary": summary or FINE.format(aggs=summary_aggs),
        "query": query,
        "expect": expect,
        "note": note,
        "traps": [{"sql": t[0], "why": t[1]} for t in traps],
    }


def build() -> list[dict]:
    c = []
    add = c.append
    # --- basic rollups ---------------------------------------------------------------------------------------------
    add(case("basic.sum", "basic", "SUM(amount) AS s, COUNT(*) AS n", "SELECT region, SUM(amount) FROM sales GROUP BY region", "rewrite", "SUM of sums"))
    add(case("basic.count-star", "basic", "COUNT(*) AS n", "SELECT region, COUNT(*) FROM sales GROUP BY region", "rewrite", "COUNT(*) is the SUM of counts", traps=[("SELECT region, COUNT(n) FROM mv0 GROUP BY region", "counts the summary rows, not the sales rows")]))
    add(case("basic.count-column-nullable", "basic", "COUNT(discount) AS n", "SELECT region, COUNT(discount) FROM sales GROUP BY region", "rewrite", "COUNT of a nullable column is the SUM of non-NULL counts"))
    add(case("basic.min-max", "basic", "MIN(amount) AS lo, MAX(amount) AS hi", "SELECT region, MIN(amount), MAX(amount) FROM sales GROUP BY region", "rewrite", "MIN of mins and MAX of maxes"))
    add(case("basic.several", "basic", "SUM(amount) AS s, COUNT(*) AS n, MAX(qty) AS m", "SELECT region, SUM(amount), COUNT(*), MAX(qty) FROM sales GROUP BY region", "rewrite", "three aggregates at once"))
    add(case("basic.same-grain", "basic", "SUM(amount) AS s", "SELECT region, product, day, SUM(amount) FROM sales GROUP BY region, product, day", "rewrite", "same grain: read the summary"))
    add(case("basic.fewer-columns", "basic", "SUM(amount) AS s, COUNT(*) AS n", "SELECT product, SUM(amount) AS total FROM sales GROUP BY product", "rewrite", "group by a different single grain column"))
    add(case("basic.filter-on-grain", "basic", "SUM(amount) AS s", "SELECT region, SUM(amount) FROM sales WHERE region = 1 GROUP BY region", "rewrite", "a filter on a grouping column keeps whole groups"))
    add(case("basic.filter-on-measure", "basic", "SUM(amount) AS s", "SELECT region, SUM(amount) FROM sales WHERE amount > 10 GROUP BY region", "none", "rows are filtered before summing; the summary has already added them up", traps=[("SELECT region, SUM(s) FROM mv0 WHERE s > 10 GROUP BY region", "filters the group totals, not the rows")]))
    add(case("basic.expression-of-grain", "basic", "SUM(amount) AS s", "SELECT day % 2, SUM(amount) FROM sales GROUP BY day % 2", "rewrite", "group by an expression of a grouping column"))
    add(case("basic.having", "basic", "SUM(amount) AS s", "SELECT region, SUM(amount) FROM sales GROUP BY region HAVING SUM(amount) > 100", "rewrite", "HAVING over the rolled-up aggregate"))
    add(case("basic.filter-grain-having", "basic", "SUM(amount) AS s, COUNT(*) AS n", "SELECT region, COUNT(*) FROM sales WHERE product = 2 GROUP BY region HAVING COUNT(*) > 1", "rewrite", "filter on a grouping column plus HAVING"))
    add(case("basic.summary-having", "basic", "SUM(amount) AS s", "SELECT region, SUM(amount) FROM sales GROUP BY region", "none", "the summary dropped small groups, so totals computed from it lose their rows", summary="SELECT region, product, day, SUM(amount) AS s FROM sales GROUP BY region, product, day HAVING SUM(amount) > 5", traps=[("SELECT region, SUM(s) FROM mv0 GROUP BY region", "the summary lacks the groups its HAVING removed")]))
    add(case("basic.missing-measure", "basic", "SUM(amount) AS s", "SELECT region, SUM(qty) FROM sales GROUP BY region", "none", "the summary never stored qty"))
    add(case("basic.finer-than-summary", "basic", "SUM(amount) AS s", "SELECT region, customer, SUM(amount) FROM sales GROUP BY region, customer", "none", "customer is finer than the summary's grain", traps=[("SELECT region, SUM(s) FROM mv0 GROUP BY region", "drops the customer column's detail")]))
    add(case("basic.sum-product", "basic", "SUM(amount * qty) AS revenue", "SELECT region, SUM(amount * qty) FROM sales GROUP BY region", "rewrite", "SUM of an expression stored as a unit"))
    add(case("basic.ratio-of-sums", "basic", "SUM(amount) AS s, SUM(qty) AS q", "SELECT region, SUM(amount) / SUM(qty) FROM sales GROUP BY region", "rewrite", "ratio of two rolled-up sums"))
    # --- averages ---------------------------------------------------------------------------------------------------
    add(case("avg.sum-over-count", "average", "SUM(amount) AS s, COUNT(amount) AS n", "SELECT region, AVG(amount) FROM sales GROUP BY region", "rewrite", "AVG is SUM over non-NULL COUNT"))
    add(case("avg.nullable-column", "average", "SUM(discount) AS s, COUNT(discount) AS n", "SELECT region, AVG(discount) FROM sales GROUP BY region", "rewrite", "NULLs are ignored by SUM, COUNT(col) and AVG alike"))
    add(case("avg.count-star-on-nullable", "average", "SUM(discount) AS s, COUNT(*) AS n", "SELECT region, AVG(discount) FROM sales GROUP BY region", "none", "COUNT(*) counts rows whose discount is NULL, so the ratio is too small", traps=[("SELECT region, SUM(s) / SUM(n) FROM mv0 GROUP BY region", "divides by all rows, not the non-NULL ones")]))
    add(case("avg.avg-of-avgs", "average", "AVG(amount) AS a", "SELECT region, AVG(amount) FROM sales GROUP BY region", "none", "groups differ in size, so the average of averages is not the average", traps=[("SELECT region, AVG(a) FROM mv0 GROUP BY region", "averaging averages ignores unequal group sizes")]))
    add(case("avg.global", "average", "SUM(amount) AS s, COUNT(amount) AS n", "SELECT AVG(amount) FROM sales", "rewrite", "global average from per-group sums and counts"))
    add(case("avg.weighted", "average", "AVG(amount) AS a, COUNT(amount) AS n", "SELECT region, AVG(amount) FROM sales GROUP BY region", "rewrite", "average weighted by the counts: SUM(a * n) / SUM(n) (exact arithmetic; a stretch for the prover)"))
    add(case("avg.avg-with-count-same-grain", "average", "AVG(amount) AS a", "SELECT region, product, day, AVG(amount) FROM sales GROUP BY region, product, day", "rewrite", "same grain: read the stored average"))
    add(case("avg.avg-qty-and-sum", "average", "SUM(amount) AS s, COUNT(amount) AS n, SUM(qty) AS q, COUNT(qty) AS m", "SELECT region, AVG(amount), AVG(qty) FROM sales GROUP BY region", "rewrite", "two averages at once"))
    # --- variance (stretch: the prover has no variance) --------------------------------------------------------------
    add(case("variance.from-sums", "variance", "SUM(amount) AS s, SUM(amount * amount) AS s2, COUNT(amount) AS n", "SELECT region, SUM(amount * amount) - SUM(amount) * SUM(amount) / COUNT(amount) FROM sales GROUP BY region", "rewrite", "the textbook sum of squares from rolled-up sums"))
    add(case("variance.stddev-from-summary", "variance", "SUM(amount) AS s, SUM(amount * amount) AS s2, COUNT(amount) AS n", "SELECT region, STDDEV_POP(amount) FROM sales GROUP BY region", "rewrite", "population standard deviation from sum, sum of squares and count (stretch: needs STDDEV and SQRT reasoning)"))
    add(case("variance.stddev-of-stddevs", "variance", "STDDEV_POP(amount) AS d", "SELECT region, STDDEV_POP(amount) FROM sales GROUP BY region", "none", "standard deviations alone do not combine", traps=[("SELECT region, STDDEV_POP(d) FROM mv0 GROUP BY region", "the spread of spreads is not the spread")]))
    # --- empty input and NULL ----------------------------------------------------------------------------------------
    add(case("empty-null.global-count", "empty-null", "COUNT(*) AS n", "SELECT COUNT(*) FROM sales", "rewrite", "an empty table has COUNT 0, an empty summary has no rows to add up", traps=[("SELECT SUM(n) FROM mv0", "NULL on empty input where the query returns 0")]))
    add(case("empty-null.global-sum", "empty-null", "SUM(amount) AS s", "SELECT SUM(amount) FROM sales", "rewrite", "SUM of an empty set is NULL on both sides", traps=[("SELECT COALESCE(SUM(s), 0) FROM mv0", "0 where the query returns NULL on empty input")]))
    add(case("empty-null.global-count-column", "empty-null", "COUNT(discount) AS n", "SELECT COUNT(discount) FROM sales", "rewrite", "COUNT of a column is 0 on empty input", traps=[("SELECT SUM(n) FROM mv0", "NULL where the query returns 0")]))
    add(case("empty-null.global-min", "empty-null", "MIN(amount) AS lo", "SELECT MIN(amount) FROM sales", "rewrite", "MIN of an empty set is NULL"))
    add(case("empty-null.all-null-group-sum", "empty-null", "SUM(discount) AS s", "SELECT region, SUM(discount) FROM sales GROUP BY region", "rewrite", "a group whose discounts are all NULL sums to NULL, and so does the sum of those NULLs", traps=[("SELECT region, COALESCE(SUM(s), 0) FROM mv0 GROUP BY region", "0 where the query returns NULL for an all-NULL group")]))
    add(case("empty-null.sum-of-counts-as-sum", "empty-null", "SUM(amount) AS s, COUNT(*) AS n", "SELECT region, SUM(amount) FROM sales GROUP BY region", "rewrite", "unused counts are ignored", traps=[("SELECT region, SUM(n) FROM mv0 GROUP BY region", "adds up the wrong column")]))
    add(case("empty-null.grouped-count-no-coalesce", "empty-null", "COUNT(*) AS n", "SELECT region, COUNT(*) FROM sales GROUP BY region", "rewrite", "with GROUP BY an empty input gives no rows on both sides"))
    add(case("empty-null.filter-everything", "empty-null", "COUNT(*) AS n", "SELECT COUNT(*) FROM sales WHERE region = 99", "rewrite", "a global count over a filter on a grouping column; zero rows still counts 0", traps=[("SELECT SUM(n) FROM mv0 WHERE region = 99", "NULL where the query returns 0")]))
    # --- distinct counts ----------------------------------------------------------------------------------------------
    cust = "SELECT region, customer, SUM(amount) AS s FROM sales GROUP BY region, customer"
    add(case("distinct.count-distinct-from-grain", "distinct", "", "SELECT region, COUNT(DISTINCT customer) FROM sales GROUP BY region", "rewrite", "customer is in the summary's grain, so its distinct values can be counted there", summary=cust))
    add(case("distinct.count-distinct-stored-per-product", "distinct", "COUNT(DISTINCT customer) AS d", "SELECT region, COUNT(DISTINCT customer) FROM sales GROUP BY region", "none", "a customer can buy several products, so per-product distinct counts overlap", traps=[("SELECT region, SUM(d) FROM mv0 GROUP BY region", "counts a customer once per product")]))
    add(case("distinct.same-grain", "distinct", "COUNT(DISTINCT customer) AS d", "SELECT region, product, day, COUNT(DISTINCT customer) FROM sales GROUP BY region, product, day", "rewrite", "same grain: read the stored distinct count"))
    add(case("distinct.sum-distinct-from-grain", "distinct", "", "SELECT region, SUM(DISTINCT amount) FROM sales GROUP BY region", "rewrite", "amount is in the summary's grain", summary="SELECT region, amount, COUNT(*) AS n FROM sales GROUP BY region, amount"))
    add(case("distinct.count-distinct-region", "distinct", "SUM(amount) AS s", "SELECT COUNT(DISTINCT region) FROM sales", "rewrite", "region is in the grain; a global distinct count"))
    add(case("distinct.count-distinct-product", "distinct", "SUM(amount) AS s", "SELECT region, COUNT(DISTINCT product) FROM sales GROUP BY region", "rewrite", "product is in the grain"))
    add(case("distinct.distinct-of-sum", "distinct", "SUM(amount) AS s", "SELECT region, SUM(DISTINCT amount) FROM sales GROUP BY region", "none", "amount is only stored as per-group totals", traps=[("SELECT region, SUM(DISTINCT s) FROM mv0 GROUP BY region", "distinct totals are not distinct amounts")]))
    add(case("distinct.count-distinct-nullable", "distinct", "", "SELECT region, COUNT(DISTINCT customer) FROM sales GROUP BY region", "rewrite", "customer may be NULL; COUNT(DISTINCT) skips NULLs on both sides", summary="SELECT region, customer, COUNT(*) AS n FROM sales GROUP BY region, customer"))
    return c


def _signature(db, sql: str):
    rows = [tuple(_norm(v) for v in row) for row in db.execute(sql).fetchall()]
    return tuple(sorted(rows, key=lambda r: tuple((v is None, repr(v)) for v in r)))


def find_non_decomposable(summary: str, query: str, trials: int = 20000):
    """Two databases with the same summary and different target results, as ``(a, b)``, or None."""

    domains = _domains([summary, query])
    ints = domains["int"]
    rng = random.Random(5)
    db = _connect(SALES)
    s_sql, q_sql = _duck(summary, "postgres"), _duck(query, "postgres")
    seen: dict = {}
    for seed in range(1, trials + 1):
        # a tiny domain per attempt, so equal summaries over different rows actually occur
        small = {**domains, "int": rng.sample(ints, min(len(ints), rng.choice([2, 3, 4])))}
        tables = random_tables(SALES, seed, small, rows=rng.choice([2, 3, 4]))
        _load(db, SALES, tables)
        s, q = _signature(db, s_sql), _signature(db, q_sql)
        if s in seen and seen[s][0] != q:
            return seen[s][1], tables
        seen.setdefault(s, (q, tables))
    return None


def _pack(tables: dict) -> dict:
    return {name: [list(row) for row in rows] for name, rows in tables.items()}


def validate_and_annotate(cases: list[dict]) -> list[str]:
    from kumosql.model_reuse import check_replacement
    from kumosql.random_check import prover_constraints

    problems = []
    for item in cases:
        if item["expect"] == "none":
            found = find_non_decomposable(item["summary"], item["query"])
            if found is None:
                problems.append(f"{item['id']}: labelled none but no two databases with equal summaries and different targets were found")
            else:
                item["witness"] = {"a": _pack(found[0]), "b": _pack(found[1])}
        for trap in item["traps"]:
            check = check_replacement(
                item["query"], item["summary"], trap["sql"], schema=SALES.columns, constraints=prover_constraints(SALES), database=SALES, trials=400
            )
            if check.status != "refuted":
                problems.append(f"{item['id']}: trap {trap['sql']!r} is {check.status}, expected refuted")
            else:
                w = check.witness
                trap["witness"] = _pack(w.tables)
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cases = build()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate ids"
    problems = validate_and_annotate(cases)
    for p in problems:
        print("LABEL PROBLEM", p)
    if problems:
        return 1
    if "--check" not in argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"held_out_families": list(HELD_OUT_FAMILIES), "schema": "sales", "cases": cases}, indent=1) + "\n", encoding="utf-8")
        print(f"{len(cases)} cases written to {OUT}")
    else:
        print(f"{len(cases)} cases, labels consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
