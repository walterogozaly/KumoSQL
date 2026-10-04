"""DISTINCT, dedup-join, set-operation and set-split rules: DISTINCT over keys and joins, UNION/INTERSECT/EXCEPT
with tails, IN/EXISTS over unions, same-source merges, split-by-CASE and OR."""

from ._base import expand

SETOP = "{UNION ALL|UNION DISTINCT|INTERSECT DISTINCT|EXCEPT DISTINCT}"
TAIL = "{|ORDER BY 1 LIMIT 2|ORDER BY 1, 2 LIMIT 3|LIMIT 1 OFFSET 1}"
PRED = "{t.x > 0|t.x IS NULL|t.y = 1|t.id < 3|t.s = 'a'|t.x = t.y|t.x IS NOT NULL}"
PRED2 = "{t.x <= 0|t.x IS NOT NULL|t.y = 2|t.id >= 3|t.s <> 'a'|t.x <> t.y|t.x IS NULL}"

TEMPLATES = [
    f"SELECT {{DISTINCT|}} t.x, t.y FROM t WHERE {PRED} {SETOP} SELECT {{DISTINCT|}} t.x, t.y FROM t WHERE {PRED2}",
    f"(SELECT t.x, t.y FROM t WHERE {PRED}) {SETOP} (SELECT t.x, t.y FROM t WHERE {PRED2}) {TAIL}",
    f"SELECT t.x FROM t WHERE {PRED} UNION ALL SELECT t.x FROM t WHERE NOT ({PRED})",
    f"SELECT t.x FROM t WHERE {PRED} UNION ALL SELECT t.x FROM t WHERE ({PRED}) IS NULL",
    f"SELECT t.x FROM t WHERE {PRED} {SETOP} SELECT t.x FROM t",
    f"SELECT t.x FROM t {SETOP} SELECT t.x FROM t WHERE {PRED}",
    f"SELECT DISTINCT t.x FROM t WHERE {PRED} {TAIL}",
    f"SELECT DISTINCT t.id FROM t {{JOIN|LEFT JOIN}} p ON p.tid = t.id",
    f"SELECT DISTINCT t.x FROM t JOIN p ON p.tid = t.id WHERE {PRED}",
    f"SELECT t.x, COUNT(*) AS c FROM t JOIN p ON p.tid = t.id GROUP BY t.x",
    f"SELECT DISTINCT t.x FROM t WHERE t.id {{IN|NOT IN}} (SELECT p.tid FROM p)",
    f"SELECT t.x FROM t WHERE t.y {{IN|NOT IN}} (SELECT u.k FROM u WHERE u.w > 0 UNION ALL SELECT u.w FROM u)",
    f"SELECT t.x FROM t WHERE t.y IN (SELECT {{u.k|u.w}} FROM u {{|WHERE u.w IS NOT NULL}}) {TAIL}",
    f"SELECT t.x FROM t WHERE {{EXISTS|NOT EXISTS}} (SELECT 1 FROM u WHERE u.k = t.y {{|AND u.w > t.x}})",
    f"SELECT t.id FROM t WHERE {{EXISTS|NOT EXISTS}} (SELECT 1 FROM p WHERE p.tid = t.id) {SETOP} SELECT p.tid FROM p",
    f"SELECT DISTINCT d.x FROM (SELECT t.x FROM t UNION ALL SELECT u.w FROM u) AS d",
    f"SELECT d.x FROM (SELECT t.x FROM t {SETOP} SELECT u.w FROM u) AS d WHERE d.x {{> 0|IS NULL|= 1}}",
    f"SELECT d.x, d.y FROM (SELECT t.x, t.y FROM t UNION ALL SELECT u.w, u.k FROM u) AS d JOIN t ON t.id = d.x",
    f"SELECT DISTINCT t.id, t.x FROM t JOIN (SELECT DISTINCT p.tid FROM p) AS q ON q.tid = t.id",
    f"SELECT t.x FROM t JOIN (SELECT DISTINCT p.tid FROM p) AS q ON q.tid = t.id",
    f"SELECT DISTINCT q.x FROM (SELECT t.x, t.y FROM t) AS q",
    f"SELECT t.x FROM t JOIN t AS t2 ON t.id = {{t2.id|t2.x|t2.y}}",
    f"SELECT DISTINCT t.x FROM t JOIN t AS t2 ON {{t.x = t2.x|t.x = t2.y|t.y = t2.y AND t.x = t2.x}}",
    f"SELECT DISTINCT t.x FROM t JOIN u ON {{CASE WHEN t.x = 1 THEN t.y WHEN t.y = 1 THEN t.x END = u.k|CASE WHEN t.x = 1 THEN t.y ELSE t.x END = u.w}}",
    f"SELECT DISTINCT t.x FROM t WHERE {{CASE WHEN t.y = 1 THEN t.x WHEN t.x = 2 THEN t.y END = 1|t.x <> t.y|t.x BETWEEN t.y AND t.id}}",
    f"SELECT DISTINCT t.x, {{t.y|t.x + 1|t.s}} FROM t WHERE {PRED} {SETOP} SELECT DISTINCT t.y, t.x FROM t WHERE {PRED2}",
    f"SELECT {{DISTINCT|}} t.x FROM t WHERE t.y IN (SELECT u.k FROM u UNION DISTINCT SELECT u.w FROM u)",
    f"(SELECT t.x FROM t ORDER BY t.id LIMIT 2) {SETOP} (SELECT t.x FROM t ORDER BY t.id DESC LIMIT 2)",
    f"SELECT x FROM (SELECT t.x FROM t {TAIL.replace('1, 2', '1')}) AS q {{UNION ALL|UNION DISTINCT}} SELECT u.w FROM u",
    f"SELECT t.x FROM t WHERE {PRED} EXCEPT DISTINCT SELECT t.x FROM t WHERE {PRED2}",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "distinct_sets")
