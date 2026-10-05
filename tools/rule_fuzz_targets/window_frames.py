"""ROWS and RANGE frames over ordered windows: a unique order (the rule fires) and ties or offsets (a guard must stop it)."""

from ._base import expand

FUNC = "{SUM(t.x)|COUNT(*)|MIN(t.y)|MAX(t.x)|AVG(t.y)|COUNTIF(t.x > 1)|LOGICAL_OR(t.y > 0)|FIRST_VALUE(t.x)|LAST_VALUE(t.x)|NTH_VALUE(t.x, 2)|COUNT(t.x)}"
FRAME = "{ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW|ROWS UNBOUNDED PRECEDING|RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW|ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING|ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING|ROWS BETWEEN CURRENT ROW AND CURRENT ROW|ROWS BETWEEN 1 PRECEDING AND CURRENT ROW|RANGE BETWEEN 1 PRECEDING AND CURRENT ROW|}"
PART = "{|PARTITION BY t.y |PARTITION BY t.x |PARTITION BY t.id }"
ORDER = "{ORDER BY t.id|ORDER BY t.id DESC|ORDER BY t.x, t.id|ORDER BY t.x|ORDER BY t.y DESC|ORDER BY t.id + 0|ORDER BY t.x NULLS FIRST, t.id|}"

TEMPLATES = [
    f"SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w FROM t",
    f"SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w FROM t WHERE {{t.x > 1|t.y IS NOT NULL|t.x IS NULL OR t.y = 2}}",
    f"SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w2 FROM t",
    f"SELECT q.id, q.w FROM (SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w FROM t) AS q WHERE q.w {{> 1|IS NULL}}",
    f"SELECT t.id FROM t QUALIFY {FUNC} OVER ({PART}{ORDER} {FRAME}) {{> 1|IS NOT NULL}}",
    f"SELECT a.id, {FUNC.replace('t.', 'a.')} OVER ({PART.replace('t.', 'a.')}{ORDER.replace('t.', 'a.')} {FRAME}) AS w FROM t AS a",
    f"SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w FROM t JOIN u ON u.k = t.y",
    f"SELECT t.id, {FUNC} OVER ({PART}{ORDER} {FRAME}) AS w FROM t {{|ORDER BY 1|ORDER BY 2 DESC, 1 LIMIT 3}}",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "window_frames")
