"""ORDER BY and cut rewrites: unused and duplicate keys, top-k cuts, and lifting."""

from ._base import expand

TEMPLATES = [
    # _drop_unread_order: no cut observes the ordering.
    "SELECT t.x FROM t ORDER BY t.y",
    # _dedupe_order_keys: the first occurrence fixes the sort key.
    "SELECT t.x FROM t ORDER BY t.x, t.y DESC, t.x DESC LIMIT 2",
    # _merge_top_k: the outer cut composes with the inner cut on the same order.
    "SELECT d.x FROM (SELECT t.x, t.y FROM t ORDER BY t.x LIMIT 10 OFFSET 2) AS d ORDER BY d.x LIMIT 3 OFFSET 1",
    # _merge_top_k: each UNION ALL branch already keeps enough of its own order.
    "SELECT d.a FROM ((SELECT t.x AS a FROM t ORDER BY t.x LIMIT 4) UNION ALL (SELECT u.w AS a FROM u ORDER BY u.w LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1",
    # _lift_cut: compute the outer expression after taking the ordered inner cut.
    "SELECT d.value + 1 AS result FROM (SELECT t.x AS value, t.y FROM t ORDER BY t.y LIMIT 2) AS d",
    # A derived order is read by the outer LIMIT, so it cannot be dropped.
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.y) AS d LIMIT 2",
    # Distinct order expressions must both remain.
    "SELECT t.x FROM t ORDER BY t.x, t.x + 1 LIMIT 2",
    # An inner cut cannot be merged when the order leaves projected values tied.
    "SELECT d.b FROM (SELECT t.x AS a, t.y AS b FROM t ORDER BY t.x LIMIT 10) AS d ORDER BY d.a LIMIT 3",
    # Different directions do not describe one composable top-k order.
    "SELECT d.x FROM (SELECT t.x FROM t ORDER BY t.x DESC LIMIT 10) AS d ORDER BY d.x LIMIT 3",
    # A branch shorter than the outer LIMIT + OFFSET can drop a required row.
    "SELECT d.a FROM ((SELECT t.x AS a FROM t ORDER BY t.x LIMIT 3) UNION ALL (SELECT u.w AS a FROM u ORDER BY u.w LIMIT 3)) AS d ORDER BY d.a LIMIT 3 OFFSET 1",
    # DISTINCT means the inner cut is not a row-for-row projection to lift.
    "SELECT d.x FROM (SELECT DISTINCT t.x FROM t ORDER BY t.x LIMIT 2) AS d",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "limits")
