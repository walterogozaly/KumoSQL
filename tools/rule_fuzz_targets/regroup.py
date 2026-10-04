"""Regrouping rules: ``_collapse_aggregate`` (a regrouping of an already-grouped subquery), ``regroup_arithmetic``
(arithmetic over the aggregates such a regrouping folds) and the ``_roll_up_aggregate`` / ``_regroup_distinct``
regroupers it drives, with the guards that must stop each of them.

The collapses are the shapes with an inner query that outputs only a key and aggregates, because every outer group
then holds exactly one inner row: SUM, MIN and MAX of that row return it, and COUNT of it does not.
"""

from ._base import expand

# Inner queries that output one key and aggregates only, each aliasing its key to ``k``.
KEY_COUNT = "SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y"
KEY_MIN = "SELECT t.y AS k, MIN(t.x) AS c FROM t GROUP BY t.y"
GLOBAL_COUNT = "SELECT COUNT(*) AS c FROM t"
# Inner queries whose aggregates ``regroup_arithmetic`` folds before putting the arithmetic back.
SUM_PAIR = "SELECT t.y AS k, SUM(t.x) AS a, SUM(t.f) AS b FROM t GROUP BY t.y"
COUNT_PAIR = "SELECT t.y AS k, COUNT(*) AS a, COUNT(t.x) AS b FROM t GROUP BY t.y"
MIN_PAIR = "SELECT t.y AS k, MIN(t.x) AS a, MIN(t.f) AS b FROM t GROUP BY t.y"
MAX_PAIR = "SELECT t.y AS k, MAX(t.x) AS a, MAX(t.f) AS b FROM t GROUP BY t.y"
DISTINCT_PAIR = "SELECT DISTINCT t.y AS k, t.x AS x FROM t"

TEMPLATES = [
    # -- _collapse_aggregate: the outer regrouping folds into the inner one
    f"SELECT g.k, SUM(g.c) AS a FROM ({KEY_COUNT}) AS g GROUP BY g.k",
    f"SELECT g.k, {{SUM(g.c)|MIN(g.c)}} AS a FROM (SELECT t.y AS k, {{COUNT(*)|MIN(t.x)|SUM(t.x)}} AS c FROM t GROUP BY t.y) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(g.a) AS a FROM (SELECT t.y AS k, SUM(t.x) AS a FROM t GROUP BY t.y) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(DISTINCT g.c) AS a FROM ({KEY_COUNT}) AS g GROUP BY g.k",
    f"SELECT {{SUM|MIN}}(g.c) AS a FROM ({GLOBAL_COUNT}) AS g",
    f"SELECT g.k, SUM(g.c) AS a, MIN(g.c) AS b FROM ({KEY_COUNT}) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g GROUP BY g.k, g.c + 0",
    # -- regroup_arithmetic: the arithmetic is split, the regrouping folds it, the arithmetic goes back
    f"SELECT g.k, SUM(g.a) / SUM(g.b) AS r FROM ({SUM_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, {{SUM(g.a) / SUM(g.b)|SUM(g.a) + SUM(g.b)|SUM(g.a) - SUM(g.b)|SUM(g.a) * SUM(g.b)}} AS r FROM ({SUM_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, {{MIN(g.a) / MIN(g.b)|MIN(g.a) + MIN(g.b)}} AS r FROM ({MIN_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, {{MAX(g.a) / MAX(g.b)|MAX(g.a) + MAX(g.b)}} AS r FROM ({MAX_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(g.a) / SUM(g.b) AS r FROM ({COUNT_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(g.a) / (SUM(g.b) + {{1|0}}) AS r FROM ({SUM_PAIR}) AS g GROUP BY g.k",
    f"SELECT g.k, SUM(g.a) / SUM(g.b) AS r FROM ({DISTINCT_PAIR}) AS g GROUP BY g.k",
    # -- near misses: a guard must decline each of these
    # COUNT of the one inner row is that row's count of one, not the aggregate it holds.
    f"SELECT g.k, COUNT(g.c) AS a FROM ({KEY_COUNT}) AS g GROUP BY g.k",
    # MAX of a COUNT is not the count, and MIN of a SUM is not the sum.
    f"SELECT {{MAX|MIN}}(g.c) AS a FROM ({GLOBAL_COUNT}) AS g",
    # The outer key must name an inner key output, not the inner key column.
    f"SELECT g.k, SUM(g.c) AS a FROM ({KEY_COUNT}) AS g GROUP BY t.y",
    # An inner output that is neither a key nor an aggregate is not part of the folding.
    f"SELECT g.k, SUM(g.c) AS a FROM (SELECT t.y AS k, t.x AS x, COUNT(*) AS c FROM t GROUP BY t.y, t.x) AS g GROUP BY g.k",
    # A group column that is not a key changes the number of rows per group.
    f"SELECT g.k, SUM(g.c) AS a FROM ({KEY_COUNT}) AS g GROUP BY g.k, g.c",
    # Arithmetic over a bare column reads a value the folded query no longer has.
    f"SELECT g.k, SUM(g.a) / g.b AS r FROM ({SUM_PAIR}) AS g GROUP BY g.k",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "regroup")