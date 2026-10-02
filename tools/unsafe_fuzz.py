"""Seeded metamorphic fuzzing for KumoSQL's rewrites and equivalence prover.

Three suites share one DuckDB oracle and one case/report format:

``fuzz``
    SQLancer-style related queries: Ternary Logic Partitioning (TLP) and NoREC
    pairs whose results must agree, plus single mutations of generated queries.
``unsafe``
    Rewrites that look valid and are not (NOT IN with NULLs, COUNT(*) vs
    COUNT(col), duplicate-producing joins, UNION vs UNION ALL, predicates moved
    across outer joins), each next to a correct variant.
``compose``
    Chains of KumoSQL's own rules in many orders with repeated application.

Everything is derived from a seed, so a failure is reproduced by its case id.

Three kinds of evidence are kept apart in every report:

``proved``     the prover claims equivalence for every database (unbounded);
``refuted``    the prover returns a database; ``replayed`` counts those where
               running both queries in DuckDB on that database really differs;
``agreement``  both queries agree on N random databases (bounded; evidence only).

Ground truth is the DuckDB oracle: a case is *different* when some random
database separates the queries, *equivalent by construction* for TLP/NoREC and
the hand-written valid variants. A proof of a different pair is a **false
proof**; the count must be 0.

    python tools/unsafe_fuzz.py fuzz --count 200 --seed 1
    python tools/unsafe_fuzz.py unsafe
    python tools/unsafe_fuzz.py compose --count 100
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import random
import sys
import time

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

TABLES = ("t", "u")
COLUMNS = ("a", "b", "c")
DOMAIN = (0, 1, 2, 3)
NULL_RATE = 0.2
PROVER_TIMEOUT_MS = 2000
BQ_SHAPES = True  # window, QUALIFY, SAFE_*, date arithmetic, UNNEST, STRUCT and NULL-heavy outer joins


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


class Oracle:
    """Random small databases (NULLs, duplicates, empty tables) in one DuckDB."""

    def __init__(self, trials: int = 40, seed: int = 7):
        import duckdb

        self._duckdb = duckdb
        self.db = duckdb.connect(":memory:")
        for table in TABLES:
            self.db.execute(f"CREATE TABLE {table} ({', '.join(c + ' BIGINT' for c in COLUMNS)})")
        self.trials = trials
        self.seed = seed

    def load(self, data: dict[str, list[tuple]]) -> None:
        for table in TABLES:
            self.db.execute(f"DELETE FROM {table}")
            rows = data.get(table, [])
            if rows:
                marks = ", ".join("?" * len(COLUMNS))
                self.db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

    def random_data(self, rng: random.Random) -> dict[str, list[tuple]]:
        data = {}
        for table in TABLES:
            count = rng.choice((0, 1, 2, 3, 4, 5, 6))
            rows = [
                tuple(None if rng.random() < NULL_RATE else rng.choice(DOMAIN) for _ in COLUMNS)
                for _ in range(count)
            ]
            if rows and rng.random() < 0.5:
                rows.append(rng.choice(rows))
            data[table] = rows
        return data

    def run(self, sql: str) -> Counter:
        text = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        return Counter(tuple(row) for row in self.db.execute(text).fetchall())

    def compare(self, left: str, right: str, data: dict[str, list[tuple]] | None = None):
        """``(left_rows, right_rows)`` on ``data`` when the bags differ, else ``None``."""

        self.load(data)
        a, b = self.run(left), self.run(right)
        return None if a == b else (a, b)

    def search(self, left: str, right: str):
        """A database separating the queries after ``trials`` random ones, or ``None``.

        Raises ``duckdb.Error`` / ``sqlglot`` errors if either side cannot run.
        """

        rng = random.Random(f"{self.seed}:{left}:{right}")
        for trial in range(self.trials):
            data = {t: [] for t in TABLES} if trial == 0 else self.random_data(rng)
            if self.compare(left, right, data) is not None:
                return data
        return None

    def replay(self, tables: dict[str, list[dict]], left: str, right: str) -> bool:
        """Whether the prover's counterexample really separates the queries."""

        lowered = {k.lower().split(".")[-1]: v for k, v in tables.items()}
        data = {
            t: [tuple(row.get(c) for c in COLUMNS) for row in lowered.get(t, [])] for t in TABLES
        }
        fractional = any(isinstance(v, float) and v != int(v) for rows in data.values() for r in rows for v in r if v is not None)
        if fractional:
            # Valid only for FLOAT64 columns: replay on DOUBLE tables.
            self._retype("DOUBLE")
        try:
            return self.compare(left, right, data) is not None
        finally:
            if fractional:
                self._retype("BIGINT")

    def _retype(self, sql_type: str) -> None:
        for table in TABLES:
            self.db.execute(f"DROP TABLE {table}")
            self.db.execute(f"CREATE TABLE {table} ({', '.join(c + ' ' + sql_type for c in COLUMNS)})")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    family: str
    left: str
    right: str
    expect: str  # "equivalent" (by construction), "different", or "either"
    heldout: bool = False
    meta: dict = field(default_factory=dict)


def atom(rng, cols):
    col = rng.choice(cols)
    roll = rng.random()
    if roll < 0.4:
        return f"{col} {rng.choice(['=', '<>', '<', '<=', '>', '>='])} {rng.choice(DOMAIN)}"
    if roll < 0.65:
        return f"{col} {rng.choice(['=', '<', '>='])} {rng.choice(cols)}"
    if roll < 0.8:
        return f"{col} IS {rng.choice(['', 'NOT '])}NULL"
    if roll < 0.9:
        return f"{col} IN ({rng.choice(DOMAIN)}, {rng.choice(DOMAIN)})"
    return f"{col} BETWEEN {rng.choice((0, 1))} AND {rng.choice((2, 3))}"


def pred(rng, cols, depth=2):
    if depth == 0 or rng.random() < 0.3:
        return atom(rng, cols)
    op = rng.choice(["AND", "OR", "NOT"])
    if op == "NOT":
        return f"NOT ({pred(rng, cols, depth - 1)})"
    return f"({pred(rng, cols, depth - 1)}) {op} ({pred(rng, cols, depth - 1)})"


def _source(rng, alias_cols=False):
    """``(FROM clause, qualified column names)`` for one table or a join."""

    roll = rng.random()
    if roll < 0.5:
        return "t", ["a", "b", "c"]
    if roll < 0.7:
        return "u", ["a", "b", "c"]
    kind = rng.choice(["JOIN", "LEFT JOIN", "JOIN", "CROSS JOIN"])
    cols = ["x.a", "x.b", "y.a", "y.b"]
    if kind == "CROSS JOIN":
        return "t AS x CROSS JOIN u AS y", cols
    return f"t AS x {kind} u AS y ON {pred(rng, cols, 1)}", cols


def _select_list(rng, cols):
    chosen = rng.sample(cols, rng.randint(1, min(3, len(cols))))
    return ", ".join(f"{c} AS k{i}" for i, c in enumerate(chosen))


# -- TLP ---------------------------------------------------------------------


def tlp_cases(rng, index: int) -> list[Case]:
    """TLP: Q ≡ Q[p] UNION ALL Q[NOT p] UNION ALL Q[p IS NULL], and its mutants."""

    source, cols = _source(rng)
    p = pred(rng, cols)
    sel = _select_list(rng, cols)
    base = f"SELECT {sel} FROM {source}"
    parts = [f"SELECT {sel} FROM {source} WHERE {q}" for q in (f"({p})", f"NOT ({p})", f"({p}) IS NULL")]
    cases = [Case(f"tlp-where-{index}", "tlp_where", base, " UNION ALL ".join(parts), "equivalent")]
    # DISTINCT partitions with UNION (set semantics).
    dbase = f"SELECT DISTINCT {sel} FROM {source}"
    dparts = [f"SELECT {sel} FROM {source} WHERE {q}" for q in (f"({p})", f"NOT ({p})", f"({p}) IS NULL")]
    cases.append(Case(f"tlp-distinct-{index}", "tlp_distinct", dbase, " UNION DISTINCT ".join(dparts), "equivalent"))
    # Aggregates: COUNT(*), MAX and SUM recombine over the partitions.
    col = rng.choice(cols)
    for fn, combine in (("COUNT(*)", "SUM"), (f"MAX({col})", "MAX"), (f"MIN({col})", "MIN"), (f"SUM({col})", "SUM")):
        inner = " UNION ALL ".join(
            f"SELECT {fn} AS v FROM {source} WHERE {q}" for q in (f"({p})", f"NOT ({p})", f"({p}) IS NULL")
        )
        cases.append(
            Case(
                f"tlp-agg-{fn.split('(')[0].lower()}-{index}",
                "tlp_aggregate",
                f"SELECT {fn} AS v FROM {source}",
                f"SELECT {combine}(v) AS v FROM ({inner})",
                "equivalent",
                heldout=True,
            )
        )
    # Mutants: a partition is lost, duplicated or replaced.
    mutants = {
        "drop-null": parts[:2],
        "drop-not": [parts[0], parts[2]],
        "dup": parts + [parts[0]],
        "null-as-not-true": [parts[0], parts[1], f"SELECT {sel} FROM {source} WHERE NOT (({p}) IS TRUE)"],
    }
    for name, chosen in mutants.items():
        cases.append(Case(f"tlp-mut-{name}-{index}", "tlp_mutant", base, " UNION ALL ".join(chosen), "either"))
    cases.append(
        Case(
            f"tlp-mut-union-{index}",
            "tlp_mutant",
            base,
            " UNION DISTINCT ".join(parts),
            "either",
        )
    )
    return cases


# -- NoREC -------------------------------------------------------------------


def norec_cases(rng, index: int) -> list[Case]:
    """NoREC: the optimised ``WHERE p`` form against an unoptimisable projection."""

    source, cols = _source(rng)
    p = pred(rng, cols)
    count = f"SELECT COUNT(*) AS v FROM {source} WHERE {p}"
    cases = [
        Case(f"norec-case-{index}", "norec", count, f"SELECT COUNT(CASE WHEN {p} THEN 1 END) AS v FROM {source}", "equivalent", heldout=True),
        Case(
            f"norec-sub-{index}",
            "norec",
            count,
            f"SELECT COUNT(*) AS v FROM (SELECT ({p}) AS f FROM {source}) WHERE f",
            "equivalent",
            heldout=True,
        ),
        Case(
            f"norec-sumcase-{index}",
            "norec_mutant",
            count,
            f"SELECT SUM(CASE WHEN {p} THEN 1 ELSE 0 END) AS v FROM {source}",
            "either",
        ),
        Case(
            f"norec-notnot-{index}",
            "norec_mutant",
            count,
            f"SELECT COUNT(*) AS v FROM {source} WHERE NOT NOT ({p}) OR ({p}) IS NULL",
            "either",
        ),
        Case(
            f"norec-rows-{index}",
            "norec",
            f"SELECT {_first(cols)} AS k FROM {source} WHERE {p}",
            f"SELECT k FROM (SELECT {_first(cols)} AS k, ({p}) AS f FROM {source}) WHERE f",
            "equivalent",
            heldout=True,
        ),
    ]
    return cases


def _first(cols):
    return cols[0]


# -- Unsafe rewrites ---------------------------------------------------------


def unsafe_cases(rng, index: int) -> list[Case]:
    """Plausible rewrites, each with the variants that are wrong and right."""

    k = rng.choice(DOMAIN)
    op = rng.choice(["<", ">", "<=", ">=", "<>"])
    x, y = rng.sample(["a", "b", "c"], 2)
    cases: list[Case] = []

    def add(name, family, left, right, expect, heldout=False):
        cases.append(Case(f"{family}-{name}-{index}", family, left, right, expect, heldout))

    # NOT IN with NULLs.
    ni = f"SELECT t.{x} AS k FROM t WHERE t.{x} NOT IN (SELECT u.{y} FROM u)"
    add("exists", "not_in", ni, f"SELECT t.{x} AS k FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.{y} = t.{x})", "either")
    add("antijoin", "not_in", ni, f"SELECT t.{x} AS k FROM t LEFT JOIN u ON t.{x} = u.{y} WHERE u.{y} IS NULL", "either")
    add(
        "notnull-filter",
        "not_in",
        ni,
        f"SELECT t.{x} AS k FROM t WHERE t.{x} NOT IN (SELECT u.{y} FROM u WHERE u.{y} IS NOT NULL)",
        "either",
    )
    add("not-in-spelling", "not_in", ni, f"SELECT t.{x} AS k FROM t WHERE NOT (t.{x} IN (SELECT u.{y} FROM u))", "equivalent")
    add(
        "in-exists",
        "not_in",
        f"SELECT t.{x} AS k FROM t WHERE t.{x} IN (SELECT u.{y} FROM u)",
        f"SELECT t.{x} AS k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.{y} = t.{x})",
        "equivalent",
        heldout=True,
    )
    # COUNT(*) vs COUNT(col).
    add("star-col", "count", f"SELECT COUNT(*) AS v FROM t", f"SELECT COUNT({x}) AS v FROM t", "either")
    add("col-filter", "count", f"SELECT COUNT(*) AS v FROM t WHERE {x} IS NOT NULL", f"SELECT COUNT({x}) AS v FROM t", "equivalent")
    add("distinct", "count", f"SELECT COUNT({x}) AS v FROM t", f"SELECT COUNT(DISTINCT {x}) AS v FROM t", "either")
    add("one", "count", "SELECT COUNT(*) AS v FROM t", "SELECT COUNT(1) AS v FROM t", "equivalent", heldout=True)
    add("grouped", "count", f"SELECT {x} AS g, COUNT(*) AS v FROM t GROUP BY {x}", f"SELECT {x} AS g, COUNT({y}) AS v FROM t GROUP BY {x}", "either")
    # Duplicate-producing joins.
    join = f"SELECT t.{x} AS k FROM t JOIN u ON t.{x} = u.{y}"
    add("in", "join_dupes", join, f"SELECT t.{x} AS k FROM t WHERE t.{x} IN (SELECT u.{y} FROM u)", "either")
    add("exists", "join_dupes", join, f"SELECT t.{x} AS k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.{y} = t.{x})", "either")
    add(
        "distinct-in",
        "join_dupes",
        f"SELECT DISTINCT t.{x} AS k FROM t JOIN u ON t.{x} = u.{y}",
        f"SELECT DISTINCT t.{x} AS k FROM t WHERE t.{x} IN (SELECT u.{y} FROM u)",
        "equivalent",
    )
    add(
        "drop-distinct",
        "join_dupes",
        f"SELECT DISTINCT t.{x} AS k FROM t JOIN u ON t.{x} = u.{y}",
        join,
        "either",
    )
    add(
        "distinct-semijoin-agg",
        "join_dupes",
        f"SELECT SUM(t.{x}) AS v FROM t JOIN u ON t.{x} = u.{y}",
        f"SELECT SUM(t.{x}) AS v FROM t WHERE t.{x} IN (SELECT u.{y} FROM u)",
        "either",
        heldout=True,
    )
    # UNION vs UNION ALL.
    ua = f"SELECT {x} AS k FROM t UNION ALL SELECT {y} AS k FROM u"
    add("union", "union", ua, f"SELECT {x} AS k FROM t UNION DISTINCT SELECT {y} AS k FROM u", "either")
    add("distinct-wrap", "union", f"SELECT {x} AS k FROM t UNION DISTINCT SELECT {y} AS k FROM u", f"SELECT DISTINCT k FROM ({ua})", "equivalent")
    add("commute", "union", ua, f"SELECT {y} AS k FROM u UNION ALL SELECT {x} AS k FROM t", "equivalent")
    add("intersect", "union", f"SELECT {x} AS k FROM t INTERSECT DISTINCT SELECT {y} AS k FROM u", f"SELECT DISTINCT t.{x} AS k FROM t JOIN u ON t.{x} = u.{y}", "either", heldout=True)
    # Outer-join predicate movement.
    lj = "t LEFT JOIN u ON t.a = u.a"
    add("where-to-inner", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE u.b {op} {k}", f"SELECT t.b AS p, u.b AS q FROM t JOIN u ON t.a = u.a WHERE u.b {op} {k}", "equivalent")
    add("is-null-to-inner", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE u.b IS NULL", "SELECT t.b AS p, u.b AS q FROM t JOIN u ON t.a = u.a WHERE u.b IS NULL", "either")
    add("left-pred-into-on", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE t.b {op} {k}", f"SELECT t.b AS p, u.b AS q FROM t LEFT JOIN u ON t.a = u.a AND t.b {op} {k}", "either")
    add("left-pred-into-subquery", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE t.b {op} {k}", f"SELECT s.b AS p, u.b AS q FROM (SELECT a, b FROM t WHERE b {op} {k}) AS s LEFT JOIN u ON s.a = u.a", "equivalent")
    add("right-where-to-on", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE u.b {op} {k}", f"SELECT t.b AS p, u.b AS q FROM t LEFT JOIN u ON t.a = u.a AND u.b {op} {k}", "either")
    add("right-on-to-subquery", "outer_join", f"SELECT t.b AS p, u.b AS q FROM t LEFT JOIN u ON t.a = u.a AND u.b {op} {k}", f"SELECT t.b AS p, s.b AS q FROM t LEFT JOIN (SELECT a, b FROM u WHERE b {op} {k}) AS s ON t.a = s.a", "equivalent", heldout=True)
    add("right-pred-into-left-subquery", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj} WHERE u.b {op} {k}", f"SELECT t.b AS p, u.b AS q FROM (SELECT a, b FROM t WHERE b {op} {k}) AS t LEFT JOIN u ON t.a = u.a", "either", heldout=True)
    add("left-to-inner", "outer_join", f"SELECT t.b AS p, u.b AS q FROM {lj}", "SELECT t.b AS p, u.b AS q FROM t JOIN u ON t.a = u.a", "either")
    add("full-to-left", "outer_join", "SELECT t.b AS p, u.b AS q FROM t FULL JOIN u ON t.a = u.a WHERE t.b IS NOT NULL", "SELECT t.b AS p, u.b AS q FROM t LEFT JOIN u ON t.a = u.a WHERE t.b IS NOT NULL", "equivalent", heldout=True)
    return cases


# -- Mutations of generated queries -----------------------------------------


_SWAPS = {
    exp.LT: exp.LTE,
    exp.LTE: exp.LT,
    exp.GT: exp.GTE,
    exp.GTE: exp.GT,
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.And: exp.Or,
    exp.Or: exp.And,
}


def mutate(sql: str, rng: random.Random) -> list[tuple[str, str]]:
    """Every single-site mutation of ``sql`` as ``(operator, mutant sql)``."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    out: list[tuple[str, str]] = []

    def emit(op, root):
        out.append((op, root.sql(dialect="bigquery")))

    nodes = list(tree.walk())
    for i, node in enumerate(nodes):
        kind = type(node)
        if kind in _SWAPS and (not isinstance(node, (exp.And, exp.Or)) or True):
            copy = tree.copy()
            target = list(copy.walk())[i]
            target.replace(_SWAPS[kind](this=target.this.copy(), expression=target.expression.copy()))
            emit(f"swap-{kind.__name__}", copy)
        if isinstance(node, (exp.And,)):
            for side in ("this", "expression"):
                copy = tree.copy()
                target = list(copy.walk())[i]
                target.replace(target.args["expression" if side == "this" else "this"].copy())
                emit(f"drop-conjunct-{side}", copy)
        if isinstance(node, exp.Select) and node.args.get("distinct"):
            copy = tree.copy()
            list(copy.walk())[i].set("distinct", None)
            emit("drop-distinct", copy)
        if isinstance(node, exp.Select) and node.args.get("where"):
            copy = tree.copy()
            list(copy.walk())[i].set("where", None)
            emit("drop-where", copy)
        if isinstance(node, exp.Join) and node.args.get("side") == "LEFT":
            copy = tree.copy()
            list(copy.walk())[i].set("side", None)
            emit("left-to-inner", copy)
        if isinstance(node, exp.Union):
            copy = tree.copy()
            target = list(copy.walk())[i]
            target.set("distinct", not target.args.get("distinct", True))
            emit("flip-union-all", copy)
        if isinstance(node, exp.Count) and not node.find(exp.Distinct):
            copy = tree.copy()
            target = list(copy.walk())[i]
            if isinstance(target.this, exp.Star):
                target.set("this", exp.column("b"))
                emit("count-star-to-col", copy)
        if isinstance(node, exp.Literal) and node.is_int:
            copy = tree.copy()
            list(copy.walk())[i].replace(exp.Literal.number(int(node.this) + 1))
            emit("bump-constant", copy)
        if isinstance(node, exp.Not):
            copy = tree.copy()
            list(copy.walk())[i].replace(node.this.copy())
            emit("drop-not", copy)
    return out


# ---------------------------------------------------------------------------
# Query generator for mutation and composition
# ---------------------------------------------------------------------------


def _trivial(rng, text: str) -> str:
    roll = rng.random()
    if roll < 0.2:
        return f"1 = 1 AND ({text})"
    if roll < 0.3:
        return f"(({text}))"
    if roll < 0.4:
        return f"({text}) AND TRUE"
    return text


def relation(rng, sources: list[str], depth: int) -> str:
    """A SELECT that outputs columns a, b, c over tables, CTE names or subqueries."""

    def src():
        if depth > 0 and rng.random() < 0.3:
            return f"({relation(rng, sources, depth - 1)})"
        return rng.choice(sources)

    shape = rng.choice(["filter", "filter", "distinct", "join", "agg", "union", "plain", "having", "case", "coalesce", "in_sub", "exists_sub", "not_in_sub", "multi_agg"]
                       + ([] if not BQ_SHAPES else ["window", "qualify", "safe", "date", "unnest", "outer_nulls", "struct"]))
    if shape == "window":
        fn = rng.choice(["SUM(x.b)", "MAX(x.b)", "MIN(x.c)", "COUNT(*)", "COUNT(x.b)"])
        return f"SELECT x.a AS a, {fn} OVER (PARTITION BY x.a) AS b, x.c AS c FROM {src()} AS x"
    if shape == "qualify":
        fn = rng.choice(["MAX(x.b)", "MIN(x.b)", "COUNT(*)", "SUM(x.c)"])
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x QUALIFY {fn} OVER (PARTITION BY x.a) {rng.choice(['>', '<=', '='])} {rng.choice(DOMAIN)}"
    if shape == "safe":
        e = rng.choice(["SAFE_DIVIDE(x.a, x.b)", "NULLIF(x.a, x.b)", "SAFE_CAST(x.a AS INT64)", "IF(x.a > x.b, x.a, x.b)", "IFNULL(NULLIF(x.a, 0), x.c)", "x.a + x.b", "x.a * 2 - x.c"])
        return f"SELECT {e} AS a, x.b AS b, x.c AS c FROM {src()} AS x WHERE {atom(rng, ['x.a', 'x.b'])}"
    if shape == "date":
        return f"SELECT DATE_DIFF(DATE_ADD(DATE '2024-01-01', INTERVAL x.a DAY), DATE '2024-01-01', DAY) AS a, x.b AS b, x.c AS c FROM {src()} AS x"
    if shape == "unnest":
        return f"SELECT x.a AS a, e AS b, x.c AS c FROM {src()} AS x CROSS JOIN UNNEST([x.a, x.b, {rng.choice(DOMAIN)}]) AS e WHERE e IS NOT NULL"
    if shape == "struct":
        return f"SELECT s.f AS a, s.g AS b, s.h AS c FROM (SELECT STRUCT(x.a AS f, x.b AS g, x.c AS h) AS s FROM {src()} AS x)"
    if shape == "outer_nulls":
        kind = rng.choice(["LEFT", "RIGHT", "FULL OUTER"])
        where = rng.choice(["", " WHERE y.b IS NULL", " WHERE x.b IS NULL", " WHERE y.a IS NOT NULL", f" WHERE y.b {rng.choice(['>', '<>'])} {rng.choice(DOMAIN)}"])
        return f"SELECT x.a AS a, y.b AS b, x.c AS c FROM {src()} AS x {kind} JOIN {src()} AS y ON x.a = y.b{where}"
    if shape == "having":
        fn = rng.choice(["MAX(x.b)", "MIN(x.b)", "SUM(x.b)", "COUNT(x.b)"])
        having = rng.choice([f"COUNT(*) {rng.choice(['>', '>=', '<'])} {rng.choice((1, 2))}", f"{fn} {rng.choice(['>', '<=', '='])} {rng.choice(DOMAIN)}"])
        return f"SELECT x.a AS a, {fn} AS b, COUNT(*) AS c FROM {src()} AS x GROUP BY x.a HAVING {having}"
    if shape == "multi_agg":
        return f"SELECT x.a AS a, SUM(x.b) AS b, COUNT(x.c) AS c FROM {src()} AS x WHERE {pred(rng, ['x.a', 'x.b'], 1)} GROUP BY x.a"
    if shape == "case":
        return f"SELECT x.a AS a, CASE WHEN {atom(rng, ['x.b', 'x.c'])} THEN x.b ELSE x.c END AS b, x.c AS c FROM {src()} AS x"
    if shape == "coalesce":
        return f"SELECT COALESCE(x.a, {rng.choice(DOMAIN)}) AS a, IFNULL(x.b, x.c) AS b, x.c AS c FROM {src()} AS x"
    if shape == "in_sub":
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x WHERE x.a IN (SELECT y.{rng.choice('abc')} FROM {src()} AS y WHERE {atom(rng, ['y.b', 'y.c'])})"
    if shape == "not_in_sub":
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x WHERE x.a NOT IN (SELECT y.{rng.choice('abc')} FROM {src()} AS y)"
    if shape == "exists_sub":
        neg = rng.choice(["", "NOT "])
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x WHERE {neg}EXISTS (SELECT 1 FROM {src()} AS y WHERE y.a = x.a AND {atom(rng, ['y.b', 'y.c'])})"
    if shape == "filter":
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x WHERE {_trivial(rng, pred(rng, ['x.a', 'x.b', 'x.c'], 1))}"
    if shape == "plain":
        return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x"
    if shape == "distinct":
        where = f" WHERE {pred(rng, ['x.a', 'x.b'], 1)}" if rng.random() < 0.5 else ""
        return f"SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x{where}"
    if shape == "join":
        kind = rng.choice(["JOIN", "JOIN", "LEFT JOIN"])
        where = f" WHERE {pred(rng, ['x.b', 'y.b'], 1)}" if rng.random() < 0.5 else ""
        return f"SELECT x.a AS a, y.b AS b, x.c AS c FROM {src()} AS x {kind} {src()} AS y ON x.a = y.a{where}"
    if shape == "agg":
        fn = rng.choice(["MAX", "MIN", "SUM"])
        return f"SELECT x.a AS a, {fn}(x.b) AS b, COUNT(*) AS c FROM {src()} AS x GROUP BY x.a"
    op = rng.choice(["UNION ALL", "UNION ALL", "UNION DISTINCT"])
    return f"SELECT x.a AS a, x.b AS b, x.c AS c FROM {src()} AS x {op} SELECT y.a AS a, y.b AS b, y.c AS c FROM {src()} AS y"


def gen_query(rng: random.Random) -> str:
    """A query with a CTE chain, an unused CTE, a duplicated CTE and subqueries."""

    sources = list(TABLES)
    ctes: list[tuple[str, str]] = []
    for i in range(rng.randint(0, 3)):
        name = f"c{i}"
        ctes.append((name, relation(rng, sources, 1)))
        sources.append(name)
    if ctes and rng.random() < 0.4:
        ctes.append(("dup", ctes[rng.randrange(len(ctes))][1]))
        sources.append("dup")
    if rng.random() < 0.3:
        ctes.append(("unused", relation(rng, TABLES, 0)))
    final = relation(rng, sources, 1)
    if not ctes:
        return final
    return "WITH " + ", ".join(f"{n} AS ({b})" for n, b in ctes) + " " + final


def mutation_cases(rng, index: int) -> list[Case]:
    query = gen_query(rng)
    mutants = mutate(query, rng)
    rng.shuffle(mutants)
    return [
        Case(f"mut-{op}-{index}-{j}", "mutation", query, sql, "either", meta={"op": op})
        for j, (op, sql) in enumerate(mutants[:6])
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def classify(result) -> str:
    from kumosql.smt_equivalence import SmtStatus

    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return "proved"
    if result.status is SmtStatus.NOT_EQUIVALENT:
        return "refuted"
    reason = result.reason
    if "timed out" in reason:
        return "timeout"
    if reason.startswith(("unsupported", "parse error")):
        return "unsupported"
    return "unknown"


SEARCH_REASON = "the queries return different rows on the attached database"


def prove(left: str, right: str):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    return prove_equivalent_algebraic(
        left,
        right,
        schema={t: list(COLUMNS) for t in TABLES},
        types={t: {c: "INT64" for c in COLUMNS} for t in TABLES},
        timeout_ms=PROVER_TIMEOUT_MS,
        compare_names=False,
        search_counterexample=True,
    )


@dataclass
class Outcome:
    case: Case
    prover: str  # proved / refuted / unknown / unsupported / timeout / error
    truth: str  # equivalent / different / unlabelled / label_error / invalid
    replayed: bool | None = None  # refuted only
    seconds: float = 0.0
    detail: str = ""
    synthetic: str | None = None  # KumoSQL's synthetic-data check, run when the prover did not refute

    @property
    def false_proof(self) -> bool:
        return self.prover == "proved" and self.truth in ("different", "label_error")

    @property
    def bad_counterexample(self) -> bool:
        return self.prover == "refuted" and self.replayed is False


def evaluate(case: Case, oracle: Oracle) -> Outcome:
    start = time.time()
    try:
        separating = oracle.search(case.left, case.right)
    except Exception as error:  # a side the oracle cannot run
        return Outcome(case, "unsupported", "invalid", None, time.time() - start, f"oracle: {error}"[:200])
    if separating is not None:
        truth = "label_error" if case.expect == "equivalent" else "different"
    else:
        truth = "equivalent" if case.expect != "different" else "unlabelled"
    try:
        result = prove(case.left, case.right)
        status = classify(result)
    except Exception as error:
        return Outcome(case, "error", truth, None, time.time() - start, f"{type(error).__name__}: {error}"[:200])
    replayed = None
    if status == "refuted":
        try:
            replayed = oracle.replay(result.counterexample.tables, case.left, case.right)
        except Exception as error:
            replayed = False
            return Outcome(case, status, truth, replayed, time.time() - start, f"replay: {error}"[:200])
        if replayed and truth == "equivalent":
            # The replayed database separates the pair: the oracle's random search missed it.
            truth = "label_error" if case.expect == "equivalent" else "different"
    synthetic = None
    if status != "refuted":
        synthetic = synthetic_check(case)
    return Outcome(case, status, truth, replayed, time.time() - start, result.reason[:120], synthetic)


SCHEMA = {t: {c: "INT64" for c in COLUMNS} for t in TABLES}


def synthetic_check(case: Case) -> str:
    """What ``check_result_equivalence`` (the product's executed check) says."""

    from kumosql.result_equivalence import check_result_equivalence

    try:
        return check_result_equivalence(
            case.left, case.right, SCHEMA, seeds=range(8), rows_per_table=8, check_column_names=False
        ).status.value
    except Exception:
        return "error"


def summarise(outcomes: list[Outcome]) -> dict:
    out: dict = {}
    for label, subset in (("all", outcomes), ("heldout", [o for o in outcomes if o.case.heldout]), ("development", [o for o in outcomes if not o.case.heldout])):
        valid = [o for o in subset if o.truth != "invalid"]
        different = [o for o in valid if o.truth == "different"]
        equivalent = [o for o in valid if o.truth == "equivalent"]
        out[label] = {
            "cases": len(subset),
            "invalid_for_oracle": len(subset) - len(valid),
            "correctness": {
                "false_proofs": sum(o.false_proof for o in valid),
                "bad_counterexamples": sum(o.bad_counterexample for o in valid),
                "label_errors": sum(o.truth == "label_error" for o in valid),
                "prover_errors": sum(o.prover == "error" for o in valid),
            },
            "coverage": dict(Counter(o.prover for o in valid)),
            "equivalent": {"cases": len(equivalent), "proved": sum(o.prover == "proved" for o in equivalent)},
            "different": {
                "cases": len(different),
                "refuted": sum(o.prover == "refuted" for o in different),
                "replayed": sum(o.replayed is True for o in different),
                "refuted_by_executed_search": sum(o.prover == "refuted" and o.detail.startswith(SEARCH_REASON) for o in different),
                "synthetic_found": sum(o.synthetic == "different" for o in different),
                "found_either_way": sum(o.prover == "refuted" or o.synthetic == "different" for o in different),
            },
            "synthetic_claims_difference_on_equivalent": sum(o.synthetic == "different" for o in equivalent),
            "seconds": round(sum(o.seconds for o in valid), 1),
        }
    by_family: dict[str, Counter] = {}
    for o in outcomes:
        by_family.setdefault(o.case.family, Counter())[f"{o.truth}/{o.prover}"] += 1
    out["families"] = {k: dict(v) for k, v in sorted(by_family.items())}
    return out


def report_line(name: str, summary: dict) -> str:
    s = summary["all"]
    c = s["correctness"]
    d, e = s["different"], s["equivalent"]
    replay = f"{d['replayed']}/{d['cases']}" if d["cases"] else "n/a"
    return (
        f"{name}: {s['cases']} cases, {c['false_proofs']} false proofs, {c['bad_counterexamples']} bad counterexamples, "
        f"{c['label_errors']} label errors; proved {e['proved']}/{e['cases']} equivalent; "
        f"refuted {d['refuted']}/{d['cases']} different, replayable {replay}; "
        f"synthetic check finds {d['synthetic_found']} more"
    )


def build_cases(suite: str, count: int, seed: int) -> list[Case]:
    rng = random.Random(seed)
    cases: list[Case] = []
    for index in range(count):
        if suite == "fuzz":
            cases += tlp_cases(rng, index)
            cases += norec_cases(rng, index)
            cases += mutation_cases(rng, index)
        elif suite == "unsafe":
            cases += unsafe_cases(rng, index)
        else:
            raise ValueError(suite)
    return cases


def run_prover_suite(suite: str, count: int, seed: int, trials: int = 40) -> tuple[list[Outcome], dict]:
    oracle = Oracle(trials=trials)
    outcomes = [evaluate(case, oracle) for case in build_cases(suite, count, seed)]
    return outcomes, summarise(outcomes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("suite", choices=["fuzz", "unsafe", "compose"])
    parser.add_argument("--count", type=int, default=50, help="seeds of generated cases (templates for unsafe)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--trials", type=int, default=40, help="random databases per pair")
    parser.add_argument("--json", type=Path, help="write the summary here")
    parser.add_argument("--dump-cases", type=Path, help="write the generated cases as JSONL (id, family, left, right, expect, heldout) and exit")
    parser.add_argument("--show", action="store_true", help="print every false proof / bad counterexample")
    args = parser.parse_args(argv)
    if args.dump_cases:
        if args.suite == "compose":
            parser.error("compose has no case pairs to dump")
        with args.dump_cases.open("w", encoding="utf-8") as handle:
            for case in build_cases(args.suite, args.count, args.seed):
                handle.write(json.dumps(case.__dict__, sort_keys=True) + "\n")
        return 0
    if args.suite == "compose":
        import compose_fuzz

        summary = compose_fuzz.run(args.count, args.seed, args.trials)
        print(compose_fuzz.report_line(summary))
        bad = summary["correctness"]["behaviour_changes"] + summary["correctness"]["errors"]
    else:
        outcomes, summary = run_prover_suite(args.suite, args.count, args.seed, args.trials)
        print(report_line(args.suite, summary))
        if args.show:
            for o in outcomes:
                if o.false_proof or o.bad_counterexample or o.truth == "label_error":
                    print(f"  {o.case.id}: {o.truth}/{o.prover}\n    {o.case.left}\n    {o.case.right}")
        bad = summary["all"]["correctness"]["false_proofs"] + summary["all"]["correctness"]["bad_counterexamples"]
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
