"""Keyed grouping, keyed DISTINCT and EXISTS over aggregate variants and their guard near misses."""

from ._base import expand
from ._variants import fire_cases


TEMPLATES = [
    # A NOT NULL key makes every group one row; fixed keys make non-key grouping columns one-valued too.
    "SELECT t.id, SUM(t.x) AS total, COUNT(*) AS n FROM t GROUP BY t.id",
    "SELECT t.y, MAX(t.x) AS largest FROM t WHERE t.id = 1 GROUP BY t.y",
    # Near misses for remove_keyed_grouping: no key in the group, or an extended grouping.
    "SELECT t.y, COUNT(*) AS n FROM t GROUP BY t.y",
    "SELECT t.id, COUNT(*) AS n FROM t GROUP BY ROLLUP(t.id)",
    # DISTINCT disappears only if the output (or fixed predicates) contains a declared non-NULL key.
    "SELECT DISTINCT t.id, t.x FROM t",
    "SELECT DISTINCT t.y FROM t",
    "SELECT DISTINCT t.id, t.x FROM t GROUP BY t.id",
    "SELECT DISTINCT t.id FROM t",
    # Aggregate-only SELECTs without row-eliminating clauses always return one row.
    "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT SUM(u.w), MAX(u.k) FROM u)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u GROUP BY u.k)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u HAVING COUNT(*) > 0)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u LIMIT 0)",
    "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) OVER () FROM u)",
]

FIRES = [
    ("remove_keyed_grouping", "SELECT t.id, SUM(t.x) AS total, COUNT(*) AS n FROM t GROUP BY t.id"),
    ("remove_keyed_grouping", "SELECT t.y, MAX(t.x) AS largest FROM t WHERE t.id = 1 GROUP BY t.y"),
    ("drop_keyed_distinct", "SELECT DISTINCT t.id, t.x FROM t"),
    ("exists_over_aggregate", "SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u)"),
    ("exists_over_aggregate", "SELECT t.id FROM t WHERE EXISTS (SELECT SUM(u.w), MAX(u.k) FROM u)"),
]


def cases(seed: int, count: int) -> list[dict]:
    generated = expand(TEMPLATES, seed, count, "keyed_variants")
    # A UNIQUE key may admit NULLs. This case checks that keyed DISTINCT still requires a NOT NULL declaration.
    for case in generated:
        if case["source"].rsplit(":", 2)[-2] == "7":
            case["constraints"].get("t", {}).get("not_null", []).remove("id")
    return generated


def fire_list() -> list[tuple[str, dict]]:
    return fire_cases("keyed_variants", FIRES)