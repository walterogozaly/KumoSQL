"""Aggregate forms the normalizer rewrites into one another: constant COUNT and GROUP BY keys, key aggregates,
shifted sums, mean times count, filters folded into a grouping, GROUP BY of a union as DISTINCT, aggregates over
UNION ALL, COUNTIF/AVG/COUNT(DISTINCT) variants and global aggregates over empty input.

Aim: ``_constant_counts``, ``_constant_keys``, ``_drop_constant_groupings`` (a Calcite-style GROUP BY of
constants, via ``options.group_by_constants``), ``_mean_times_count``, ``_regroup_distinct``, ``_fold_filter_into_grouping``,
``_group_by_to_distinct``, ``_key_aggregates``, ``_shifted_sums``, ``_fold_count_coalesce``, ``_split_aggregates`` and
``aggregate_rules.rewrite_aggregates``. SUM over no rows is NULL, COUNT is 0, COUNT(col) skips NULLs and a global
aggregate returns one row over empty input.
"""

from ._base import expand

CONST = "{'a'|NULL|2.5|1 + 1|CAST(3 AS INT64)|TRUE|COALESCE(NULL, 2)}"

TEMPLATES = [
    # --- _constant_counts: COUNT(NULL) is 0 unless it is the select's only aggregate (a global one-row result) ---
    "SELECT {COUNT(NULL)|COUNT(NULL), COUNT(*)|COUNT(NULL), SUM(t.x)|COUNT(NULL) + 1|COUNT(DISTINCT NULL), COUNT(*)} AS a{|, COUNT(*) AS b} FROM t {|WHERE t.x > 0|WHERE FALSE}",
    "SELECT t.y, {COUNT(NULL)|COUNT(NULL) + COUNT(t.x)|COUNT(DISTINCT NULL)} AS a FROM t {|WHERE t.x > 0} GROUP BY t.y",
    "SELECT COUNT(NULL) AS a FROM t {|WHERE FALSE|WHERE t.x > 0} {|GROUP BY t.y|GROUP BY TRUE}",
    "SELECT d.k, SUM(d.c) AS a FROM (SELECT t.y AS k, COUNT(NULL) AS c FROM t GROUP BY t.y) AS d GROUP BY d.k",
    # --- _constant_keys: a number left in GROUP BY or ORDER BY is a constant ------------------------------------
    f"SELECT {{t.y, |}}COUNT(*) AS n FROM t GROUP BY {{t.y, |}}{{1 + 1|CAST(3 AS INT64)|2.5}}",
    "SELECT 1 AS c, COUNT(*) AS n, SUM(t.x) AS s FROM t {|WHERE t.x > 0} GROUP BY 1",
    "SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y ORDER BY {2.5, t.y|1, 2.5|t.y, 1 + 1}",
    # --- _key_aggregates: MIN/MAX/SUM(DISTINCT) of a group key is the key ------------------------------------
    "SELECT t.y, {MIN(t.y)|MAX(t.y)|SUM(DISTINCT t.y)|COUNT(DISTINCT t.y)|SUM(t.y)|AVG(t.y)|MIN(DISTINCT t.y)} AS a{|, MAX(t.x) AS b} FROM t {|WHERE t.x > 0} GROUP BY t.y",
    "SELECT t.y, MIN(t.y) AS a, MAX(t.y) AS b, SUM(DISTINCT t.y) AS c, SUM(t.y) AS d FROM t GROUP BY t.y {|HAVING MAX(t.y) > 1|HAVING COUNT(*) > 1}",
    "SELECT t.y, MIN(t.y + 1) AS a, MAX(t.y * 2) AS b, SUM(DISTINCT t.y + 1) AS c, COUNT(DISTINCT t.y) AS d FROM t GROUP BY t.y",
    "SELECT t.y, t.x, MIN(t.y) AS a, MAX(t.x) AS b, SUM(DISTINCT t.x) AS c FROM t GROUP BY {t.y, t.x|ROLLUP (t.y, t.x)|GROUPING SETS ((t.y), (t.x))}",
    "SELECT t.y, MIN(t.y) FILTER (WHERE t.x > 1) AS a, MAX(t.y) OVER (PARTITION BY t.y) AS b FROM t GROUP BY t.y",
    # --- _shifted_sums: SUM(x + c) is SUM(x) + c * COUNT(x) --------------------------------------------------
    "SELECT {SUM(t.x + 1)|SUM(1 + t.x)|SUM(t.x - 2)|SUM(t.x + 0)|SUM(t.f + 1)|SUM(t.id - 1)|SUM(t.x + 1) + SUM(t.x - 1)|SUM(DISTINCT t.x + 1)|SUM(2 - t.x)} AS a FROM t {|WHERE t.x > 0|WHERE FALSE}",
    "SELECT t.y, {SUM(t.x + 1)|SUM(t.x - 3)|SUM(t.x + 1) / COUNT(t.x)|SUM(t.x + 1), COUNT(t.x)} AS a FROM t GROUP BY t.y",
    "SELECT SUM(p.n + {1|1.5}) AS a, SUM(p.n - 2) AS b FROM p {|WHERE p.tid IS NULL}",
    "SELECT t.y, SUM(t.x + t.y) AS a, SUM((t.x + 1)) AS b FROM t GROUP BY t.y",
    # --- _mean_times_count: AVG(x) * COUNT(x) is SUM(x). AVG is a double, so over INT64 values past 2^53 the product
    # rounds where the sum does not; these templates read FLOAT64 and NUMERIC columns, where the identity holds to
    # the digits the harness compares.
    "SELECT {m.a * m.n|m.n * m.a|m.a * m.n + 1} AS p{|, m.y} FROM (SELECT t.y, AVG(t.f) AS a, COUNT(t.f) AS n FROM t GROUP BY t.y) AS m",
    "SELECT SUM(m.a * m.n) AS p FROM (SELECT t.y, AVG(t.f) AS a, COUNT(t.f) AS n FROM t {|WHERE t.x > 1} GROUP BY t.y) AS m",
    "SELECT SUM(m.a * m.n) / SUM(m.n) AS p FROM (SELECT t.y, AVG(t.f) AS a, COUNT(t.f) AS n FROM t GROUP BY t.y) AS m",
    "SELECT SUM(m.a * m.n) AS p FROM (SELECT p.tid, AVG(p.n) AS a, COUNT(p.n) AS n FROM p GROUP BY p.tid) AS m",
    "SELECT m.a * m.n AS p FROM (SELECT t.y, AVG(t.f) AS a, COUNT({*|t.id|t.y|t.x}) AS n FROM t GROUP BY t.y) AS m",
    "SELECT m.a * m.n AS p FROM (SELECT t.y, AVG({t.f|DISTINCT t.f|t.x}) AS a, COUNT({t.f|DISTINCT t.f|t.f}) AS n FROM t GROUP BY t.y) AS m WHERE m.n > 0",
    "SELECT m.a * m.n AS p FROM (SELECT AVG(t.f) AS a, COUNT(t.f) AS n FROM t {|WHERE FALSE}) AS m",
    # --- _regroup_distinct (mostly shadowed by distinct_rules.regroup_distinct; these are its variants) ---------
    f"SELECT g.k, {{SUM|COUNT|AVG|MIN|MAX}}(g.x) AS s{{|, SUM(g.p) AS q|, COALESCE(SUM(g.n), 0) AS c|, 7 AS z|, MAX(g.m) AS mm}} FROM (SELECT t.y AS k, t.x AS x, SUM(t.id) AS p, COUNT(*) AS n, MAX(t.id) AS m FROM t GROUP BY t.y, t.x) AS g GROUP BY g.k",
    "SELECT g.k, SUM(g.p) AS q, COUNT(g.z) AS c FROM (SELECT t.y AS k, t.x AS x, t.id AS z, SUM(t.id) AS p FROM t GROUP BY t.y, t.x, t.id) AS g GROUP BY g.k",
    "SELECT {SUM(g.p)|COALESCE(SUM(g.n), 0)|SUM(g.n)|SUM(g.x)} AS q FROM (SELECT t.x AS x, SUM(t.id) AS p, COUNT(*) AS n FROM t {|WHERE FALSE} GROUP BY t.x) AS g {|GROUP BY g.x}",
    "SELECT g.k, COUNT(g.x) AS c FROM (SELECT t.y AS k, t.x AS x FROM t GROUP BY t.y, t.x {|HAVING COUNT(*) > 1}) AS g GROUP BY g.k",
    "SELECT g.k, SUM(g.x) AS c, 1 AS z FROM (SELECT t.y AS k, t.x AS x FROM t GROUP BY t.y, t.x) AS g GROUP BY g.k",
    # an unaliased derived table is out of distinct_rules' reach, so these reach _regroup_distinct
    "SELECT k, {SUM(p)|SUM(x)|COUNT(x)|AVG(x)|MIN(x)|MAX(x)|MAX(m)|COALESCE(SUM(n), 0)|SUM(n)|COUNT(p)|SUM(DISTINCT x)}{|, SUM(p)|, 7|, k} AS a FROM (SELECT t.y AS k, t.x AS x, SUM(t.id) AS p, COUNT(*) AS n, MAX(t.id) AS m FROM t {|WHERE t.id > 1} GROUP BY t.y, t.x) GROUP BY k",
    "SELECT k, {SUM(p)|COUNT(x)|COUNT(z)|MIN(x)|COALESCE(SUM(n), 0)} AS a FROM (SELECT t.y AS k, t.x AS x, t.id AS z, SUM(t.id) AS p, COUNT(t.x) AS n FROM t GROUP BY t.y, t.x, t.id) GROUP BY k",
    "SELECT {SUM(p)|COALESCE(SUM(n), 0)|SUM(n)|SUM(x)|COUNT(x)} AS a FROM (SELECT t.x AS x, SUM(t.id) AS p, COUNT(*) AS n FROM t {|WHERE FALSE} GROUP BY t.x)",
    "SELECT k, SUM(p) AS a FROM (SELECT t.y AS k, t.x AS x, SUM(t.id) AS p FROM t GROUP BY t.y, t.x {|HAVING COUNT(*) > 1}) {|WHERE k > 0} GROUP BY k",
    # --- _fold_filter_into_grouping: a filter over a grouped derived table is its HAVING -------------------------
    "SELECT {s|s + 1|k} FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS m WHERE {s > 1|s IS NULL|s IS NOT NULL|k > 0 AND s > 1|k = 1}",
    "SELECT m.n FROM (SELECT t.y, COUNT(*) AS n FROM t {|WHERE t.x > 0} GROUP BY t.y {|HAVING MAX(t.x) > 0}) AS m WHERE m.n {>|=|<} 2",
    "SELECT m.n FROM (SELECT COUNT(*) AS n, SUM(t.x) AS s FROM t {|WHERE FALSE}) AS m WHERE {m.n > 0|m.n = 0|m.s IS NULL|m.s > 1}",
    "SELECT 1 AS a FROM (SELECT COUNT(*) AS n FROM t {|WHERE FALSE}) AS m WHERE m.n {>|=} 0",
    "SELECT m.k FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY {t.y|ROLLUP (t.y)}) AS m WHERE m.s > 1",
    "SELECT m.s FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS m WHERE m.k IN (SELECT u.k FROM u)",
    "SELECT SUM(m.s) AS q FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS m WHERE m.s > 1",
    # --- _group_by_to_distinct: GROUP BY of a derived union with no aggregate ------------------------------------
    "SELECT m.k{|, m.x} FROM (SELECT t.y AS k, t.x AS x FROM t {UNION ALL|UNION DISTINCT|INTERSECT DISTINCT|EXCEPT DISTINCT} SELECT u.k, u.w FROM u) AS m GROUP BY m.k{|, m.x}",
    "SELECT m.k, 1 AS one FROM (SELECT t.y AS k FROM t UNION ALL SELECT u.k FROM u) AS m GROUP BY m.k",
    "SELECT m.k, MAX(m.k) AS mk FROM (SELECT t.y AS k FROM t UNION ALL SELECT u.k FROM u) AS m GROUP BY m.k",
    "SELECT m.k FROM (SELECT t.y AS k FROM t UNION ALL SELECT u.k FROM u) AS m GROUP BY m.k {HAVING m.k > 1|ORDER BY m.k LIMIT 2|}",
    "SELECT m.k + 1 AS k FROM (SELECT t.y AS k FROM t UNION ALL SELECT u.k FROM u) AS m GROUP BY m.k + 1",
    # --- _fold_count_coalesce: COALESCE(COUNT(..), 0) is the count --------------------------------------------
    "SELECT {COALESCE(COUNT(t.x), 0)|COALESCE(COUNT(*), 0)|COALESCE(COUNT(t.x), 1)|COALESCE(SUM(t.x), 0)|COALESCE(COUNT(DISTINCT t.x), 0)} AS a{|, MAX(t.x) AS b} FROM t {|WHERE t.x > 0|WHERE FALSE}",
    "SELECT t.y, COALESCE(COUNT(t.x), 0) AS a FROM t GROUP BY t.y",
    # --- _split_aggregates / aggregates over UNION ALL ----------------------------------------------------------
    "SELECT {SUM|MAX|MIN|COUNT|AVG}(d.c) AS a{|, COUNT(*) AS n|, SUM(d.c) AS s2} FROM (SELECT t.x AS c FROM t {|WHERE t.x > 0} UNION ALL SELECT u.w AS c FROM u {|WHERE FALSE}) AS d",
    "SELECT d.k, {SUM(d.c)|MAX(d.c)|MIN(d.c)|COUNT(d.c)|AVG(d.c)|COUNT(DISTINCT d.c)} AS a FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k",
    "SELECT d.k, SUM(d.c) AS a FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u UNION ALL SELECT p.tid AS k, p.id AS c FROM p) AS d {WHERE d.c > 1|} GROUP BY d.k {|HAVING SUM(d.c) > 1}",
    "SELECT SUM(d.c) AS a, COUNT(d.c) AS b FROM (SELECT t.x AS c FROM t UNION ALL SELECT u.w AS c FROM u) AS d WHERE FALSE",
    "SELECT d.k, SUM(d.c) AS a FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y UNION ALL SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS d GROUP BY d.k",
    "SELECT SUM(d.c) AS a, MAX(d.c) AS m FROM (SELECT COUNT(*) AS c FROM t UNION ALL SELECT COUNT(*) AS c FROM u) AS d",
    "SELECT SUM(d.a) / COUNT(d.a) AS m FROM (SELECT t.x AS a FROM t UNION ALL SELECT u.w AS a FROM u) AS d",
    "SELECT d.k, SUM(d.c) + 1 AS a, COUNT(d.c) * 2 AS b FROM (SELECT t.y AS k, t.x AS c FROM t UNION ALL SELECT u.k AS k, u.w AS c FROM u) AS d GROUP BY d.k",
    # --- rewrite_aggregates: COUNTIF, COUNT(DISTINCT), AVG, SUM of an empty input, key aggregates ------------------
    "SELECT {COUNTIF(t.x > 1)|COUNTIF(t.x IS NULL)|COUNTIF(FALSE)|SUM(t.x)|AVG(t.x)|COUNT(DISTINCT t.x)|COUNT(t.x)|COUNT(*)|MAX(t.x)}{|, COUNT(*)} AS a FROM t WHERE {FALSE|1 = 0|t.x IS NULL AND t.x IS NOT NULL|t.x > 0}",
    "SELECT t.y, {COUNTIF(t.x > 1)|AVG(t.x)|COUNT(DISTINCT t.x)|SUM(DISTINCT t.x)|COUNTIF(t.s = 'a')} AS a FROM t {|WHERE t.x > 0} GROUP BY t.y",
    "SELECT COUNT(*) AS a, MAX(1) AS b, MIN(NULL) AS c, SUM(2) AS d, COUNT(7) AS e",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 0 THEN 1 END) AS b, COUNT(CASE WHEN t.x > 0 THEN t.id END) AS c FROM t",
    "SELECT {COUNTIF(t.x > 1)|COUNT(CASE WHEN t.x > 1 THEN 1 END)|AVG(CASE WHEN t.x > 1 THEN t.x END)|SUM(CASE WHEN t.x > 1 THEN t.y END)|MAX(CASE WHEN t.x > 1 THEN t.y END)|COUNT(DISTINCT CASE WHEN t.x > 1 THEN t.y END)|SUM(IF(t.x > 1, t.y))} AS a{|, SUM(CASE WHEN t.x > 1 THEN t.y END) AS b|, COUNT(CASE WHEN t.x > 1 THEN 1 END) AS c|, MIN(CASE WHEN t.x > 1 AND t.y > 0 THEN t.y END) AS d|, COUNT(*) AS e|, SUM(CASE WHEN t.x > 1 THEN t.y ELSE 0 END) AS f} FROM t {|WHERE t.s = 'a'|WHERE FALSE} {|GROUP BY t.s}",
    "SELECT t.y, SUM(CASE WHEN t.x > 0 AND t.y > 1 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 0 AND t.y > 1 THEN t.x END) AS b FROM t {|WHERE t.id > 1} GROUP BY t.y",
    "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) AS a, COUNT(CASE WHEN t.x > 1 THEN 1 END) AS b FROM t",
    "SELECT t.y, COUNT(t.x) AS a FROM t WHERE {t.x = t.y|t.x > 2|t.x IS NOT NULL|t.x IN (1, 2)|t.x IS NULL} GROUP BY t.y",
    "SELECT COUNT({t.x|t.y|t.s}) AS a, COUNT(*) AS b FROM t WHERE {t.x = t.y|t.x > 2|t.y <> 3|t.x BETWEEN 1 AND 2|NOT (t.x < 1)}",
    "SELECT t.y, COUNT(*) AS a FROM t GROUP BY t.y HAVING {COUNT(*) >= 1|COUNT(*) > 0|COUNT(*) = 0|COUNT(*) < 1|COUNT(*) <= 1|COUNT(t.x) >= 1}",
    "SELECT t.y, {MIN|MAX}(t.y + 1) AS a, {MIN|MAX}(t.y * 2) AS b, SUM(DISTINCT t.y + 1) AS c, COUNT(DISTINCT t.y) AS d, COUNT(DISTINCT t.y + 1) AS e FROM t GROUP BY t.y",
    "SELECT t.y, t.x, COUNT(DISTINCT t.y) AS d, COUNT(DISTINCT t.x) AS e, COUNT(DISTINCT t.y + t.x) AS f FROM t GROUP BY t.y, t.x",
    "SELECT t.y FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) {>= 1|> 0|>= 2|= 0}",
    "SELECT t.y, COUNT(*) AS n FROM t GROUP BY t.y HAVING SUM(CASE WHEN t.x > 1 THEN 1 ELSE 0 END) >= 1",
    "SELECT d.k, d.c FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS d WHERE d.c {= 1|> 1|> 0}",
    "SELECT t.id, g.m + 1 AS z, g.m * 2 AS y FROM t {JOIN|LEFT JOIN} (SELECT p.tid AS k, MAX(p.n) * 2 AS m FROM p GROUP BY p.tid) AS g ON t.id = g.k",
    "SELECT d.k, BOOL_AND(d.c) AS a, LOGICAL_OR(d.c) AS b FROM (SELECT t.y AS k, t.x > 1 AS c FROM t UNION ALL SELECT u.k, u.w > 1 FROM u) AS d GROUP BY d.k",
    "SELECT a.y, a.s, b.c FROM (SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y) AS a {JOIN|LEFT JOIN} (SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y) AS b ON a.y = b.y",
    "SELECT a.y, a.s, b.s FROM (SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y) AS a JOIN (SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y) AS b ON a.y = b.y",
    "SELECT a.s * b.c AS z FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS b ON a.k = b.k",
    "SELECT SUM(z.v) AS q FROM (SELECT a.s * b.c AS v FROM (SELECT t.y AS k, SUM(t.x) AS s FROM t GROUP BY t.y) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS b ON a.k = b.k) AS z",
    "SELECT d.x, d.n FROM (SELECT t.x, 1 AS n FROM t UNION ALL SELECT u.w, COUNT(*) FROM u GROUP BY u.w) AS d WHERE d.n {> 0|= 1}",
    "SELECT d.m FROM (SELECT p.tid AS k, MAX(p.n) AS m FROM p GROUP BY p.tid UNION ALL SELECT u.k, COUNT(*) FROM u GROUP BY u.k) AS d WHERE d.m > 1",
    "SELECT SUM(m.s) AS a, MAX(m.s) AS b, MIN(m.s) AS c FROM (SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y {|HAVING COUNT(*) > 1}) AS m",
    "SELECT COALESCE(SUM(m.n), 0) AS a FROM (SELECT t.y, COUNT({*|t.x}) AS n FROM t {|WHERE t.id > 1} GROUP BY t.y) AS m",
    "SELECT {MAX|MIN}(m.s) AS a FROM (SELECT t.y, {MAX|MIN}(t.x) AS s FROM t GROUP BY t.y) AS m",
]

# Calcite reads a literal in GROUP BY as a constant (``options.group_by_constants``), which ``_drop_constant_groupings`` drops
CONSTANT_GROUPING = [
    f"SELECT {{t.y, |}}COUNT(*) AS n FROM t {{|WHERE t.x > 0|WHERE FALSE}} GROUP BY {{t.y, |}}{CONST}{{|, t.x}}",
    f"SELECT COUNT(*) AS n, SUM(t.x) AS s FROM t {{|WHERE t.x > 0|WHERE FALSE}} GROUP BY {CONST}",
    f"SELECT d.n FROM (SELECT COUNT(*) AS n FROM t GROUP BY {CONST}) AS d",
    f"SELECT t.y, COUNT(*) AS n FROM t GROUP BY ROLLUP (t.y, {CONST})",
    f"SELECT t.y, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((t.y, {CONST}), ({CONST}))",
]


def cases(seed: int, count: int) -> list[dict]:
    out = expand(TEMPLATES + CONSTANT_GROUPING, seed, count, "aggregate_forms")
    for index, case in enumerate(out):
        if index % (len(TEMPLATES) + len(CONSTANT_GROUPING)) >= len(TEMPLATES):
            case["options"] = {"group_by_constants": True}
    return out
