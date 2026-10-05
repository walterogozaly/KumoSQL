"""Null guards, constant sources and columns, single-row sources, probes, UNION ALL distribution, VALUES.

Rules aimed at ``_fold_null_guards``, ``_drop_derived_null_guard``, ``_drop_global_null_filter``,
``_inline_constant_source``, ``_inline_constant_columns``, ``_single_row_source``, ``_probe_and_nth_value``,
``_distribute``, ``_values_to_union``, ``distribute_over_constant_union`` and the ``ast_utils`` helpers
``canonical_negation``, ``parenthesize_is_operands`` and ``expand_alias_columns``. Templates reuse the helpers of
``folding`` (the ``(sql, spec)`` form: ``spec`` may set the dialect, the schema and the constraints).
"""

from __future__ import annotations

from .folding import NOT_NULL_XY, NULLABLE, build

# the constraints of the default schema leave t.x, t.y nullable (and NOT NULL one time in three); these two pin it
N = {"constraints": NULLABLE}
NN = {"constraints": NOT_NULL_XY}

NULL_GUARDS = [
    ("SELECT t.id FROM t WHERE t.x {>|=|<>|<=} {0|t.y|1} AND t.x IS NOT NULL {AND t.y IS NOT NULL|}", N),
    ("SELECT t.id FROM t WHERE t.s {LIKE 'a%'|= 'a'|<> ''} AND t.s IS NOT NULL AND t.x IS NOT NULL", N),
    ("SELECT t.id FROM t WHERE t.x {BETWEEN 0 AND 2|IN (1, 2)|NOT IN (1, 2)|IS DISTINCT FROM t.y|IS NULL|IS NULL OR t.y > 0} AND t.x IS NOT NULL", N),
    ("SELECT t.id FROM t WHERE {NOT (t.x > 0)|COALESCE(t.x, 0) > 0|t.x > 0 OR t.y > 0|t.x = ANY (SELECT u.w FROM u)|t.x > ALL (SELECT u.w FROM u WHERE u.k > 5)|t.x > (SELECT MAX(u.w) FROM u)|(t.x > 0) IS TRUE} AND t.x IS NOT NULL", N),
    ("SELECT t.id FROM t WHERE t.x IS NOT NULL AND t.y > 0", NN),
    ("SELECT t.id FROM t WHERE t.id IS NOT NULL AND t.x IS NOT NULL AND t.y IS NOT NULL AND t.s IS NOT NULL", NN),
    ("SELECT t.id FROM t WHERE NOT (t.x IS NULL) AND NOT t.y IS NULL AND t.x > 0", NN),
    ("SELECT t.id FROM t WHERE x > 0 AND t.x IS NOT NULL AND y IS NOT NULL", NN),
    ("SELECT d.id FROM (SELECT t.id, t.x AS v FROM t) AS d WHERE d.v {>|=} 0 AND d.v IS NOT NULL", N),
    ("SELECT t.id FROM t JOIN u ON u.k = t.y AND u.k IS NOT NULL AND t.y IS NOT NULL AND u.w IS NOT NULL", N),
    ("SELECT t.id FROM t JOIN u ON u.k = t.y AND u.k IS NOT NULL AND t.y IS NOT NULL AND u.w IS NOT NULL", NN),
    ("SELECT t.id FROM t {LEFT|RIGHT|FULL} JOIN u ON u.k = t.y AND {u.w IS NOT NULL|u.w > 0 AND u.w IS NOT NULL|t.x > 0 AND t.x IS NOT NULL|t.y IS NOT NULL}", N),
    ("SELECT t.id FROM t {LEFT|RIGHT|FULL} JOIN u ON u.k = t.y WHERE {u.k|u.w|t.id|t.x} IS NOT NULL", N),
    ("SELECT t.id FROM t {LEFT|RIGHT|FULL} JOIN u ON u.k = t.y WHERE {u.k|u.w|t.id|t.x} IS NOT NULL", NN),
    ("SELECT t.id FROM t {LEFT|RIGHT|FULL} JOIN u ON u.k = t.y WHERE u.w > 0 AND u.w IS NOT NULL AND t.x IS NOT NULL", NN),
    ("SELECT t.id FROM t LEFT JOIN u ON u.k = t.y JOIN p ON p.tid = t.id WHERE p.id IS NOT NULL AND p.tid IS NOT NULL", N),
    ("SELECT t.y, MIN(t.x) AS m FROM t GROUP BY t.y HAVING {MIN(t.x)|MAX(t.x)|SUM(t.x)|AVG(t.x)|SUM(t.x) + 1|AVG(t.x) * 2|MAX(t.x) - MIN(t.y)|COUNT(t.x)|SUM(t.x) / SUM(t.y)|MIN(t.f)|MIN(t.x) + MAX(t.f)} IS NOT NULL", NN),
    ("SELECT t.y, MIN(t.x) AS m FROM t GROUP BY t.y HAVING {MIN(t.x)|SUM(t.x) + 1} IS NOT NULL", N),
    ("SELECT {MIN(t.x)|SUM(t.x)|COUNT(*)} AS m FROM t {WHERE FALSE|WHERE t.x > 100|} HAVING {MIN(t.x)|SUM(t.x)} IS NOT NULL", NN),
    ("SELECT t.y, SUM(t.x) AS m FROM t GROUP BY {ROLLUP(t.y)|CUBE(t.y)|GROUPING SETS ((t.y), ())|()} HAVING SUM(t.x) IS NOT NULL", NN),
    ("SELECT t.y, MIN(u.w) AS m FROM t LEFT JOIN u ON u.k = t.y GROUP BY t.y HAVING MIN(u.w) IS NOT NULL", NN),
    ("SELECT t.y, COUNT(*) AS c FROM t GROUP BY {ROLLUP(t.y)|CUBE(t.y)|GROUPING SETS ((t.y), ())} HAVING t.y IS NOT NULL {AND t.y > 0|}", NN),
    ("SELECT t.y, t.x, COUNT(*) AS c FROM t GROUP BY {ROLLUP(t.y, t.x)|CUBE(t.y, t.x)|GROUPING SETS ((t.y, t.x), (t.y))} HAVING t.y IS NOT NULL AND t.x IS NOT NULL", NN),
    ("SELECT t.y, MIN(t.x) FILTER (WHERE t.id > 1) AS m FROM t GROUP BY t.y HAVING MIN(t.x) FILTER (WHERE t.id > 1) IS NOT NULL", NN),
    ("SELECT t.y, SUM(t.x) AS m FROM t WHERE FALSE GROUP BY t.y HAVING SUM(t.x) IS NOT NULL AND SUM(t.x) > {0|-1}", NN),
]

_HAVING_VALUE = "{0.5 * SUM(t.x)|SUM(t.x) + MIN(t.y)|AVG(t.x)|MAX(t.x) - MIN(t.y)|COUNT(*)|SUM(t.f)|SUM(t.x) / SUM(t.y)|SUM(t.x) * 2 + 1|MIN(t.x)}"
_GROUPED = f"(SELECT t.y AS k, {_HAVING_VALUE} AS h FROM t GROUP BY t.y) AS g"
# an outer select that _fold_filter_into_grouping and _push_filter_into_derived decline, so the guard is dropped by
# _drop_derived_null_guard itself
DERIVED_GUARDS = [
    (f"SELECT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL ORDER BY g.k", NN),
    (f"SELECT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL ORDER BY g.k LIMIT {{1|3}}", NN),
    (f"SELECT g.k, COUNT(*) AS n FROM {_GROUPED} WHERE g.h IS NOT NULL GROUP BY g.k", NN),
    (f"SELECT t.id FROM t WHERE t.x IN (SELECT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL)", NN),
    (f"SELECT t.id FROM t WHERE {{EXISTS|NOT EXISTS}} (SELECT 1 FROM {_GROUPED} WHERE g.h IS NOT NULL AND g.k = t.x)", NN),
    (f"SELECT g.k, SUM(g.h) OVER () AS w FROM {_GROUPED} WHERE g.h IS NOT NULL", NN),
    (f"SELECT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL ORDER BY g.k", N),
    (f"SELECT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL", NN),
    (f"SELECT DISTINCT g.k FROM {_GROUPED} WHERE g.h IS NOT NULL", NN),
    ("SELECT g.k FROM (SELECT t.y AS k, SUM(t.x) AS h FROM t GROUP BY {ROLLUP(t.y)|CUBE(t.y)|GROUPING SETS ((t.y), ())|()|t.y, ()}) AS g WHERE g.h IS NOT NULL ORDER BY g.k", NN),
    ("SELECT g.k FROM (SELECT t.y AS k, MIN(t.x) AS h FROM t {WHERE FALSE|WHERE t.x > 100|} GROUP BY t.y {HAVING MIN(t.x) > 0|}) AS g WHERE g.h IS NOT NULL {AND g.k > 0|} ORDER BY g.k", NN),
    ("SELECT g.k FROM (SELECT t.y AS k, SUM(u.w) AS h FROM t LEFT JOIN u ON u.k = t.y GROUP BY t.y) AS g WHERE g.h IS NOT NULL ORDER BY g.k", NN),
    ("SELECT g.k FROM (SELECT t.y AS k, SUM(t.x) AS h, COUNT(*) AS c FROM t GROUP BY t.y) AS g WHERE g.h IS NOT NULL AND g.c IS NOT NULL ORDER BY g.k", NN),
    ("SELECT g.k, g.h FROM (SELECT t.id AS k, MIN(u.w) AS h FROM t JOIN u ON u.k = t.y GROUP BY t.id) AS g WHERE g.h IS NOT NULL ORDER BY g.k", NN),
    ("SELECT g.k FROM (SELECT t.y AS k, SUM(t.x) AS h FROM t GROUP BY t.y) AS g WHERE g.h IS NOT NULL ORDER BY g.k", N),
]

GLOBAL_FILTERS = [
    ("SELECT {MIN|MAX|SUM|AVG|COUNT}(t.x) AS m, COUNT(*) AS c FROM t WHERE t.x IS NOT NULL {AND t.y > 0|}", N),
    ("SELECT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL", N),
    ("SELECT COUNT(*) AS c, SUM(t.x) AS s, AVG(t.x) AS a, MAX(t.x) AS m FROM t WHERE NOT t.x IS NULL", N),
    ("SELECT COUNT(*) AS c, {COUNT(DISTINCT t.x)|SUM(t.y)|SUM(t.x) + 1|COUNT(t.y)|MIN(t.x) + MAX(t.x)} AS m FROM t WHERE t.x IS NOT NULL", N),
    ("SELECT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL {GROUP BY t.y|HAVING COUNT(*) > 1|}", N),
    ("SELECT DISTINCT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL", N),
    ("SELECT COUNT(*) AS c, COUNT(*) OVER () AS w FROM t WHERE t.x IS NOT NULL", N),
    ("SELECT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL AND t.x IS NOT NULL", N),
    ("SELECT COUNT(*) AS c, MIN(u.w) AS m FROM t JOIN u ON u.k = t.y WHERE u.w IS NOT NULL", N),
    ("SELECT t.id, (SELECT COUNT(*) FROM u WHERE u.k = t.y AND {t.x|u.w|t.y} IS NOT NULL) AS c FROM t", N),
    ("SELECT t.id, (SELECT {MIN|SUM}(u.w) FROM u WHERE t.x IS NOT NULL) AS c, (SELECT COUNT(*) FROM u WHERE t.x IS NOT NULL) AS d FROM t", N),
    ("SELECT COUNT(*) AS c, COUNT(*) FILTER (WHERE t.y > 0) AS d FROM t WHERE t.x IS NOT NULL", dict(N, dialect="duckdb")),
    ("SELECT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL AND t.x IS NULL", N),
    ("SELECT COUNT(*) AS c FROM t WHERE t.x IS NOT NULL LIMIT {1|0}", N),
    ("SELECT COUNT(*) AS c FROM (SELECT t.x FROM t WHERE t.y > 0) AS d WHERE d.x IS NOT NULL", N),
]

CONSTANT_SOURCES = [
    "SELECT c.x, c.s, c.b FROM (SELECT {10|0|-1|9223372036854775807} AS x, {'a'|''|'é'} AS s, {TRUE|FALSE|NULL} AS b) AS c",
    "SELECT c.x + 1 AS y FROM (SELECT {1|NULL|-3} AS x) AS c WHERE c.x {>|=|IS NULL} {0|1}",
    "SELECT COUNT(*) AS n, SUM(c.x) AS s, MAX(c.x) AS m FROM (SELECT 1 AS x) AS c WHERE c.x {=|<>} {1|2}",
    "SELECT t.id, c.k FROM t JOIN (SELECT {1|NULL|0} AS k) AS c ON t.x = c.k",
    "SELECT t.id, c.k FROM (SELECT {1|NULL|0} AS k) AS c JOIN t ON t.x = c.k",
    "SELECT t.id, c.k FROM (SELECT 1 AS k) AS c, t WHERE t.x = c.k {AND t.y > 0|}",
    "SELECT t.id, c.k FROM t, (SELECT 1 AS k) AS c WHERE t.x >= c.k",
    "SELECT t.id, a.k, b.m FROM t CROSS JOIN (SELECT 1 AS k) AS a CROSS JOIN (SELECT 'z' AS m) AS b",
    "SELECT t.id, c.k FROM t {LEFT|RIGHT|FULL} JOIN (SELECT 1 AS k) AS c ON t.x = c.k",
    "SELECT c.k, COUNT(*) AS n FROM t JOIN (SELECT 1 AS k) AS c ON TRUE {GROUP BY c.k|GROUP BY c.k HAVING COUNT(*) > 1}",
    "SELECT c.k, COUNT(*) AS n FROM (SELECT 1 AS k) AS c {JOIN t ON t.x = c.k|CROSS JOIN t} GROUP BY c.k",
    "SELECT DISTINCT c.k, t.y FROM t JOIN (SELECT 1 AS k) AS c ON t.x = c.k",
    "SELECT t.id FROM t JOIN (SELECT 1 AS k) AS c ON TRUE ORDER BY c.k, t.id",
    "SELECT t.id, ROW_NUMBER() OVER (PARTITION BY c.k ORDER BY t.id) AS r FROM t JOIN (SELECT 1 AS k) AS c ON TRUE",
    "SELECT t.id FROM t JOIN (SELECT 1 AS k) AS c ON TRUE WHERE EXISTS (SELECT 1 FROM u WHERE u.k = c.k AND u.w = t.x)",
    "SELECT t.id, (SELECT MAX(u.w) FROM u WHERE u.k = c.k) AS m FROM t JOIN (SELECT {1|0} AS k) AS c ON TRUE",
    "SELECT c.x FROM (SELECT {1 + 2|CAST('5' AS INT64)|-1|1 / 2|UPPER('a')|IF(TRUE, 1, 2)|CASE WHEN TRUE THEN 1 END|CAST(NULL AS INT64)} AS x) AS c",
    "SELECT c.x, c.y FROM (SELECT 1 AS x, 2 AS x) AS c",
    "SELECT c.x FROM (SELECT 1 AS x FROM t) AS c",
    "SELECT c.x FROM (SELECT 1 AS x LIMIT {1|0}) AS c",
    "SELECT c.x FROM (SELECT 1 AS x WHERE {TRUE|FALSE}) AS c",
    "SELECT t.id FROM t WHERE t.x IN (SELECT c.k FROM (SELECT {1|NULL} AS k) AS c) {AND t.y > 0|}",
    "SELECT t.id FROM t WHERE t.x = (SELECT c.k FROM (SELECT 1 AS k) AS c) OR t.y IN (SELECT c.k FROM (SELECT 2 AS k) AS c)",
]

CONSTANT_COLUMNS = [
    "SELECT d.id, d.{g|one|n} FROM (SELECT t.id, {TRUE|1|NULL} AS g, 1 AS one, NULL AS n FROM t) AS d",
    "SELECT d.id FROM (SELECT t.id, 1 AS one, TRUE AS g FROM t) AS d WHERE d.one {=|>|<>} {1|0} {AND|OR} d.g",
    "SELECT d.one, COUNT(*) AS c FROM (SELECT t.id, 1 AS one FROM t) AS d GROUP BY d.one",
    "SELECT d.one, d.y, SUM(d.id) AS c FROM (SELECT t.id, t.y, {1|NULL|TRUE} AS one FROM t) AS d GROUP BY d.one, d.y",
    "SELECT d.id FROM (SELECT t.id, 1 AS one FROM t) AS d ORDER BY d.one, d.id",
    "SELECT d.id, SUM(d.one) OVER (PARTITION BY d.one ORDER BY d.id) AS s FROM (SELECT t.id, 1 AS one FROM t) AS d",
    "SELECT SUM(d.one) AS s, COUNT(DISTINCT d.one) AS c, MAX(d.n) AS m FROM (SELECT t.id, 1 AS one, NULL AS n FROM t) AS d",
    "SELECT d.id FROM (SELECT t.id, 1 AS one FROM t) AS d WHERE EXISTS (SELECT 1 FROM u WHERE u.k = d.one AND u.w = d.id)",
    "SELECT d.id FROM (SELECT t.id, 1 AS one FROM t) AS d WHERE d.id IN (SELECT d.k FROM (SELECT u.k, 2 AS one FROM u) AS d WHERE d.one = 2)",
    "SELECT d.id, u.w FROM (SELECT t.id, t.y, 1 AS one FROM t) AS d {LEFT|RIGHT|FULL|} JOIN u ON u.k = d.y AND d.one = 1",
    "SELECT d.id, d.one FROM t {LEFT|RIGHT|} JOIN (SELECT u.k AS id, 1 AS one FROM u) AS d ON d.id = t.y",
    "SELECT d.one, d.c FROM (SELECT 1 AS one, COUNT(*) AS c FROM t) AS d WHERE d.one = 1",
    "SELECT d.one FROM (SELECT 1 AS one FROM t GROUP BY t.y) AS d GROUP BY d.one",
    "SELECT d.s FROM (SELECT t.id, 'a' AS s FROM t) AS d WHERE d.s = 'a'",
    "SELECT d.id FROM (SELECT t.id, 1 AS one FROM t) AS d GROUP BY d.id, d.one HAVING d.one = 1",
    "SELECT d.id, d.one + d.two AS s FROM (SELECT t.id, 1 AS one, 2 AS two FROM t) AS d WHERE d.one < d.two",
]

SINGLE_ROW = [
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.x",
    "SELECT d.x, d.y FROM (SELECT t.x, t.y FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY {d.x, d.y|d.x|d.y, d.x}",
    "SELECT {SUM|COUNT|AVG|MIN|MAX}(DISTINCT d.x) AS s FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d",
    "SELECT d.y, {SUM|COUNT|MAX}(DISTINCT d.x) AS s FROM (SELECT t.x, t.y FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.y",
    "SELECT d.x, COUNT(*) AS c FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.x {HAVING d.x > 0|HAVING COUNT(*) = 1|}",
    "SELECT d.x + 1 AS y FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT {1|2|0}) AS d GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1 OFFSET {0|1}) AS d GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY {ROLLUP(d.x)|CUBE(d.x)|GROUPING SETS ((d.x), ())}",
    "SELECT d.x, COUNT(DISTINCT d.y) OVER () AS w FROM (SELECT t.x, t.y FROM t ORDER BY t.id LIMIT 1) AS d",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d JOIN u ON u.k = d.x GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t WHERE t.x > 100 ORDER BY t.id LIMIT 1) AS d GROUP BY d.x",
    "SELECT COUNT(DISTINCT d.x) AS c, SUM(DISTINCT d.x) AS s FROM (SELECT t.x FROM t WHERE t.x > 100 ORDER BY t.id LIMIT 1) AS d",
    "SELECT d.x FROM (SELECT DISTINCT t.x FROM t ORDER BY t.x LIMIT 1) AS d GROUP BY d.x",
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 1) AS d GROUP BY d.x ORDER BY d.x LIMIT 1",
]

PROBES = [
    "SELECT t.id FROM t WHERE (SELECT {1|2|1.5|0} FROM u {WHERE u.k = t.y|WHERE u.w > t.x|} LIMIT 1) IS {NOT |}NULL",
    "SELECT t.id, (SELECT 1 FROM u WHERE u.k = t.y {ORDER BY u.w|} LIMIT 1) IS {NOT |}NULL AS has FROM t",
    "SELECT t.id FROM t WHERE NOT ((SELECT 1 FROM u WHERE u.k = t.y LIMIT 1) IS NULL) {AND|OR} t.x > 0",
    "SELECT t.id FROM t WHERE (SELECT {u.w|'a'|NULL|TRUE|COUNT(*)|MAX(1)} FROM u {WHERE u.k = t.y|} LIMIT 1) IS {NOT |}NULL",
    "SELECT t.id FROM t WHERE (SELECT 1 FROM u WHERE u.k = t.y {LIMIT 2|LIMIT 1 OFFSET 1|GROUP BY u.k LIMIT 1|HAVING COUNT(*) > 1 LIMIT 1|LIMIT 0}) IS {NOT |}NULL",
    "SELECT t.id FROM t WHERE (SELECT DISTINCT 1 FROM u WHERE u.k = t.y LIMIT 1) IS NOT NULL AND (SELECT 1 FROM (SELECT u.k FROM u UNION ALL SELECT u.w FROM u) AS q WHERE q.k = t.y LIMIT 1) IS NULL",
    "SELECT t.id FROM t WHERE (SELECT 1 FROM u WHERE u.k = t.y LIMIT 1) IS NOT NULL OR (SELECT 1 FROM u WHERE u.k = t.x AND u.w IS NULL LIMIT 1) IS NULL",
    "SELECT NTH_VALUE(t.x, {1|2|0}) OVER (PARTITION BY t.y ORDER BY t.id {ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING|ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING|}) AS a, FIRST_VALUE(t.x) OVER (PARTITION BY t.y ORDER BY t.id) AS b FROM t",
    "SELECT t.id, NTH_VALUE(t.x, 1) OVER (ORDER BY t.id ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) AS a, NTH_VALUE(t.x, 1 + 0) OVER (ORDER BY t.id) AS b FROM t",
    "SELECT t.id, NTH_VALUE(t.x, 1) {IGNORE|RESPECT} NULLS OVER (ORDER BY t.id) AS a FROM t",
    ("SELECT t.id, NTH_VALUE(t.x, 1) {IGNORE|RESPECT} NULLS OVER (PARTITION BY t.y ORDER BY t.id) AS a, NTH_VALUE(t.x, 1) FROM {FIRST|LAST} OVER (ORDER BY t.id) AS b FROM t", {"dialect": "{postgres|duckdb}"}),
    ("SELECT t.id FROM t QUALIFY NTH_VALUE(t.x, 1) OVER (PARTITION BY t.y ORDER BY t.id) {>|=|IS NULL} {0|t.x}", {"dialect": "duckdb"}),
]

DISTRIBUTE = [
    "SELECT c.x + 1 AS y FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c {WHERE c.x > 0|}",
    "SELECT c.x, c.s FROM (SELECT t.x, t.s FROM t UNION ALL SELECT u.w, u.v FROM u UNION ALL SELECT t.y, 'z' FROM t) AS c {WHERE c.x IS NOT NULL|}",
    "SELECT c.q FROM (SELECT t.x AS q FROM t WHERE t.y > 0 UNION ALL SELECT u.w AS z FROM u WHERE u.k > 0) AS c",
    "SELECT a.x, b.x AS x2 FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS a JOIN (SELECT t.y AS x FROM t UNION ALL SELECT u.k FROM u) AS b ON a.x = b.x",
    "SELECT a.x, t.id FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS a JOIN t ON t.x = a.x",
    "SELECT c.x FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c {LEFT JOIN u ON u.k = c.x|RIGHT JOIN u ON u.k = c.x|FULL JOIN u ON u.k = c.x}",
    "SELECT {DISTINCT |}c.x FROM (SELECT t.x FROM t UNION {ALL|DISTINCT} SELECT u.w FROM u) AS c",
    "SELECT c.x FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c {ORDER BY c.x LIMIT 2|LIMIT 3 OFFSET 1|GROUP BY c.x|WHERE c.x > (SELECT MIN(u.w) FROM u)}",
    "SELECT SUM(c.x) AS s, COUNT(*) AS n FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c",
    "SELECT c.x, COUNT(*) OVER () AS n FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c",
    "SELECT c.x * 2 AS y, UPPER(c.s) AS z FROM (SELECT t.x, t.s FROM t UNION ALL SELECT u.w, u.v FROM u) AS c WHERE c.x {>|=} 0 AND c.s {LIKE 'a%'|IS NULL|= 'a'}",
    "SELECT c.x FROM ((SELECT t.x FROM t UNION ALL SELECT u.w FROM u) UNION ALL SELECT t.y FROM t) AS c",
    "SELECT c.x FROM (SELECT t.x FROM t INTERSECT DISTINCT SELECT u.w FROM u) AS c",
    "SELECT c.x FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS c WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = c.x)",
    "SELECT c.x, c.y FROM (SELECT t.x, t.y FROM t UNION ALL SELECT u.w AS y, u.k AS x FROM u) AS c",
    "SELECT c.x FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u UNION ALL SELECT t.y FROM t UNION ALL SELECT u.k FROM u) AS c JOIN (SELECT t.x FROM t UNION ALL SELECT u.w FROM u UNION ALL SELECT t.y FROM t UNION ALL SELECT u.k FROM u) AS e ON e.x = c.x",
]

# A union whose branches give one output different numeric types (INT64 and FLOAT64) is declined before any rule
# runs (``set_operation_types.mixed_types``: a filter or projection moved into a branch would run before the
# conversion, which matters past 2**53), so no template mixes them; ``1e0`` and ``0.1`` are both FLOAT64.
UNION_CONSTANTS = [
    "SELECT c.x FROM (SELECT {1e0|0.5e0|2e0|0.1} AS x UNION DISTINCT SELECT {1.0000000000000001e0|0.5000000000000000001e0|2.0000000000000001e0|1e0|3e0|0.10000000000000000555}) AS c",
    "SELECT {UPPER|LOWER}(c.x) AS u FROM (SELECT {'a'|'A'|''} AS x UNION {DISTINCT|ALL} SELECT {'b'|'a'|NULL}) AS c",
    "SELECT c.x + 1 AS y, c.x * 2 AS z FROM (SELECT {1|0|-1} AS x UNION {DISTINCT|ALL} SELECT {2|1|1.0|NULL} UNION {DISTINCT|ALL} SELECT {3|NULL|1.5}) AS c {WHERE c.x > 0|}",
    "SELECT COALESCE(c.x, 0) AS a, NULLIF(c.x, 1) AS b, c.x IS NULL AS n, IF(c.x > 1, 'a', 'b') AS i, -c.x AS m FROM (SELECT 1 AS x UNION DISTINCT SELECT 1.5 UNION ALL SELECT NULL) AS c",
    "SELECT c.x, c.y FROM (SELECT {1|NULL} AS x, {'a'|NULL} AS y UNION DISTINCT SELECT {1|NULL}, {'a'|'b'|NULL}) AS c",
    "SELECT c.x FROM (SELECT 1 AS x UNION {DISTINCT|ALL} SELECT 1 UNION {DISTINCT|ALL} SELECT 2) AS c",
    "SELECT c.x FROM (SELECT 1 AS x UNION ALL SELECT 2 UNION DISTINCT SELECT 1) AS c",
    "SELECT c.x FROM (SELECT 1 AS x UNION DISTINCT SELECT 2 UNION ALL SELECT 1) AS c",
    "SELECT c.x, t.id FROM (SELECT 1 AS x UNION DISTINCT SELECT 2) AS c, t",
    "SELECT c.x, t.id FROM (SELECT 1 AS x UNION DISTINCT SELECT 2) AS c JOIN t ON t.x = c.x",
    "SELECT c.x FROM (SELECT 1 AS x UNION DISTINCT SELECT 2) AS c {ORDER BY c.x|WHERE c.x > (SELECT MIN(u.w) FROM u)|GROUP BY c.x|LIMIT 1}",
    "SELECT {DISTINCT |}c.x % 2 AS r FROM (SELECT 1 AS x UNION DISTINCT SELECT 2 UNION DISTINCT SELECT 3) AS c",
    "SELECT SUM(c.x) AS s, COUNT(*) AS n FROM (SELECT 1 AS x UNION DISTINCT SELECT 2) AS c",
    "SELECT c.x FROM (SELECT 1 AS x UNION DISTINCT SELECT 2 AS y) AS c",
    "SELECT c.x FROM (SELECT 1 AS x, 2 AS x UNION DISTINCT SELECT 2, 3) AS c",
    "WITH c AS (SELECT 'a' AS x UNION DISTINCT SELECT 'b') SELECT UPPER(c.x) AS u FROM c",
    "SELECT UPPER(c.x) AS u FROM (SELECT 'a' AS x UNION DISTINCT SELECT 'b') AS c UNION ALL SELECT t.s FROM t",
    ("SELECT v.n, UPPER(v.s) AS u FROM (VALUES ({1|NULL}, {'a'|'b'}), (2, 'b'), ({3|2}, {'c'|'b'})) AS v(n, s) {WHERE v.n > 0|}", {"dialect": "duckdb"}),
]

VALUES = [
    ("SELECT v.n, v.s FROM (VALUES ({1|NULL}, 'a'), ({2|1}, {'b'|NULL}), (3, 'c')) AS v(n, s) {WHERE v.n > 1|}", {"dialect": "{duckdb|postgres}"}),
    ("SELECT t.id, v.k FROM t JOIN (VALUES (1), (2), ({3|NULL})) AS v(k) ON t.x = v.k", {"dialect": "{duckdb|postgres}"}),
    ("SELECT t.id, v.k FROM t {LEFT|RIGHT|FULL} JOIN (VALUES (1), (2), (3)) AS v(k) ON t.x = v.k", {"dialect": "{duckdb|postgres}"}),
    ("SELECT t.id FROM t WHERE t.x IN (SELECT v.k FROM (VALUES (1), (1), ({2|NULL})) AS v(k))", {"dialect": "{duckdb|postgres}"}),
    ("SELECT {DISTINCT |}v.k FROM (VALUES (1), (1), (2)) AS v(k)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT COUNT(*) AS c, SUM(v.k) AS s FROM (VALUES (1), (1), (2)) AS v(k) {WHERE v.k > 5|}", {"dialect": "{duckdb|postgres}"}),
    ("SELECT v.k + 1 AS a, UPPER(v.s) AS b FROM (VALUES (1 + 1, 'a'), (NULL, 'b'), (3, CONCAT('c', 'd'))) AS v(k, s)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v(n, s)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT v.n FROM (VALUES (1, 'a'), (2)) AS v(n, s)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT v.n FROM (VALUES (1, 'a'), (2, 'b')) AS v(n)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT v.n FROM (VALUES (1), (2)) AS v(n) WHERE v.n > (SELECT MIN(t.x) FROM t)", {"dialect": "{duckdb|postgres}"}),
    ("SELECT v.k FROM (VALUES (1), (2)) AS v(k) UNION ALL SELECT t.x FROM t", {"dialect": "{duckdb|postgres}"}),
    ("SELECT t.id FROM t, (VALUES (1, 2)) AS v(a, b) WHERE t.x = v.a AND t.y = v.b", {"dialect": "{duckdb|postgres}"}),
    ("SELECT t.id FROM t WHERE (t.x, t.y) IN (SELECT v.a, v.b FROM (VALUES (1, 2), (2, NULL)) AS v(a, b))", {"dialect": "{duckdb|postgres}"}),
]

AST_UTILS = [
    ("SELECT t.id FROM t WHERE t.x IS NOT NULL AND t.s NOT LIKE 'a%' {AND t.y > 0|OR t.y IS NOT NULL|}", {"dialect": "{postgres|duckdb|mysql}"}),
    ("SELECT t.id, t.x IS NOT NULL AS a, t.s NOT LIKE '%b' AS b FROM t", {"dialect": "{postgres|duckdb|mysql}"}),
    ("SELECT t.id FROM t WHERE (t.x IS NOT NULL) IS NOT NULL AND NOT (t.s NOT LIKE 'a%')", {"dialect": "{postgres|duckdb}"}),
    ("SELECT t.id FROM t WHERE t.s NOT ILIKE 'a%' OR t.s ILIKE 'b%'", {"dialect": "{postgres|duckdb}"}),
    ("SELECT t.id FROM t WHERE t.x IS NOT NULL IS NOT NULL", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a, d.b FROM t AS d({a|id}, {b|x}) WHERE d.a > 0", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a, d.y FROM t AS d(a) WHERE d.y > 0", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a, d.y FROM t AS d(a, b, c, d, e, f, g)", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a, e.c FROM (SELECT t.x, t.y FROM t) AS d(a, b) JOIN u AS e(c, d, f) ON e.c = d.b", {"dialect": "{postgres|duckdb}"}),
    ("WITH c AS (SELECT t.x, t.y FROM t) SELECT d.a FROM c AS d(a) WHERE d.a > 0", {"dialect": "{postgres|duckdb}"}),
    ("WITH c(a, b) AS (SELECT t.x, t.y FROM t) SELECT a FROM c WHERE b > 0", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a, d.id FROM (SELECT t.x AS a, t.y AS id FROM t) AS d(id, a) WHERE d.id > 0", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a FROM unknown_table AS d(a, b)", {"dialect": "{postgres|duckdb}"}),
    ("SELECT d.a FROM UNNEST(ARRAY[1, 2]) AS d(a) WHERE d.a > 0", {"dialect": "{postgres|duckdb}"}),
    "SELECT d.id FROM (SELECT t.id, t.x IS NOT NULL AS g FROM t) AS d WHERE d.id IS NOT NULL AND {TRUE|FALSE|NOT TRUE} = d.g",
    "SELECT d.id FROM (SELECT t.id, t.x IS NULL AS g, t.y IS NOT NULL AS h FROM t) AS d WHERE (d.id > 0) = d.h AND d.h = d.g AND d.g = (d.h)",
    "SELECT d.id FROM (SELECT t.id, t.x IS NOT NULL AS g FROM t) AS d WHERE d.id IS NOT NULL AND (d.id > 0) {=|<>|IS NOT DISTINCT FROM} d.g",
    "SELECT d.id, d.g = d.h AS e FROM (SELECT t.id, t.x IS NOT NULL AS g, t.y IS NULL AS h FROM t) AS d WHERE d.id IS NOT NULL",
]

TEMPLATES = (
    NULL_GUARDS + DERIVED_GUARDS + GLOBAL_FILTERS + CONSTANT_SOURCES + CONSTANT_COLUMNS + SINGLE_ROW + PROBES + DISTRIBUTE + UNION_CONSTANTS + VALUES + AST_UTILS
)


def cases(seed: int, count: int) -> list[dict]:
    return build(TEMPLATES, seed, count, "guards")
