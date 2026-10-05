"""Count and sum shapes over grouped derived tables: constant regroupings, CASE arms on grouped counts, singleton
joins to a grouped COUNT, tuple counts, grouped SUM(COALESCE), SUM of grouped counts, key counts, GROUPING-set
expansions and integer filters carried across a grouped join.

Aim: ``collapse_constant_regroup``, ``fold_grouped_count_cases``, ``singleton_count_sum``, ``regroup_tuple_count``,
``drop_grouped_sum_coalesce``, ``sum_of_grouped_counts``, ``normalize_key_counts``, ``collapse_grouping_expansion`` and
``propagate_grouped_join_facts``, each with near misses a guard must decline (SUM over no rows is NULL, COUNT is 0,
``COUNT(col)`` skips NULLs, a grand-total group exists without rows).
"""

from ._base import expand

INNER_AGG = "{COUNT(*)|COUNT(t.x)|SUM(t.x)|MIN(t.x)|MAX(t.x)|AVG(t.x)|COUNT(DISTINCT t.x)|SUM(t.id)}"
INNER_WHERE = "{|WHERE t.x > 0|WHERE FALSE|WHERE t.x IS NULL}"
GROUPED_COUNT = "(SELECT p.tid AS k, {COUNT(*)|COUNT(p.id)|COUNT(p.n)|COUNT(DISTINCT p.n)} AS c FROM p {|WHERE p.n > 1} GROUP BY p.tid) AS g"
GROUPED_COUNT_STAR = "(SELECT p.tid AS k, COUNT(*) AS c FROM p {|WHERE p.n > 1} GROUP BY p.tid) AS g"
WEIGHT_SIDE = "(SELECT {t.id AS id, t.x AS x|t.id AS id, t.y AS y|t.y AS y, t.x AS x|t.id AS id} FROM t WHERE {t.id = 1|t.id = 2|t.y = 1|t.id = 1 AND t.x > 0|t.id > 1|t.id = 3 OR t.id = 4}) AS a"

TEMPLATES = [
    # --- collapse_constant_regroup: GROUP BY TRUE over GROUP BY TRUE --------------------------------------------
    f"SELECT {{SUM|MAX|MIN}}(d.c) AS n{{|, 1 AS k|, NULL AS z|, TRUE AS b}} FROM (SELECT {INNER_AGG} AS c FROM t {INNER_WHERE} GROUP BY TRUE) AS d GROUP BY TRUE",
    f"SELECT {{SUM|MAX|MIN}}(d.c) AS n FROM (SELECT {INNER_AGG} AS c, {INNER_AGG} AS e FROM t {INNER_WHERE} GROUP BY TRUE) AS d GROUP BY TRUE",
    "SELECT SUM(DISTINCT d.c) AS n FROM (SELECT {COUNT(*)|SUM(t.x)|MAX(t.x)} AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE",
    "SELECT {MAX|MIN|SUM}(d.c) AS n FROM (SELECT {COUNT(*)|SUM(t.x)} AS c FROM t JOIN u ON t.y = u.k {|WHERE u.w > 0} GROUP BY TRUE) AS d GROUP BY TRUE",
    # near misses: the outer select regroups, filters or reads more than one inner value
    "SELECT {AVG(d.c)|COUNT(d.c)|COUNT(DISTINCT d.c)} AS n FROM (SELECT {COUNT(*)|SUM(t.x)} AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE",
    "SELECT SUM(d.c) AS n FROM (SELECT {COUNT(*)|SUM(t.x)} AS c FROM t {|WHERE t.x > 0} GROUP BY TRUE) AS d",
    "SELECT SUM(d.c) AS n FROM (SELECT {COUNT(*)|SUM(t.x)} AS c FROM t) AS d GROUP BY TRUE",
    "SELECT SUM(d.c) AS n FROM (SELECT {COUNT(*)|SUM(t.x)} AS c FROM t GROUP BY TRUE) AS d {WHERE d.c > 1|WHERE d.c IS NULL|GROUP BY TRUE HAVING SUM(d.c) > 1|GROUP BY TRUE HAVING COUNT(*) > 0}",
    "SELECT SUM(d.c) AS n, d.c AS e FROM (SELECT COUNT(*) AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE, d.c",
    "SELECT SUM(d.c) AS n FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY TRUE, t.y) AS d GROUP BY TRUE",
    "SELECT MAX(d.k) AS n FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY TRUE, t.y) AS d GROUP BY TRUE",
    # --- fold_grouped_count_cases: a CASE arm that grouped counts cannot satisfy ---------------------------------
    f"SELECT t.id, CASE WHEN g.c {{= 0|< g.c|> g.c|<> g.c}} THEN {{'z'|1}} {{WHEN g.c > 1 THEN 'm'|}} ELSE {{'o'|2}} END AS a FROM t {{JOIN|LEFT JOIN}} {GROUPED_COUNT} ON t.id = g.k",
    f"SELECT t.id, CASE WHEN {{0 = g.c|g.c = 0}} THEN TRUE WHEN g.c IS NULL THEN TRUE ELSE FALSE END AS a FROM t {{JOIN|LEFT JOIN|FULL JOIN}} {GROUPED_COUNT} ON t.id = g.k",
    f"SELECT t.id FROM t {{JOIN|LEFT JOIN}} {GROUPED_COUNT} ON t.id = g.k WHERE CASE WHEN g.c {{= 0|< g.c|> g.c}} THEN TRUE ELSE FALSE END",
    f"SELECT t.id, CASE WHEN g.c <> g.c THEN 1 WHEN g.c = h.c THEN 2 END AS a FROM t JOIN {GROUPED_COUNT_STAR} ON t.id = g.k JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid) AS h ON t.id = h.k",
    f"SELECT t.id, CASE WHEN g.c = 0 AND t.x > 1 THEN 1 WHEN t.x IS NULL THEN 2 ELSE 3 END AS a FROM t {{JOIN|LEFT JOIN}} {GROUPED_COUNT} ON t.id = g.k",
    f"SELECT t.id, CASE WHEN g.c = 0 THEN 1 ELSE 0 END AS a FROM t LEFT JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid {{HAVING COUNT(*) > 1|}}) AS g ON t.id = g.k",
    "SELECT t.id, CASE WHEN g.c = 0 THEN 1 ELSE 0 END AS a FROM t LEFT JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY ROLLUP (p.tid)) AS g ON t.id = g.k",
    "SELECT t.id, CASE WHEN g.c = 0 THEN 1 ELSE 0 END AS a FROM t LEFT JOIN (SELECT COUNT(*) AS c, 1 AS k FROM p) AS g ON t.id = g.k",
    "SELECT t.id, CASE WHEN g.c = 0 THEN 1 ELSE 0 END AS a FROM t LEFT JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid, p.b) AS g ON t.id = g.k",
    "SELECT t.id, CASE WHEN COALESCE(g.c, 0) = 0 THEN 'none' ELSE 'some' END AS a FROM t LEFT JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid) AS g ON t.id = g.k",
    # --- singleton_count_sum: a one-row left side joined to a grouped COUNT(*) ------------------------------------
    "SELECT a.id{|, a.y}, a.{id|x|y} * g.c AS w{|, g.c * a.id AS w2} FROM (SELECT t.id AS id, t.x AS x, t.y AS y FROM t WHERE t.id = {1|2|3}) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS g ON a.{id|y} = g.k",
    "SELECT a.id, a.{id|x} * g.c AS w FROM (SELECT t.id AS id, t.x AS x FROM t WHERE t.id = {1|2}) AS a JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid) AS g ON a.id = g.k",
    "SELECT a.y, a.{id|x} * g.c AS w FROM (SELECT t.y AS y, t.id AS id, t.x AS x FROM t WHERE t.id = {1|2}) AS a JOIN (SELECT u.{k|w} AS k, COUNT(*) AS c FROM u GROUP BY u.{k|w}) AS g ON a.y = g.k",
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id = 2 {|AND t.x > 0}) AS a JOIN (SELECT p.tid AS k, COUNT(*) AS c FROM p GROUP BY p.tid) AS g ON a.id = g.k {|WHERE g.c > 1}",
    # near misses: the key is not fixed, the count is not COUNT(*), a second group key is unmatched, an outer join
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id, t.x AS x FROM t WHERE {t.y = 1|t.id > 1|t.id = 3 OR t.id = 4|t.x = 1}) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS g ON a.id = g.k",
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id = 1) AS a JOIN (SELECT u.k AS k, u.v AS v, COUNT(*) AS c FROM u GROUP BY u.k, u.v) AS g ON a.id = g.k",
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id = 1) AS a JOIN (SELECT u.k AS k, COUNT({u.w|u.k|DISTINCT u.w}) AS c FROM u GROUP BY u.k) AS g ON a.id = g.k",
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id = 1) AS a {LEFT JOIN|RIGHT JOIN|FULL JOIN} (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS g ON a.id = g.k",
    "SELECT a.id, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id = 1) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u {WHERE u.w > 0|} GROUP BY u.k {HAVING COUNT(*) > 1|}) AS g ON a.id = g.k",
    "SELECT a.s, a.y * g.c AS w FROM (SELECT t.s AS s, t.y AS y FROM t WHERE t.id = 1) AS a JOIN (SELECT u.v AS k, COUNT(*) AS c FROM u GROUP BY u.v) AS g ON a.s = g.k",
    "SELECT a.y, a.y * g.c AS w FROM (SELECT t.y AS y FROM t WHERE t.id = 1) AS a JOIN (SELECT CAST(u.v AS INT64) AS k, COUNT(*) AS c FROM u GROUP BY u.v) AS g ON a.y = g.k",
    "SELECT a.y, a.f * g.c AS w FROM (SELECT t.y AS y, t.f AS f FROM t WHERE t.id = 1) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS g ON a.y = g.k",
    # --- regroup_tuple_count: COUNT over the key columns of a finer grouping -------------------------------------
    f"SELECT g.k, COUNT({{g.x, g.z|g.z, g.x|g.x, g.z, g.k}}) AS c{{|, SUM(g.p) AS s|, MAX(g.p) AS m|, MIN(g.x) AS mx}} FROM (SELECT t.y AS k, t.x AS x, t.id AS z, {{SUM(t.id)|COUNT(t.x)|MAX(t.id)}} AS p FROM t GROUP BY t.y, t.x, t.id) AS g GROUP BY g.k",
    "SELECT g.k, COUNT(g.x, g.z) AS c FROM (SELECT t.y AS k, t.x AS x, t.s AS z FROM t GROUP BY t.y, t.x, t.s) AS g GROUP BY g.k",
    "SELECT g.k, COUNT(g.x, g.z) AS c, COUNT(*) AS n FROM (SELECT t.y AS k, t.x AS x, t.s AS z FROM t GROUP BY t.y, t.x, t.s) AS g GROUP BY g.k",
    "SELECT g.k, COUNT(g.x) AS c FROM (SELECT t.y AS k, t.x AS x, t.s AS z FROM t GROUP BY t.y, t.x, t.s) AS g GROUP BY g.k",
    "SELECT g.k, COUNT(g.x, g.z) AS c FROM (SELECT t.y AS k, t.x AS x, t.s AS z FROM t GROUP BY t.y, t.x, t.s) AS g WHERE g.x > 0 GROUP BY g.k",
    # --- drop_grouped_sum_coalesce -------------------------------------------------------------------------------
    "SELECT d.k, COALESCE(SUM(d.c), {0|5}) AS a FROM (SELECT t.y AS k, {COUNT(*)|COUNT(t.x)} AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, {COUNT(*)|COUNT(u.w)|1|u.k} AS c FROM u {|WHERE u.w > 0} GROUP BY u.k) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, {t.id|t.x|COUNT(*)|2} AS c FROM t {|GROUP BY t.y} UNION ALL SELECT u.k AS k, {u.k|u.w|COUNT(*)|3} AS c FROM u {|GROUP BY u.k}) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, {t.id|t.x} AS c FROM t) AS d GROUP BY d.k",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, t.id AS c FROM t UNION ALL SELECT u.k AS k, u.k AS c FROM u) AS d GROUP BY {d.k|ROLLUP (d.k)|GROUPING SETS ((d.k), ())}",
    "SELECT COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, t.id AS c FROM t UNION ALL SELECT u.k AS k, u.k AS c FROM u) AS d",
    "SELECT d.k, COALESCE(SUM(d.c), 0) AS a FROM (SELECT t.y AS k, t.id AS c FROM t LEFT JOIN u ON u.k = t.y UNION ALL SELECT u.k AS k, u.k AS c FROM u) AS d GROUP BY d.k",
    # --- sum_of_grouped_counts: a global SUM of per-group counts is NULL, not 0, over no rows ---------------------
    "SELECT {SUM(g.c)|SUM(g.c) + 1|SUM(g.c) * 2|SUM(g.c), SUM(g.c) + SUM(g.c)} AS a FROM (SELECT {t.y|t.x} AS k, {COUNT(*)|COUNT(t.x)|COUNT(DISTINCT t.x)} AS c FROM t {|WHERE t.x > 0|WHERE FALSE} GROUP BY {t.y|t.x|t.y, t.s}) AS g",
    "SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY {ROLLUP (t.y)|GROUPING SETS ((t.y), ())|CUBE (t.y)}) AS g",
    "SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y {|HAVING COUNT(*) > 1}) AS g",
    "SELECT {SUM|MAX|MIN|AVG|COUNT}(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g",
    "SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g {|WHERE g.k > 1}",
    "SELECT SUM(g.c) AS a, g.k FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g GROUP BY g.k",
    "SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t JOIN u ON t.y = u.k GROUP BY t.y) AS g",
    "SELECT SUM(g.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y LIMIT 2) AS g",
    # --- normalize_key_counts: COUNT(DISTINCT key) is COUNT(key) ------------------------------------------------
    "SELECT COUNT(DISTINCT {t.id|t.x|t.y}) AS a{|, COUNT(DISTINCT t.id) AS b|, COUNT(*) AS c} FROM t {|WHERE t.x > 0}",
    "SELECT t.y, COUNT(DISTINCT {t.id|t.x}) AS a FROM t GROUP BY t.y",
    "SELECT COUNT(DISTINCT {u.k|u.w}) AS a FROM u",
    "SELECT COUNT(DISTINCT t.id) AS a FROM t {JOIN u ON t.y = u.k|LEFT JOIN u ON t.y = u.k|JOIN p ON p.tid = t.id}",
    "SELECT COUNT(DISTINCT t.id) OVER (PARTITION BY t.y) AS a FROM t",
    "SELECT q.y, COUNT(DISTINCT q.id) AS a FROM (SELECT t.id, t.y FROM t) AS q GROUP BY q.y",
    "SELECT t.id, g.c FROM t JOIN (SELECT t2.id AS k, COUNT(DISTINCT t2.id) AS c FROM t AS t2 GROUP BY t2.id) AS g ON t.id = g.k",
    # --- collapse_grouping_expansion: one aggregate per grouping set, selected with FILTER ------------------------
    "SELECT k, COUNT(a) FILTER (WHERE g = 0) AS ca{|, MAX(s) FILTER (WHERE g = 1) AS m|, SUM(s) FILTER (WHERE g = 1) AS ss|, COUNT(a) FILTER (WHERE g = 1) AS cb|, SUM(s) FILTER (WHERE g = 0) AS sa} FROM (SELECT t.y AS k, t.x AS a, GROUPING(t.x) AS g, SUM(t.id) AS s FROM t {|WHERE t.id > 1} GROUP BY GROUPING SETS ((t.y, t.x), (t.y))) AS d GROUP BY k",
    "SELECT {COUNT(a) FILTER (WHERE g = 0)|MAX(s) FILTER (WHERE g = 1)|SUM(s) FILTER (WHERE g = 1)|MAX(s) FILTER (WHERE g = 0)|COUNT(a) FILTER (WHERE g = 1)}{| AS ca, COUNT(a) FILTER (WHERE g = 0)} AS cb FROM (SELECT t.x AS a, GROUPING(t.x) AS g, SUM(t.id) AS s FROM t {WHERE t.id > 1|} GROUP BY GROUPING SETS ((t.x), ())) AS d",
    "SELECT k, COUNT(a) FILTER (WHERE g = 0) AS ca FROM (SELECT t.y AS k, t.x AS a, GROUPING(t.x) AS g FROM t GROUP BY GROUPING SETS ((t.y, t.x), (t.y), {(t.y, t.x)|()|(t.y)})) AS d GROUP BY k",
    "SELECT COUNT(a) FILTER (WHERE g = 0) AS ca, COUNT(a) AS cn FROM (SELECT t.x AS a, GROUPING(t.x) AS g FROM t GROUP BY GROUPING SETS ((t.x), ())) AS d",
    "SELECT k, COUNT(DISTINCT a) FILTER (WHERE g = 0) AS ca FROM (SELECT t.y AS k, t.x AS a, GROUPING(t.x) AS g FROM t GROUP BY GROUPING SETS ((t.y, t.x), (t.y))) AS d GROUP BY k",
    "SELECT k, COUNT(a) FILTER (WHERE g = 0) AS ca FROM (SELECT t.y AS k, t.x AS a, GROUPING(t.x) AS g FROM t GROUP BY GROUPING SETS ((t.y, t.x), (t.y)) HAVING COUNT(*) > 1) AS d GROUP BY k",
    "SELECT k, COUNT(a) FILTER (WHERE g = 0) AS ca FROM (SELECT t.y AS k, t.x AS a, GROUPING(t.x) AS g FROM t GROUP BY GROUPING SETS ((t.y, t.x), (t.y))) AS d WHERE k > 1 GROUP BY k",
    # --- propagate_grouped_join_facts: a filter on a grouped key carried across an equality ----------------------
    f"SELECT {{a.id|a.id, g.c}}, a.id * g.c AS w FROM (SELECT t.id AS id FROM t WHERE t.id {{=|>|<|>=|<=|<>}} {{2|0|3}}) AS a JOIN (SELECT p.tid AS k, {{COUNT(*)|SUM(p.id)}} AS c FROM p GROUP BY p.tid) AS g ON a.id = g.k",
    "SELECT g.k, g.c, a.id FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid {>|=|<=} {1|2} GROUP BY p.tid) AS g JOIN t AS a ON a.id = g.k {|JOIN u ON u.k = a.id}",
    "SELECT g.k, a.id FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid, p.b) AS g {JOIN|LEFT JOIN|FULL JOIN} t AS a ON a.id = g.k",
    "SELECT g.k, a.id FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g JOIN t AS a ON a.{id|x|y} = g.k WHERE a.y < 3",
    "SELECT g.k, a.f FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g JOIN t AS a ON a.f = g.k",
    "SELECT g.k, a.s FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g JOIN t AS a ON a.s = g.k",
    "SELECT g.k, h.k FROM (SELECT p.tid AS k, COUNT(*) AS c FROM p WHERE p.tid > 1 GROUP BY p.tid) AS g JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS h ON h.k = g.k",
    "SELECT g.c, a.id FROM (SELECT COUNT(*) AS c, 1 AS k FROM p WHERE p.tid > 1) AS g JOIN t AS a ON a.id = g.k",
    "SELECT g.k, a.id FROM (SELECT p.tid AS k, SUM(p.id) AS c FROM p WHERE p.tid + 1 > 1 GROUP BY p.tid) AS g JOIN t AS a ON a.id = g.k",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "count_shapes")
