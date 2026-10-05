"""Derived-table rules: computed projections, grouped joins and structural near misses."""

from ._base import expand


TEMPLATES = [
    # Lift a deterministic computed output above an outer-join projection that an aggregate reads.
    "SELECT d.x + 1 AS value, COUNT(*) AS n FROM (SELECT t.x + u.w AS x FROM t LEFT JOIN u ON t.id = u.k) AS d GROUP BY d.x + 1",
    # The derived expression stays below the boundary without a grouped/aggregate consumer.
    "SELECT d.x FROM (SELECT t.x + u.w AS x FROM t LEFT JOIN u ON t.id = u.k) AS d",
    # A pass-through projection can be replaced with its base table.
    "SELECT d.x FROM (SELECT t.x AS x, t.y AS unused FROM t) AS d",
    # A computed projection over one plain table can be inlined into its consumer.
    "SELECT d.x + 1 AS value FROM (SELECT t.y * 2 AS x FROM t) AS d",
    # A filtered projection can merge into its grouped consumer.
    "SELECT d.k, SUM(d.v) AS total FROM (SELECT t.y AS k, t.x AS v FROM t WHERE t.x > 0) AS d GROUP BY d.k",
    # A derived projection of an outer join can be flattened into a grouped query.
    "SELECT d.x, SUM(d.w) AS total FROM (SELECT t.x, u.w FROM t LEFT JOIN u ON t.id = u.k) AS d GROUP BY d.x",
    # Filters and row limits keep their derived-table boundary.
    "SELECT d.x FROM (SELECT t.x AS x FROM t WHERE t.y > 0 LIMIT 2) AS d",
    # DISTINCT makes projection pruning observable.
    "SELECT DISTINCT d.x FROM (SELECT t.x AS x, t.y AS y FROM t) AS d",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "derived_structural")
