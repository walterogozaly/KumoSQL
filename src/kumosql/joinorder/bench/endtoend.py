"""End-to-end join-order evaluation shared by the STATS-CEB and JOB benchmarks.

For every query, each cardinality source (an estimator, Postgres' estimates, the
true sizes) feeds the same DPccp optimizer. The chosen join tree is then

* costed with the true sub-join sizes (C_out, so plan quality free of timing
  noise; the plan chosen from true sizes is the optimum), and
* executed in DuckDB with its own join reordering switched off, next to DuckDB's
  default plan.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..planner import Plan, optimize, plan_cost, to_sql
from ..query import JoinQuery


def connected_subsets(query: JoinQuery) -> list[frozenset[str]]:
    seen: set[frozenset[str]] = set()
    frontier = [frozenset([a]) for a in query.tables]
    seen.update(frontier)
    while frontier:
        nxt = []
        for s in frontier:
            for a in set().union(*(query.neighbours(x) for x in s)) - s:
                t = s | {a}
                if t not in seen:
                    seen.add(t)
                    nxt.append(t)
        frontier = nxt
    return sorted(seen, key=lambda s: (len(s), sorted(s)))


def subset_sql(query: JoinQuery, subset: frozenset[str], dialect: str = "postgres") -> str:
    """``SELECT COUNT(*)`` over one sub-join, with its filters and join predicates."""
    froms = ", ".join(f"{query.tables[a]} AS {a}" for a in sorted(subset))
    conds = [f"{e.left}.{e.left_col} = {e.right}.{e.right_col}" for e in query.edges_within(subset)]
    for a in sorted(subset):
        conds += [f.sql(dialect=dialect) for f in query.filters.get(a, [])]
    conds += [n.sql(dialect=dialect) for refs, n in query.residual if refs <= subset]
    sql = f"SELECT COUNT(*) FROM {froms}"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return sql


def subset_key(subset: frozenset[str]) -> str:
    return ",".join(sorted(subset))


def run_sql(con: Any, sql: str, timeout: float) -> tuple[float, Any]:
    """Wall time of one execution; ``inf`` when it hits the timeout."""
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    start = time.perf_counter()
    try:
        result = con.execute(sql).fetchall()
        return time.perf_counter() - start, result
    except Exception as exc:  # interrupted
        if "INTERRUPT" in str(exc).upper() or time.perf_counter() - start >= timeout * 0.99:
            return float("inf"), None
        raise
    finally:
        timer.cancel()


def plan_with(query: JoinQuery, card: Callable[[frozenset[str]], float], model: str = "cout") -> Plan:
    return optimize(query, card, model)


def true_cost(plan: Plan, truth: dict[str, float], model: str = "cout") -> float:
    return plan_cost(plan, lambda s: truth[subset_key(s)], model)


def forced_sql(query: JoinQuery, plan: Plan) -> str:
    return to_sql(query, plan, "duckdb")
