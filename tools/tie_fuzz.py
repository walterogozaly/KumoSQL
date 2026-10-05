"""Soundness fuzzer for ``kumosql.tie_determinism.analyze``.

Random windows, QUALIFY, ORDER BY .. LIMIT, ANY_VALUE / MAX_BY / ARRAY_AGG aggregates and ARRAY subqueries run
over ``events(id, user_id, ts, value)`` in three schemas: ``key`` (``id`` is a declared NOT NULL key), ``nokey``
(no constraints) and ``composite`` (``(user_id, ts)`` is a declared NOT NULL key). Every query ``analyze`` calls
deterministic runs on DuckDB (one thread, so ties fall in storage order) over small databases (2 to 4 rows, tiny
value domains so ties are common, every declared fact respected) with the rows stored in **every** permutation; the
result bag must never change. A change is an unsound verdict and is printed as it happens.

A sample of the ``unknown`` queries runs the same way as a control: the fuzzer is only worth something if it
finds the tie dependence the analysis is afraid of (``control_varied`` counts how often it does).

    python tools/tie_fuzz.py --count 4000 --seed 1            # prints each failure as it happens, then a summary
    python tools/tie_fuzz.py --count 4000 --seed 1 --json out.json

Query ``i`` of seed ``s`` is generated from ``Random(f"{s}:{i}")`` alone, so ``--start`` reruns one query and a long
run can be split across processes. Reuses ``result_equivalence.DatasetRunner`` (one DuckDB connection per schema,
rows swapped per permutation) and ``smt_equivalence.TableConstraints``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import itertools
import json
import logging
import random
import re
import time
from pathlib import Path

from kumosql.result_equivalence import (  
    DatasetRunner,
    ExecutionError,
    SyntheticDataset,
    SyntheticTable,
    _normalize_value,
)
from kumosql.smt_equivalence import TableConstraints  
from kumosql.tie_determinism import analyze  

COLUMNS = ("id", "user_id", "ts", "value")
TYPES = (("id", "INT64"), ("user_id", "INT64"), ("ts", "INT64"), ("value", "INT64"))
SCHEMAS = {
    "key": {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))},
    "nokey": {},
    "composite": {"events": TableConstraints(not_null=frozenset({"user_id", "ts"}), keys=(("user_id", "ts"),))},
}
CONTROL_RATE = 0.2  # share of unknown queries that also run, as the control
DATABASES = 6  # random databases per query


# ----- databases -----------------------------------------------------------------------------------------------


def databases(rng: random.Random, schema: str, count: int = DATABASES) -> list[list[tuple]]:
    """Small databases that respect the schema's declared facts and tie often.

    Each database draws its own two or three values per column from a pool that holds 0, NULL and values 1 apart or
    2 apart, so ties are common and an expression such as ``COALESCE(ts, 0)`` or ``MOD(ts, 2)`` can tie rows whose
    columns differ.
    """

    result = []
    for _ in range(count):
        n = rng.randint(2, 4)
        nullable = schema != "composite"
        pools = [
            rng.sample([-1, 0, 1, 2, 3] + ([None] if nullable else []), rng.choice([2, 3])),  # user_id
            rng.sample([-1, 0, 1, 2, 3] + ([None] if nullable else []), rng.choice([2, 3])),  # ts
            rng.sample([0, 5, 6, 7, None], rng.choice([2, 3])),  # value
        ]
        if schema == "key":
            ids = rng.sample(range(1, 9), n)
            rows = [(i, *(rng.choice(p) for p in pools)) for i in ids]
        elif schema == "nokey":
            rows = [(rng.choice([1, 2, 3, None]), *(rng.choice(p) for p in pools)) for _ in range(n)]
        else:
            pairs = rng.sample([(u, t) for u in pools[0] for t in pools[1]], min(n, len(pools[0]) * len(pools[1])))
            rows = [(rng.choice([1, 2, 3, None]), u, t, rng.choice(pools[2])) for u, t in pairs]
        result.append(rows)
    return result


# ----- queries -------------------------------------------------------------------------------------------------


SOURCES = (
    # name, SQL of a relation with the columns id, user_id, ts, value (where a declared key is kept, lost or unproven)
    "SELECT * FROM events UNION ALL SELECT * FROM events",
    "SELECT a.id AS id, a.user_id AS user_id, b.ts AS ts, b.value AS value FROM events AS a JOIN events AS b ON a.user_id = b.user_id",
    "SELECT a.id AS id, a.user_id AS user_id, b.ts AS ts, b.value AS value FROM events AS a LEFT JOIN events AS b ON a.ts = b.ts",
    "SELECT a.id AS id, b.user_id AS user_id, a.ts AS ts, b.value AS value FROM events AS a CROSS JOIN events AS b",
    "SELECT MIN(id) AS id, user_id, ts, SUM(value) AS value FROM events GROUP BY user_id, ts",
    "SELECT MIN(id) AS id, user_id, MAX(ts) AS ts, COUNT(*) AS value FROM events GROUP BY user_id",
    "SELECT DISTINCT id, user_id, ts, value FROM events",
    "SELECT DISTINCT user_id AS id, user_id, ts, value FROM events",
    "SELECT id, user_id, ts, value FROM events WHERE ts IS NOT NULL",
    "SELECT id, user_id, ts, value FROM events ORDER BY ts LIMIT 3",
    "SELECT id, user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, id) = 1",
    "SELECT id, user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1",
    "SELECT id, user_id, ts, value FROM events, UNNEST([1, 2]) AS k",
    "SELECT a.id AS id, a.user_id AS user_id, b.ts AS ts, b.value AS value FROM events AS a JOIN events AS b ON a.id = b.id",
    "SELECT a.id AS id, a.user_id AS user_id, b.ts AS ts, b.value AS value FROM events AS a FULL OUTER JOIN events AS b ON a.id = b.id",
    "SELECT id, user_id, ts, value FROM events WHERE id IN (SELECT id FROM events WHERE value IS NOT NULL)",
    "SELECT id, user_id, ts, value FROM events WHERE EXISTS (SELECT 1 FROM events AS f WHERE f.user_id = events.user_id)",
    "SELECT id, user_id, ts, value FROM events UNION DISTINCT SELECT id, user_id, ts, value FROM events",
    "SELECT id, user_id, ts, value FROM events EXCEPT DISTINCT SELECT id, user_id, ts, value FROM events WHERE value = 5",
    "SELECT id, user_id, ts, value FROM events INTERSECT DISTINCT SELECT id, user_id, ts, value FROM events",
    "SELECT MIN(id) AS id, user_id, ts, SUM(value) AS value FROM events GROUP BY ROLLUP(user_id, ts)",
    "SELECT MIN(id) AS id, user_id, MAX(ts) AS ts, SUM(value) AS value FROM events GROUP BY GROUPING SETS ((user_id), ())",
    "SELECT MAX(id) AS id, 1 AS user_id, MAX(ts) AS ts, SUM(value) AS value FROM events",
    "SELECT id, user_id, ts, value FROM events AS e1 WHERE ts = (SELECT MAX(ts) FROM events)",
    "SELECT id, user_id, ts, value FROM events ORDER BY id LIMIT 2",
    "SELECT id, user_id, ts, value FROM events ORDER BY ts, id LIMIT 2",
)


def _source(rng, alias: str = "") -> str:
    """``events`` (60%) or a derived table that keeps, loses or never had the declared keys."""

    if rng.random() < 0.6:
        return f"events AS {alias}" if alias else "events"
    return f"({rng.choice(SOURCES)}) AS {alias or 's'}"


def _shadowed(rng, columns: list[str]) -> tuple[list[str], list[str]]:
    """Select items for ``columns`` and the names they output; some are renamed to another column's name
    (``value AS ts``), so a later ``ORDER BY ts`` or ``OVER (ORDER BY ts)`` can mean the alias or the column."""

    if not getattr(rng, "shadow", True) or rng.random() > 0.35:
        return list(columns), list(columns)
    taken, items, names = set(), [], []
    for column in columns:
        alias = rng.choice(COLUMNS) if rng.random() < 0.6 else column
        if alias in taken:
            alias = column
        if alias in taken:
            items.append(column)
            names.append(column)
            continue
        taken.add(alias)
        items.append(column if alias == column else f"{column} AS {alias}")
        names.append(alias)
    return items, names


def _cols(rng, low, high):
    return rng.sample(COLUMNS, rng.randint(low, high))


def _order_item(rng, source: tuple[str, ...] = COLUMNS) -> str:
    base = rng.choice(source)
    if base != "id" and rng.random() < 0.18:
        other = rng.choice(COLUMNS)
        base = rng.choice(
            [f"COALESCE({base}, 0)", f"MOD({base}, 2)", f"ABS({base})", f"GREATEST({base}, {other})", f"CASE WHEN {base} > 1 THEN 1 ELSE 0 END",
             f"{base} + {other}", f"IFNULL({base}, {other})"]
        )
    if rng.random() < 0.4:
        base += " DESC"
    if rng.random() < 0.15:
        base += rng.choice([" NULLS FIRST", " NULLS LAST"])
    return base


def _order_by(rng, low=1, high=3, source: tuple[str, ...] = COLUMNS) -> str:
    seen, items = set(), []
    for _ in range(rng.randint(low, high)):
        item = _order_item(rng, source)
        key = item.split()[0]
        if key not in seen:
            seen.add(key)
            items.append(item)
    return ", ".join(items)


def _predicate(rng) -> str:
    column = rng.choice(COLUMNS[1:])
    operator = rng.choice(["=", "<>", ">", "<=", "IS NULL", "IS NOT NULL"])
    if operator.startswith("IS"):
        return f"{column} {operator}"
    return f"{column} {operator} {rng.choice([1, 2, 5, 6])}"


def window_call(rng, order_columns: tuple[str, ...] = COLUMNS) -> str:
    """One analytic call with a random PARTITION BY, ORDER BY and frame."""

    partition = rng.sample(COLUMNS, rng.choice([0, 0, 1, 1, 2])) if rng.random() < 0.8 else []
    if partition and rng.random() < 0.12:
        partition[0] = rng.choice([f"MOD({partition[0]}, 2)", f"COALESCE({partition[0]}, 0)"])
    order = _order_by(rng, 0 if rng.random() < 0.1 else 1, 3, order_columns) if rng.random() < 0.92 else ""
    column = rng.choice(COLUMNS)
    kind = rng.choice(
        ["row_number", "rank", "dense_rank", "ntile", "lag", "lead", "first", "last", "nth", "sum", "count", "min", "max",
         "any", "array_agg", "percent_rank", "cume_dist", "row_number", "lag"]
    )
    frame = ""
    call = {
        "row_number": "ROW_NUMBER()", "rank": "RANK()", "dense_rank": "DENSE_RANK()", "ntile": f"NTILE({rng.randint(1, 3)})",
        "lag": f"LAG({column})", "lead": f"LEAD({column}, {rng.randint(1, 2)})", "percent_rank": "PERCENT_RANK()",
        "cume_dist": "CUME_DIST()",
    }.get(kind)
    if call is None:
        call = {
            "first": f"FIRST_VALUE({column})", "last": f"LAST_VALUE({column})", "nth": f"NTH_VALUE({column}, 2)",
            "sum": f"SUM({column})", "count": rng.choice(["COUNT(*)", f"COUNT({column})"]), "min": f"MIN({column})",
            "max": f"MAX({column})", "any": f"ANY_VALUE({column})", "array_agg": f"ARRAY_AGG({column})",
        }[kind]
        if order and rng.random() < 0.75:
            frame = rng.choice(
                [
                    "ROWS UNBOUNDED PRECEDING", "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW", "ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING",
                    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING", "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
                    "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
                ]
            )
    spec = []
    if partition:
        spec.append("PARTITION BY " + ", ".join(partition))
    if order:
        spec.append("ORDER BY " + order)
    if frame:
        spec.append(frame)
    return f"{call} OVER ({' '.join(spec)})"


def _window_query(rng) -> tuple[str, list[str]]:
    """``(sql, plain output columns)``; the window columns are named w0, w1."""

    calls = [window_call(rng) for _ in range(rng.choice([1, 1, 1, 2]))]
    names = [f"w{i}" for i in range(len(calls))]
    qualify = ""
    if rng.random() < 0.55:
        which = rng.randrange(len(calls))
        target = calls[which] if rng.random() < 0.5 else names[which]
        qualify = f" QUALIFY {target} {rng.choice(['=', '<=', '>'])} {rng.randint(1, 2)}"
    items, plain = _shadowed(rng, _cols(rng, 0, 4))
    select = items + [f"{call} AS {name}" for call, name in zip(calls, names)]
    where = f" WHERE {_predicate(rng)}" if rng.random() < 0.3 else ""
    sql = f"SELECT {', '.join(select)} FROM {_source(rng)}{where}{qualify}"
    if plain and rng.random() < 0.3:
        sql += f" ORDER BY {_order_by(rng, 1, 2, tuple(plain))}"
        if rng.random() < 0.7:
            sql += f" LIMIT {rng.randint(1, 3)}"
    return sql, plain


def _wrapped_query(rng) -> str:
    """A window query read by an outer query: a filter on its rank, a projection, an aggregate, another window."""

    inner, columns = _window_query(rng)
    shape = rng.choice(["filter", "project", "aggregate", "window", "join", "star", "twice", "using", "in"])
    names = columns + ["w0"]
    maker = rng.choice(["cte", "subquery"])

    def wrap(body: str) -> str:
        return f"WITH d AS ({inner}) {body.format(s='d')}" if maker == "cte" else body.format(s=f"({inner}) AS d")

    if shape == "filter":
        test = rng.choice(["w0 = 1", "w0 <= 2", "w0 > 1", "w0 IS NULL"])
        return wrap(f"SELECT {', '.join(rng.sample(names, rng.randint(1, len(names))))} FROM {{s}} WHERE {test}")
    if shape == "project":
        picked = rng.sample(names, rng.randint(1, len(names)))
        return wrap(f"SELECT {', '.join(picked)} FROM {{s}}")
    if shape == "aggregate":
        group = rng.choice(names)
        agg = rng.choice(["COUNT(*)", "SUM(w0)", f"MAX({rng.choice(names)})", f"ARRAY_AGG({rng.choice(names)} ORDER BY {rng.choice(names)})"])
        if rng.random() < 0.5:
            return wrap(f"SELECT {group}, {agg} FROM {{s}} GROUP BY {group}")
        return wrap(f"SELECT {agg} FROM {{s}}")
    if shape == "window":
        order = ", ".join(rng.sample(names, rng.randint(1, min(2, len(names)))))
        return wrap(f"SELECT {rng.choice(names)}, ROW_NUMBER() OVER (ORDER BY {order}) AS r FROM {{s}}")
    if shape == "star":
        return wrap(rng.choice(["SELECT * FROM {s}", "SELECT d.* FROM {s}", "SELECT COUNT(*) AS n FROM {s} WHERE TO_JSON_STRING(d) IS NOT NULL"]))
    if shape == "twice":
        key = rng.choice(names)
        return f"WITH d AS ({inner}) SELECT a.{rng.choice(names)}, b.{rng.choice(names)} FROM d AS a JOIN d AS b ON a.{key} = b.{key}"
    if shape == "using":
        key = [c for c in names if c != "w0"] or ["w0"]
        return wrap(f"SELECT d.{rng.choice(names)}, e.id FROM {{s}} JOIN events AS e USING ({rng.choice(key)})")
    if shape == "in":
        return wrap(f"SELECT id, value FROM events WHERE {rng.choice(names)} IN (SELECT {rng.choice(names)} FROM {{s}})")
    # join the (de-duplicated) rows back to the table
    key = rng.choice(["user_id", "ts", "value"])
    if key not in names:
        key = names[0]
    return wrap(f"SELECT e.id, e.value, d.{key} FROM events AS e JOIN {{s}} ON e.{key} = d.{key}")


def _limit_query(rng) -> str:
    shape = rng.choice(["plain", "plain", "distinct", "group", "union", "subquery"])
    where = f" WHERE {_predicate(rng)}" if rng.random() < 0.25 else ""
    limit = f" LIMIT {rng.randint(1, 3)}" + (f" OFFSET {rng.randint(1, 2)}" if rng.random() < 0.25 else "")
    if shape == "plain":
        cols = rng.choice(["*", ", ".join(_shadowed(rng, _cols(rng, 1, 4))[0])])
        order = f" ORDER BY {_order_by(rng, 1, 3)}" if rng.random() < 0.9 else ""
        return f"SELECT {cols} FROM {_source(rng)}{where}{order}{limit}"
    if shape == "distinct":
        cols = _cols(rng, 1, 3)
        order = ", ".join(rng.sample(cols, rng.randint(1, len(cols))))
        return f"SELECT DISTINCT {', '.join(cols)} FROM {_source(rng)}{where} ORDER BY {order}{limit}"
    if shape == "group":
        group = _cols(rng, 1, 2)
        agg = rng.choice(["SUM(value)", "COUNT(*)", "MAX(ts)", "MIN(id)"])
        order = rng.choice([agg, f"{agg} DESC", ", ".join(group), f"{agg}, {group[0]}"])
        return f"SELECT {', '.join(group)}, {agg} AS a FROM {_source(rng)}{where} GROUP BY {', '.join(group)} ORDER BY {order.replace(agg, 'a')}{limit}"
    if shape == "union":
        cols = ", ".join(_cols(rng, 1, 2))
        return f"SELECT {cols} FROM events UNION ALL SELECT {cols} FROM events{where} ORDER BY {cols.split(', ')[0]}{limit}"
    inner_cols = _cols(rng, 2, 4)
    inner = f"SELECT {', '.join(inner_cols)} FROM {_source(rng)}{where} ORDER BY {_order_by(rng, 1, 2, tuple(inner_cols))} LIMIT {rng.randint(1, 3)}"
    outer = rng.choice([f"SELECT {', '.join(rng.sample(inner_cols, rng.randint(1, len(inner_cols))))} FROM ({inner}) AS d", f"SELECT COUNT(*), SUM({inner_cols[0]}) FROM ({inner}) AS d"])
    return outer


def _aggregate_query(rng) -> str:
    group = rng.sample(COLUMNS, rng.choice([0, 1, 1, 2]))
    order = _order_by(rng, 1, 2)
    column = rng.choice(COLUMNS)

    def one() -> str:
        return rng.choice(
            [
                f"ANY_VALUE({column})", f"MAX_BY({column}, {rng.choice(COLUMNS)})", f"MIN_BY({column}, {rng.choice(COLUMNS)})",
                f"ARRAY_AGG({column} ORDER BY {order})", f"ARRAY_AGG({column} ORDER BY {order} LIMIT 1)[OFFSET(0)]",
                f"STRING_AGG(CAST({column} AS STRING), ',' ORDER BY {order})",
                f"ARRAY_AGG(DISTINCT {column} IGNORE NULLS ORDER BY {column})", f"ARRAY_AGG(e ORDER BY {order} LIMIT 1)[OFFSET(0)].{rng.choice(COLUMNS)}",
                "SUM(value)", "COUNT(*)", f"MAX({column})", f"ARRAY_AGG({column} IGNORE NULLS)", f"ANY_VALUE({rng.choice(COLUMNS)})",
            ]
        )

    names = [f"a{i}" for i in range(2)]
    if getattr(rng, "shadow", True) and rng.random() < 0.35:
        names = rng.sample(COLUMNS, 2)
    aggregates = ", ".join(f"{one()} AS {names[i]}" for i in range(rng.randint(1, 2)))
    select = ", ".join(group + [aggregates])
    where = f" WHERE {_predicate(rng)}" if rng.random() < 0.25 else ""
    sql = f"SELECT {select} FROM {_source(rng, 'e')}{where}" + (f" GROUP BY {', '.join(group)}" if group else "")
    if group and rng.random() < 0.2:
        sql += " HAVING COUNT(*) > 1"
    return sql


def _array_query(rng) -> str:
    column = rng.choice(COLUMNS)
    order = f" ORDER BY {_order_by(rng, 1, 3)}" if rng.random() < 0.85 else ""
    limit = f" LIMIT {rng.randint(1, 2)}" if rng.random() < 0.4 else ""
    sub = f"SELECT {column} FROM {_source(rng)}{order}{limit}"
    return rng.choice(
        [
            f"SELECT ARRAY({sub}) AS a",
            f"SELECT id, ARRAY(SELECT {column} FROM events AS f WHERE f.user_id = e.user_id{order.replace('ORDER BY ', 'ORDER BY f.') if order and '(' not in order else ''}{limit}) AS a FROM events AS e",
            f"SELECT id FROM events WHERE EXISTS ({sub})",
            f"SELECT id FROM events WHERE {column} IN ({sub})",
            f"SELECT id, ({sub}) AS v FROM events AS e",
            f"SELECT id, (SELECT {column} FROM events AS f WHERE f.user_id = e.user_id{order.replace('ORDER BY ', 'ORDER BY f.') if order and '(' not in order else ''} LIMIT 1) AS v FROM events AS e",
        ]
    )


def gen_query(rng: random.Random, shadow: bool = True) -> str:
    """Query for ``rng``; ``shadow`` lets a SELECT alias take a column's name (``value AS ts``)."""

    rng.shadow = shadow  # read by the generators below
    return rng.choices(
        [lambda r: _window_query(r)[0], _wrapped_query, _limit_query, _aggregate_query, _array_query], weights=[34, 18, 22, 18, 8]
    )[0](rng)


# ----- running -------------------------------------------------------------------------------------------------


def _bag(output) -> frozenset:
    return frozenset(Counter(_normalize_value(tuple(row), None) for row in output.rows).items())


class _Warnings(logging.Handler):
    """sqlglot warns when it drops what DuckDB cannot say (NULLS FIRST in a window MAX, LIMIT inside ARRAY_AGG)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        self.count += 1


def runnable(sql: str) -> str:
    """DuckDB has no LIMIT inside ARRAY_AGG; ``ARRAY_AGG(.. ORDER BY o LIMIT 1)[OFFSET(0)]`` is its first element."""

    return re.sub(r" LIMIT 1\)\[OFFSET\(0\)\]", ")[OFFSET(0)]", sql)


class Fuzzer:
    def __init__(self):
        self.warnings = _Warnings()
        self.dropped: set[str] = set()
        logging.getLogger("sqlglot").addHandler(self.warnings)
        self.runners = {
            name: DatasetRunner({"events": dict(TYPES)}, settings=("SET threads=1",)) for name in SCHEMAS
        }

    def varies(self, schema: str, sql: str, dbs: list[list[tuple]]) -> tuple[bool, list | None, bool]:
        """(varied, witness (rows, two differing bags), ran at all) over every storage order of every database."""

        runner = self.runners[schema]
        sql = runnable(sql)
        before = self.warnings.count
        try:
            runner.prepare(sql)
        except ExecutionError:
            return False, None, False
        if self.warnings.count != before:  # translated with something dropped: not the query we judged
            self.dropped.add(sql)
        if sql in self.dropped:
            return False, None, False
        ran = False
        for rows in dbs:
            first = None
            for order in dict.fromkeys(itertools.permutations(rows)):
                dataset = SyntheticDataset(0, {"events": SyntheticTable(TYPES, tuple(order))})
                try:
                    bag = _bag(runner.run(sql, dataset))
                except ExecutionError:
                    return False, None, ran
                ran = True
                if first is None:
                    first = (order, bag)
                elif bag != first[1]:
                    return True, [first[0], order, sorted(map(repr, first[1])), sorted(map(repr, bag))], True
        return False, None, ran


def run(count: int, seed: int = 1, start: int = 0, show: bool = True, schemas: tuple[str, ...] = tuple(SCHEMAS), shadow: bool = True) -> dict:
    fuzzer = Fuzzer()
    totals: Counter = Counter()
    by_kind: Counter = Counter()
    failures: list[dict] = []
    began = time.time()
    for index in range(start, start + count):
        rng = random.Random(f"{seed}:{index}")
        sql = gen_query(rng, shadow)
        for name in schemas:
            totals["checks"] += 1
            report = analyze(sql, constraints=SCHEMAS[name] or None)
            if report.unsupported:
                totals["unsupported"] += 1
                continue
            dbs = databases(random.Random(f"{seed}:{index}:{name}"), name)
            if report.deterministic:
                totals["deterministic"] += 1
                varied, witness, ran = fuzzer.varies(name, sql, dbs)
                if not ran:
                    totals["deterministic_not_run"] += 1
                    continue
                totals["deterministic_run"] += 1
                by_kind.update(site.kind for site in report.sites)
                if not report.sites:
                    totals["no_sites"] += 1
                if varied:
                    totals["unsound"] += 1
                    failure = {"index": index, "seed": seed, "schema": name, "sql": sql, "witness": witness,
                               "sites": [site.to_json() for site in report.sites]}
                    failures.append(failure)
                    if show:
                        print("UNSOUND", json.dumps(failure, default=str), flush=True)
            else:
                totals["unknown"] += 1
                if random.Random(f"{seed}:{index}:{name}:control").random() < CONTROL_RATE:
                    varied, _, ran = fuzzer.varies(name, sql, dbs)
                    totals["control_run"] += ran
                    totals["control_varied"] += varied
        if show and (index - start + 1) % 250 == 0:
            print(f"[{index - start + 1}/{count}] {dict(totals)} {time.time() - began:.0f}s", flush=True)
    for runner in fuzzer.runners.values():
        runner.close()
    return {"queries": count, "seed": seed, "start": start, "schemas": list(schemas), **{k: totals[k] for k in (
        "checks", "unsupported", "deterministic", "deterministic_run", "deterministic_not_run", "no_sites", "unknown",
        "control_run", "control_varied", "unsound")}, "deterministic_sites_by_kind": dict(by_kind), "failures": failures,
        "seconds": round(time.time() - began, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--start", type=int, default=0, help="first query index (reruns one query with --count 1)")
    parser.add_argument("--schema", choices=sorted(SCHEMAS), action="append", help="only these schemas (default: all three)")
    parser.add_argument("--no-shadow", action="store_true", help="never let a SELECT alias take a column's name")
    parser.add_argument("--json", help="write the summary here")
    args = parser.parse_args(argv)
    summary = run(args.count, args.seed, args.start, schemas=tuple(args.schema or SCHEMAS), shadow=not args.no_shadow)
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1, default=str))
    return 1 if summary["unsound"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
