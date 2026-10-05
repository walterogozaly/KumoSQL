"""The first (or latest) row of each group: ``latest_row_rules`` and ``rank_interchange``.

Templates over ``t(id, x, y, ..)`` with declared keys that sometimes make ``(x, y)`` a total order and sometimes
leave ties, nullable and NOT NULL order columns, both directions and NULL placements, and the near misses
(non-total ``ROW_NUMBER``, ascending order on a nullable column, ``MAX_BY`` of a nullable value, ``= 2``).
"""

import random

from ._base import expand

FIRST = "{= 1|<= 1|< 2}"
NUMBERING = "{ROW_NUMBER|RANK|DENSE_RANK}"
ORDER = "{t.y|t.y DESC|t.y DESC NULLS FIRST|t.y NULLS LAST|t.y ASC NULLS FIRST}"

TEMPLATES = [
    f"SELECT t.id, t.y FROM t QUALIFY {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) {FIRST}",
    f"SELECT t.x, t.y FROM t QUALIFY {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) {FIRST}",
    f"SELECT t.x, t.y + 1 AS z FROM t WHERE t.id > 0 QUALIFY {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) {FIRST}",
    f"SELECT t.id FROM t QUALIFY {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) {FIRST} AND t.id > 1",
    f"SELECT t.id FROM t QUALIFY {NUMBERING}() OVER (ORDER BY {ORDER}) {FIRST}",
    f"SELECT q.id, q.n FROM (SELECT t.id, {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) AS n FROM t) AS q WHERE q.n {FIRST}",
    f"SELECT q.x, q.y FROM (SELECT t.x, t.y, {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) AS n FROM t) AS q WHERE q.n {FIRST}",
    f"SELECT t.x, {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) AS n FROM t",
    f"SELECT t.id FROM t QUALIFY {NUMBERING}() OVER (PARTITION BY t.x ORDER BY {ORDER}) {{= 2|<= 2|< 3}}",
    "SELECT t.x, {MAX_BY|MIN_BY}(t.id, t.y) AS v FROM t GROUP BY t.x",
    "SELECT t.x, MAX(t.y) AS m, MAX_BY(t.id, t.y) AS v FROM t GROUP BY t.x",
    "SELECT t.x, ARRAY_AGG(t.id ORDER BY {t.y|t.y DESC} LIMIT 1)[OFFSET(0)] AS v FROM t GROUP BY t.x",
    "SELECT t.id FROM t WHERE (t.x, t.y) IN (SELECT t2.x, {MAX|MIN}(t2.y) FROM t AS t2 GROUP BY t2.x)",
    "SELECT t.id FROM t WHERE t.id > 1 AND (t.x, t.y) IN (SELECT t2.x, MAX(t2.y) FROM t AS t2 WHERE t2.id > 0 GROUP BY t2.x)",
    "SELECT t.id FROM t WHERE (t.x, t.y) NOT IN (SELECT t2.x, MAX(t2.y) FROM t AS t2 GROUP BY t2.x)",
]

KEYS = [
    None,  # the generator's own: id only
    {"keys": [["id"], ["x", "y"]], "not_null": ["id", "x", "y"]},
    {"keys": [["id"], ["x", "y"]], "not_null": ["id", "x"]},
    {"keys": [["id"], ["y"]], "not_null": ["id", "y"]},
    {"keys": [["id"]], "not_null": ["id", "x", "y"]},
]


def cases(seed: int, count: int) -> list[dict]:
    made = expand(TEMPLATES, seed, count, "latest_rows")
    rng = random.Random(seed)
    for case in made:
        chosen = rng.choice(KEYS)
        if chosen is not None:
            case["constraints"] = {**case["constraints"], "t": chosen}
    return made
