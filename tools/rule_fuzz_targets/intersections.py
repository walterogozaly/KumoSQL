"""Counted intersections: ``collapse_counted_intersection`` and ``collapse_named_counted_intersection`` rewrite
``SELECT k, COUNT(*) = n`` over ``n`` UNION ALL branches that each group by exactly those columns into an
``INTERSECT`` of the branches' ``SELECT DISTINCT``, because each branch outputs a key at most once so the count is
the number of branches holding it.

The near misses turn each condition off in turn: a count that does not match the number of branches, an output the
outer query does not group by, a branch that groups by more than it selects, branches that are already ``DISTINCT``,
an outer ``WHERE``, and a second ``HAVING`` conjunct.
"""

from ._base import expand

# Each alternative selects exactly the columns it groups by, which is what the shape check requires; the columns are
# spelled out together because ``_base.expand`` cannot nest choice groups and ``GROUP BY`` ordinals add no coverage.
BRANCHES = "{SELECT t.x AS x, t.y AS y FROM t GROUP BY t.x, t.y|SELECT t.y AS x, t.x AS y FROM t GROUP BY t.y, t.x|SELECT t.id AS x, t.y AS y FROM t GROUP BY t.id, t.y}"

TEMPLATES = [
    # -- renamed outputs: the rule carries the output names onto the INTERSECT operands
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2",
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w UNION ALL SELECT p.id AS x, p.tid AS y FROM p GROUP BY p.id, p.tid) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 3",
    f"SELECT u.x AS xx FROM (SELECT t.x AS x FROM t GROUP BY t.x UNION ALL SELECT u.k AS x FROM u GROUP BY u.k) AS u GROUP BY u.x HAVING COUNT(*) = 2",
    # -- the outer group order does not have to match the branch order
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.y, u.x HAVING COUNT(*) = 2",
    # -- bare outputs: the same shape without the renamed aliases
    f"SELECT u.x, u.y FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2",
    f"SELECT u.x FROM (SELECT t.x AS x FROM t GROUP BY t.x UNION ALL SELECT u.k AS x FROM u GROUP BY u.k) AS u GROUP BY u.x HAVING COUNT(*) = 2",
    # -- near misses: a guard must decline each of these
    # n must be the number of branches, or COUNT(*) = n no longer means "every branch".
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 3",
    # A branch that groups by a column it does not select can hold a key more than once.
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w, u.v) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2",
    # Branch columns that are already DISTINCT are not the counted-partials shape.
    f"SELECT u.x AS xx, u.y AS yy FROM (SELECT DISTINCT t.x AS x, t.y AS y FROM t UNION ALL SELECT DISTINCT u.k AS x, u.w AS y FROM u) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2",
    # The outer query must be a bare grouped count.
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u WHERE u.x > 1 GROUP BY u.x, u.y HAVING COUNT(*) = 2",
    f"SELECT u.x AS xx, u.y AS yy FROM ({BRANCHES} UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2 AND u.x > 0",
    # The selected branch columns must be exactly the grouped ones, in any order but all of them.
    f"SELECT u.x AS xx, u.y AS yy FROM (SELECT t.x AS x, t.id AS y FROM t GROUP BY t.x, t.id UNION ALL SELECT u.k AS x, u.w AS y FROM u GROUP BY u.k, u.w) AS u GROUP BY u.x, u.y HAVING COUNT(*) = 2",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "intersections")