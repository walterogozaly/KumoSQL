"""Whether a window's ``ORDER BY`` is a total order, from the declared keys.

A window's ``ORDER BY`` is total when no two rows of one partition can tie on it. ``covers_a_key`` says so for a
select that reads one plain table: the ``PARTITION BY`` and ``ORDER BY`` expressions are plain columns of that table and include every
column of one declared key, and every column of that key is declared NOT NULL. A key is unique across the whole
table, so no two rows of any partition (or of the rows left by ``WHERE``) agree on it. A key column that may be
NULL is left out (two NULL keys may tie), and so is a column that is only part of a key (another column of the
key tells the rows apart, the column alone does not). A join, derived table, ``LATERAL`` or table function can repeat
or invent rows, so there the answer is no.
"""

from __future__ import annotations

from sqlglot import exp


def covers_a_key(
    select: exp.Select,
    partition: list[exp.Expression] | None,
    order: list[exp.Expression],
    keys: dict[str, list[tuple[str, ...]]] | None,
    not_null: dict[str, frozenset[str]] | None,
) -> bool:
    """Whether the window's ``PARTITION BY`` and ``ORDER BY`` plain columns hold all columns of a declared NOT NULL key.

    The partition columns count: they are equal on every row of one partition, so a key made of them and of the
    order columns is still unique within it.
    """

    source = select.args.get("from_") or select.args.get("from")
    if source is None or select.args.get("joins") or select.args.get("laterals") or not isinstance(source.this, exp.Table):
        return False
    table = source.this
    if table.args.get("pivots") or table.args.get("joins"):
        return False
    name, alias = table.name.lower(), table.alias_or_name.lower()
    columns = set()
    for item in [*(partition or []), *order]:
        value = item.this if isinstance(item, exp.Ordered) else item
        if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star) and (not value.table or value.table.lower() in (alias, name)):
            columns.add(value.name.lower())
    declared = {c.lower() for c in (not_null or {}).get(name, frozenset())}
    return any(key and {c.lower() for c in key} <= columns and {c.lower() for c in key} <= declared for key in (keys or {}).get(name, []))
