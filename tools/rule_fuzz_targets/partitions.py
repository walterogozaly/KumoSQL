"""Partition recombination: ``recombine_partitions`` merges UNION ALL branches that are one query split by a filter,
so ``Q WHERE p UNION ALL Q WHERE NOT p UNION ALL Q WHERE p IS NULL`` is ``Q``, and folds a global aggregate over a
UNION ALL of global aggregates back onto the single query the branches were split from.

The partition check runs in three-valued logic over the branches' atoms, so the templates cover a complete partition
(``p`` / ``NOT p`` / ``p IS NULL``), an incomplete one that only merges into an OR, and the ``IN`` / ``BETWEEN`` /
``<>`` atom shapes that decide whether the three branches cover every row.
"""

from ._base import expand

ATOM = "{t.x > 1|t.y = 2|t.id <> 3|t.x IN (1, 2)|t.x BETWEEN 1 AND 2|t.x IS NOT NULL}"
SAME = "{t.x|t.y|t.id}"
TAIL = "{|JOIN|LEFT JOIN}"

TEMPLATES = [
    # -- a complete three-way partition of one query: the branches recombine to the unfiltered query
    f"SELECT t.id, t.x FROM t WHERE {ATOM} UNION ALL SELECT t.id, t.x FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id, t.x FROM t WHERE {ATOM} IS NULL",
    f"SELECT t.id FROM t WHERE {ATOM} UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id FROM t WHERE {SAME} IS NULL",
    f"SELECT t.id FROM t JOIN u ON u.k = t.y WHERE {ATOM} UNION ALL SELECT t.id FROM t JOIN u ON u.k = t.y WHERE NOT ({ATOM}) UNION ALL SELECT t.id FROM t JOIN u ON u.k = t.y WHERE {SAME} IS NULL",
    f"SELECT t.id, t.x FROM (SELECT t.id, t.x FROM t WHERE {ATOM} UNION ALL SELECT t.id, t.x FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id, t.x FROM t WHERE {ATOM} IS NULL) AS d",
    f"SELECT COUNT(*) AS a FROM (SELECT t.id FROM t WHERE {ATOM} UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id FROM t WHERE {ATOM} IS NULL) AS d",
    # -- a four-way partition, and one branch that reads a different column's NULLs
    f"SELECT t.id FROM t WHERE t.x > 1 UNION ALL SELECT t.id FROM t WHERE t.x = 1 UNION ALL SELECT t.id FROM t WHERE t.x < 1 UNION ALL SELECT t.id FROM t WHERE t.x IS NULL",
    f"SELECT t.id FROM t WHERE t.x > 1 UNION ALL SELECT t.id FROM t WHERE t.y IS NULL UNION ALL SELECT t.id FROM t WHERE NOT (t.x > 1)",
    # -- an incomplete partition: the branches stay disjoint but the OR is not TRUE for every row
    f"SELECT t.id FROM t WHERE {ATOM} UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM})",
    f"SELECT t.id FROM t WHERE {ATOM} UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id FROM t WHERE {ATOM} IS NULL",
    # -- a global aggregate over a UNION ALL of global aggregates, recomputed once
    f"SELECT SUM(d.c) AS a FROM (SELECT COUNT(*) AS c FROM t WHERE {ATOM} UNION ALL SELECT COUNT(*) AS c FROM t WHERE NOT ({ATOM}) UNION ALL SELECT COUNT(*) AS c FROM t WHERE {ATOM} IS NULL) AS d",
    f"SELECT {{SUM(d.c)|MAX(d.c)|MIN(d.c)}} AS a FROM (SELECT COUNT(t.x) AS c FROM t WHERE {ATOM} UNION ALL SELECT COUNT(t.x) AS c FROM t WHERE NOT ({ATOM}) UNION ALL SELECT COUNT(t.x) AS c FROM t WHERE {ATOM} IS NULL) AS d",
    # -- near misses: a guard must decline each of these
    # Two different queries are not one query split by a filter, so the aggregate is not recomputed onto either.
    f"SELECT SUM(d.c) AS a FROM (SELECT COUNT(*) AS c FROM t UNION ALL SELECT COUNT(*) AS c FROM u) AS d",
    f"SELECT SUM(d.a) AS a, SUM(d.b) AS b FROM (SELECT SUM(t.x) AS a, MAX(t.y) AS b FROM t UNION ALL SELECT SUM(t.x) AS a, MAX(t.y) AS b FROM u) AS d",
    # A branch that aggregates is not a filterable branch: its WHERE cannot move between branches.
    f"SELECT t.id, SUM(t.x) AS s FROM t WHERE {ATOM} UNION ALL SELECT t.id, SUM(t.x) AS s FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id, SUM(t.x) AS s FROM t WHERE {ATOM} IS NULL",
    # p and NOT p are disjoint but do not cover a NULL row.
    f"SELECT t.id FROM t WHERE t.x > 1 UNION ALL SELECT t.id FROM t WHERE t.x <= 1",
    f"SELECT t.id FROM t WHERE {ATOM} AND t.y > 2 UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM})",
    # A duplicated branch is left beside the unfiltered query rather than merged into an OR.
    f"SELECT t.id FROM t WHERE {ATOM} UNION ALL SELECT t.id FROM t WHERE NOT ({ATOM}) UNION ALL SELECT t.id FROM t WHERE {ATOM}",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "partitions")