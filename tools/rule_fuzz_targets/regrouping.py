"""Regrouping variants and guard near misses for the rarely exercised aggregate rules.

Cover equal and coarser grouping keys, global aggregates, DISTINCT inputs,
arithmetic over several aggregates, and COUNT/HAVING/filter/grouping-set guards.
"""

from ._base import expand


TEMPLATES = [
    # One inner row per outer group, including an empty global aggregate.
    "SELECT q.k, SUM(q.n) AS n FROM (SELECT t.y AS k, COUNT({*|t.x}) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
    "SELECT q.k, {SUM|MIN|MAX}(q.s) AS s FROM (SELECT t.y AS k, {SUM|MIN|MAX}(t.x) AS s FROM t GROUP BY t.y) q GROUP BY q.k",
    "SELECT SUM(q.n) AS n FROM (SELECT COUNT({*|t.x}) AS n FROM t) q",
    "SELECT {MIN|MAX}(q.s) AS s FROM (SELECT {MIN|MAX}(t.x) AS s FROM t) q",
    "SELECT q.k, SUM(DISTINCT q.n) AS n FROM (SELECT t.y AS k, COUNT(*) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
    # Arithmetic takes several leaves through the regrouping rules together.
    "SELECT q.k, SUM(q.s) {+|-} SUM(q.n) AS a FROM (SELECT t.y AS k, SUM(t.x) AS s, COUNT(t.x) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
    "SELECT q.k, SUM(q.s) / (SUM(q.n) + 1) AS a FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS s, COUNT(t.x) AS n FROM t GROUP BY t.y, t.s) q GROUP BY q.k",
    "SELECT SUM(q.s) {+|-} MAX(q.m) AS a FROM (SELECT t.y AS k, SUM(t.x) AS s, MAX(t.x) AS m FROM t GROUP BY t.y) q",
    "SELECT q.k, SUM(q.s) * 2 + COUNT(q.j) AS a FROM (SELECT t.y AS k, t.x AS j, SUM(t.x) AS s FROM t GROUP BY t.y, t.x) q GROUP BY q.k",
    "SELECT q.k, SUM(q.s) + 1 AS a, SUM(q.n) - 1 AS b FROM (SELECT t.y AS k, SUM(t.x) AS s, COUNT(t.x) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
    # Guard cases: COUNT is not a sum; global SUM of grouped COUNT is NULL on empty input.
    "SELECT q.k, COUNT(q.n) AS n FROM (SELECT t.y AS k, COUNT(*) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
    "SELECT SUM(q.n) AS n FROM (SELECT t.y AS k, COUNT(*) AS n FROM t GROUP BY t.y) q",
    "SELECT q.k, SUM(q.s) + SUM(q.n) AS a FROM (SELECT t.y AS k, SUM(t.x) AS s, COUNT(*) AS n FROM t GROUP BY t.y HAVING COUNT(*) > 1) q GROUP BY q.k",
    "SELECT q.k, SUM(q.s) + 1 AS a FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) q WHERE q.s > 0 GROUP BY q.k",
    "SELECT q.k, SUM(q.n) AS n FROM (SELECT t.y AS k, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((t.y), (t.y))) q GROUP BY q.k",
    "SELECT q.k, SUM(q.s) + SUM(q.n) AS a FROM (SELECT t.y AS k, SUM(DISTINCT t.x) AS s, COUNT(DISTINCT t.x) AS n FROM t GROUP BY t.y) q GROUP BY q.k",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "regrouping")
