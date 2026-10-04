"""Casts, integer division, dates, LIKE, string literals, constant correlation, quantified comparisons, scalar
subqueries, decorrelation, UNNEST and constant folding."""

from ._base import expand

TEMPLATES = [
    "SELECT t.id FROM t WHERE CAST(t.x AS {FLOAT64|NUMERIC|STRING|INT64}) {=|<|>} {1|1.5|'1'|CAST(t.y AS FLOAT64)}",
    "SELECT CAST(t.x AS {INT64|FLOAT64|NUMERIC}) AS a, CAST(CAST(t.x AS {FLOAT64|STRING|NUMERIC}) AS {INT64|FLOAT64}) AS b FROM t",
    "SELECT DIV(t.x, {2|0|3|-2}) AS q, t.x / {2|3} AS d FROM t WHERE t.x IS NOT NULL",
    "SELECT DIV(t.x, 2) AS a, CAST(t.x / 2 AS INT64) AS b, SAFE_DIVIDE(t.x, t.y) AS c FROM t",
    "SELECT t.id FROM t WHERE t.x * 1.0 = t.f",
    "SELECT t.id FROM t WHERE t.f {=|<|>=} {0.1|0.30000000000000004|1e-324|1e309|0.5}",
    "SELECT t.id FROM t WHERE t.d {>=|<|=} DATE '2024-01-{01|31}' AND t.d {<|<=} DATE_ADD(DATE '2024-01-01', INTERVAL {30|1} {DAY|MONTH})",
    "SELECT t.id FROM t WHERE EXTRACT({YEAR|MONTH|DAY} FROM t.d) = {2024|1|3}",
    "SELECT DATE_TRUNC(t.d, {MONTH|YEAR}) AS m, COUNT(*) AS c FROM t GROUP BY 1",
    "SELECT t.id FROM t WHERE t.s LIKE {'a%'|'%a'|'a_'|'%'|'a'} {AND t.s LIKE 'a%'|OR t.s LIKE 'b%'|}",
    "SELECT t.id FROM t WHERE t.s {=|<>|<} {'a'|'A'|'a '|''} AND t.s IS NOT NULL",
    "SELECT UPPER(LOWER(t.s)) AS a, CONCAT(t.s, 'x') AS b, t.s || 'y' AS c, LOWER(TRIM(t.s)) AS d FROM t",
    "SELECT t.id FROM t WHERE t.x = {1|2} AND t.y = t.x AND EXISTS (SELECT 1 FROM u WHERE u.k = t.y AND u.w = t.x)",
    "SELECT t.id FROM t WHERE t.x = 1 AND t.id IN (SELECT p.tid FROM p WHERE p.n > t.x)",
    "SELECT t.id FROM t WHERE t.x {>|<|=} {ANY|ALL} (SELECT u.w FROM u {WHERE u.k > 0|})",
    "SELECT t.id FROM t WHERE t.x {>|<|=|<>} {ANY|ALL|SOME} (SELECT u.w FROM u WHERE u.k = t.y)",
    "SELECT t.id FROM t WHERE t.x {>|<|=} (SELECT {MAX|MIN|COUNT|SUM}(u.w) FROM u {WHERE u.k = t.y|})",
    "SELECT t.id, (SELECT {MAX|SUM|COUNT|MIN}(u.w) FROM u WHERE u.k = t.y) AS m FROM t",
    "SELECT t.id, (SELECT u.w FROM u WHERE u.k = t.y {LIMIT 1|}) AS m FROM t",
    "SELECT t.id, t.x IN (SELECT u.w FROM u) AS m FROM t",
    "SELECT t.id, EXISTS (SELECT 1 FROM u WHERE u.k = t.y) AS m, {COUNT(*) OVER ()|1} AS c FROM t",
    "SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u WHERE u.k = t.y {AND u.w > 0|})",
    "SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE u.k = t.y AND u.w {>|=|IS NULL OR u.w >} t.x)",
    "SELECT t.id FROM t, UNNEST([1, 2, {3|NULL}]) AS e WHERE e {>|=} t.x",
    "SELECT t.id, o FROM t CROSS JOIN UNNEST({[t.x, t.y]|[1, 2]|[]}) AS e WITH OFFSET AS o",
    "SELECT t.id FROM t WHERE {1 = 1|NULL IS NULL|1 > 2|TRUE AND NULL|FALSE OR t.x IS NULL} {AND|OR} t.x {> 0|= 1}",
    "SELECT t.id FROM t WHERE NOT (t.x {> 0|IS NULL|= 1}) {AND|OR} NOT (t.y {< 2|IS NOT NULL})",
    "SELECT IF(t.x > 0, t.x, NULL) AS a, COALESCE(t.x, t.y, 0) AS b, NULLIF(t.x, 0) AS c, IFNULL(t.x, 1) AS d FROM t",
    "SELECT CASE WHEN t.x > 0 THEN 1 WHEN t.x > 1 THEN 2 ELSE 3 END AS a, CASE t.x WHEN 1 THEN 'a' END AS b FROM t",
    "SELECT t.id FROM t WHERE t.x BETWEEN {0|t.y} AND {2|t.id} {AND t.x IS NOT NULL|}",
    "SELECT t.id FROM t WHERE t.x IN ({1, 2|1, NULL|0}) {AND|OR} t.x NOT IN ({1|2, 3|1, NULL})",
    "SELECT t.id FROM t WHERE t.x IS DISTINCT FROM t.y {AND|OR} t.x {IS NOT DISTINCT FROM|=} t.id",
    "WITH c AS (SELECT t.x, t.y FROM t) SELECT c.x FROM c {WHERE c.y > 0|} {UNION ALL SELECT c.y FROM c|}",
    "WITH c AS (SELECT t.id AS x FROM t) SELECT c.x, c2.x AS x2 FROM c JOIN c AS c2 ON c.x = c2.x",
    "WITH t AS (SELECT u.k AS id, u.w AS x FROM u) SELECT t.id FROM t WHERE t.x > 0",
    "SELECT * FROM t WHERE t.x > 0",
    "SELECT * EXCEPT (f, d) FROM t WHERE t.x > 0",
    "SELECT t.id, t.x FROM t JOIN u ON u.k = t.id",
    "SELECT q.* FROM (SELECT t.id AS a, t.x AS b FROM t) AS q WHERE q.b > 0",
    "SELECT t.id FROM t WHERE t.x = (SELECT 1) AND t.y IN ((SELECT 1), (SELECT 2))",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "scalars")
