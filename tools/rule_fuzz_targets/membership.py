"""Membership and existence rules: ``IN``/``NOT IN`` in a select list (``_select_list_in_to_exists``,
``normalize_projected_in``), over a grouped or unioned subquery (``_drop_group_in_membership_tests``,
``_grouped_in_to_derived``, ``_in_over_union``), quantified comparisons (``rewrite_quantified``), semi and anti
joins (``_semi_joins_to_exists``), EXISTS inside a derived table (``_pull_up_exists``, ``_drop_implied_exists``) and
the lateral boolean group (``nullable_lateral_boolean_group``).

Three-valued logic is what these rules are about: each shape runs under constraint sets where the outer column, the
subquery column, both or neither are NOT NULL, and the near misses (a nullable side, a ``LIMIT``, a grouping set, an
outer join that pads the tested column) are what a guard has to decline.
"""

from ._constraints import T, expand_constrained

ALL = ("not_null", "left_not_null", "right_not_null", "plain", "nullable_key", "no_key")
NN = ("not_null", "left_not_null", "right_not_null", "plain")
KEYS = ("plain", "nullable_key", "no_key", "not_null", "fk")

TEMPLATES = [
    # --- _select_list_in_to_exists / normalize_projected_in: a membership value in the select list -------------------
    T("SELECT t.id, t.x {IN|NOT IN} (SELECT u.w FROM u {|WHERE u.k > 1|WHERE u.k = t.y|WHERE u.w > 0|WHERE FALSE}) AS m FROM t", *ALL),
    T("SELECT t.id, NOT (t.x IN (SELECT u.w FROM u {|WHERE u.k > 1})) AS m FROM t", *ALL),
    T("SELECT {x|t.x|(t.x)|T.X} {IN|NOT IN} (SELECT {w|u.w|(u.w)|U.W} FROM u {|WHERE u.k > 1}) AS m FROM t", *ALL),
    T("SELECT t.id, (t.x) IN (SELECT (u.w) FROM u {|WHERE u.k > 1|WHERE u.k = t.y}) AS m FROM t", *ALL),
    T("SELECT t.id, (t.x) NOT IN (SELECT u.w FROM u {|WHERE u.k = t.y}) AS m FROM t", *ALL),
    T("SELECT t.id, {t.x IN (SELECT u.w FROM u)|t.y IN (SELECT u.k FROM u)|t.x IN (SELECT u.w FROM u WHERE u.k > t.y)} AS m, COUNT(*) OVER () AS c FROM t", *ALL),
    T("SELECT d.id, d.m FROM (SELECT t.id AS id, t.x IN (SELECT {u.w|(u.w)} FROM u {|WHERE u.k > 0}) AS m FROM t) AS d", *ALL),
    T("SELECT t.id, CASE WHEN t.x IN (SELECT u.w FROM u) THEN 1 ELSE 0 END AS m FROM t", *ALL),
    T("SELECT t.id, t.x IN (SELECT d.w FROM (SELECT u.w FROM u {|WHERE u.k > 1}) AS d) AS m FROM t", *ALL),
    T("SELECT t.id, CASE WHEN EXISTS (SELECT 1 FROM u WHERE u.k = t.y {|AND u.w > 0}) THEN TRUE ELSE FALSE END AS m FROM t", *ALL),
    T("SELECT t.id, CASE WHEN EXISTS (SELECT 1 FROM u WHERE u.k = t.y) THEN {TRUE|FALSE|1|NULL} ELSE {FALSE|TRUE|NULL|0} END AS m FROM t", *ALL),
    T("SELECT t.x IN (SELECT u.w FROM u) AS m, COUNT(*) AS c FROM t GROUP BY t.x", *ALL),
    T("SELECT t.x IN (SELECT u.w FROM u) AS m, COUNT(*) AS c FROM t GROUP BY {ROLLUP(t.x)|CUBE(t.x)|GROUPING SETS ((t.x), ())}", *ALL),
    T("SELECT COUNT(*) AS c, MAX(t.x IN (SELECT u.w FROM u)) AS m FROM t", *ALL),
    T("SELECT t.id, ({t.x|t.x + 1|t.y}, {t.y|t.x}) IN (SELECT u.w, u.k FROM u {|WHERE u.k > 0}) AS m FROM t", *ALL),
    T("SELECT t.id, t.x IN (SELECT u.w FROM u {GROUP BY u.w|LIMIT 1|LIMIT 5 OFFSET 1|ORDER BY u.w LIMIT 2}) AS m FROM t", *ALL),
    T("SELECT t.id, t.x IN (SELECT {DISTINCT u.w|MAX(u.w)|u.w + 1|COUNT(*)|u.w * 1.0|CAST(u.w AS FLOAT64)|1} FROM u) AS m FROM t", *ALL),
    T("SELECT t.id, p.tid {IN|NOT IN} (SELECT u.k FROM u) AS m FROM t {LEFT JOIN|RIGHT JOIN|FULL JOIN|JOIN} p ON p.tid = t.id", *ALL),
    T("SELECT p.id, t.x {IN|NOT IN} (SELECT u.w FROM u) AS m FROM p LEFT JOIN t ON p.tid = t.id", *ALL),
    T("SELECT t.id, t.x IN (SELECT u.w FROM u WHERE u.k = p.id) AS m FROM t JOIN p ON p.tid = t.id", *ALL),
    T("SELECT t.id, t.x IN (SELECT t.w FROM u AS t) AS m FROM t", *ALL),
    T("SELECT t.id, t.x IN (SELECT w FROM u) AS m, x IN (SELECT w FROM u) AS n FROM t", *ALL),
    T("WITH u AS (SELECT t.id AS k, t.x AS w FROM t WHERE t.x > 0) SELECT t.id, t.x IN (SELECT u.w FROM u) AS m FROM t", *ALL),
    # --- _drop_group_in_membership_tests ------------------------------------------------------------------------------
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u {|WHERE u.k > 1|WHERE u.k = t.y} GROUP BY u.w)", *ALL),
    T("SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT {1|u.w|u.w, 1|u.v|NULL|u.w + 1|COUNT(*)|MAX(u.w)} FROM u {|WHERE u.k = t.y} GROUP BY {u.w|u.v|u.w, u.v|()|ROLLUP(u.w)|GROUPING SETS ((u.w), ())})", *ALL),
    T("SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y GROUP BY u.w {|HAVING COUNT(*) > 1|HAVING u.w > 0|HAVING FALSE|LIMIT 0})", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w {HAVING u.w > 0|HAVING COUNT(*) > 1|HAVING MAX(u.k) > 1|}) {|OR t.y = 1}", *ALL),
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT {u.w|u.w + 1|u.w, u.k|MAX(u.w)|u.k} FROM u GROUP BY {u.w|u.k|u.w, u.k|u.w + 1|()})", *ALL),
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u GROUP BY u.w {LIMIT 2|ORDER BY u.w LIMIT 1|ORDER BY u.w})", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT {DISTINCT u.w|u.w} FROM u GROUP BY u.w)", *ALL),
    T("SELECT t.id, EXISTS (SELECT 1 FROM u WHERE u.k = t.y GROUP BY {u.w|()}) AS e FROM t", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u UNION ALL SELECT u.k FROM u GROUP BY u.k)", *ALL),
    # --- _grouped_in_to_derived: an IN over a grouped subquery with a HAVING ----------------------------------------
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u GROUP BY u.w HAVING {COUNT(*) > 1|COUNT(*) = 1|MAX(u.k) > 1|SUM(u.k) > 2|COUNT(u.k) > 0|MIN(u.k) IS NULL|COUNT(*) > 1 AND MAX(u.k) > 0|COUNT(*) > 1 OR u.w > 2|u.w > 0|u.w IS NULL OR COUNT(*) > 1|NOT COUNT(*) > 1|COUNT(DISTINCT u.k) > 1|AVG(u.k) > 1})", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u {|WHERE u.k > 0|WHERE u.k = t.y|WHERE u.k < t.id} GROUP BY u.w HAVING {COUNT(*) > 1|MAX(u.k) > 1|COUNT(*) > t.id|SUM(u.k) > t.y})", *ALL),
    T("SELECT t.id, t.x IN (SELECT u.w FROM u GROUP BY u.w HAVING COUNT(*) > 1) AS m FROM t", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w HAVING {COUNT(*) > 1|SUM(u.k) > 1} {ORDER BY u.w|LIMIT 1|ORDER BY u.w LIMIT 2|})", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT {u.w|u.w AS z|MAX(u.k)|u.w, u.w|u.w + 1} FROM u GROUP BY u.w HAVING COUNT(*) > 0)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY {u.w, u.v|u.w|ROLLUP(u.w)|CUBE(u.w)|GROUPING SETS ((u.w), ())} HAVING COUNT(*) > 1)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u JOIN p ON p.tid = u.k GROUP BY u.w HAVING {SUM(p.n) > 1|COUNT(p.id) > 1|COUNT(*) > 1})", *ALL),
    T("SELECT t.id FROM t WHERE (t.x, t.y) IN (SELECT u.w, u.k FROM u GROUP BY u.w, u.k HAVING COUNT(*) > 0)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w HAVING COUNT(*) > (SELECT COUNT(*) FROM p))", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u GROUP BY u.w HAVING {MAX(COUNT(*)) > 0|SUM(u.k) OVER () > 0|EXISTS (SELECT 1 FROM p WHERE p.tid = u.w)|COUNT(*) > 1 QUALIFY TRUE})", *ALL),
    # --- _in_over_union ------------------------------------------------------------------------------------------------
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u {|WHERE u.k > 1} UNION ALL SELECT u.k FROM u {|WHERE u.w > 1})", *ALL),
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u UNION DISTINCT SELECT t2.y FROM t AS t2)", *ALL),
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u UNION ALL SELECT u.k FROM u UNION ALL SELECT t2.id FROM t AS t2)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u WHERE {FALSE|u.w IS NULL|u.k = t.y} UNION ALL SELECT u.k FROM u {WHERE FALSE|})", *ALL),
    T("SELECT t.id, t.x {IN|NOT IN} (SELECT u.w FROM u UNION ALL SELECT p.tid FROM p) AS m FROM t", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u UNION ALL SELECT u.k FROM u {LIMIT 1|ORDER BY 1 LIMIT 2|ORDER BY 1}) {|OR t.y = 1}", *ALL),
    T("SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u {INTERSECT DISTINCT|EXCEPT DISTINCT|UNION ALL} SELECT u.k FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE (t.x, t.y) IN (SELECT u.w, u.k FROM u UNION ALL SELECT p.tid, p.id FROM p)", *ALL),
    T("SELECT t.id FROM t WHERE (t.x + (SELECT COUNT(*) FROM p)) IN (SELECT u.w FROM u UNION ALL SELECT u.k FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE {t.x|t.x + 1|COALESCE(t.x, 0)} IN (SELECT u.w FROM u UNION ALL SELECT 1 AS k)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT q.w FROM (SELECT u.w FROM u UNION ALL SELECT u.k FROM u) AS q {WHERE q.w > 0|})", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u UNION ALL SELECT u.k FROM u) AND t.y NOT IN (SELECT p.tid FROM p UNION ALL SELECT p.id FROM p)", *ALL),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u UNION ALL SELECT MAX(u.k) FROM u)", *ALL),
    # --- rewrite_quantified: ANY / SOME / ALL in every position ---------------------------------------------------------
    T("SELECT t.id FROM t WHERE t.x {>|>=|<|<=|=|<>} {ANY|SOME|ALL} (SELECT u.w FROM u {|WHERE u.k > 1|WHERE u.k = t.y|WHERE FALSE|WHERE u.w IS NOT NULL})", *ALL),
    T("SELECT t.id FROM t WHERE NOT (t.x {>|<=|=|<>} {ANY|ALL} (SELECT u.w FROM u {|WHERE u.k = t.y}))", *ALL),
    T("SELECT t.id, t.x {>|<|=|<>} {ANY|ALL} (SELECT u.w FROM u {|WHERE u.k = t.y|WHERE FALSE}) AS m FROM t", *ALL),
    T("SELECT t.id FROM t WHERE t.y = 1 {AND|OR} t.x {>|<>} {ANY|ALL} (SELECT u.w FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE (t.x {>|=} {ANY|ALL} (SELECT u.w FROM u)) IS {NULL|NOT TRUE|NOT FALSE|TRUE}", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<>} {ANY|ALL} (SELECT {u.w|u.w + 1|NULL|1|u.w * t.x} FROM u {|LIMIT 1|ORDER BY u.w LIMIT 2|WHERE u.k > 5})", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT {MAX(u.w)|MIN(u.w)|COUNT(*)|SUM(u.w)} FROM u {|WHERE u.k = t.y|WHERE FALSE})", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT u.w FROM u GROUP BY u.w {|HAVING COUNT(*) > 1})", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT DISTINCT u.w FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT u.w FROM u UNION ALL SELECT u.k FROM u)", *ALL),
    T("SELECT t.id FROM t JOIN p ON p.tid = t.id AND p.n {>|<} {ANY|ALL} (SELECT u.k FROM u)", *ALL),
    T("SELECT t.id FROM t LEFT JOIN p ON p.tid = t.id AND t.x {>|<} {ANY|ALL} (SELECT u.w FROM u WHERE u.k = p.id)", *ALL),
    T("SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y HAVING t.y {>|<} {ANY|ALL} (SELECT u.w FROM u)", *ALL),
    T("SELECT t.y, SUM(t.x) AS s FROM t GROUP BY t.y HAVING SUM(t.x) {>|<} {ANY|ALL} (SELECT u.w FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT u.w FROM u WHERE u.k {>|=} {ANY|ALL} (SELECT p.tid FROM p))", *ALL),
    T("SELECT t.id FROM t WHERE (t.x, t.y) = {ANY|ALL} (SELECT u.w, u.k FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE t.x = {ANY|SOME} (SELECT u.w FROM u) {AND|OR} t.y <> ALL (SELECT u.k FROM u)", *ALL),
    T("SELECT t.id, CASE WHEN t.x > {ANY|ALL} (SELECT u.w FROM u) THEN 1 WHEN t.x <= {ANY|ALL} (SELECT u.w FROM u) THEN 2 END AS m FROM t", *ALL),
    T("SELECT t.id FROM t WHERE t.x > ALL (SELECT u.w FROM u WHERE {u.w IS NOT NULL|u.w > 0|u.k = t.y})", *ALL),
    T("SELECT t.id FROM t WHERE t.s {=|<>} {ANY|ALL} (SELECT u.v FROM u)", *ALL),
    T("SELECT t.id FROM t WHERE t.x {>|<} {ANY|ALL} (SELECT u.w FROM u) ORDER BY t.id {LIMIT 2|}", *ALL),
    # Calcite's own expansions of a quantified test: a one-row aggregate joined ON TRUE, read in a three-valued CASE.
    T("SELECT s.id FROM (SELECT t.id, t.x, g.m, g.c, g.ck FROM t INNER JOIN (SELECT {MIN|MAX}(u.w) AS m, COUNT(*) AS c, COUNT(u.w) AS ck FROM u {|WHERE u.k > 1}) AS g ON TRUE) AS s WHERE CASE WHEN s.c = 0 THEN FALSE WHEN (s.x {>|<=} s.m) IS TRUE THEN TRUE WHEN s.c {>|>=} s.ck THEN NULL ELSE (s.x {>|<=} s.m) END", *ALL),
    T("SELECT s.id, CASE WHEN s.c = 0 THEN FALSE WHEN s.x IS NULL THEN NULL WHEN s.i IS NOT NULL THEN TRUE WHEN s.ck {<|<=} s.c THEN NULL ELSE FALSE END AS m FROM (SELECT t.id, t.x, g.c, g.ck, ind.i FROM t INNER JOIN (SELECT COUNT(*) AS c, COUNT(u.w) AS ck FROM u) AS g ON TRUE LEFT JOIN (SELECT u.w AS k, TRUE AS i FROM u {GROUP BY u.w|GROUP BY u.w, TRUE|}) AS ind ON t.x = ind.k) AS s", *ALL),
    # --- _semi_joins_to_exists -----------------------------------------------------------------------------------------
    T("SELECT t.id FROM t LEFT {SEMI|ANTI} JOIN u ON {u.k = t.y|u.k = t.y AND u.w > 0|u.w = t.x|u.k = t.y AND u.w = t.x|u.k > t.y|TRUE}", *KEYS),
    T("SELECT t.id FROM t LEFT SEMI JOIN u ON u.k = t.y LEFT ANTI JOIN p ON p.tid = t.id {|WHERE t.x > 0}", *KEYS),
    T("SELECT t.id, t.x FROM t JOIN p ON p.tid = t.id LEFT {SEMI|ANTI} JOIN u ON u.k = p.tid {|WHERE p.n > 0}", *KEYS),
    T("SELECT t.id FROM t LEFT {SEMI|ANTI} JOIN (SELECT u.k, u.w FROM u {|WHERE u.w > 1} {|GROUP BY u.k, u.w}) AS q ON q.k = t.y", *KEYS),
    T("SELECT COUNT(*) AS c FROM t LEFT {SEMI|ANTI} JOIN u ON u.k = t.y", *KEYS),
    T("SELECT t.y, COUNT(*) AS c FROM t LEFT {SEMI|ANTI} JOIN u ON u.k = t.y GROUP BY t.y", *KEYS),
    T("SELECT t.id FROM t LEFT SEMI JOIN u ON u.k = t.y LEFT JOIN p ON p.tid = t.id", *KEYS),
    T("SELECT t.id FROM t LEFT {SEMI|ANTI} JOIN u ON u.k = t.y AND u.w = (SELECT MAX(p.tid) FROM p)", *KEYS),
    # --- _pull_up_exists: an EXISTS filtering a derived table that is joined by inner joins ------------------------------
    T("SELECT d.id, p.n FROM (SELECT t.id, t.y FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y {|AND u.w > 0}) {|AND t.x > 0}) AS d {JOIN|INNER JOIN|CROSS JOIN|LEFT JOIN|RIGHT JOIN|FULL JOIN} p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM p JOIN (SELECT t.id, t.y AS yy FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d ON p.tid = d.id WHERE p.n > 0", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {t.y|t.x|t.y AND u.w = t.x|t.y + 0|t.y OR t.x = 1})) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) {LIMIT 2|ORDER BY t.id LIMIT 2|OFFSET 1}) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.y, COUNT(*) AS c FROM (SELECT t.id, t.y FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN p ON p.tid = d.id GROUP BY d.y", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN (SELECT p.tid FROM p WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)) AS e ON e.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t JOIN p ON p.tid = t.id WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN p AS p2 ON p2.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT DISTINCT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) OR t.x = 1) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT COUNT(*) FROM u WHERE u.k = t.y {HAVING COUNT(*) > 1|})) AS d JOIN p ON p.tid = d.id", *KEYS),
    T("SELECT d.id FROM (SELECT t.id, t.y FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)) AS d JOIN p ON p.tid = d.id AND EXISTS (SELECT 1 FROM u WHERE u.k = d.y)", *KEYS),
    # --- _drop_implied_exists: a grouped derived table joined on its key repeats an EXISTS the select already makes ----------
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid = g.k WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, {SUM(t.x)|COUNT(*)|MAX(t.x)} AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid = g.k AND EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y {|AND u.w > 0}) GROUP BY t.y) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid {|AND u.w > 1})", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {t.y|t.x|t.id}) GROUP BY t.y) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p {JOIN|LEFT JOIN|RIGHT JOIN|FULL JOIN} (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid {=|<|>=|IS NOT DISTINCT FROM} g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y {|HAVING SUM(t.x) > 0}) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.id)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y, t.x) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) AND t.x > 0 GROUP BY t.y) AS g ON p.tid = g.k WHERE p.n > 0 AND EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY ROLLUP(t.y)) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.n FROM p JOIN (SELECT t.y AS k, COUNT(*) AS n FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid) OR p.n > 1", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM({t2.x|t.x}) AS s FROM t JOIN t AS t2 ON t2.id = t.x WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {t.y|t2.y|t2.x}) GROUP BY {t.y|t2.y}) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    T("SELECT p.id, g.s FROM p JOIN (SELECT y AS k, SUM(x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {y|t.y|x}) GROUP BY y) AS g ON p.tid = g.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)", *KEYS),
    # --- nullable_lateral_boolean_group ---------------------------------------------------------------------------------------
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT {u.w|u.k|u.k + 0} IS NOT NULL AS m FROM u WHERE u.k = t.y {|AND u.w > 0|AND u.w IS NOT NULL} GROUP BY {u.w|u.k|u.k + 0} IS NOT NULL) AS g ON TRUE", *NN, "nullable_key", "no_key"),
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE {u.k = t.y|u.k > t.y|u.w = t.x|t.y = u.k AND u.v = t.s} GROUP BY u.w IS NOT NULL) AS g ON TRUE", *NN),
    T("SELECT t.id, g.m IS NULL AS a, COALESCE(g.m, FALSE) AS b, g.m AND t.x > 0 AS c FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w IS NOT NULL GROUP BY u.w IS NOT NULL) AS g ON TRUE", *NN),
    T("SELECT t.id FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w IS NOT NULL GROUP BY u.w IS NOT NULL) AS g ON TRUE WHERE {g.m|g.m IS NULL|NOT g.m|g.m IS NOT NULL|g.m = TRUE}", *NN),
    T("SELECT t.id, g.m FROM t {LEFT JOIN|JOIN|CROSS JOIN|INNER JOIN|RIGHT JOIN} LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w > 0 GROUP BY u.w IS NOT NULL) AS g {ON TRUE|ON t.x > 0|}", *NN),
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w > 0 GROUP BY u.w IS NOT NULL {HAVING TRUE|LIMIT 1|ORDER BY 1}) AS g ON TRUE", *NN),
    T("SELECT t.id, g.m, h.m AS m2 FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w > 0 GROUP BY u.w IS NOT NULL) AS g ON TRUE LEFT JOIN LATERAL (SELECT p.n IS NOT NULL AS m FROM p WHERE p.tid = t.id AND p.n > 0 GROUP BY p.n IS NOT NULL) AS h ON TRUE", *NN),
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT {u.w IS NOT NULL|u.w IS NULL|u.w > 0|NOT u.w IS NULL|NOT (u.w IS NULL)} AS m FROM u WHERE u.k = t.y {AND u.w > 0|AND u.w < 0|} GROUP BY {u.w IS NOT NULL|u.w IS NULL|u.w > 0|NOT u.w IS NULL|NOT (u.w IS NULL)}) AS g ON TRUE", *NN),
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.y AND u.w > 0 GROUP BY u.w IS NOT NULL) AS g ON TRUE WHERE g.m IS NULL OR t.x > g.m", *NN),
    T("SELECT t.id, g.m FROM t LEFT JOIN LATERAL (SELECT p.n IS NOT NULL AS m FROM p WHERE p.tid = t.id AND p.n > 0 GROUP BY p.n IS NOT NULL) AS g ON TRUE", *NN),
    T("SELECT t.id, g.m FROM p AS t LEFT JOIN LATERAL (SELECT u.w IS NOT NULL AS m FROM u WHERE u.k = t.tid AND u.w > 0 GROUP BY u.w IS NOT NULL) AS g ON TRUE", *NN),
]


def cases(seed: int, count: int) -> list[dict]:
    return expand_constrained(TEMPLATES, seed, count, "membership")
