"""Outer-join rules: LEFT/RIGHT/FULL to inner, indicator joins, padded-side filters, exists in ON, null-extended
filters, grouped outer joins and foreign-key joins."""

from ._base import expand

JOIN = "{LEFT JOIN|RIGHT JOIN|FULL JOIN|JOIN}"
WHERE = "{|WHERE p.b|WHERE p.n > 0|WHERE p.id IS NULL|WHERE p.id IS NOT NULL|WHERE t.x > 0|WHERE p.tid = t.id|WHERE COALESCE(p.n, 0) > 0|WHERE p.b IS NULL OR t.x > 0}"
ON = "{p.tid = t.id|p.tid = t.id AND p.n > 0|p.tid = t.id AND t.x > 0|p.tid = t.id AND p.b|p.tid = t.x}"

TEMPLATES = [
    f"SELECT t.id, p.n FROM t {JOIN} p ON {ON} {WHERE}",
    f"SELECT t.id, p.id AS pid FROM t {JOIN} p ON {ON} {WHERE}",
    f"SELECT COUNT(*) AS c, COUNT(p.id) AS d FROM t {JOIN} p ON {ON} {WHERE}",
    f"SELECT t.id, COUNT(p.id) AS c, SUM(p.n) AS s FROM t {JOIN} p ON {ON} {WHERE} GROUP BY t.id",
    f"SELECT t.x, COUNT(*) AS c FROM t {JOIN} p ON {ON} GROUP BY t.x {{|HAVING COUNT(p.id) > 0|HAVING SUM(p.n) > 0}}",
    f"SELECT t.id FROM t LEFT JOIN p ON {ON} WHERE p.id IS NULL",
    f"SELECT t.id FROM t WHERE NOT EXISTS (SELECT 1 FROM p WHERE {{p.tid = t.id|p.tid = t.id AND p.b}})",
    f"SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM p WHERE p.tid = t.id {{|AND p.n > 0}})",
    f"SELECT t.id, u.v FROM t {{LEFT JOIN|JOIN}} p ON {ON} {{LEFT JOIN|JOIN|RIGHT JOIN}} u ON {{u.k = p.tid|u.k = t.y|u.w = p.id|u.k = COALESCE(p.tid, 0)}}",
    f"SELECT t.id, q.c FROM t {{LEFT JOIN|JOIN}} (SELECT p.tid, COUNT(*) AS c FROM p GROUP BY p.tid) AS q ON q.tid = t.id {{|WHERE q.c > 0|WHERE q.c IS NULL|WHERE COALESCE(q.c, 0) = 0}}",
    f"SELECT t.id, q.n FROM t LEFT JOIN (SELECT p.* FROM p WHERE {{p.b|p.n > 0}}) AS q ON q.tid = t.id",
    f"SELECT t.id FROM t LEFT JOIN p ON p.tid = t.id AND {{EXISTS (SELECT 1 FROM u WHERE u.k = 1)|EXISTS (SELECT 1 FROM u WHERE u.k = t.y)|NOT EXISTS (SELECT 1 FROM u)}}",
    f"SELECT t.id FROM t LEFT JOIN p ON p.tid = t.id WHERE t.id = {{1|2|p.tid}}",
    f"SELECT a.id, b.id AS bid FROM t AS a {{LEFT JOIN|FULL JOIN|JOIN}} t AS b ON {{a.id = b.id|a.x = b.y|a.id = b.id AND a.x > 0}} {{|WHERE b.id IS NULL|WHERE b.id IS NOT NULL}}",
    f"SELECT t.id, COUNT(*) AS c FROM t {{LEFT JOIN|FULL JOIN}} p ON p.tid = t.id GROUP BY t.id",
    f"SELECT p.tid, COUNT(*) AS c FROM p {{LEFT JOIN|JOIN}} t ON t.id = p.tid GROUP BY p.tid",
    f"SELECT DISTINCT t.id FROM t {{LEFT JOIN|JOIN}} p ON p.tid = t.id",
    f"SELECT t.id FROM t {{LEFT JOIN|JOIN}} p ON p.tid = t.id {{LEFT JOIN|JOIN}} u ON u.k = t.y",
    f"SELECT t.x FROM t JOIN p ON p.tid = t.id WHERE {{p.n IS NOT NULL|p.tid IS NOT NULL|TRUE}}",
    f"SELECT t.id, p.n FROM t LEFT JOIN p USING (id)",
    f"SELECT t.id FROM t LEFT JOIN p ON p.tid = t.id AND p.id IS NULL",
    f"SELECT {{MAX|MIN|SUM|COUNT}}(p.n) AS a FROM t LEFT JOIN p ON p.tid = t.id WHERE t.x {{> 0|IS NULL}}",
    f"SELECT t.id FROM (SELECT t.id, t.x FROM t LEFT JOIN p ON p.tid = t.id) AS t WHERE t.x > 0",
    f"SELECT q.id FROM (t LEFT JOIN (p JOIN u ON u.k = p.tid) ON p.tid = t.id) AS q",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "outer_joins")
