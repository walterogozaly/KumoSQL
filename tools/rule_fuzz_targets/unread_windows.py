"""Drop unread window columns, and decline when the window can affect a result or read."""

from ._base import expand


TEMPLATES = [
    # The outer query ignores the row-number value.
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (PARTITION BY t.y ORDER BY t.id) AS rn FROM t) AS d",
    # Aggregation over the derived rows does not consume the window column.
    "SELECT d.y, SUM(d.x) AS total FROM (SELECT t.x, t.y, RANK() OVER (PARTITION BY t.y ORDER BY t.x) AS r FROM t) AS d GROUP BY d.y",
    # COUNT(*) reads the derived relation's rows, not its window value.
    "SELECT COUNT(*) AS n FROM (SELECT t.x, LAG(t.x) OVER (ORDER BY t.id) AS previous FROM t) AS d",
    # Multiple unreferenced windows can be removed while ordinary columns remain.
    "SELECT d.x FROM (SELECT t.x, SUM(t.x) OVER (PARTITION BY t.y) AS total, COUNT(*) OVER () AS n FROM t) AS d WHERE d.x IS NULL OR d.x >= 0",
    # The pass also reaches a nested derived table and leaves at least one column.
    "SELECT q.x FROM (SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d) AS q",
    # The selected window column is read from the outer query.
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d WHERE d.rn = 1",
    # QUALIFY reads the inner window before the outer query sees the derived table.
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t QUALIFY rn = 1) AS d",
    # Stars and row values expose every derived column.
    "SELECT * FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d",
    "SELECT TO_JSON_STRING(d) AS encoded FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d",
    # Ordering by the window column reads it even when it is absent from SELECT.
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d ORDER BY d.rn",
    # DISTINCT and stars in the inner projection change how the derived rows are formed.
    "SELECT d.x FROM (SELECT DISTINCT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d",
    "SELECT d.x FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY t.id) AS rn FROM t) AS d",
    # USING/NATURAL joins can read the derived table's column set implicitly.
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS k FROM t) AS d JOIN u USING (k)",
    "SELECT d.x FROM (SELECT t.x, ROW_NUMBER() OVER (ORDER BY t.id) AS k FROM t) AS d NATURAL JOIN u",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "unread_windows")
