"""Prefix-LIKE subsumption and patterns that the rewrite must leave untouched."""

from ._base import expand

TEMPLATES = [
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' OR t.s LIKE 'ABC%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' AND t.s LIKE 'ABC%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB' OR t.s LIKE 'AB%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' AND t.s LIKE 'AB'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' OR t.s LIKE 'AB%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' OR t.s LIKE 'CD%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB_' OR t.s LIKE 'ABC%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'A%B%' OR t.s LIKE 'AB%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'A!_%' ESCAPE '!' OR t.s LIKE 'A%'",
    "SELECT t.id FROM t WHERE t.s ILIKE 'AB%' OR t.s ILIKE 'ABC%'",
    "SELECT t.id FROM t CROSS JOIN u WHERE t.s LIKE 'AB%' OR u.v LIKE 'ABC%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'AB%' OR t.s LIKE 'ABC%' OR t.s LIKE 'ABCD%'",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "like_patterns")
