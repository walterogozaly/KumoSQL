"""The variants inside ``rewrite_aggregates`` (``aggregate_rules``: an empty or FROM-less global aggregate, a shared
CASE filter, aggregates of group keys, COUNT of a filtered column, an always-true HAVING COUNT(*), a counted sum, a
filter on a grouped derived table, lifted aggregate expressions, compound aggregates over a UNION ALL, an existence
HAVING, merged grouped copies, aggregates over aggregating branches, a projection over a grouped join) and the
neighbouring ``_split_aggregates``. Each shape comes with near misses that a guard must decline (a GROUP BY that would
lose empty groups, COUNT(*) under a filter, a HAVING COUNT(*) over a global aggregate, an outer join, ...)."""

from ._base import expand
from ._variants import fire_cases

# --- _empty_global_aggregate / _fromless_aggregate ------------------------------------------------------------------
EMPTY = [
    "SELECT COUNT(*) AS a, SUM(t.x) AS b, MAX(t.x) AS c, COUNT(t.x) AS d FROM t WHERE FALSE",
    "SELECT COALESCE(SUM(t.x), 0) AS a, COALESCE(MAX(t.x), 1) + 1 AS b, CASE WHEN COUNT(*) = 0 THEN 'none' END AS c FROM t WHERE FALSE",
    "SELECT AVG(t.x) AS a, MIN(t.f) AS b, COUNT(DISTINCT t.x) AS c FROM t WHERE (FALSE)",
    "SELECT LOGICAL_AND(p.b) AS a, LOGICAL_OR(p.b) AS b FROM p WHERE FALSE",
    "SELECT COUNT(*) AS a FROM t JOIN u ON t.y = u.k WHERE FALSE",
    "SELECT COUNT(*) AS a, 1 AS b FROM t WHERE FALSE",
    # near misses: GROUP BY keeps no row, a column or a subquery in the output, an aggregate that is not NULL-ignoring, HAVING
    "SELECT t.y, COUNT(*) AS a FROM t WHERE FALSE GROUP BY t.y",
    "SELECT COUNT(*) AS a FROM t WHERE FALSE GROUP BY ()",
    "SELECT COUNT(*) AS a, (SELECT COUNT(*) FROM u) AS b FROM t WHERE FALSE",
    "SELECT COUNT(*) AS a FROM t WHERE FALSE HAVING COUNT(*) = 0",
    "SELECT ARRAY_LENGTH(ARRAY_AGG(t.x)) AS a, COUNT(*) AS b FROM t WHERE FALSE",
    "SELECT STRING_AGG(t.s) AS a, COUNT(*) AS b FROM t WHERE FALSE",
    "SELECT ANY_VALUE(t.x) AS a FROM t WHERE FALSE",
    "SELECT COUNT(*) AS a FROM t WHERE FALSE LIMIT 1 OFFSET 1",
    "SELECT COUNT(*) AS a FROM t WHERE 1 = 0",
    "SELECT COUNT(*) AS a FROM t WHERE t.x > 1 AND FALSE",
    "SELECT COUNT(*) AS a, t.y FROM t WHERE FALSE GROUP BY t.y HAVING COUNT(*) = 0",
]
FROMLESS = [
    "SELECT {COUNT(*)|COUNT(1)|COUNT(5)|COUNT(NULL)|COUNT('a')|COUNT(DISTINCT 1)|COUNT(TRUE)} AS a",
    "SELECT {SUM|MIN|MAX|AVG}({1|2 + 3|NULL|-4|1.5|'a'}) AS a, COUNT(*) AS b",
    "SELECT SUM(1) AS a, MAX(1) AS b, MIN(2) AS c, COUNT(*) AS d",
    "SELECT SUM(t) AS a FROM (SELECT 1 AS t) AS d WHERE FALSE",
    "SELECT {SUM|MAX}(RAND()) IS NULL AS a",
    "SELECT COUNT(*) AS a WHERE FALSE",
    "SELECT COUNT(*) AS a HAVING COUNT(*) > 1",
    "SELECT COUNT(*) AS a, SUM(1) AS b GROUP BY ()",
    "SELECT SUM(1) + COUNT(*) AS a, COALESCE(SUM(NULL), 7) AS b",
    "SELECT SUM(DISTINCT 3) AS a, MAX(DISTINCT 3) AS b",
    "SELECT 1 AS a, (SELECT COUNT(*)) AS b",
]

# --- _pull_shared_filter -----------------------------------------------------------------------------------------
SHARED = [
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 0 THEN 1 END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, MAX(CASE WHEN t.x > 0 AND t.y > 1 THEN t.y END) AS b, MIN(IF(t.x > 0, t.f, NULL)) AS c FROM t",
    "SELECT AVG(CASE WHEN t.x > 0 THEN t.f END) AS a FROM t WHERE t.y IS NOT NULL",
    "SELECT COUNT(DISTINCT CASE WHEN t.x > 0 THEN t.y END) AS a, SUM(DISTINCT CASE WHEN t.x > 0 THEN t.y END) AS b FROM t",
    "SELECT SUM(CASE WHEN (t.x > 0) IS TRUE THEN t.x END) AS a, SUM(CASE WHEN t.x > 0 THEN t.y END) AS b FROM t",
    "SELECT LOGICAL_AND(CASE WHEN t.x > 0 THEN p.b END) AS a, COUNT(CASE WHEN t.x > 0 THEN 'a' END) AS b FROM t JOIN p ON p.tid = t.id",
    "SELECT SUM(CASE WHEN t.x > 0 AND t.y > 0 THEN t.x END) AS a, SUM(CASE WHEN t.x > 0 AND t.s = 'a' THEN t.y END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x ELSE NULL END) AS a, COUNT(CASE WHEN t.x > 0 THEN t.y ELSE NULL END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) + 1 AS a, COALESCE(MAX(CASE WHEN t.x > 0 THEN t.y END), 0) AS b FROM t",
    # near misses: ELSE 0, COUNT(*), a different filter, a column outside the aggregates, GROUP BY, an aggregate in the test
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x ELSE 0 END) AS a, COUNT(CASE WHEN t.x > 0 THEN 1 ELSE 0 END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(*) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, SUM(CASE WHEN t.y > 0 THEN t.x END) AS b FROM t",
    "SELECT t.y, SUM(CASE WHEN t.x > 0 THEN t.x END) AS a FROM t GROUP BY t.y",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, t.y FROM t GROUP BY t.y",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, MAX(t.id) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x WHEN t.y > 0 THEN t.y END) AS a, SUM(CASE WHEN t.x > 0 THEN t.f END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > SUM(t.y) THEN t.x END) AS a FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a FROM t HAVING SUM(CASE WHEN t.x > 0 THEN t.x END) > 0",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, ROW_NUMBER() OVER () AS r FROM t",
    "SELECT SUM(CASE WHEN t.x IS NULL THEN 1 END) AS a, COUNT(CASE WHEN t.x IS NULL THEN 1 END) AS b FROM t",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, ANY_VALUE(CASE WHEN t.x > 0 THEN t.y END) AS b FROM t",
]

# --- _key_expression_aggregates / _count_of_filtered_value / _drop_nonempty_group_having --------------------------------
KEYS = [
    "SELECT t.y, MAX(t.y + 1) AS a, MIN(UPPER(CAST(t.y AS STRING))) AS b, COUNT(DISTINCT t.y) AS c FROM t GROUP BY t.y",
    "SELECT t.y, SUM(DISTINCT t.y * 2) AS a, COUNT(DISTINCT t.y + 1) AS b, COUNT(DISTINCT t.y) AS c FROM t GROUP BY t.y",
    "SELECT t.y, t.x, MAX(t.y * t.x) AS a, MIN(t.x - t.y) AS b, COUNT(DISTINCT t.x + t.y) AS c FROM t GROUP BY t.y, t.x",
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING COUNT(DISTINCT t.y) {>|=|<|>=|<>} {0|1|2}",
    "SELECT t.y, MAX(CASE WHEN t.y > 1 THEN 1 ELSE 0 END) AS a, COUNT(DISTINCT CASE WHEN t.y IS NULL THEN 5 END) AS b FROM t GROUP BY t.y",
    "SELECT t.y, MAX(t.y) AS a, MIN(t.y) AS b, SUM(t.y) AS c FROM t GROUP BY t.y",
    # near misses: a column outside the keys, a plain SUM or COUNT, a key in WHERE or ROLLUP
    "SELECT t.y, MAX(t.x) AS a, COUNT(DISTINCT t.x) AS b FROM t GROUP BY t.y",
    "SELECT t.y, SUM(t.y + 1) AS a, COUNT(t.y + 1) AS b FROM t GROUP BY t.y",
    "SELECT t.y, MAX(t.y + t.x) AS a FROM t GROUP BY t.y",
    "SELECT t.y, MAX(t.y + (SELECT MIN(u.k) FROM u)) AS a FROM t GROUP BY t.y",
    "SELECT t.y, MAX(t.y + RAND()) IS NULL AS a FROM t GROUP BY t.y",
    "SELECT t.y, MAX(t.y + 1) AS a FROM t GROUP BY ROLLUP(t.y)",
    "SELECT t.y, MAX(t.y + 1) AS a FROM t GROUP BY GROUPING SETS ((t.y), ())",
    "SELECT MAX(t.y + 1) AS a, COUNT(DISTINCT t.y) AS b FROM t",
    "SELECT t.y, COUNT(DISTINCT t.y) AS a FROM t GROUP BY t.y QUALIFY ROW_NUMBER() OVER (ORDER BY t.y) < 3",
]
COUNT_FILTERED = [
    "SELECT t.y, COUNT(t.x) AS a FROM t WHERE t.x {>|<|=|<>|>=} 0 GROUP BY t.y",
    "SELECT COUNT(t.x) AS a, COUNT(t.y) AS b FROM t WHERE t.x = t.y",
    "SELECT COUNT(t.x) AS a, COUNT(*) AS b FROM t WHERE t.x IS NOT NULL",
    "SELECT COUNT(t.x) AS a FROM t WHERE NOT t.x IS NULL AND t.y > 1",
    "SELECT t.s, COUNT((t.x)) AS a FROM t WHERE (t.x > 1) GROUP BY t.s",
    "SELECT COUNT(t.x) AS a FROM t JOIN u ON t.y = u.k WHERE t.x < u.w",
    # near misses: the filter does not reject NULL, the argument is not the filtered column, DISTINCT, OR
    "SELECT COUNT(t.x) AS a FROM t WHERE t.y > 0",
    "SELECT COUNT(t.x) AS a FROM t WHERE t.x IS NULL",
    "SELECT COUNT(t.x) AS a FROM t WHERE t.x > 0 OR t.y > 0",
    "SELECT COUNT(t.x + 1) AS a FROM t WHERE t.x > 0",
    "SELECT COUNT(DISTINCT t.x) AS a FROM t WHERE t.x > 0",
    "SELECT COUNT(t.x) AS a FROM t WHERE COALESCE(t.x, 0) > 0",
    "SELECT COUNT(t.x) AS a FROM t WHERE t.x IS NOT NULL OR t.y IS NULL",
    "SELECT COUNT(t.x) FILTER (WHERE t.y > 0) AS a FROM t WHERE t.x > 0",
]
NONEMPTY_HAVING = [
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(*) {>= 1|> 0|<> 0|>= 1 AND SUM(t.x) > 0}",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING {1 <= COUNT(*)|0 < COUNT(*)} AND t.y > 0",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(*) >= 1 OR SUM(t.x) > 0",
    # near misses: COUNT(x) can be 0, a larger bound, no GROUP BY (a global aggregate has a row over no rows), ROLLUP
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(t.x) >= 1",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(*) >= {0|2}",
    "SELECT SUM(t.x) AS a FROM t HAVING COUNT(*) {>= 1|> 0}",
    "SELECT SUM(t.x) AS a FROM t WHERE t.id < 0 HAVING COUNT(*) {>= 1|<= 1}",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY ROLLUP(t.y) HAVING COUNT(*) >= 1",
    "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(DISTINCT t.x) >= 1",
]

# --- _coalesce_counted_sum / _filter_into_having / _lift_aggregate_expressions ---------------------------------------
GROUPED = [
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, COUNT(t.x) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, 1 AS c FROM u) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, SUM(t.x) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    "SELECT COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d",
    "SELECT d.k, SUM(d.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    # filter into HAVING
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c {>|=|<>|<=} 1",
    "SELECT d.k, d.c FROM (SELECT t.y AS k, COUNT(*) AS c, SUM(t.x) AS s FROM t GROUP BY t.y) AS d WHERE d.c > 1 AND d.s > 0 AND d.k > 0",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) + 1 AS c FROM t GROUP BY t.y HAVING COUNT(*) > 0) AS d WHERE d.c > 2",
    "SELECT d.k FROM (SELECT t.y AS k, MAX(t.x) AS m FROM t GROUP BY t.y) AS d WHERE d.m > d.k",
    "SELECT d.k FROM (SELECT t.y AS k, MAX(t.x) AS m FROM t GROUP BY t.y) AS d WHERE d.m IS NOT NULL",
    "SELECT d.k, d.m FROM (SELECT t.y AS k, t.s AS j, MAX(t.x) AS m FROM t GROUP BY t.y, t.s) AS d WHERE d.m = 1",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1 OR d.k = 1",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.k > 1",
    "SELECT t2.id FROM t AS t2 WHERE t2.y IN (SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1)",
    # near misses: no keys, a LIMIT inside, a join, RAND(), a correlated reference
    "SELECT d.c FROM (SELECT COUNT(*) AS c FROM t) AS d WHERE d.c > 1",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y ORDER BY c LIMIT 2) AS d WHERE d.c > 1",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d JOIN u ON u.k = d.k WHERE d.c > 1",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > RAND()",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > (SELECT COUNT(*) FROM u)",
    "SELECT d.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY ROLLUP(t.y)) AS d WHERE d.c > 1",
    "SELECT d.k FROM (SELECT DISTINCT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1",
    "SELECT d.k, ROW_NUMBER() OVER (ORDER BY d.k) AS r FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1",
    # lifted aggregate expressions
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + 1 AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, COALESCE(SUM(p.n), 0) AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    "SELECT t.id, d.v, d.w FROM t JOIN (SELECT p.tid AS k, SUM(p.n) * COUNT(*) AS v, MAX(p.n) - MIN(p.n) AS w FROM p GROUP BY p.tid) AS d ON t.id = d.k WHERE d.v > 1",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + p.tid AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    "SELECT d.v FROM (SELECT p.tid AS k, COUNT(*) * 2 AS v, COUNT(*) AS c FROM p GROUP BY p.tid) AS d",
    "SELECT d.v FROM (SELECT COUNT(*) * 2 AS v FROM p) AS d WHERE d.v > 1",
    "SELECT t.id, d.v FROM t, (SELECT p.tid AS k, CASE WHEN COUNT(*) > 1 THEN 1 ELSE 0 END AS v FROM p GROUP BY p.tid) AS d WHERE t.id = d.k",
    "SELECT t.id, d.v FROM t {LEFT JOIN|RIGHT JOIN} (SELECT p.tid AS k, MAX(p.n) + 1 AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + t.x AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + 1 AS v FROM p GROUP BY p.tid HAVING v > 1) AS d ON t.id = d.k",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + 1 AS v FROM p GROUP BY p.tid ORDER BY v LIMIT 2) AS d ON t.id = d.k",
    "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, (SELECT MAX(u.w) FROM u) + MAX(p.n) AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
]

# --- _having_existence_to_where -------------------------------------------------------------------------------------
EXISTENCE = [
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) {>= 1|> 0}",
    "SELECT t.y FROM t GROUP BY t.y HAVING {1 <= SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END)|0 < SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END)}",
    "SELECT t.y, t.y + 1 AS z FROM t WHERE t.id > 0 GROUP BY t.y HAVING COUNT(CASE WHEN t.x > 1 THEN 1 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 2 END) >= 1 AND t.y > 0",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 3 ELSE NULL END) > 0",
    "SELECT t.y FROM t GROUP BY t.y HAVING COUNT(CASE WHEN t.x > 1 THEN 'a' END) > 0",
    "SELECT t.y FROM t GROUP BY t.y HAVING COUNT(CASE WHEN t.x > 1 THEN 1 ELSE NULL END) >= 1 ORDER BY t.y",
    "SELECT DISTINCT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 AND t.s = 'a' THEN 1 ELSE 0 END) >= 1",
    # near misses: another aggregate in the output, ORDER BY an aggregate, bigger bounds, a negative else, DISTINCT, an aggregate in the test
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1 ORDER BY COUNT(*)",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= {2|0}",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE -1 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 0 ELSE 1 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(DISTINCT CASE WHEN t.x > 1 THEN 1 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > MAX(t.y) THEN 1 ELSE 0 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1 AND COUNT(*) > 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1 AND SUM(CASE WHEN t.x < 0 THEN 1 ELSE 0 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN t.x ELSE 0 END) >= 1",
    "SELECT SUM(t.x) AS a FROM t HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1",
    "SELECT t.y FROM t GROUP BY ROLLUP(t.y) HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1 LIMIT 2",
]

# --- _merge_joined_aggregates ---------------------------------------------------------------------------------------
TS = "(SELECT t.y AS k, SUM(t.x) AS a FROM t GROUP BY t.y)"
TM = "(SELECT t.y AS k, MAX(t.f) AS b, COUNT(*) AS c FROM t GROUP BY t.y)"
MERGE = [
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b, d2.c FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k WHERE d1.a > 1",
    f"SELECT d1.k, d1.a, d2.b, d3.c FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k JOIN (SELECT t.y AS k, MIN(t.x) AS c FROM t GROUP BY t.y) AS d3 ON d3.k IS NOT DISTINCT FROM d1.k",
    "SELECT d1.a, d2.b FROM (SELECT SUM(t.x) AS a FROM t) AS d1 JOIN (SELECT MAX(t.f) AS b FROM t) AS d2 ON TRUE",
    "SELECT d1.a, d2.b FROM (SELECT SUM(t.x) AS a FROM t) AS d1 CROSS JOIN (SELECT MAX(t.f) AS b FROM t) AS d2",
    "SELECT d1.a, d2.b FROM (SELECT SUM(t.x) AS a FROM t WHERE t.id < 0) AS d1 CROSS JOIN (SELECT COUNT(*) AS b FROM t WHERE t.id < 0) AS d2",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN (SELECT t.y AS k, MAX(t.f) AS b FROM t GROUP BY t.y HAVING COUNT(*) > 1) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b FROM (SELECT t.y AS k, SUM(t.x) AS a FROM t GROUP BY t.y HAVING SUM(t.x) > 1) AS d1 JOIN (SELECT t.y AS k, MAX(t.f) AS b FROM t GROUP BY t.y HAVING COUNT(*) > 1) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    "SELECT d1.k, d1.j, d1.a, d2.b FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS a FROM t GROUP BY t.y, t.s) AS d1 JOIN (SELECT t.y AS k, t.s AS j, MAX(t.f) AS b FROM t GROUP BY t.y, t.s) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k AND d1.j IS NOT DISTINCT FROM d2.j",
    # near misses: an ordinary equality drops the NULL group, a different filter or table, one key linked of two, outer joins
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k = d2.k",
    "SELECT d1.k, d1.a, d2.b FROM (SELECT t.y AS k, SUM(t.x) AS a FROM t WHERE t.x > 0 GROUP BY t.y) AS d1 JOIN (SELECT t.y AS k, MAX(t.f) AS b FROM t GROUP BY t.y) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    "SELECT d1.k, d1.a, d2.b FROM (SELECT t.y AS k, SUM(t.x) AS a FROM t GROUP BY t.y) AS d1 JOIN (SELECT t.y AS k, MAX(t.w) AS b FROM (SELECT t.y, t.x AS w FROM t) AS t GROUP BY t.y) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    "SELECT d1.k, d1.a, d2.b FROM (SELECT t.y AS k, SUM(t.x) AS a FROM t GROUP BY t.y) AS d1 JOIN (SELECT u.k AS k, MAX(u.w) AS b FROM u GROUP BY u.k) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    "SELECT d1.k, d1.a, d2.b FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS a FROM t GROUP BY t.y, t.s) AS d1 JOIN (SELECT t.y AS k, t.s AS j, MAX(t.f) AS b FROM t GROUP BY t.y, t.s) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 {{LEFT JOIN|FULL JOIN}} {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k AND d1.a > d2.b",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN (SELECT t.y AS k, MAX(t.f) AS b FROM t GROUP BY t.y LIMIT 3) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN (SELECT DISTINCT t.y AS k, MAX(t.f) AS b FROM t GROUP BY t.y) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k",
    f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k WHERE d2.b > 0 AND EXISTS (SELECT 1 FROM u WHERE u.k = d1.k)",
]

# --- _split_compound_aggregates / _split_aggregates / _distribute_over_aggregating_branches -------------------------------
BRANCHES = [
    "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    "SELECT d.k, SUM(d.c) * 2 + COUNT(d.c) AS a FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a, MAX(d.c) - MIN(d.c) AS b FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k",
    "SELECT LOGICAL_AND(d.c) AS a, LOGICAL_OR(d.c) AS b FROM (SELECT p.b AS c FROM p UNION ALL SELECT p.b AS c FROM p WHERE p.n > 1) AS d",
    "SELECT d.k, COUNT(d.c) AS a, SUM(d.c) AS b FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k",
    "SELECT d.j, d.k, SUM(d.c) AS a FROM (SELECT t.s AS j, t.y AS k, t.x AS c FROM t UNION ALL SELECT u.v AS j, u.k AS k, u.w AS c FROM u) AS d GROUP BY d.j, d.k",
    "SELECT SUM(d.c) AS a, 7 AS b FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    "SELECT SUM(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u UNION ALL SELECT p.id AS c FROM p) AS d",
    "SELECT d.c FROM (SELECT COUNT(*) AS c FROM t UNION ALL SELECT COUNT(*) AS c FROM u) AS d WHERE d.c > 0",
    "SELECT d.c + 1 AS e FROM (SELECT COUNT(*) AS c FROM t UNION ALL SELECT MAX(u.w) AS c FROM u) AS d",
    "SELECT d.c, v.v FROM (SELECT SUM(t.x) AS c FROM t UNION ALL SELECT COUNT(*) AS c FROM u) AS d, (SELECT 1 AS v UNION ALL SELECT 2 AS v) AS v",
    # near misses: DISTINCT aggregates, AVG, nested aggregates, a HAVING, a join, UNION DISTINCT, a non-key group expression
    "SELECT SUM(DISTINCT d.c) / COUNT(DISTINCT d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    "SELECT AVG(d.c) + 1 AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION DISTINCT SELECT u.w AS c FROM u) AS d",
    "SELECT d.k, SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k HAVING COUNT(d.c) > 1",
    "SELECT d.k + 1 AS k1, SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k + 1",
    "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d JOIN u ON u.k = d.c",
    "SELECT SUM(d.c) / COUNT(*) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u ORDER BY c LIMIT 3) AS d",
    "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d GROUP BY ROLLUP(d.c)",
]

# --- _merge_projection_over_grouped_join / _collapse_aggregate ---------------------------------------------------------
GT2 = "(SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y)"
GU2 = "(SELECT u.w AS k, COUNT(*) AS c FROM u GROUP BY u.w)"
PROJECTIONS = [
    f"SELECT d.x + 1 AS y FROM (SELECT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.x FROM (SELECT g.k, g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d WHERE d.k > 1 AND d.x > 0",
    f"SELECT d.k, d.x * 2 FROM (SELECT g.k, g.s + h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.x FROM (SELECT g.s * h.c AS x FROM {GT2} AS g CROSS JOIN {GU2} AS h) AS d",
    f"SELECT SUM(d.x) AS a FROM (SELECT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.k, SUM(d.x) AS a FROM (SELECT g.k, g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d GROUP BY d.k",
    # near misses: an outer join, a window, DISTINCT or LIMIT inside, a correlated reference, plain tables
    f"SELECT d.x FROM (SELECT g.s * h.c AS x FROM {GT2} AS g LEFT JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.x FROM (SELECT ROW_NUMBER() OVER (ORDER BY g.k) AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.x FROM (SELECT DISTINCT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    f"SELECT d.x FROM (SELECT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k LIMIT 3) AS d",
    "SELECT d.x FROM (SELECT t.x + u.w AS x FROM t JOIN u ON t.y = u.k) AS d WHERE d.x > 1",
    f"SELECT d.x FROM (SELECT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d WHERE d.x > (SELECT COUNT(*) FROM u)",
    f"SELECT d.x FROM (SELECT g.s * h.c AS x, RAND() AS r FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
]

# one query per rewrite that must make it fire (the tests trace these). Not listed: _distribute_over_aggregating_branches,
# which _distribute (earlier in normalize's list, same conditions) always takes first, and _roll_up_aggregate likewise.
FIRES = [
    ("aggregate_rules._empty_global_aggregate", "SELECT COUNT(*) AS a, SUM(t.x) AS b, MAX(t.x) AS c, COUNT(t.x) AS d FROM t WHERE FALSE"),
    ("aggregate_rules._fromless_aggregate", "SELECT SUM(1) AS a, MAX(1) AS b, MIN(2) AS c, COUNT(*) AS d"),
    ("aggregate_rules._pull_shared_filter", "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 0 THEN 1 END) AS b FROM t"),
    (
        "aggregate_rules._key_expression_aggregates",
        "SELECT t.y, MAX(t.y + 1) AS a, MIN(UPPER(CAST(t.y AS STRING))) AS b, COUNT(DISTINCT t.y) AS c FROM t GROUP BY t.y",
    ),
    ("aggregate_rules._key_expression_aggregates", "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING COUNT(DISTINCT t.y) = 1"),
    ("aggregate_rules._count_of_filtered_value", "SELECT t.y, COUNT(t.x) AS a FROM t WHERE t.x <> 0 GROUP BY t.y"),
    ("aggregate_rules._drop_nonempty_group_having", "SELECT t.y, SUM(t.x) AS a FROM t GROUP BY t.y HAVING COUNT(*) > 0"),
    (
        "aggregate_rules._coalesce_counted_sum",
        "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    ),
    (
        "aggregate_rules._filter_into_having",
        "SELECT d.k, ROW_NUMBER() OVER (ORDER BY d.k) AS r FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c > 1",
    ),
    (
        "aggregate_rules._lift_aggregate_expressions",
        "SELECT t.id, d.v FROM t JOIN (SELECT p.tid AS k, MAX(p.n) + 1 AS v FROM p GROUP BY p.tid) AS d ON t.id = d.k",
    ),
    (
        "aggregate_rules._lift_aggregate_expressions",
        "SELECT t.id, d.v, d.w FROM t JOIN (SELECT p.tid AS k, SUM(p.n) * COUNT(*) AS v, MAX(p.n) - MIN(p.n) AS w FROM p GROUP BY p.tid) AS d ON t.id = d.k WHERE d.v > 1",
    ),
    (
        "aggregate_rules._split_compound_aggregates",
        "SELECT SUM(d.c) / COUNT(d.c) AS a FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d",
    ),
    (
        "aggregate_rules._split_compound_aggregates",
        "SELECT LOGICAL_AND(d.c) AS a, LOGICAL_OR(d.c) AS b FROM (SELECT p.b AS c FROM p UNION ALL SELECT p.b AS c FROM p WHERE p.n > 1) AS d",
    ),
    ("aggregate_rules._having_existence_to_where", "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1"),
    ("aggregate_rules._having_existence_to_where", "SELECT t.y FROM t GROUP BY t.y HAVING COUNT(CASE WHEN t.x > 1 THEN 1 END) > 0"),
    ("aggregate_rules._merge_joined_aggregates", f"SELECT d1.k, d1.a, d2.b FROM {TS} AS d1 JOIN {TM} AS d2 ON d1.k IS NOT DISTINCT FROM d2.k"),
    (
        "aggregate_rules._merge_joined_aggregates",
        "SELECT d1.k, d1.j, d1.a, d2.b FROM (SELECT t.y AS k, t.s AS j, SUM(t.x) AS a FROM t GROUP BY t.y, t.s) AS d1 JOIN (SELECT t.y AS k, t.s AS j, MAX(t.f) AS b FROM t GROUP BY t.y, t.s) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k AND d1.j IS NOT DISTINCT FROM d2.j",
    ),
    (
        "aggregate_rules._merge_projection_over_grouped_join",
        f"SELECT d.x + 1 AS y FROM (SELECT g.s * h.c AS x FROM {GT2} AS g JOIN {GU2} AS h ON g.k = h.k) AS d",
    ),
]

TEMPLATES = EMPTY + FROMLESS + SHARED + KEYS + COUNT_FILTERED + NONEMPTY_HAVING + GROUPED + EXISTENCE + MERGE + BRANCHES + PROJECTIONS


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "aggregate_variants")


def fire_list() -> list[tuple[str, dict]]:
    return fire_cases("aggregate_variants", FIRES)
