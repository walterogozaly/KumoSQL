"""Grouping sets, windows, QUALIFY, ORDER BY / LIMIT rules, unread windows and empty-relation propagation."""

from ._base import expand

TAIL = "{|ORDER BY 1 LIMIT 2|ORDER BY 1, 2 LIMIT 3|ORDER BY 2 DESC LIMIT 1 OFFSET 1|LIMIT 2}"
GROUPING = "{ROLLUP (t.x, t.y)|CUBE (t.x, t.y)|GROUPING SETS ((t.x), (t.y))|GROUPING SETS ((t.x, t.y), ())|ROLLUP (t.x)|t.x, ROLLUP (t.y)|GROUPING SETS ((), ())|()}"
WIN = "{ROW_NUMBER() OVER (ORDER BY t.id)|RANK() OVER (ORDER BY t.x)|SUM(t.x) OVER (PARTITION BY t.y)|COUNT(*) OVER ()|SUM(t.x) OVER (ORDER BY t.id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)|MAX(t.x) OVER (PARTITION BY t.y ORDER BY t.id)|LAG(t.x) OVER (ORDER BY t.id)|FIRST_VALUE(t.x) OVER (PARTITION BY t.y ORDER BY t.id)|COUNT(t.x) OVER (PARTITION BY t.id)}"

TEMPLATES = [
    f"SELECT t.x, t.y, COUNT(*) AS c, SUM(t.id) AS s FROM t GROUP BY {GROUPING} {TAIL}",
    f"SELECT t.x, COUNT(*) AS c FROM t GROUP BY {GROUPING} {{|HAVING COUNT(*) > 1|HAVING t.x IS NOT NULL}}",
    f"SELECT COUNT(*) AS c FROM t WHERE {{FALSE|t.x > 0}} GROUP BY {GROUPING}",
    f"SELECT t.x, GROUPING(t.x) AS g, COUNT(*) AS c FROM t GROUP BY {{ROLLUP (t.x, t.y)|CUBE (t.x)|GROUPING SETS ((t.x), ())}}",
    f"SELECT t.x, SUM(t.id) AS s FROM t GROUP BY t.x UNION ALL SELECT NULL, SUM(t.id) FROM t",
    f"SELECT t.x, t.y, SUM(t.id) AS s FROM t GROUP BY t.x, t.y UNION ALL SELECT t.x, NULL, SUM(t.id) FROM t GROUP BY t.x UNION ALL SELECT NULL, NULL, SUM(t.id) FROM t",
    f"SELECT t.id, {WIN} AS w FROM t {TAIL}",
    f"SELECT t.id, {WIN} AS w, {WIN} AS w2 FROM t",
    f"SELECT q.id FROM (SELECT t.id, {WIN} AS w FROM t) AS q",
    f"SELECT q.id, q.w FROM (SELECT t.id, {WIN} AS w FROM t) AS q WHERE {{q.w > 1|q.w IS NULL|q.id > 1}}",
    f"SELECT t.id FROM t WHERE TRUE QUALIFY {{ROW_NUMBER() OVER (PARTITION BY t.y ORDER BY t.id) = 1|RANK() OVER (ORDER BY t.x) <= 2|COUNT(*) OVER (PARTITION BY t.y) > 1}}",
    f"SELECT t.y, {WIN} AS w FROM t GROUP BY t.y {{|HAVING COUNT(*) > 1}}".replace("COUNT(t.x) OVER (PARTITION BY t.id)", "COUNT(*) OVER ()"),
    # qualify_filter: QUALIFY of a grouped select
    "SELECT t.y, COUNT(*) AS c FROM t GROUP BY t.y {|HAVING COUNT(*) > 1} QUALIFY {RANK() OVER (ORDER BY COUNT(*) DESC) = 1|DENSE_RANK() OVER (ORDER BY SUM(t.x)) <= 2|COUNT(*) OVER () > 1} {|AND t.y IS NOT NULL}",
    "SELECT {|DISTINCT} COUNT(*) AS c, {|RANK() OVER (ORDER BY COUNT(*)) AS r,} MAX(t.x) AS m FROM t {|WHERE t.x > 0} GROUP BY t.y QUALIFY RANK() OVER (ORDER BY COUNT(*)) {<= 2|= 1}",
    # window_pushdown: a filter on PARTITION BY columns above and below the window
    "SELECT q.y, q.w FROM (SELECT t.y, t.id, {ROW_NUMBER() OVER (PARTITION BY t.y ORDER BY t.id)|SUM(t.x) OVER (PARTITION BY t.y)|COUNT(*) OVER (PARTITION BY t.y, t.x)|RANK() OVER (PARTITION BY t.y ORDER BY t.x)} AS w FROM t {|WHERE t.id > 1}) AS q WHERE {q.y = 1|q.y IS NULL|q.y IS NOT NULL|q.y IN (0, 1)|q.y > 0 OR q.y IS NULL|q.w > 1|q.id > 1|q.y = 1 AND q.id > 1|q.y = q.w}",
    "SELECT q.y FROM (SELECT t.y, t.x, SUM(t.id) OVER (PARTITION BY t.y, t.x) AS a, COUNT(*) OVER (PARTITION BY {t.y, t.x|t.y|t.x}) AS b FROM t QUALIFY {a > 1|b > 1|TRUE}) AS q WHERE {q.y = 1|q.x = 1|q.x IS NULL|q.y = q.x}",
    f"SELECT DISTINCT t.y, {{ROW_NUMBER() OVER (ORDER BY t.y)|SUM(t.x) OVER (PARTITION BY t.y)}} AS w FROM t",
    f"SELECT t.x FROM t ORDER BY t.x {{|LIMIT 0|LIMIT 1|LIMIT 2 OFFSET 1}}",
    f"SELECT q.x FROM (SELECT t.x FROM t ORDER BY t.x {{|LIMIT 2|LIMIT 1}}) AS q {{|ORDER BY q.x|ORDER BY q.x DESC LIMIT 1}}",
    f"SELECT q.x FROM (SELECT t.x, t.y FROM t ORDER BY t.id LIMIT 3) AS q WHERE q.y {{> 0|IS NULL}}",
    f"SELECT q.x FROM (SELECT t.x FROM t ORDER BY t.id LIMIT 3) AS q {{JOIN|LEFT JOIN}} u ON u.k = q.x",
    f"SELECT COUNT(*) AS c FROM (SELECT t.x FROM t ORDER BY t.x LIMIT {{0|2}}) AS q",
    f"SELECT MAX(q.x) AS m FROM (SELECT t.x FROM t ORDER BY t.x DESC LIMIT 2) AS q",
    f"SELECT t.x FROM t WHERE t.x > {{1|0}} ORDER BY t.x LIMIT 5 {{|OFFSET 1}}",
    f"(SELECT t.x FROM t ORDER BY t.x LIMIT 2) UNION ALL (SELECT u.w FROM u ORDER BY u.w LIMIT 2) {TAIL}",
    f"SELECT t.x FROM t WHERE FALSE {{UNION ALL|UNION DISTINCT|EXCEPT DISTINCT|INTERSECT DISTINCT}} SELECT u.w FROM u",
    f"SELECT t.x FROM t {{UNION ALL|EXCEPT DISTINCT}} SELECT u.w FROM u WHERE FALSE",
    f"SELECT t.x, q.c FROM t JOIN (SELECT COUNT(*) AS c FROM u WHERE FALSE) AS q ON TRUE",
    f"SELECT COUNT(*) AS c, SUM(t.x) AS s FROM t WHERE {{FALSE|1 = 0|t.x IS NULL AND t.x IS NOT NULL}}",
    f"SELECT {{t.x|COUNT(*)}} FROM t WHERE FALSE {{GROUP BY t.x|}}",
    f"SELECT SUM(COUNT(*)) OVER () AS s FROM t {{WHERE FALSE|}}",
    f"SELECT COUNT(*) AS c FROM t WHERE FALSE HAVING COUNT(*) {{= 0|> 0}}",
    f"SELECT t.x FROM t WHERE EXISTS (SELECT 1 FROM u WHERE FALSE {{|GROUP BY ()}})",
    f"SELECT t.x FROM t WHERE t.y IN (SELECT u.k FROM u WHERE FALSE)",
    f"SELECT t.x FROM t WHERE t.y NOT IN (SELECT u.k FROM u WHERE FALSE) {{|AND t.y IS NOT NULL}}",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "grouping_windows")
