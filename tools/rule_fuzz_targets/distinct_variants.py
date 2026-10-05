"""The variants inside the DISTINCT rules that report as one rule: ``distinct_rules`` (membership dedup, merging a
grouped source, dropping a read-as-set DISTINCT, DISTINCT join to EXISTS, regrouping, parentheses, GROUP BY under
DISTINCT, DISTINCT over group keys, count casts), the dedup join rules (``drop_unread_outer_join``,
``strip_distinct_sources``), ``_push_distinct_into_sources`` and the single-SELECT CASE splits of
``split_distinct_select``. Each shape comes with near misses that one guard must decline (a LIMIT that sees
duplicates, a COUNT(*) that counts repeats, an unqualified column that resolves outward, ...)."""

from ._base import expand
from ._variants import fire_cases

# --- drop_membership_dedup -------------------------------------------------------------------------------------
MEMBERSHIP = [
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT DISTINCT u.w FROM u {|WHERE u.k > 1})",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u GROUP BY u.w)",
    "SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT DISTINCT u.w FROM u WHERE u.k = t.y)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT u.k, u.w FROM u WHERE u.w = t.x GROUP BY u.k, u.w)",
    "SELECT t.id, t.x IN (SELECT DISTINCT u.w FROM u) AS m FROM t",
    # near misses: a LIMIT, a window, an outer join, a HAVING or an aggregate sees the duplicates
    "SELECT t.id FROM t WHERE t.x IN (SELECT DISTINCT u.w FROM u ORDER BY u.w LIMIT {1|2|3})",
    "SELECT t.id FROM t WHERE t.x NOT IN (SELECT DISTINCT u.w FROM u ORDER BY u.w DESC LIMIT 2)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT w FROM (SELECT DISTINCT u.w, ROW_NUMBER() OVER (ORDER BY u.w) AS r FROM u) AS z WHERE r < 3)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT DISTINCT u.w FROM u LEFT JOIN p ON u.k = p.tid)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w HAVING COUNT(*) > 1)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT MAX(u.w) FROM u GROUP BY u.k)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w, u.k)",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT DISTINCT MAX(u.w) FROM u GROUP BY u.k)",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT DISTINCT u.w FROM u WHERE u.k IN (SELECT p.tid FROM p))",
    "SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY ROLLUP(u.w))",
    "SELECT t.id FROM t WHERE t.x IN (SELECT DISTINCT u.w FROM u OFFSET 1)",
]

# --- merge_grouped_source ----------------------------------------------------------------------------------------
MERGE = [
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(t.x) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c {>|>=|=} 1",
    "SELECT DISTINCT d.k, d.c FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1 AND d.k IS NOT NULL",
    "SELECT d.k FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y, t.s) AS d WHERE d.s > 0 GROUP BY d.k",
    "SELECT DISTINCT d.k + 1 AS k1 FROM (SELECT t.y AS k, MAX(t.x) AS m FROM t GROUP BY t.y, t.s HAVING COUNT(*) >= 1) AS d WHERE d.m {>|<} 2",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, MIN(t.x) AS m FROM t GROUP BY t.y, t.s) AS d WHERE d.m > 0 ORDER BY d.k",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, MIN(t.x) AS m FROM t GROUP BY t.y, t.s) AS d WHERE d.m > 0 ORDER BY d.m, d.k",
    "SELECT DISTINCT d.m FROM (SELECT t.y AS k, MIN(t.x) AS m FROM t WHERE t.id < 4 GROUP BY t.y, t.s) AS d",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1 AND d.k > 0",
    "SELECT DISTINCT d.k FROM (SELECT DISTINCT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1",
    "SELECT DISTINCT 1 AS one FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1",
    "SELECT DISTINCT d.k, d.k AS k2 FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1 AND d.c < 4",
    # near misses: a join, a subquery, a LIMIT, duplicate names, an aggregate or HAVING on the outer select
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d JOIN u ON u.k = d.k WHERE d.c > 1",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > (SELECT COUNT(*) FROM u)",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y ORDER BY c LIMIT 2) AS d WHERE d.c > 1",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY ROLLUP(t.y)) AS d WHERE d.c > 1",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d(k, c) WHERE d.c > 1",
    "SELECT DISTINCT d.k, COUNT(*) AS n FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1 GROUP BY d.k",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1 LIMIT 3",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS k, t.y AS k2, COUNT(*) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1",
    "SELECT DISTINCT d.k FROM (SELECT t.y AS n, t.x AS k, COUNT(*) AS c FROM t GROUP BY n, k) AS d WHERE d.c > 1",
]

# --- drop_dedup_read_as_set ---------------------------------------------------------------------------------------
READ_AS_SET = [
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t) AS d",
    "SELECT d.y, {MAX|MIN}(d.x) AS a FROM (SELECT DISTINCT t.x, t.y FROM t) AS d GROUP BY d.y",
    "SELECT {SUM(DISTINCT d.x)|COUNT(DISTINCT d.x)|AVG(DISTINCT d.x)|MAX(d.x)} AS a FROM (SELECT DISTINCT t.x FROM t) AS d",
    "SELECT d.y, {COUNT(*)|COUNT(d.x)|SUM(d.x)|AVG(d.x)|COUNT(*), MAX(d.x)} AS a FROM (SELECT DISTINCT t.x, t.y FROM t) AS d GROUP BY d.y",
    "SELECT {COUNT(*)|SUM(d.x)} AS a FROM (SELECT t.x FROM t GROUP BY t.x) AS d",
    "SELECT DISTINCT a.x FROM (SELECT DISTINCT t.x FROM t) AS a {LEFT JOIN|RIGHT JOIN|JOIN|FULL JOIN} (SELECT DISTINCT u.k FROM u) AS b ON a.x = b.k",
    "SELECT DISTINCT a.x, b.w FROM (SELECT DISTINCT t.x FROM t) AS a LEFT JOIN (SELECT u.k, u.w FROM u GROUP BY u.k, u.w) AS b ON a.x = b.k",
    "SELECT a.x, MAX(b.k) AS m FROM (SELECT DISTINCT t.x FROM t) AS a {JOIN|LEFT JOIN} (SELECT DISTINCT u.k FROM u) AS b ON a.x = b.k GROUP BY a.x",
    "SELECT a.x, COUNT(b.k) AS m FROM (SELECT DISTINCT t.x FROM t) AS a {JOIN|LEFT JOIN} (SELECT DISTINCT u.k FROM u) AS b ON a.x = b.k GROUP BY a.x",
    "SELECT DISTINCT a.x FROM t AS a JOIN (SELECT DISTINCT u.k, u.v, u.w FROM u) AS b ON a.x = b.k",
    "SELECT DISTINCT a.x FROM t AS a JOIN (SELECT DISTINCT u.k FROM u) AS b ON a.x = b.k",
    "SELECT COUNT(DISTINCT a.x) AS c FROM t AS a JOIN (SELECT DISTINCT u.k FROM u) AS b ON a.x = b.k",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t UNION ALL SELECT DISTINCT u.w AS x FROM u) AS d",
    "SELECT {COUNT(DISTINCT d.x)|COUNT(d.x)|COUNT(*)|MAX(d.x)} AS a FROM (SELECT DISTINCT t.x FROM t UNION ALL SELECT DISTINCT u.w FROM u) AS d",
    "SELECT DISTINCT e.x FROM (SELECT d.x FROM (SELECT DISTINCT t.x FROM t) AS d WHERE d.x > 1) AS e",
    "SELECT e.x, {COUNT(*)|MAX(e.y)} AS a FROM (SELECT d.x, d.y FROM (SELECT DISTINCT t.x, t.y FROM t) AS d WHERE d.x > 1) AS e GROUP BY e.x",
    "SELECT t.id FROM t WHERE t.x IN (SELECT d.x FROM (SELECT DISTINCT u.w AS x FROM u) AS d)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT d.x FROM (SELECT DISTINCT u.w AS x FROM u) AS d WHERE d.x = t.x)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT d.x FROM (SELECT DISTINCT u.w AS x FROM u) AS d JOIN p ON p.tid = d.x)",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t) AS d, (SELECT DISTINCT u.w FROM u) AS e",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x, t.y FROM t) AS d WHERE d.y IN (SELECT DISTINCT u.k FROM u)",
    "SELECT d.x, MAX(d.y) AS m FROM (SELECT DISTINCT t.x, t.y FROM t) AS d WHERE d.y NOT IN (SELECT DISTINCT u.k FROM u) GROUP BY d.x",
    "SELECT d.x, MAX(e.k) AS m FROM (SELECT DISTINCT t.x FROM t) AS d CROSS JOIN (SELECT DISTINCT u.k FROM u) AS e GROUP BY d.x",
    # near misses: LIMIT/window readers, an unqualified column that would resolve to the outer query, SEMI-like reads
    "SELECT d.x FROM (SELECT DISTINCT t.x FROM t) AS d",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t) AS d ORDER BY d.x LIMIT 2",
    "SELECT SUM(d.x) AS a FROM (SELECT DISTINCT t.x FROM t) AS d GROUP BY ROLLUP(d.x)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT DISTINCT d.x FROM (SELECT DISTINCT u.w AS x FROM u) AS d WHERE y = d.x)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT MAX(d.x) FROM (SELECT DISTINCT u.w AS x FROM u) AS d WHERE y > 0 GROUP BY d.x)",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x, ROW_NUMBER() OVER (ORDER BY t.x) AS r FROM t) AS d",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t ORDER BY t.x LIMIT 2) AS d",
    "SELECT d.x, COUNT(*) AS a FROM (SELECT DISTINCT t.x FROM t UNION DISTINCT SELECT DISTINCT u.w FROM u) AS d GROUP BY d.x",
    "SELECT d.y, COUNT(*) AS a FROM (SELECT DISTINCT t.y, t.x FROM t) AS d JOIN u ON u.k = d.y GROUP BY d.y",
]

# --- distinct_join_to_exists ---------------------------------------------------------------------------------------
JOIN_TO_EXISTS = [
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u {|WHERE u.w > 0}) AS d ON t.y = d.k",
    "SELECT t.id, t.x FROM t JOIN (SELECT DISTINCT u.k, u.w FROM u) AS d ON t.y = d.k AND t.x = d.w",
    "SELECT t.id FROM t JOIN (SELECT u.k FROM u GROUP BY u.k) AS d ON d.k = t.y WHERE t.x > 0",
    "SELECT t.id FROM t, (SELECT DISTINCT u.k FROM u) AS d WHERE t.y = d.k",
    "SELECT t.id FROM (SELECT DISTINCT u.k FROM u) AS d JOIN t ON t.y = d.k",
    "SELECT t.id FROM (SELECT DISTINCT u.k FROM u) AS d JOIN t ON t.y = d.k JOIN p ON p.tid = t.id",
    "SELECT t.id, p.n FROM t JOIN p ON p.tid = t.id JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y + 1 = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON d.k = t.y AND d.k > 1",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON d.k = t.y AND d.k = t.x",
    "SELECT DISTINCT t.x FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.y, COUNT(*) AS c FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k GROUP BY t.y",
    "SELECT COUNT(*) AS c FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    # near misses: the derived table is read, only one of its columns is equated, outer/lateral/using joins, a subquery
    "SELECT t.id, d.k FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k, u.w FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k, u.w FROM u) AS d ON t.y = d.k WHERE d.w > t.x",
    "SELECT t.id FROM t {LEFT JOIN|RIGHT JOIN|FULL JOIN} (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d USING (k)",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k WHERE d.k IN (SELECT p.tid FROM p)",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k JOIN (SELECT DISTINCT p.tid FROM p) AS e ON e.tid = d.k",
    "SELECT t.id FROM t INNER JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k INNER JOIN p ON p.tid = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k OR t.x = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.x = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k, u.k AS k2 FROM u) AS d ON t.y = d.k AND t.x = d.k2",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d(z) ON t.y = d.z",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON k = t.y",
    "SELECT * FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t, UNNEST([1, 2]) AS z JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k AND d.k IN (SELECT p.tid FROM p WHERE p.id = t.id)",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u LIMIT 3) AS d ON t.y = d.k",
    "SELECT t.id FROM t JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = d.k)",
]

# --- regroup_distinct ---------------------------------------------------------------------------------------------
REGROUP = [
    "SELECT {SUM|COUNT|AVG|MIN|MAX}(g.x) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x) AS g",
    "SELECT g.y, {SUM|COUNT|AVG|MIN|MAX}(g.x) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, {SUM|MIN|MAX}(g.n) AS a FROM (SELECT t.y, t.x, {COUNT(*)|SUM(t.f)|MIN(t.f)|MAX(t.f)|COUNT(t.s)} AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT {SUM|MIN|MAX}(g.n) AS a FROM (SELECT t.y, t.x, {COUNT(*)|SUM(t.f)|MIN(t.f)|MAX(t.f)} AS n FROM t GROUP BY t.y, t.x) AS g",
    "SELECT COALESCE(SUM(g.n), 0) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x) AS g",
    "SELECT g.y, COALESCE(SUM(g.n), 0) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, COALESCE(SUM(g.n), 0) + SUM(g.x) AS a, SUM(g.x) / COUNT(g.x) AS b FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.x) AS s, SUM(g.n) AS c FROM (SELECT t.y, t.x, COUNT(t.f) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT SUM(g.x) AS s, SUM(g.n) AS c FROM (SELECT t.x, COUNT(t.f) AS n FROM t GROUP BY t.x) AS g",
    "SELECT g.y, SUM(g.n) AS a, MAX(g.m) AS b FROM (SELECT t.y, t.x, t.s, COUNT(*) AS n, MAX(t.f) AS m FROM t GROUP BY t.y, t.x, t.s) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.x) AS a FROM (SELECT t.y, t.x, t.s, COUNT(*) AS n FROM t GROUP BY t.y, t.x, t.s) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x HAVING t.x = {1|2}) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x HAVING t.x > 1) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t WHERE t.x = 1 GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x HAVING COUNT(*) > 1) AS g GROUP BY g.y",
    "SELECT SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x HAVING t.x = 1) AS g",
    "SELECT CAST(SUM(g.n) AS INT64) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x) AS g",
    "SELECT g.y, CAST(SUM(g.x) / COUNT(g.x) AS INT64) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    # near misses: keys that are not columns, DISTINCT or a join around, a derived table outputting a constant,
    # a bare SUM of a COUNT over no groups (NULL, not 0), aggregates of a hidden key, ROLLUP
    "SELECT SUM(g.n) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x) AS g",
    "SELECT SUM(g.n) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t WHERE t.id < 0 GROUP BY t.x) AS g",
    "SELECT SUM(g.x) AS a FROM (SELECT t.x + 1 AS x, COUNT(*) AS n FROM t GROUP BY t.x + 1) AS g",
    "SELECT SUM(g.x) AS a FROM (SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x ORDER BY t.x LIMIT 2) AS g",
    "SELECT g.y, SUM(g.x) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g WHERE g.n > 1 GROUP BY g.y",
    "SELECT g.y, SUM(DISTINCT g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.x) AS a FROM (SELECT DISTINCT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT SUM(g.x) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g",
    "SELECT g.x, SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.x",
    "SELECT SUM(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY ROLLUP(t.y, t.x)) AS g",
    "SELECT g.y, AVG(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, MAX(g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
    "SELECT g.y, SUM(g.x + g.n) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y",
]

# --- small DISTINCT rules ------------------------------------------------------------------------------------------
SMALL = [
    "SELECT DISTINCT(t.x) FROM t",
    "SELECT DISTINCT(t.x), t.y FROM t",
    "SELECT t.y, COUNT(DISTINCT(t.x)) AS a FROM t GROUP BY t.y",
    "SELECT DISTINCT (t.x) AS a, (t.y) AS b FROM t",
    "SELECT (t.x) AS a, SUM(DISTINCT (t.y)) AS b FROM t GROUP BY (t.x)",
    "SELECT DISTINCT t.y FROM t GROUP BY t.y, t.x",
    "SELECT DISTINCT t.y, t.x + 1 AS z FROM t GROUP BY t.y, t.x",
    "SELECT DISTINCT UPPER(t.s) AS z FROM t GROUP BY t.s, t.x",
    "SELECT DISTINCT t.y AS k FROM t GROUP BY t.y",
    "SELECT DISTINCT t.y FROM t WHERE t.x > 0 GROUP BY t.y",
    # near misses for the GROUP BY rules: HAVING, aggregate, a column outside the keys, subquery output, ROLLUP
    "SELECT DISTINCT t.y FROM t GROUP BY t.y HAVING COUNT(*) > 1",
    "SELECT DISTINCT t.y, t.x FROM t GROUP BY t.y",
    "SELECT DISTINCT t.y, (SELECT COUNT(*) FROM u WHERE u.k = t.y) AS c FROM t GROUP BY t.y",
    "SELECT DISTINCT t.y FROM t GROUP BY ROLLUP(t.y, t.x)",
    "SELECT DISTINCT t.y, COUNT(*) AS c FROM t GROUP BY t.y",
    "SELECT DISTINCT t.y, t.x, SUM(t.f) AS c FROM t GROUP BY t.y, t.x",
    "SELECT DISTINCT t.y, COUNT(*) AS c FROM t GROUP BY t.y, t.x",
    "SELECT DISTINCT t.y, MAX(t.x) AS c, ROW_NUMBER() OVER (ORDER BY t.y) AS r FROM t GROUP BY t.y",
    "SELECT DISTINCT t.y AS k, t.x AS y FROM t GROUP BY k, y",
    "SELECT DISTINCT t.y + t.x AS z FROM t GROUP BY t.y, t.x",
    "SELECT DISTINCT t.y, SUM(t.x) AS c FROM t GROUP BY t.y, t.s HAVING SUM(t.x) > 0",
    "SELECT CAST(COUNT(*) AS INT64) AS c FROM t",
    "SELECT t.y, CAST(COUNT(t.x) AS INT64) AS c, CAST(COUNT(DISTINCT t.s) AS INT64) AS d FROM t GROUP BY t.y",
    "SELECT CAST(COUNT(*) AS {FLOAT64|STRING|NUMERIC}) AS c FROM t",
    "SELECT CAST(SUM(t.x) AS INT64) AS c FROM t",
]

# --- dedup_join_rules ---------------------------------------------------------------------------------------------
DEDUP_JOINS = [
    "SELECT DISTINCT t.x FROM t {LEFT JOIN u ON t.y = u.k|LEFT JOIN p ON p.tid = t.id|LEFT JOIN u ON t.y = u.k AND u.w > 1}",
    "SELECT DISTINCT t.x FROM u {RIGHT JOIN t ON t.y = u.k|RIGHT JOIN t ON u.k = t.y AND u.w > 0}",
    "SELECT t.x, MAX(t.y) AS m, MIN(t.id) AS n FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x",
    "SELECT t.x, COUNT(DISTINCT t.y) AS m, SUM(DISTINCT t.id) AS n FROM t LEFT JOIN p ON p.tid = t.id GROUP BY t.x",
    "SELECT DISTINCT d.x FROM (SELECT t.x, t.y FROM t LEFT JOIN u ON t.y = u.k) AS d",
    "SELECT d.x, MAX(d.y) AS m FROM (SELECT t.x, t.y FROM t LEFT JOIN u ON t.y = u.k WHERE t.id > 1) AS d GROUP BY d.x",
    "SELECT DISTINCT d.x FROM (SELECT t.x, t.y FROM u RIGHT JOIN t ON t.y = u.k) AS d WHERE d.y > 0",
    "SELECT DISTINCT t.x FROM t LEFT JOIN (SELECT DISTINCT u.k FROM u) AS d ON t.y = d.k",
    # near misses: the far side is read, a count sees repeats, two joins, USING, DISTINCT ON, a window, an inner source with GROUP BY
    "SELECT DISTINCT t.x, u.w FROM t LEFT JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x FROM t LEFT JOIN u ON t.y = u.k WHERE u.w > 1",
    "SELECT DISTINCT t.x FROM t LEFT JOIN u ON t.y = u.k WHERE u.w IS NULL",
    "SELECT t.x, COUNT(*) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x",
    "SELECT t.x, SUM(t.id) AS c FROM t LEFT JOIN p ON p.tid = t.id GROUP BY t.x",
    "SELECT DISTINCT COUNT(*) AS c FROM t LEFT JOIN p ON p.tid = t.id",
    "SELECT DISTINCT t.x FROM t LEFT JOIN u ON t.y = u.k LEFT JOIN p ON p.tid = t.id",
    "SELECT DISTINCT t.x FROM t LEFT JOIN u USING (id)",
    "SELECT DISTINCT ON (t.x) t.x FROM t LEFT JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x, ROW_NUMBER() OVER (ORDER BY t.x) AS r FROM t LEFT JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x FROM t FULL JOIN u ON t.y = u.k",
    "SELECT d.x FROM (SELECT t.x FROM t LEFT JOIN u ON t.y = u.k) AS d",
    "SELECT DISTINCT d.x FROM (SELECT t.x, COUNT(*) AS c FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x) AS d",
    "SELECT DISTINCT d.x FROM (SELECT t.x FROM t LEFT JOIN u ON t.y = u.k LIMIT 3) AS d",
    # strip_distinct_sources
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x, t.y FROM t) AS d WHERE d.y > 0",
    "SELECT DISTINCT d.x, e.w FROM (SELECT DISTINCT t.x, t.y FROM t) AS d JOIN (SELECT u.k, u.w FROM u GROUP BY u.k, u.w) AS e ON d.y = e.k",
    "SELECT d.x, MAX(e.w) AS m FROM (SELECT DISTINCT t.x, t.y FROM t) AS d JOIN (SELECT DISTINCT u.k, u.w FROM u) AS e ON d.y = e.k GROUP BY d.x",
    "SELECT d.x, MAX(d.y) AS m FROM (SELECT t.x, t.y FROM (SELECT DISTINCT t.x, t.y FROM t) AS t) AS d GROUP BY d.x",
    "SELECT DISTINCT f.x FROM (SELECT d.x FROM (SELECT DISTINCT t.x FROM t) AS d WHERE d.x > 0) AS f",
    "SELECT d.x, {COUNT(*)|SUM(d.y)|COUNT(d.y)|AVG(d.y)} AS m FROM (SELECT DISTINCT t.x, t.y FROM t) AS d GROUP BY d.x",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x, COUNT(*) AS c FROM t GROUP BY t.x) AS d",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t HAVING COUNT(*) > 0) AS d",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT ON (t.y) t.x, t.y FROM t) AS d",
    "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t ORDER BY t.x LIMIT 2) AS d",
]

# --- _push_distinct_into_sources ------------------------------------------------------------------------------------
PUSH_DISTINCT = [
    "SELECT {DISTINCT t.x|DISTINCT t.x, u.w} FROM t JOIN u ON t.y = u.k",
    "SELECT t.x FROM t JOIN u ON t.y = u.k GROUP BY t.x",
    "SELECT t.x, u.w FROM t JOIN u ON t.y = u.k GROUP BY t.x, u.w",
    "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k WHERE u.w > 1 AND t.x > 0",
    "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k WHERE u.w > t.x",
    "SELECT DISTINCT t.x FROM t, u WHERE t.y = u.k AND u.v = 'a'",
    "SELECT DISTINCT t.x FROM t JOIN p ON t.id = p.tid JOIN u ON t.y = u.k",
    "SELECT DISTINCT p.tid FROM p JOIN t ON t.id = p.tid",
    "SELECT DISTINCT t.s FROM t JOIN t AS t2 ON t.y = t2.y",
    "SELECT DISTINCT t.x + u.w AS z FROM t JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x FROM t CROSS JOIN u",
    "SELECT t.x FROM t JOIN u ON t.y = u.k GROUP BY t.x, t.x",
    # near misses: key columns read, every column read, outer join, HAVING, aggregates, ROLLUP, DISTINCT ON
    "SELECT DISTINCT t.id, u.w FROM t JOIN u ON t.y = u.k",
    "SELECT DISTINCT u.k, u.v, u.w FROM t JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x FROM t LEFT JOIN u ON t.y = u.k",
    "SELECT t.x FROM t JOIN u ON t.y = u.k GROUP BY t.x HAVING COUNT(*) > 1",
    "SELECT t.x, MAX(u.w) AS m FROM t JOIN u ON t.y = u.k GROUP BY t.x",
    "SELECT t.x FROM t JOIN u ON t.y = u.k GROUP BY ROLLUP(t.x)",
    "SELECT DISTINCT ON (t.x) t.x, u.w FROM t JOIN u ON t.y = u.k",
    "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k ORDER BY t.x LIMIT 2",
    "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = t.id)",
    "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k WHERE u.w IS NOT NULL OR t.x = 1",
]

# --- split_distinct_select: single-SELECT CASE shapes ---------------------------------------------------------------
CASE_SPLITS = [
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.tid = 1 THEN p.id WHEN p.id = 2 THEN p.tid END AS k FROM p) AS d ON d.k = t.x",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.tid = 1 THEN 5 WHEN p.id = 2 THEN 5 END AS k FROM p) AS d ON d.k = t.x",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.tid = 1 THEN p.tid WHEN p.tid = 1 AND p.id = 2 THEN 1 END AS k FROM p) AS d ON d.k = t.x",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT DISTINCT p.id AS pid, CASE WHEN p.b THEN p.id WHEN p.n > 1 THEN p.tid ELSE NULL END AS k FROM p) AS d ON t.x = d.k",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.b THEN p.id WHEN p.n > 1 THEN p.tid ELSE 0 END AS k FROM p) AS d ON t.x = d.k",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.b THEN p.id END AS k FROM p) AS d ON t.x = d.k",
    "SELECT DISTINCT t.id FROM t JOIN (SELECT p.id AS pid, CASE WHEN p.b THEN p.id WHEN p.n > 1 THEN p.tid END AS k FROM p) AS d ON t.x < d.k",
    "SELECT DISTINCT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y WHEN t.y = 1 THEN t.x END = {2|t.id}",
    "SELECT DISTINCT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y WHEN t.x > 0 THEN 3 END = t.id",
    "SELECT DISTINCT t.id FROM t JOIN u ON CASE WHEN t.x = 1 THEN t.y WHEN t.y = 1 THEN t.x END = u.k",
    "SELECT DISTINCT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y ELSE t.id END = 2",
    "SELECT DISTINCT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y WHEN t.y = 1 THEN t.x END = CASE WHEN t.s = 'a' THEN 1 END",
    "SELECT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y WHEN t.y = 1 THEN t.x END = 2",
    "SELECT t.id FROM t WHERE t.x IN (SELECT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u)",
    "SELECT t.id FROM t WHERE t.x NOT IN (SELECT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT DISTINCT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u WHERE u.w > 0)",
    "SELECT t.id, t.x IN (SELECT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u) AS m FROM t",
]

# one query per rewrite that must make it fire (the tests trace these)
FIRES = [
    ("distinct_rules.drop_membership_dedup", "SELECT t.id FROM t WHERE EXISTS (SELECT DISTINCT u.w FROM u WHERE u.k = t.y)"),
    ("distinct_rules.merge_grouped_source", "SELECT DISTINCT d.k FROM (SELECT t.y AS k, COUNT(t.x) AS c FROM t GROUP BY t.y, t.s) AS d WHERE d.c > 1"),
    (
        "distinct_rules.drop_dedup_read_as_set",
        "SELECT DISTINCT a.x, b.w FROM (SELECT DISTINCT t.x FROM t) AS a LEFT JOIN (SELECT u.k, u.w FROM u GROUP BY u.k, u.w) AS b ON a.x = b.k",
    ),
    ("distinct_rules.distinct_join_to_exists", "SELECT t.id, t.x FROM t JOIN (SELECT DISTINCT u.k, u.w FROM u) AS d ON t.y = d.k AND t.x = d.w"),
    ("distinct_rules.regroup_distinct", "SELECT g.y, COUNT(g.x) AS a FROM (SELECT t.y, t.x, COUNT(*) AS n FROM t GROUP BY t.y, t.x) AS g GROUP BY g.y"),
    ("distinct_rules.unwrap_column_parens", "SELECT DISTINCT(t.x), t.y FROM t"),
    ("distinct_rules.drop_group_under_distinct", "SELECT DISTINCT t.y FROM t GROUP BY t.y, t.x"),
    ("distinct_rules.drop_distinct_over_group_keys", "SELECT DISTINCT t.y, COUNT(*) AS c FROM t GROUP BY t.y"),
    ("distinct_rules.fold_count_casts", "SELECT CAST(COUNT(*) AS INT64) AS c FROM t"),
    ("dedup_join_rules._drop_join", "SELECT DISTINCT t.x FROM t LEFT JOIN u ON t.y = u.k"),
    ("drop_unread_outer_join", "SELECT t.x, MAX(t.y) AS m FROM t LEFT JOIN u ON t.y = u.k GROUP BY t.x"),
    (
        "strip_distinct_sources",
        "SELECT DISTINCT d.x, e.w FROM (SELECT DISTINCT t.x, t.y FROM t) AS d JOIN (SELECT u.k, u.w FROM u GROUP BY u.k, u.w) AS e ON d.y = e.k",
    ),
    ("_push_distinct_into_sources", "SELECT DISTINCT t.x FROM t JOIN u ON t.y = u.k"),
    ("_distinct_over_union_all", "SELECT DISTINCT d.x FROM (SELECT DISTINCT t.x FROM t UNION ALL SELECT DISTINCT u.w AS x FROM u) AS d"),
    ("_split_aggregates", "SELECT COUNT(d.x) AS a FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS d"),
    (
        "set_split_rules._split_case_key",
        "SELECT DISTINCT t.id FROM t JOIN (SELECT DISTINCT p.id AS pid, CASE WHEN p.b THEN p.id WHEN p.n > 1 THEN p.tid ELSE NULL END AS k FROM p) AS d ON t.x = d.k",
    ),
    ("set_split_rules._split_case_comparison", "SELECT DISTINCT t.id FROM t WHERE CASE WHEN t.x = 1 THEN t.y WHEN t.x > 0 THEN 3 END = t.id"),
    ("set_split_rules._split_in_over_union", "SELECT t.id FROM t WHERE t.x IN (SELECT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u)"),
    ("split_distinct_select", "SELECT t.id FROM t WHERE t.x IN (SELECT CASE WHEN u.v = 'a' THEN u.w WHEN u.k = 1 THEN u.k END FROM u)"),
]

TEMPLATES = MEMBERSHIP + MERGE + READ_AS_SET + JOIN_TO_EXISTS + REGROUP + SMALL + DEDUP_JOINS + PUSH_DISTINCT + CASE_SPLITS


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "distinct_variants")


def fire_list() -> list[tuple[str, dict]]:
    return fire_cases("distinct_variants", FIRES)
