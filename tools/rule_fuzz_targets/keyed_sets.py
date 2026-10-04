"""``lift_keyed_set_join``: lifting a derived ``DISTINCT`` through one keyed inner join whose projection is injective.

The rewrite drops the inner ``DISTINCT`` and adds an outer one, which preserves the row bag only when every output
column of the ``DISTINCT`` is an integer column of its own scope, the outer query projects all of them, and the joined
table's declared key is NOT NULL and pinned to them by an equality in ``ON`` or ``WHERE``. The near misses turn each
of those off in turn: a ``STRING`` output, a ``DISTINCT ON``, an outer join, a second join, an equality that does not
pin the key, and a ``WHERE`` that reads a column the join cannot see.
"""

from ._base import expand

TEMPLATES = [
    # -- the lift itself: every DISTINCT output is projected, and the keyed column is pinned to one of them
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id",
    f"SELECT t.id, d.id, d.k FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id",
    f"SELECT d.k, d.i2, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS i2 FROM u) AS d JOIN t ON t.id = d.i2",
    f"SELECT d.k, d.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id",
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u WHERE u.w > 0) AS d JOIN t ON t.id = d.id",
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id WHERE d.k > 1",
    # A CTE spelling is inlined before the rule sees it, and a redundant ORDER BY is trimmed before it too.
    f"WITH q AS (SELECT DISTINCT u.k AS k, u.w AS id FROM u) SELECT d.k, d.id, t.id FROM q AS d JOIN t ON t.id = d.id",
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id ORDER BY d.k",
    # The same shape with the keys on the other side: ``u`` keyed and ``t`` as the derived source.
    f"SELECT d.k, d.id, u.k FROM (SELECT DISTINCT t.y AS k, t.id AS id FROM t) AS d JOIN u ON u.k = d.k",
    f"SELECT d.id, d.k, u.k FROM (SELECT DISTINCT t.id AS k, t.y AS id FROM t) AS d JOIN u ON u.k = d.k",
    # -- near misses: a guard must decline each of these
    # A STRING output is compared by collation, so the projection is not injective on integers.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.v AS id FROM u) AS d JOIN t ON t.id = d.id",
    # DISTINCT ON picks one row per key; there is no DISTINCT to lift.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT ON (u.k) u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id",
    # An outer join null-extends, so a derived row can match more than one table row.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d LEFT JOIN t ON t.id = d.id",
    # The table's key must be pinned to a DISTINCT output.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.y = d.id",
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.k",
    # Exactly one join: a second one adds rows the DISTINCT does not cover.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u) AS d JOIN t ON t.id = d.id JOIN p ON p.tid = t.id",
    # A filter that reads a column outside the joined row is not scoped to the lifted shape.
    f"SELECT d.k, d.id, t.id FROM (SELECT DISTINCT u.k AS k, u.w AS id FROM u WHERE u.v = 'a') AS d JOIN t ON t.id = d.id",
]


def cases(seed: int, count: int) -> list[dict]:
    return expand(TEMPLATES, seed, count, "keyed_sets")