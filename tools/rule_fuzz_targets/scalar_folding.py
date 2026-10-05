"""Scalar rules: literal integer division, date folding/ranges, safe cast elimination and rounding defaults."""

from ._base import expand


TEMPLATES = [
    "SELECT DIV(10, 3) AS quotient",
    "SELECT DIV(-10, 3) AS quotient",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(MONTH FROM t.d) = 2",
    "SELECT t.id FROM t WHERE EXTRACT(DAY FROM t.d) = 1",
    "SELECT DATE_ADD(DATE '2024-01-01', INTERVAL 30 DAY) AS shifted",
    "SELECT CAST(t.x AS NUMERIC) AS widened FROM t",
    "SELECT ROUND(t.f) AS rounded FROM t",
    "SELECT CAST(t.x AS INT64) AS unchanged FROM t WHERE CAST(t.y AS INT64) = 1",
    "SELECT CAST(t.f AS INT64) AS narrowed FROM t",
    "SELECT DIV(10, 0) AS quotient",
    "SELECT DATE_ADD(DATE '2024-01-01', INTERVAL t.x DAY) AS shifted FROM t",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "scalar_folding")
