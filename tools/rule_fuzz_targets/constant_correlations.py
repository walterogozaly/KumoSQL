"""``propagate_constant_correlations``: reading a correlated column pinned to an integer constant as that constant
inside a subquery, either because the outer ``WHERE`` compares it to an integer literal or because every row of a
derived table passed such a filter.

The pin has to survive to the subquery, so the templates cover both routes to it and then the ways it must not: a
``STRING`` column or literal (compared by collation, not exact equality), a float literal, a comparison inside the
subquery that is not correlated, a derived table a RIGHT join can null-extend, and a derived column whose value is an
aggregate rather than one row's.

The pinned column and the reference to it are the same column, and ``_base.expand`` cannot nest choice groups, so the
shapes are built once per correlated column rather than spelled out for each.
"""

from ._base import expand

# (correlated column, matching pin) pairs; the subquery reads the same column the pin fixes.
PINS = [("t.x", "t.x = 5"), ("t.y", "t.y = 200"), ("t.id", "t.id = 3")]

# The subquery shapes that read the correlated column, one per template so no group nests.
SUBQUERIES = [
    "EXISTS (SELECT 1 FROM u WHERE u.k = {col})",
    "EXISTS (SELECT 1 FROM u WHERE u.k = {col} AND u.w > 1)",
    "{col} IN (SELECT u.w FROM u WHERE u.k = {col})",
    "EXISTS (SELECT 1 FROM (SELECT u.k AS k FROM u WHERE u.k = {col}) AS s)",
]

TEMPLATES = [
    # -- the literal pins the outer column, and the subquery reads it
    *(f"SELECT t.id FROM t WHERE {pin} AND {shape.format(col=col)}" for col, pin in PINS for shape in SUBQUERIES),
    "SELECT t.id FROM t WHERE t.x = 5 AND NOT EXISTS (SELECT 1 FROM u WHERE u.w > t.x)",
    "SELECT t.id, (SELECT MAX(u.w) FROM u WHERE u.k = t.x) AS m FROM t WHERE t.x = 5",
    # -- the derived table's own filter pins its output, so the subquery sees a constant
    "SELECT d.id FROM (SELECT t.id AS id, t.x AS k FROM t WHERE t.x = 200) AS d WHERE EXISTS (SELECT 1 FROM u WHERE u.k = d.k)",
    "SELECT d.id FROM (SELECT t.id AS id, t.y AS k FROM t WHERE t.y = 7) AS d JOIN u ON u.k = d.k WHERE u.w > 0",
    # -- near misses: a guard must decline each of these
    # Only integer columns and integer literals are touched: a STRING pin compares by collation.
    "SELECT t.id FROM t WHERE t.s = 'a' AND EXISTS (SELECT 1 FROM u WHERE u.k = t.s)",
    "SELECT t.id FROM t WHERE t.x = 5 AND EXISTS (SELECT 1 FROM u WHERE u.v = t.x)",
    # A float literal is not an integer, so the equality is not the exact one the rule relies on.
    "SELECT t.id FROM t WHERE t.x = {5.5|5.0|0.5} AND EXISTS (SELECT 1 FROM u WHERE u.k = t.x)",
    # The subquery comparison is not correlated, so there is nothing to propagate.
    "SELECT t.id FROM t WHERE t.x = 5 AND EXISTS (SELECT 1 FROM u WHERE u.k = u.w)",
    # A derived table a RIGHT join null-extends can have rows that never passed the pin.
    "SELECT d.id FROM (SELECT t.id AS id, t.x AS k FROM t WHERE t.x = 200) AS d RIGHT JOIN u ON u.k = d.k",
    # The derived column is an aggregate, so its value is not the constant any single row held.
    "SELECT d.id FROM (SELECT t.y AS k, MIN(t.x) AS id FROM t GROUP BY t.y) AS d WHERE EXISTS (SELECT 1 FROM u WHERE u.k = d.id)",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "constant_correlations")