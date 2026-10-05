"""Work features of a join query, of the same query over a stored view, and of building the view.

The materialization advisor needs to know, before anything is stored, how much
work a query does now and how much it would do if a join it contains were read
from a stored table instead. Both come from the cardinality estimator:

* ``scan``: rows read from stored tables, times the columns read from each
  (a column store reads only those);
* ``join``: C_out of the best join tree under the estimates (the rows every
  join produces, the final one included);
* ``write``: rows times columns written, for building a stored view.

Over a stored view, the view's tables become one leaf: its scan is the view's
whole stored size (every row, the columns the query reads), its rows after the
query's filters are the estimate for those tables with those filters, and the
joins inside the view disappear. ``kumosql.cost_model.calibrate`` turns these
features into seconds or slot time from measured runs.
"""

from __future__ import annotations

from sqlglot import exp

from .estimator import Estimator
from .planner import optimize, plan_cost
from .query import Edge, JoinQuery

VIEW = "__stored_view__"


def columns_used(query: JoinQuery, alias: str) -> set[str]:
    """Columns of ``alias`` that the query's filters, joins and residual predicates read."""

    used: set[str] = set()
    for predicate in query.filters.get(alias, []):
        used |= {c.name for c in predicate.find_all(exp.Column)}
    for edge in query.edges:
        if alias in (edge.left, edge.right):
            used.add(edge.col(alias))
    for refs, node in query.residual:
        if alias in refs:
            used |= {c.name for c in node.find_all(exp.Column) if c.table == alias}
    for node in query.select:
        used |= {c.name for c in node.find_all(exp.Column) if c.table == alias}
    return used


def _width(query: JoinQuery, alias: str) -> int:
    return max(len(columns_used(query, alias)), 1)


def query_features(query: JoinQuery, estimator: Estimator, rows: dict[str, float]) -> dict[str, float]:
    """Scan and join work of ``query`` as written."""

    card = lambda subset: estimator.estimate(query, subset)  # noqa: E731
    plan = optimize(query, card)
    scan = sum(rows[table] * _width(query, alias) for alias, table in query.tables.items())
    return {"scan": float(scan), "join": float(plan_cost(plan, card)), "query": 1.0}


def contract(query: JoinQuery, group: frozenset[str]) -> JoinQuery:
    """The query with the aliases in ``group`` replaced by one leaf (the stored view)."""

    tables = {alias: table for alias, table in query.tables.items() if alias not in group}
    tables[VIEW] = VIEW
    edges = []
    for edge in query.edges:
        left = VIEW if edge.left in group else edge.left
        right = VIEW if edge.right in group else edge.right
        if left != right:
            edges.append(Edge(left, edge.left_col, right, edge.right_col))
    return JoinQuery(sql=query.sql, tables=tables, filters={alias: [] for alias in tables}, edges=edges)


def query_over_view_features(
    query: JoinQuery,
    group: frozenset[str],
    estimator: Estimator,
    rows: dict[str, float],
    view_rows: float,
) -> dict[str, float]:
    """Scan and join work of ``query`` when the join of ``group`` is read from a stored view of ``view_rows`` rows."""

    contracted = contract(query, group)

    def card(subset: frozenset[str]) -> float:
        real: set[str] = set()
        for alias in subset:
            real |= group if alias == VIEW else {alias}
        return estimator.estimate(query, frozenset(real))

    plan = optimize(contracted, card)
    width = max(sum(_width(query, alias) for alias in group), 1)
    scan = sum(rows[table] * _width(query, alias) for alias, table in query.tables.items() if alias not in group)
    return {"scan": float(scan + view_rows * width), "join": float(plan_cost(plan, card)), "query": 1.0}


def build_features(view: JoinQuery, estimator: Estimator, rows: dict[str, float]) -> dict[str, float]:
    """Work of storing ``view`` (a join without filters on the rows it keeps): scan, join and write."""

    card = lambda subset: estimator.estimate(view, subset)  # noqa: E731
    plan = optimize(view, card)
    size = estimator.estimate(view, frozenset(view.tables))
    scan = sum(rows[table] * _width(view, alias) for alias, table in view.tables.items())
    return {
        "scan": float(scan),
        "join": float(plan_cost(plan, card)),
        "write": float(size * max(len(view.select), 1)),
        "query": 1.0,
        "rows": float(size),
    }


__all__ = ["VIEW", "build_features", "columns_used", "contract", "query_features", "query_over_view_features"]
