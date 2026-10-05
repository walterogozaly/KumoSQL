"""Aggregate/count rules: empty counts, grouped counts, singleton joins and tuple counts after regrouping."""

from ._base import expand


TEMPLATES = [
    # COUNT(NULL) is zero beside another aggregate, while the global row remains.
    "SELECT COUNT(NULL) AS zero_count, COUNT(*) AS row_count FROM t",
    # A plain grouped COUNT(*) has at least one row in every group.
    "SELECT CASE WHEN g.c = 0 THEN 'empty' ELSE 'present' END AS state FROM (SELECT t.y AS k, COUNT(*) AS c FROM t GROUP BY t.y) AS g",
    # A nullable counted column can be all NULL in a nonempty group.
    "SELECT CASE WHEN g.c = 0 THEN 'empty' ELSE 'present' END AS state FROM (SELECT t.y AS k, COUNT(t.x) AS c FROM t GROUP BY t.y) AS g",
    # Summing per-key counts becomes a guarded count over the original rows.
    "SELECT SUM(g.c) AS row_count FROM (SELECT t.y AS k, COUNT(*) AS c FROM t WHERE t.x > 0 GROUP BY t.y) AS g",
    # Per-group distinct counts do not add to a global distinct count.
    "SELECT SUM(g.c) AS row_count FROM (SELECT t.y AS k, COUNT(DISTINCT t.x) AS c FROM t GROUP BY t.y) AS g",
    # The equality fixes the left primary key, so the grouped right side has at most one match.
    "SELECT a.k, a.n * b.c AS weighted FROM (SELECT t.y AS k, t.x AS n FROM t WHERE t.id = 1) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS b ON a.k = b.k",
    # A range does not fix a unique key to one value.
    "SELECT a.k, a.n * b.c AS weighted FROM (SELECT t.y AS k, t.x AS n FROM t WHERE t.id > 1) AS a JOIN (SELECT u.k AS k, COUNT(*) AS c FROM u GROUP BY u.k) AS b ON a.k = b.k",
    # Two extra grouping keys make tuple COUNT a multi-column DISTINCT count.
    "SELECT g.y, COUNT(g.x, g.id) AS tuples, SUM(g.s) AS total FROM (SELECT t.y AS y, t.x AS x, t.id AS id, SUM(t.x) AS s FROM t GROUP BY t.y, t.x, t.id) AS g GROUP BY g.y",
    # Counting only one of two extra grouping keys does not count distinct tuples.
    "SELECT g.y, COUNT(g.x) AS tuples FROM (SELECT t.y AS y, t.x AS x, t.id AS id FROM t GROUP BY t.y, t.x, t.id) AS g GROUP BY g.y",
    # A lone global COUNT(NULL) still returns one row and must not be folded to zero rows.
    "SELECT COUNT(NULL) AS zero_count FROM t",
]


def cases(seed: int, count: int) -> list[dict]:
    generated = expand(TEMPLATES, seed, count, "aggregate_counts")
    for case in generated:
        template = int(case["source"].split(":")[2])
        constraints = case["constraints"]
        constraints.setdefault("t", {}).setdefault("not_null", []).append("id")
        constraints.setdefault("t", {}).setdefault("keys", []).append(["id"])
        if template == 2:
            # Keep x nullable even on seeds where the base schema marks it NOT NULL.
            constraints["t"]["not_null"] = [c for c in constraints["t"].get("not_null", []) if c.lower() != "x"]
        for table in ("t", "u", "p"):
            rules = constraints.setdefault(table, {})
            rules["not_null"] = sorted(set(rules.get("not_null", [])))
            rules["keys"] = [list(key) for key in dict.fromkeys(tuple(key) for key in rules.get("keys", []))]
    return generated
