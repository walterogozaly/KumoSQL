"""Windowed aggregates and LAG/LEAD, which the later ``window_joins`` attempt spells as joins (``rule_fuzz.py`` runs it for any query with OVER)."""

from ._base import expand

AGG = "{SUM(t.x)|COUNT(*)|MIN(t.y)|MAX(t.x)|AVG(t.y)|COUNT(t.x)|SUM(t.x + t.y)|SUM(DISTINCT t.x)}"
PART = "{PARTITION BY t.y|PARTITION BY t.x|PARTITION BY t.x, t.y|PARTITION BY t.id|PARTITION BY t.f|}"
ORDER = "{ORDER BY t.id|ORDER BY t.id DESC|ORDER BY t.x, t.id|ORDER BY t.x|ORDER BY t.y NULLS FIRST, t.id}"
LAG = "{LAG(t.x)|LEAD(t.x)|LAG(t.y, 2)|LEAD(t.x, 2, -1)|LAG(t.x, 1, 0)|LAG(t.s, 1, 'z')|LEAD(t.x, 0)|LAG(t.x IGNORE NULLS)}"
WHERE = "{|WHERE t.x > 1|WHERE t.y IS NOT NULL|WHERE t.x IS NULL OR t.y = 2|WHERE t.y IN (SELECT u.k FROM u)}"
TAIL = "{|ORDER BY 1|ORDER BY 2 DESC, 1 LIMIT 3}"

TEMPLATES = [
    f"SELECT t.id, {AGG} OVER ({PART}) AS w FROM t {WHERE} {TAIL}",
    f"SELECT t.id, {AGG} OVER ({PART}) AS w, {AGG} OVER ({PART}) AS w2 FROM t {WHERE}",
    f"SELECT t.id, t.x * 10 / {{SUM(t.y)|COUNT(*)|MAX(t.x)}} OVER ({PART}) AS w FROM t",
    f"SELECT DISTINCT t.y, {AGG} OVER ({PART}) AS w FROM t {WHERE}",
    f"SELECT t.id, {AGG} OVER ({PART} {{ORDER BY t.id|ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING|ORDER BY t.id ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING}}) AS w FROM t",
    f"SELECT a.id, {AGG.replace('t.', 'a.')} OVER ({PART.replace('t.', 'a.')}) AS w FROM t AS a {WHERE.replace('t.', 'a.')}",
    f"SELECT t.id, {AGG} OVER ({PART}) AS w FROM t JOIN u ON u.k = t.y",
    f"SELECT t.id, {LAG} OVER ({PART} {ORDER}) AS p FROM t {WHERE} {TAIL}",
    f"SELECT t.id, {LAG} OVER ({PART} {ORDER}) AS p, {LAG} OVER ({PART} {ORDER}) AS q FROM t",
    f"SELECT t.id, t.x, {LAG} OVER ({PART} {ORDER}) AS p FROM t",
    f"SELECT q.id, q.p FROM (SELECT t.id, {LAG} OVER ({PART} {ORDER}) AS p FROM t) AS q WHERE q.p {{> 1|IS NULL}}",
    f"SELECT t.id, {LAG} OVER ({PART} {ORDER}) AS p FROM t JOIN u ON u.k = t.y",
    # hand-written numbering joins: unread columns of the derived tables, and the several spellings of the neighbour condition
    f"WITH r AS (SELECT t.id, t.x, t.y, ROW_NUMBER() OVER ({PART} {ORDER}) AS rn FROM t {WHERE}) "
    f"SELECT a.id, b.x FROM r AS a {{LEFT JOIN|JOIN}} r AS b ON {{b.rn = a.rn - 1|a.rn = b.rn + 1|a.rn + 1 = b.rn|b.rn - 1 = a.rn|b.rn = a.rn + 2|a.rn = b.rn}}",
    f"SELECT a.id, b.id AS bid FROM (SELECT t.id, t.x, ROW_NUMBER() OVER ({PART} {ORDER}) AS rn FROM t) AS a LEFT JOIN (SELECT t.id, t.y, ROW_NUMBER() OVER ({PART} {ORDER}) AS rn FROM t) AS b ON {{a.rn = b.rn + 1|b.rn = a.rn - 1}}",
    f"SELECT q.id FROM (SELECT t.id, t.x, {AGG} OVER ({PART}) AS s, {{ROW_NUMBER() OVER (ORDER BY t.id)|RANK() OVER (ORDER BY t.x)}} AS n FROM t) AS q WHERE q.n {{= 1|> 1}}",
    f"SELECT q.id, q.s FROM (SELECT t.id, t.x, {LAG} OVER ({PART} {ORDER}) AS s FROM t) AS q",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "window_joins")
