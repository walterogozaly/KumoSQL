"""Foreign-key, key and EXISTS rules: ``drop_fk_join``, ``exists_constant_rules`` (a test a foreign key witnesses, a
correlation a filter fixes), ``expose_correlated_key_groups``, ``drop_keyed_distinct``, ``remove_keyed_grouping``,
``exists_over_aggregate``, ``keyed_join_to_exists`` and ``_drop_exists_witnessed_by_join``.

Each shape is run under constraint sets that satisfy the rule's facts and under sets that miss one (a nullable
foreign-key column, a unique key that admits NULL, no key at all, a composite key covered in part), so a guard that
stopped checking is caught as a differing firing.
"""

from ._constraints import T, expand_constrained

FK = ("fk", "fk_nullable", "no_key", "plain")
NN = ("not_null", "plain", "nullable_key", "no_key", "fk")
KEYS = ("plain", "nullable_key", "no_key", "not_null")

TEMPLATES = [
    # --- drop_fk_join: a foreign key with a NOT NULL child column and a unique parent column ----------------------
    T("SELECT {p.id, p.n|p.id|t.id, p.b|p.tid|p.id, t.id AS r|t.id, p.id, p.tid} FROM p {JOIN|INNER JOIN} t ON p.tid = t.id {|WHERE p.n > 0|WHERE p.tid IS NOT NULL|WHERE p.id > 1}", *FK),
    T("SELECT {COUNT(*)|COUNT(t.id)|SUM(p.n)|COUNT(DISTINCT p.tid)|MAX(t.id)} AS a FROM p JOIN t ON t.id = p.tid", *FK),
    T("SELECT p.tid, {COUNT(*)|SUM(p.n)|COUNT(t.id)} AS a FROM t JOIN p ON t.id = p.tid GROUP BY p.tid", *FK),
    T("SELECT {t.id|t.id, u.k|u.k|t.x} FROM t JOIN u ON t.y = u.k {|WHERE t.x > 0|WHERE t.y IS NOT NULL}", *FK),
    T("SELECT {t.id|u.k, t.x} FROM u JOIN t ON u.k = t.y", *FK),
    T("SELECT p.id, u.v FROM p JOIN t ON p.tid = t.id JOIN u ON u.k = t.y", *FK),
    T("SELECT p.id, t.x FROM p JOIN t ON p.tid = t.id", *FK),
    T("SELECT {p.id|p.id, t.x|*} FROM p {LEFT JOIN|RIGHT JOIN|FULL JOIN|JOIN} t ON p.tid = t.id", *FK),
    T("SELECT p.id FROM p JOIN t ON {p.tid = t.x|p.tid = t.id AND t.x > 0|p.tid = t.id AND p.n > 0|p.tid = t.id OR p.id = 1|p.tid + 0 = t.id|t.id = p.id|p.tid = t.id AND p.tid = t.y}", *FK),
    T("SELECT p.id FROM p JOIN t ON p.tid = t.id JOIN t AS t2 ON t2.id = p.tid", *FK),
    T("SELECT p.id, a.id AS aid FROM p JOIN t AS a ON a.id = p.tid JOIN t AS b ON b.id = a.id", *FK),
    T("SELECT t.id FROM t JOIN u ON {t.y = u.k AND t.x = u.w|t.y = u.w|t.y = u.k AND u.w > 1}", *FK),
    T("SELECT c.id, c.v FROM c JOIN d ON c.a = d.a AND c.b = d.b", "composite_fk", "composite_nullable"),
    T("SELECT c.id FROM c JOIN d ON {c.a = d.a|c.a = d.b AND c.b = d.a|c.a = d.a AND c.b = d.a|c.a = d.a AND c.b = d.b AND d.w > 0}", "composite_fk", "composite_nullable"),
    T("SELECT {c.id|c.id, d.w} FROM c JOIN d ON c.a = d.a AND c.b = d.b {|WHERE c.v > 0|WHERE c.b IS NOT NULL}", "composite_fk", "composite_nullable"),
    # --- exists_constant_rules 1: an uncorrelated EXISTS a foreign key witnesses -----------------------------------
    T("SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM t {|WHERE t.id = t.id|WHERE t.id IS NOT NULL|WHERE t.x > 0|WHERE t.id = p.tid|WHERE t.x IS NOT NULL|WHERE t.s IS NOT NULL})", *FK),
    T("SELECT p.id FROM p WHERE {p.n > 0|p.b|p.id > 1} AND EXISTS (SELECT 1 FROM t {|WHERE t.id IS NOT NULL})", *FK),
    T("SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u {|WHERE u.k = u.k|WHERE u.k IS NOT NULL|WHERE u.w > 0})", *FK),
    T("SELECT p.id FROM p WHERE NOT EXISTS (SELECT 1 FROM t {|WHERE t.id IS NOT NULL})", *FK),
    T("SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM t {|WHERE t.id IS NOT NULL}) OR p.n > 1", *FK),
    T("SELECT p.id FROM p {LEFT JOIN|RIGHT JOIN|FULL JOIN|JOIN} u ON u.k = p.id WHERE EXISTS (SELECT 1 FROM t)", *FK),
    T("SELECT p.id, u.k FROM u {LEFT JOIN|RIGHT JOIN|JOIN} p ON u.k = p.id WHERE EXISTS (SELECT 1 FROM t WHERE t.id IS NOT NULL)", *FK),
    T("SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM t {WHERE t.id IS NOT NULL|} {LIMIT 0|LIMIT 1|})", *FK),
    T("SELECT p.id FROM p WHERE EXISTS (SELECT t.x FROM t WHERE t.id = t.id GROUP BY t.x)", *FK),
    T("SELECT c.id FROM c WHERE EXISTS (SELECT 1 FROM d {|WHERE d.a IS NOT NULL|WHERE d.b IS NOT NULL|WHERE d.a = d.a})", "composite_fk", "composite_nullable"),
    # --- exists_constant_rules 2: a correlation a filter fixes -------------------------------------------------------
    T("SELECT q.id FROM (SELECT p.id, p.tid FROM p WHERE p.tid = {2|3|0}) AS q WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = q.tid {|AND u.w > 1})", *KEYS),
    T("SELECT t.id FROM t WHERE t.x = {1|2|0} AND {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.x {|AND u.w > 1})", *KEYS),
    T("SELECT t.id FROM t WHERE {t.f = 2|t.s = '2'|t.x = 2.5|t.x > 1|t.x = 2 OR t.y = 1|t.d = DATE '2020-01-01'} AND EXISTS (SELECT 1 FROM u WHERE u.k = {t.f|t.s|t.x|t.d})", *KEYS),
    T("SELECT t.id FROM t LEFT JOIN (SELECT p.tid FROM p WHERE p.tid = 2) AS q ON q.tid = t.id WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = q.tid)", *KEYS),
    T("SELECT q.n FROM (SELECT {p.tid, SUM(p.n) AS n|p.tid, COUNT(*) AS n|MAX(p.tid) AS tid, COUNT(*) AS n} FROM p {WHERE p.tid = 2|} {GROUP BY p.tid|}) AS q WHERE EXISTS (SELECT 1 FROM u WHERE u.k = q.tid)", *KEYS),
    T("SELECT q.id FROM (SELECT p.id, p.tid FROM p WHERE p.tid = 2 {LIMIT 1|ORDER BY p.id LIMIT 2|}) AS q WHERE EXISTS (SELECT 1 FROM u WHERE u.k = q.tid)", *KEYS),
    T("SELECT q.id FROM (SELECT p.id, p.tid FROM p WHERE p.tid = 2) AS q JOIN u ON u.k = q.id WHERE EXISTS (SELECT 1 FROM t WHERE t.y = q.tid {AND t.x = q.id|})", *KEYS),
    T("SELECT t.id FROM t WHERE t.x = 1 AND EXISTS (SELECT 1 FROM p AS t WHERE t.tid = 1 AND EXISTS (SELECT 1 FROM u WHERE u.k = t.tid))", *KEYS),
    T("SELECT t.id FROM t WHERE t.y = 1 AND (SELECT COUNT(*) FROM u WHERE u.k = t.y) {> 0|= 0}", *KEYS),
    # --- expose_correlated_key_groups ---------------------------------------------------------------------------------
    T("SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y GROUP BY u.{w|v|w, u.v} {|HAVING COUNT(*) > 0|HAVING MAX(u.w) > 1})", *KEYS),
    T("SELECT t.id, (SELECT {MAX(u.w)|COUNT(*)|SUM(u.w)|MIN(u.v)} FROM u WHERE u.k = t.y GROUP BY u.{w|v}) AS m FROM t", *KEYS),
    T("SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT DISTINCT u.{w|v|k} FROM u WHERE u.k = t.y)", *KEYS),
    T("SELECT t.id FROM t WHERE t.x IN (SELECT {DISTINCT u.w|u.w} FROM u WHERE u.k = t.y {|GROUP BY u.w})", *KEYS),
    T("SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM t AS t2 WHERE t2.id = {t.x|t.y|t.id|1} GROUP BY t2.{x|y|s})", *KEYS),
    T("SELECT p.id FROM p WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM t WHERE t.id = p.tid GROUP BY t.x {|HAVING COUNT(*) > 0|HAVING SUM(t.y) > 0})", *KEYS),
    T("SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {t.f|t.s|t.d|t.y + 0|t.y * 1.0} GROUP BY u.w)", *KEYS),
    T("SELECT t.id, g.w FROM t CROSS JOIN LATERAL (SELECT u.w FROM u WHERE u.k = t.y) AS g", *KEYS),
    T("SELECT t.id, g.w FROM t {CROSS JOIN|INNER JOIN|LEFT JOIN} LATERAL (SELECT u.w, u.v FROM u WHERE u.k = t.y {|AND u.w > 0}) AS g {ON TRUE|}", *KEYS),
    T("SELECT t.id, g.w FROM t CROSS JOIN LATERAL (SELECT u.w FROM u WHERE u.k = {t.f|t.y + 0|t.s}) AS g", *KEYS),
    T("SELECT t.id, SUM(g.w) AS a FROM t CROSS JOIN LATERAL (SELECT u.w FROM u WHERE u.k = t.y {|GROUP BY u.w}) AS g GROUP BY t.id", *KEYS),
    # --- drop_keyed_distinct ------------------------------------------------------------------------------------------
    T("SELECT DISTINCT {t.id|t.id, t.x|t.x|t.id, t.s|t.id AS i|t.y, t.id + 1|t.id, t.id} FROM t {|WHERE t.x > 0|WHERE t.id = 1}", *KEYS),
    T("SELECT DISTINCT {u.k|u.k, u.v|u.w|u.v, u.w|u.k AS i} FROM u {|WHERE u.w > 0}", *KEYS),
    T("SELECT DISTINCT {p.id|p.tid|p.id, p.n|p.tid, p.n} FROM p {|WHERE p.tid = 1|WHERE p.id = 1}", *KEYS),
    T("SELECT DISTINCT {t.x|t.s|t.x, t.y} FROM t WHERE t.id = {1|2|NULL}", *KEYS),
    T("SELECT DISTINCT {a.id|a.x} FROM t AS a", *KEYS),
    T("SELECT DISTINCT {d.a, d.b|d.a|d.b|d.a, d.b, d.w|d.w} FROM d {|WHERE d.a = 1}", "composite_fk", "composite_nullable"),
    T("SELECT DISTINCT {c.id|c.a, c.b|c.b} FROM c", "composite_fk", "composite_nullable"),
    # --- remove_keyed_grouping ----------------------------------------------------------------------------------------
    T("SELECT t.id, {COUNT(*)|COUNT(t.x)|SUM(t.x)|MIN(t.x)|MAX(t.y)|AVG(t.x)|SUM(DISTINCT t.x)|COUNT(DISTINCT t.x)|ARRAY_AGG(t.x)|STRING_AGG(t.s)|ANY_VALUE(t.x)|LOGICAL_OR(t.x > 1)|COUNTIF(t.x > 1)|SUM(t.x) + COUNT(*)|MAX(t.x) OVER ()} AS a FROM t GROUP BY t.id {|HAVING COUNT(*) > 0|HAVING SUM(t.x) > 1|HAVING t.id > 1|HAVING MIN(t.x) IS NULL}", *KEYS),
    T("SELECT u.k, {COUNT(*)|COUNT(u.w)|SUM(u.w)|MAX(u.v)|MIN(u.w)} AS a FROM u {|WHERE u.w > 0} GROUP BY u.k {|HAVING COUNT(u.w) > 0}", *KEYS),
    T("SELECT {t.x|t.y|t.s}, {COUNT(*)|SUM(t.x)|COUNT(t.y)|MAX(t.f)} AS a FROM t WHERE t.id = {1|2} GROUP BY {t.x|t.y|t.s}", *KEYS),
    T("SELECT {t.x|t.y}, COUNT(*) AS a FROM t WHERE {t.id > 1|t.id = t.x|t.id IN (1, 2)|t.id = 1 OR t.x = 1|t.id = 1.5|t.id = '1'} GROUP BY {t.x|t.y}", *KEYS),
    T("SELECT t.id, {COUNT(*)|SUM(t.x)} AS a FROM t GROUP BY t.id, t.x", *KEYS),
    T("SELECT t.x, {COUNT(*)|SUM(t.x)} AS a FROM t GROUP BY t.x, t.id", *KEYS),
    T("SELECT {d.a, d.b|d.a|d.b}, {COUNT(*)|SUM(d.w)|MAX(d.w)} AS n FROM d GROUP BY {d.a, d.b|d.a|d.b} {|HAVING COUNT(*) > 0}", "composite_fk", "composite_nullable"),
    T("SELECT t.id, {COUNT(*)|SUM(t.x)} AS a FROM t GROUP BY {ROLLUP(t.id)|CUBE(t.id)|GROUPING SETS ((t.id), ())}", *KEYS),
    T("SELECT t.id, GROUPING(t.id) AS g, COUNT(*) AS a FROM t GROUP BY ROLLUP(t.id)", *KEYS),
    # --- exists_over_aggregate ----------------------------------------------------------------------------------------
    T("SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT {COUNT(*)|MAX(u.w)|SUM(u.w), COUNT(*)|COUNT(*) + 1|MAX(u.w) + 1|COUNT(*) OVER ()|u.w|COUNT(*), u.w|MAX(t.x)|COUNT(t.x)|COUNT(DISTINCT u.w)} FROM u {|WHERE u.k = t.y|WHERE FALSE|WHERE u.w > t.x|WHERE u.k = t.id} {|LIMIT 0|LIMIT 1 OFFSET 1|HAVING COUNT(*) > 5|GROUP BY ()|HAVING COUNT(*) > 0})", *KEYS),
    T("SELECT t.id, {EXISTS|NOT EXISTS} (SELECT {COUNT(*)|MAX(u.w)|MAX(u.w), COUNT(*)} FROM u {|WHERE u.k = t.y|WHERE FALSE}) AS e FROM t", *KEYS),
    T("SELECT t.id FROM t WHERE {t.x > 0|t.y IS NULL} OR EXISTS (SELECT {MIN(u.w)|COUNT(*)} FROM u {|WHERE u.w > t.x})", *KEYS),
    T("SELECT t.id FROM t WHERE EXISTS (SELECT {MAX(q.w)|COUNT(*)} FROM (SELECT u.w FROM u {WHERE u.k = t.y|}) AS q {WHERE q.w > 1|})", *KEYS),
    T("SELECT COUNT(*) AS c FROM t WHERE EXISTS (SELECT {SUM(u.w)|COUNT(*)} FROM u WHERE {FALSE|u.k < 0|u.w > 1}) {|GROUP BY t.y}", *KEYS),
    # --- keyed_join_to_exists ---------------------------------------------------------------------------------------
    T("SELECT DISTINCT t.id FROM t {JOIN|INNER JOIN} p ON p.tid = t.id {|WHERE p.n > 0|WHERE t.x > 0|WHERE p.b|WHERE p.n > t.x}", *KEYS),
    T("SELECT DISTINCT {u.k|u.k, u.v|u.k, u.w} FROM u JOIN {t ON u.k = t.y|p ON u.k = p.tid AND p.n > 0|t ON u.k = t.y AND t.x > u.w|t ON u.k = t.y OR t.x > 0|t ON u.k = t.y AND u.k = t.x}", *KEYS),
    T("SELECT DISTINCT u.k FROM u JOIN t ON {COALESCE(u.k, 0) = t.y|u.k IS NOT DISTINCT FROM t.y|u.k > t.y|u.w = t.y|u.k + 0 = t.y|u.k = t.y AND u.k IS NULL}", *KEYS),
    T("SELECT DISTINCT {u.k, t.y|t.y|u.k, t.id|u.w|u.k + 1|*} FROM u JOIN t ON u.k = t.y", *KEYS),
    T("SELECT DISTINCT u.k FROM u {LEFT JOIN|RIGHT JOIN|FULL JOIN|CROSS JOIN} t ON u.k = t.y", *KEYS),
    T("SELECT DISTINCT t.id FROM t JOIN t AS t2 ON t2.{y|x} = t.id {|WHERE t2.s = 'a'}", *KEYS),
    T("SELECT DISTINCT {d.a, d.b|d.a|d.a, d.b, d.w} FROM d JOIN c ON {d.a = c.a AND d.b = c.b|d.a = c.a|d.b = c.b|d.a = c.a AND d.b = c.b AND c.v > 0}", "composite_fk", "composite_nullable"),
    T("SELECT DISTINCT t.id FROM t JOIN p ON p.tid = t.id JOIN u ON u.k = t.y", *KEYS),
    # --- _drop_exists_witnessed_by_join -----------------------------------------------------------------------------
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y {|AND u.w = s.w|AND u.w > 0})", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y AND s.w = t.x WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y AND u.w = t.x)", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y AND u.w = t.x)", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y WHERE {NOT EXISTS|EXISTS} (SELECT 1 FROM u WHERE u.k = t.y {|AND u.w = t.x})", *KEYS),
    T("SELECT t.id FROM t {LEFT JOIN|JOIN} u AS s ON s.k = t.y WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON {s.k = t.y OR t.x = 1|s.k > t.y|s.w = t.y|s.k = t.x} WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)", *KEYS),
    T("SELECT t.id FROM t JOIN p AS s ON s.tid = t.id WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = t.id {|AND p.n > 0})", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y WHERE EXISTS (SELECT 1 FROM u AS s WHERE s.k = t.y)", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y JOIN p ON p.tid = s.k WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {t.y|p.tid})", *KEYS),
    T("SELECT t.id FROM t JOIN u AS s ON s.k = t.y WHERE t.x > 1 OR EXISTS (SELECT 1 FROM u WHERE u.k = t.y)", *KEYS),
]


def cases(seed: int, count: int) -> list[dict]:
    return expand_constrained(TEMPLATES, seed, count, "exists_keys")
