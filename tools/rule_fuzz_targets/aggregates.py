"""Aggregate, eager-aggregation, regrouping and keyed rules: pre-aggregated joins, aggregates over UNION ALL,
COUNT/SUM over grouped derived tables, global aggregates over empty input, keyed grouping and DISTINCT."""

from ._base import expand

AGG = "{COUNT(*)|COUNT(t.x)|SUM(t.x)|MIN(t.x)|MAX(t.x)|AVG(t.x)|COUNT(DISTINCT t.x)|SUM(t.f)|COUNTIF(t.x > 1)}"
AGG_Q = "{COUNT(*)|COUNT(q.x)|SUM(q.x)|MIN(q.x)|MAX(q.x)|SUM(q.n)|COUNT(DISTINCT q.x)}"
FILTER = "{t.x > 0|t.x IS NOT NULL|t.y = 1|t.s = 'a'|t.id < 3|FALSE|t.x = t.y}"

TEMPLATES = [
    f"SELECT t.y AS k, {AGG} AS a FROM t WHERE {FILTER} GROUP BY t.y",
    f"SELECT {AGG} AS a FROM t WHERE {FILTER}",
    f"SELECT t.id AS k, {AGG} AS a FROM t GROUP BY t.id",
    f"SELECT t.id, t.x, {AGG} AS a FROM t GROUP BY t.id, t.x",
    f"SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g",
    f"SELECT {{SUM|MAX|MIN|COUNT}}(g.a) AS a FROM (SELECT t.y AS k, {AGG} AS a FROM t GROUP BY t.y) AS g",
    f"SELECT {{SUM|COUNT}}(d.c) AS a FROM (SELECT COUNT(t.x) AS c FROM t UNION ALL SELECT COUNT(u.w) AS c FROM u) AS d",
    f"SELECT {{SUM|MAX|MIN|COUNT}}(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    f"SELECT {{SUM|MAX|MIN|COUNT}}(d.c) AS a FROM (SELECT {{MIN|MAX|SUM|COUNT}}(t.x) AS c FROM t UNION ALL SELECT {{MIN|MAX|SUM|COUNT}}(u.w) AS c FROM u) AS d",
    f"SELECT d.k, SUM(d.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    f"SELECT t.id, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.id",
    f"SELECT t.id, {{SUM|COUNT|MIN}}(g.s) AS a FROM t {{JOIN|LEFT JOIN}} (SELECT p.tid AS k, {{SUM(p.n)|COUNT(*)|MAX(p.n)}} AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.id",
    f"SELECT SUM(t.x * g.c) AS a FROM t JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid) AS g ON t.id = g.k",
    f"SELECT g.k, g.c * h.c AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS h ON g.k = h.k",
    f"SELECT t.y, {AGG} AS a FROM t GROUP BY t.y HAVING {{COUNT(*) >= 1|MIN(t.x) IS NOT NULL|SUM(t.x) > 1|t.y > 0|t.y IS NULL OR COUNT(*) > 1}}",
    f"SELECT COUNT(*) AS a FROM t WHERE EXISTS (SELECT {{COUNT(*)|SUM(u.w)|1}} FROM u {{WHERE u.k = t.y|WHERE u.w > t.x|}})",
    f"SELECT t.id FROM t WHERE {{EXISTS|NOT EXISTS}} (SELECT {{COUNT(*)|MAX(u.w)}} FROM u WHERE u.k = t.y {{|HAVING COUNT(*) > 1|HAVING MAX(u.w) > 1}})",
    f"SELECT t.y AS k, {{SUM(t.x)|MAX(t.x)}} AS a FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, {{SUM(u.w)|MAX(u.w)}} AS a FROM u GROUP BY u.k",
    f"SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 0 THEN 1 END) AS b FROM t",
    f"SELECT {{COUNT(*)|SUM(t.x)}} AS a FROM t WHERE t.x IS NOT NULL",
    f"SELECT COALESCE(SUM(c), 0) AS a FROM (SELECT COUNT(*) AS c FROM t UNION ALL SELECT COUNT(*) AS c FROM u) AS d",
    f"SELECT {{MIN|MAX}}(t.id) AS a, {{MIN|MAX}}(t.id + 1) AS b FROM t GROUP BY t.y",
    f"SELECT DISTINCT {{t.id|t.id, t.x|t.y|t.x, t.y}} FROM t {{WHERE t.id > 1|}}",
    f"SELECT DISTINCT t.y, {AGG} AS a FROM t GROUP BY t.y",
    f"SELECT t.y, COUNT(DISTINCT t.x) AS a, SUM(DISTINCT t.x) AS b FROM t GROUP BY t.y",
    f"SELECT t.x, SUM(q.s) AS a FROM t JOIN (SELECT t2.y AS k, SUM(t2.x) AS s FROM t AS t2 GROUP BY t2.y) AS q ON q.k = t.y GROUP BY t.x",
    f"SELECT SUM(a) AS a, COUNT(*) AS c FROM (SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y) AS q",
    f"SELECT AVG(t.x) AS a, SUM(t.x) / COUNT(t.x) AS b FROM t",
    f"SELECT {{SUM(t.x + 1)|SUM(t.x) + COUNT(t.x)|SUM(t.x * 2)|SUM(t.x) * 2}} AS a FROM t",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "aggregates")
