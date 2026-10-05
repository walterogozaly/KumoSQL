"""The variants inside the eager-aggregation, join and HAVING rules that report as one rule each:
``unnest_grouped_source``, ``flatten_grouped_join``, ``pull_up_aggregate`` (``eager_aggregation``),
``propagate_grouped_join_facts`` (``grouped_join_facts``), ``key_having_to_where`` (``having_rules``) and
``join_rewrites`` (foreign key, HAVING, nested join group, equalities through a LEFT JOIN). Each shape comes
with near misses that a guard must decline (a global SUM of COUNT, a hidden grouping key, an outer join, an
aggregate that sees multiplicities, ...)."""

from ._base import expand
from ._variants import fire_cases

PSUM = "(SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid)"
PCNT = "(SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid)"
PBOTH = "(SELECT p.tid AS k, SUM(p.n) AS s, COUNT(*) AS c, COUNT(p.n) AS cn, MAX(p.n) AS m, MIN(p.n) AS lo FROM p GROUP BY p.tid)"

# --- unnest_grouped_source ----------------------------------------------------------------------------------------
UNNEST = [
    f"SELECT t.id, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.id",
    f"SELECT t.id, SUM(g.s * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.id",
    f"SELECT t.y, SUM(g.c) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.c * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT SUM(g.c) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k",
    f"SELECT COALESCE(SUM(g.c), 0) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 1",
    f"SELECT NULLIF(SUM(g.cn), 0) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 1",
    f"SELECT SUM(g.cn) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 1",
    f"SELECT t.y, SUM(g.cn) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.cn * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, {{MAX|MIN}}(g.{{m|lo}}) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a, MAX(g.m) AS b, SUM(g.c * 2) AS d FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, COUNT(DISTINCT g.k) AS a, SUM(DISTINCT t.x) AS b, MAX(t.x) AS m FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y HAVING SUM(g.c) > 1",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 0 AND g.k > 1 GROUP BY t.y",
    f"SELECT g.k, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY g.k",
    f"SELECT t.y, SUM(g.s) AS a FROM t, {PBOTH} AS g WHERE t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM {PBOTH} AS g JOIN t ON t.id = g.k JOIN u ON u.k = t.y GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.b GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p JOIN u ON u.k = p.tid GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, p.b AS j, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid + 1 AS k, SUM(p.n) AS s FROM p GROUP BY p.tid + 1) AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.x = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT tid AS k, SUM(n) AS s FROM p GROUP BY tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT tid AS k, SUM(n) AS s FROM p JOIN u ON u.k = p.tid GROUP BY tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid IN (SELECT u.k FROM u) GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid) GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n * t.x) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p, UNNEST([1, 2]) AS z GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g(k, s) ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, p.tid AS k2, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s, MAX(SUM(p.n)) AS z FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, p.n AS s FROM p GROUP BY p.tid, p.n) AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s * g.c) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    # near misses: COUNT/AVG/plain SUM of a non-additive value, outer joins, a condition on an aggregate, DISTINCT inside,
    # a HAVING or LIMIT inside, an unqualified column, a second aggregated source
    f"SELECT t.y, {{COUNT(*)|AVG(g.s)|SUM(g.m)|MAX(g.s)|MIN(g.c)|COUNT(g.s)|SUM(g.s + 1)}} AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM t {{LEFT JOIN|RIGHT JOIN|FULL JOIN}} {PBOTH} AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE g.s > 1 GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k AND g.c > 1 GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(DISTINCT p.n) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid HAVING COUNT(*) > 1) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid ORDER BY k LIMIT 3) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT DISTINCT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY ROLLUP(p.tid)) AS g ON t.id = g.k GROUP BY t.y",
    f"SELECT t.y, SUM(g.s) AS a, SUM(h.c) AS b FROM t JOIN {PBOTH} AS g ON t.id = g.k JOIN {PBOTH} AS h ON t.id = h.k GROUP BY t.y",
    f"SELECT y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY y",
    f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y",
]

# --- flatten_grouped_join ----------------------------------------------------------------------------------------
GT = "(SELECT t.y AS k, SUM(t.x) AS s, COUNT(*) AS c, MAX(t.x) AS m, COUNT(t.x) AS cx FROM t GROUP BY t.y)"
GU = "(SELECT u.w AS k, COUNT(*) AS c, SUM(u.k) AS s, MIN(u.k) AS lo FROM u GROUP BY u.w)"
GP = "(SELECT p.tid AS k, COUNT(*) AS c, SUM(p.n) AS s FROM p GROUP BY p.tid)"
FLATTEN = [
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.c * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, h.s * g.c AS a, g.s * h.c AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.m AS a, h.lo AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c * i.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k JOIN {GP} AS i ON i.k = g.k",
    f"SELECT g.k, g.s * h.c * i.c AS a, i.s * g.c * h.c AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k JOIN {GP} AS i ON i.k = g.k",
    f"SELECT g.k, g.s * h.c + 1 AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, COALESCE(g.s * h.c, 0) AS a, GREATEST(g.s * h.c, 1) AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, CASE WHEN g.k > 1 THEN g.s * h.c ELSE 0 END AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, IF(g.k > 1, g.s * h.c, NULL) AS a, CAST(g.s * h.c AS FLOAT64) AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k AND g.k + h.k > 2",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k AND g.s > 1",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k WHERE g.k IS NOT NULL",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k AND g.k IN (SELECT p.tid FROM p)",
    f"SELECT g.k, g.* FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS g ON g.k = g.k",
    f"SELECT g.k, g.s * h.c * h.s AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid HAVING COUNT(*) > 1) AS i ON i.k = g.k",
    f"SELECT g.k, (g.s * h.c) AS a, g.k + 1 AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c * 2 AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c * g.k AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.cx * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k AS key, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k WHERE g.k > 1",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g, {GU} AS h WHERE g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g CROSS JOIN {GU} AS h",
    f"SELECT g.k, g.s AS a FROM {GT} AS g JOIN (SELECT u.w AS k FROM u GROUP BY u.w) AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k JOIN (SELECT p.tid AS k FROM p GROUP BY p.tid) AS i ON i.k = g.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k AND h.k > 1",
    "SELECT g.k, g.s * h.c AS a FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE t.x > 0 GROUP BY t.y) AS g JOIN (SELECT u.w AS k, COUNT(*) AS c FROM u JOIN p ON p.tid = u.k GROUP BY u.w) AS h ON g.k = h.k",
    "SELECT g.k, g.s * h.c AS a FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS s FROM t GROUP BY t.y, t.s) AS g JOIN (SELECT u.w AS k, COUNT(*) AS c FROM u GROUP BY u.w) AS h ON g.k = h.k",
    f"SELECT g.k, g.j, g.s * h.c AS a FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS s FROM t GROUP BY t.y, t.s) AS g JOIN {GU} AS h ON g.k = h.k",
    # near misses: a non-key condition, two sums multiplied, a hidden key, a sum without the other side's count, a HAVING,
    # a derived table that is a plain table, an outer join, an unqualified column, a source with no aggregate read as a count
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k AND g.s > h.s",
    f"SELECT g.k, g.s * h.s AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.m * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g {{LEFT JOIN|RIGHT JOIN|FULL JOIN}} {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN (SELECT u.w AS k, COUNT(*) AS c FROM u GROUP BY u.w HAVING COUNT(*) > 1) AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN u AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = g.k)",
    f"SELECT k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h USING (k)",
    f"SELECT DISTINCT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k ORDER BY g.k LIMIT 2",
    f"SELECT g.k, g.s / h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    f"SELECT SUM(g.s) AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k",
    # a projection over the join, read by _merge_projection_over_grouped_join first
    f"SELECT d.a FROM (SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k) AS d WHERE d.k > 0",
    f"SELECT d.a + 1 AS b FROM (SELECT g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k) AS d",
]

# --- pull_up_aggregate ---------------------------------------------------------------------------------------------
PULL = [
    f"SELECT t.id, t.x, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k",
    f"SELECT t.s, g.c, g.m FROM t JOIN {PBOTH} AS g ON g.k = t.id",
    f"SELECT t.id, g.s * 2 + g.c AS z FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 0",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE g.c > 1",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k AND g.s > t.x",
    f"SELECT t.id, g.k FROM t, {PBOTH} AS g WHERE t.id = g.k",
    f"SELECT t.id, g.cn FROM {PBOTH} AS g JOIN t ON g.k = t.id",
    f"SELECT t.id, g.s FROM t JOIN {PSUM} AS g ON t.y = g.k",
    f"SELECT t.id, g.c FROM t JOIN {PCNT} AS g ON t.y = g.k WHERE t.s = 'a'",
    f"SELECT t.id, g.c FROM t JOIN {PCNT} AS g ON t.id = g.k AND t.y = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, p.b AS j, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k AND t.x = g.j",
    "SELECT t.id, g.s, g.j FROM t JOIN (SELECT p.tid AS k, p.b AS j, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k AND t.s = g.j",
    "SELECT u.v, g.s FROM u JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS g ON u.k = g.k",
    "SELECT u.v, g.s FROM u JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE t.id > 1 GROUP BY t.y) AS g ON u.k = g.k WHERE u.w IS NOT NULL",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p JOIN u ON u.k = p.tid GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.b GROUP BY p.tid) AS g ON t.id = g.k",
    # near misses: LEFT/RIGHT joins, a hidden key, a non-key table, a missing tie, an aggregate or ORDER BY outside, an unqualified column
    f"SELECT t.id, g.s FROM t {{LEFT JOIN|RIGHT JOIN|FULL JOIN}} {PBOTH} AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k",
    "SELECT p2.n, g.s FROM p AS p2 JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g ON p2.tid = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id + 1 = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON g.k > t.id",
    f"SELECT t.id, g.s FROM t CROSS JOIN {PBOTH} AS g",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k JOIN u ON u.k = t.y",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k AND g.k = t.x",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k AND t.id = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE g.s > (SELECT AVG(p.n) FROM p)",
    f"SELECT t.id, g.s FROM t AS g JOIN {PBOTH} AS g ON g.id = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k AND g.lo IS NOT NULL",
    f"SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s, p.b AS j FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY ALL",
    f"SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p GROUP BY p.tid) AS g ON t.id = g.k AND g.k > ALL (SELECT u.k FROM u)",
    f"SELECT t.id, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.id",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k ORDER BY t.id",
    f"SELECT id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k",
    f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.y IN (SELECT u.k FROM u)",
    f"SELECT t.id, g.s, h.c FROM t JOIN {PBOTH} AS g ON t.id = g.k JOIN {PBOTH} AS h ON t.id = h.k",
    f"SELECT DISTINCT t.y, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k",
    f"SELECT g.* FROM t JOIN {PBOTH} AS g ON t.id = g.k",
]

# --- propagate_grouped_join_facts --------------------------------------------------------------------------------
FACTS = [
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid {>|>=|<|<=|=|<>} 2 GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE 2 {<|>=|=} p.tid GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 AND p.tid < 5 GROUP BY p.tid) AS g ON g.k = t.id",
    "SELECT t.id FROM t, (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g WHERE t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN u ON u.k = t.y JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON g.k = t.id AND u.k = t.y",
    "SELECT t.id, u.v FROM t JOIN u ON t.x = u.w JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON g.k = u.w",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k, p.id AS i FROM p WHERE p.tid > 1) AS g ON t.id = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON t.id = g.k JOIN (SELECT u.k AS j FROM u WHERE u.k < 5) AS h ON h.j = g.k",
    "SELECT t.id FROM t JOIN (SELECT d.k FROM (SELECT p.tid AS k FROM p WHERE p.tid > 1) AS d) AS g ON t.id = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON t.id = g.k JOIN (SELECT q.tid AS k, MAX(q.n) AS m FROM p AS q WHERE q.tid < 9 GROUP BY q.tid) AS h ON t.id = h.k",
    # near misses: a global aggregate, a filter on a non-key, a string literal or arithmetic, a FLOAT column, an outer join
    "SELECT t.id, g.s FROM t JOIN (SELECT SUM(p.n) AS s FROM p WHERE p.tid > 1) AS g ON t.id = g.s",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.n > 1 GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid + 1 > 2 GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid, p.b) AS g ON t.id = g.k",
    "SELECT t.id, g.s FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY ROLLUP(p.tid)) AS g ON t.id = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k FROM p WHERE p.tid > 1) AS g ON t.f = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.n AS k FROM p WHERE p.n > 1) AS g ON t.f = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k FROM p WHERE p.tid > 1) AS g ON t.s = CAST(g.k AS STRING)",
    "SELECT t.id FROM t {LEFT JOIN|RIGHT JOIN|FULL JOIN} (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON t.id <> g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g(k, s) ON t.id = g.k",
    "SELECT t.id FROM t JOIN (SELECT p.tid AS k FROM p WHERE p.tid > 1 AND RAND() < 2) AS g ON t.id = g.k",
]

# --- key_having_to_where --------------------------------------------------------------------------------------------
HAVING = [
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING t.y {> 1|= 1|<> 1|IS NULL|IS NOT NULL|IN (1, 2)|BETWEEN 1 AND 3} {|AND SUM(t.x) > 1}",
    "SELECT t.y, t.s, COUNT(*) AS c FROM t GROUP BY t.y, t.s HAVING t.y > 1 AND t.s = 'a' AND COUNT(*) > 1",
    "SELECT t.y, COUNT(*) AS c FROM t WHERE t.x > 0 GROUP BY t.y HAVING t.y > 1",
    "SELECT t.y, MAX(t.x) AS m FROM t GROUP BY t.y HAVING t.y IS NULL OR t.y > 2",
    "SELECT t.y, MAX(t.x) AS m FROM t GROUP BY t.y HAVING NOT (t.y > 2)",
    "SELECT y, MAX(x) AS m FROM t GROUP BY y HAVING y > 1",
    "SELECT t.y, MAX(t.x) AS m FROM t GROUP BY t.y HAVING UPPER(CAST(t.y AS STRING)) = '1'",
    "SELECT t.y, MAX(t.x) AS m FROM t GROUP BY t.y HAVING t.y > 1 AND t.y < 5 AND MAX(t.x) > 1",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING (t.y > 1 AND SUM(t.x) > 0) OR (t.y > 3)",
    "SELECT t.y, t.x, COUNT(*) AS c FROM t GROUP BY t.y, t.x HAVING t.y = t.x",
    "SELECT d.y, COUNT(*) AS c FROM (SELECT t.y, t.x FROM t) AS d GROUP BY d.y HAVING d.y > 1",
    # near misses: an alias that shadows the key, a global aggregate, ROLLUP, a subquery or aggregate in the conjunct,
    # a non-key column, a key expression, a window
    "SELECT t.x AS y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING y > 1",
    "SELECT t.y + 1 AS y, COUNT(*) AS c FROM t GROUP BY t.y HAVING y > 2",
    "SELECT COUNT(*) AS c FROM t HAVING COUNT(*) > 1",
    "SELECT COUNT(*) AS c FROM t HAVING 1 > 2",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY ROLLUP(t.y) HAVING t.y > 1",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y IN (SELECT u.k FROM u)",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y > (SELECT MIN(u.k) FROM u)",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING EXISTS (SELECT 1 FROM u WHERE u.k = t.y)",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y > RAND()",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y + 1 > 2",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y + 1, t.y HAVING t.y > 2",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING MAX(t.y) > 2",
    "SELECT t.y, COUNT(*) AS c, ROW_NUMBER() OVER (ORDER BY t.y) AS r FROM t GROUP BY t.y HAVING t.y > 1",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.x > 1",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY ALL HAVING t.y > 1",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY 1 HAVING t.y > 1",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y > 1 QUALIFY ROW_NUMBER() OVER (ORDER BY t.y) < 3",
]

# --- join_rewrites (the constraints below make the foreign key variants applicable) ---------------------------------
JOINS = [
    # a LEFT JOIN along a foreign key, from a NOT NULL child column
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON t.id = p.tid WHERE p.n > 1",
    "SELECT p.id, t.s, u.v FROM p LEFT JOIN t ON p.tid = t.id LEFT JOIN u ON t.y = u.k",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id AND t.x > 1",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id AND p.n > 1",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id OR p.n > 1",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.id = t.id",
    "SELECT t.id, p.n FROM t LEFT JOIN p ON p.tid = t.id",
    "SELECT t.id, u.v FROM t LEFT JOIN u ON t.y = u.k",
    "SELECT q.id, t.s FROM (SELECT p.id, p.tid FROM p) AS q LEFT JOIN t ON q.tid = t.id",
    "SELECT p.id, t.s, p2.n FROM p LEFT JOIN t ON p.tid = t.id LEFT JOIN p AS p2 ON p2.tid = t.id",
    "SELECT p.id, t.s FROM t RIGHT JOIN p ON p.tid = t.id",
    "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id LEFT JOIN p AS p2 ON p2.tid = p.tid",
    "SELECT p.id, p3.n FROM t LEFT JOIN p AS p2 ON p2.tid = t.id LEFT JOIN p AS p3 ON p3.tid = p2.tid",
    # a HAVING that rejects the groups made only of null-extended rows
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) {>|>=|<>} {0|1}",
    "SELECT t.x, SUM(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(u.w) {> 10|< 10|= 10|IS NOT NULL}",
    "SELECT t.x, MAX(u.w) AS c, MIN(u.w + 1) AS d FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING MAX(u.w) > 1",
    "SELECT t.x, AVG(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING AVG(u.w) > 1",
    "SELECT t.x, COUNT(DISTINCT u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(DISTINCT u.w) > 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) > 0 AND t.x > 1",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING NOT (COUNT(u.w) = 0)",
    "SELECT t.x, COUNT(u.w) AS c, SUM(u.w) AS s FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(u.w) > 0 OR COUNT(u.w) > 2",
    "SELECT t.x, SUM(u.w + t.x) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(u.w + t.x) > 0",
    "SELECT SUM(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k HAVING SUM(u.w) > 0",
    "SELECT t.x, SUM(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k LEFT JOIN p ON p.tid = t.id GROUP BY t.x HAVING SUM(u.w) > 0",
    # near misses: COUNT(*), a NULL-handling argument, a HAVING true for empty groups, the other side's columns, FULL/RIGHT joins
    "SELECT t.x, COUNT(*) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) > 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(*) > 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) = 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) >= 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) < 1",
    "SELECT t.x, SUM(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(u.w) IS NULL",
    "SELECT t.x, SUM(COALESCE(u.w, 0)) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(COALESCE(u.w, 0)) > 0",
    "SELECT t.x, SUM(t.id) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(t.id) > 0",
    "SELECT t.x, MAX(u.w) AS c, COUNT(t.y) AS d FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING MAX(u.w) > 1",
    "SELECT t.x, COUNT(u.w) AS c FROM t {RIGHT JOIN|FULL JOIN} u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) > 0",
    "SELECT t.x, COUNT(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(u.w) > 0 ORDER BY t.x LIMIT 2",
    # a nested join group read as a derived table
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.w, p.n FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k AND p.n > 1",
    "SELECT t.id FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT COUNT(*) AS c, COUNT(a.w) AS d FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, p.n FROM (u AS a JOIN p ON p.tid = a.k) RIGHT JOIN t ON t.y = a.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a LEFT JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.w FROM t JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p USING (k)) ON t.y = a.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k AND p.id = t.id) ON t.y = a.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = a.k)",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS t JOIN p ON p.tid = t.k) ON t.y = t.k",
    "SELECT t.id, w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.* FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k LEFT JOIN (u AS b JOIN p AS p2 ON p2.tid = b.k) ON t.x = b.k",
    "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p AS a ON p.tid = a.k) ON t.y = a.k",
    "SELECT t.id, a.zz FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k",
    # equalities copied into the ON clause of a LEFT JOIN
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y {=|>|<=|<>} 2",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y BETWEEN 1 AND 3",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y IN (1, 2) AND t.x > 1",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON u.k = t.y WHERE t.y = 2 AND t.y > 0",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k AND t.x = u.w WHERE t.y = 2 AND t.x = 1",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y = 2 AND u.w IS NULL",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y = 2 OR t.x = 1",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y = t.x",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y IN (SELECT p.tid FROM p)",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y + 1 = u.k WHERE t.y = 2",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE u.k = 2",
    "SELECT t.id, u.w, p.n FROM t LEFT JOIN u ON t.y = u.k LEFT JOIN p ON p.tid = t.id WHERE t.y = 2 AND t.id = 3",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.s = u.v WHERE t.s = 'a'",
]

# one query per rewrite that must make it fire (the tests trace these)
FIRES = [
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.s * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.c) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.c * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT COALESCE(SUM(g.c), 0) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE t.x > 1"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.cn) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.cn * t.x) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, MAX(g.m) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, MIN(g.lo) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y"),
    ("unnest_grouped_source", f"SELECT t.y, SUM(g.s) AS a FROM t JOIN {PBOTH} AS g ON t.id = g.k GROUP BY t.y HAVING SUM(g.c) > 1"),
    (
        "unnest_grouped_source",
        "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, p.b AS j, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k GROUP BY t.y",
    ),
    (
        "unnest_grouped_source",
        "SELECT t.y, SUM(g.s) AS a FROM t JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p JOIN u ON u.k = p.tid GROUP BY p.tid) AS g ON t.id = g.k GROUP BY t.y",
    ),
    ("flatten_grouped_join", f"SELECT g.k, g.s * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k"),
    ("flatten_grouped_join", f"SELECT g.k, g.c * h.c AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k"),
    ("flatten_grouped_join", f"SELECT g.k, g.m AS a, h.lo AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k"),
    (
        "flatten_grouped_join",
        f"SELECT g.k, g.s * h.c * i.c AS a, i.s * g.c * h.c AS b FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k JOIN {GP} AS i ON i.k = g.k",
    ),
    ("flatten_grouped_join", f"SELECT g.k, g.s * h.c + 1 AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k"),
    ("flatten_grouped_join", f"SELECT g.k, g.s * h.c * 2 AS a FROM {GT} AS g JOIN {GU} AS h ON g.k = h.k"),
    ("pull_up_aggregate", f"SELECT t.id, t.x, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k"),
    ("pull_up_aggregate", f"SELECT t.id, g.s FROM t JOIN {PBOTH} AS g ON t.id = g.k WHERE g.c > 1"),
    (
        "pull_up_aggregate",
        "SELECT t.id, g.s, g.j FROM t JOIN (SELECT p.tid AS k, p.b AS j, SUM(p.n) AS s FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k AND t.s = g.j",
    ),
    (
        "propagate_grouped_join_facts",
        "SELECT t.id, g.s FROM t JOIN u ON u.k = t.y JOIN (SELECT p.tid AS k, SUM(p.n) AS s FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g ON g.k = t.id AND u.k = t.y",
    ),
    ("propagate_grouped_join_facts", "SELECT t.id FROM t JOIN (SELECT p.tid AS k, p.id AS i FROM p WHERE p.tid > 1) AS g ON t.id = g.k"),
    ("key_having_to_where", "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING t.y > 1 AND SUM(t.x) > 1"),
    ("key_having_to_where", "SELECT t.y, t.s, COUNT(*) AS c FROM t GROUP BY t.y, t.s HAVING t.y IS NULL OR t.s = 'a'"),
    ("join_rewrites._fk_left_join_to_inner", "SELECT p.id, t.s FROM p LEFT JOIN t ON p.tid = t.id"),
    ("join_rewrites._having_left_join_to_inner", "SELECT t.x, SUM(u.w) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING SUM(u.w) > 10"),
    ("join_rewrites._having_left_join_to_inner", "SELECT t.x, MAX(u.w) AS c, MIN(u.w + 1) AS d FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x HAVING MAX(u.w) > 1"),
    ("join_rewrites._nested_join_to_derived", "SELECT t.id, a.w FROM t LEFT JOIN (u AS a JOIN p ON p.tid = a.k) ON t.y = a.k"),
    ("join_rewrites._map_equalities_into_left_join", "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k WHERE t.y = 2 AND u.w IS NULL"),
    ("join_rewrites._map_equalities_into_left_join", "SELECT t.id, u.w FROM t LEFT JOIN u ON t.y = u.k AND t.x = u.w WHERE t.y = 2 AND t.x = 1"),
]

TEMPLATES = UNNEST + FLATTEN + PULL + FACTS + HAVING + JOINS

# the foreign key variants of join_rewrites need a NOT NULL child column that references its parent
_JOIN_START = len(UNNEST) + len(FLATTEN) + len(PULL) + len(FACTS) + len(HAVING)


def _constrain(case: dict) -> None:
    """p.tid is NOT NULL and references t.id; t.y is NOT NULL and references u.k when u.k is a key."""

    constraints = case["constraints"]
    constraints.setdefault("p", {}).setdefault("not_null", [])
    if "tid" not in constraints["p"]["not_null"]:
        constraints["p"]["not_null"].append("tid")
    constraints["p"]["foreign_keys"] = [[["tid"], "t", ["id"]]]
    if "u" in constraints and constraints["u"].get("keys"):
        constraints["t"].setdefault("foreign_keys", [])
        if not constraints["t"]["foreign_keys"]:
            constraints["t"]["foreign_keys"].append([["y"], "u", ["k"]])
        if "y" not in constraints["t"]["not_null"]:
            constraints["t"]["not_null"].append("y")


def cases(seed: int, count: int) -> list[dict]:
    out = expand(TEMPLATES, seed, count, "eager_variants")
    for case in out:
        if int(case["source"].split(":")[2]) >= _JOIN_START:
            _constrain(case)
    return out


def fire_list() -> list[tuple[str, dict]]:
    return fire_cases("eager_variants", FIRES, lambda case, target: _constrain(case) if target.startswith("join_rewrites") else None)
