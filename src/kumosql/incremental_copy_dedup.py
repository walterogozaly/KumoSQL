"""A key de-duplication of a source drops only copies, so it keeps every column stable.

:mod:`kumosql.incremental_monotone` reads ``QUALIFY ROW_NUMBER() OVER (PARTITION BY p ...) = 1`` as keeping
one arbitrary row per partition: stable on ``p`` and on what ``p`` determines, nothing else. That is right
for a partition on a non-key column and too weak for the de-duplication of a source on its own key.

**Claim.** Let ``T`` be a source with a declared key ``K`` that is never NULL, and let a ``SELECT ... FROM T
[WHERE c] QUALIFY ROW_NUMBER() OVER (PARTITION BY P ...) = 1`` have ``K ⊆ P``. In every state a contract
without ``null_key`` reaches, two rows of ``T`` that agree on ``K`` are identical (the key is unique, and
the only way a key repeats is ``duplicate``, an exact re-delivery; ``update`` and ``update_touch`` change
every copy together). Rows that agree on ``P`` agree on ``K``, so each partition holds identical copies,
whichever one ``ROW_NUMBER`` numbers first. The result is then the set of rows of ``T`` that pass ``c``,
each once, and each of those rows is kept, never replaced by a different one, as ``T`` gains rows.
So the select is stable wherever the same select without the ``QUALIFY`` is (and only gains rows if it
does), and any value computed from its columns stays stable.

A ``null_key`` change breaks the claim (all NULL keys fall in one partition) and so does a table that
is a CTE, a derived table, a join or the model's own table: those are not read here.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlglot import exp


def is_key_copy_dedup(
    select: exp.Select,
    sources: dict[str, SourceTable],  # noqa: F821 - keys are lower case
    states: dict[str, tuple[str, frozenset[str] | None]],
    kinds: Iterable[str],
    ctes: Iterable[str],
    target: str,
) -> bool:
    """Whether ``select`` is the de-duplication described in the module docstring."""

    from .incremental_merge import _is_one, _row_number

    qualify = select.args.get("qualify")
    source = select.args.get("from_") or select.args.get("from")
    if qualify is None or source is None or select.args.get("joins") or not isinstance(source.this, exp.Table):
        return False
    table = source.this
    name = table.name.lower()
    if table.args.get("db") or name in {c.lower() for c in ctes} or name == target.lower() or name not in sources:
        return False
    if "null_key" in set(kinds) or states.get(name, ("changing",))[0] not in ("frozen", "growing", "keyed"):
        return False
    declared = {k.lower() for k in sources[name].key}
    if not declared:
        return False
    condition = qualify.this.unnest() if isinstance(qualify.this, exp.Paren) else qualify.this
    if not isinstance(condition, exp.EQ):
        return False
    side, one = condition.left, condition.right
    if _is_one(side):
        side, one = one, side
    if not _is_one(one):
        return False
    if isinstance(side, exp.Column) and not side.table:
        side = next((e.unalias() for e in select.expressions if e.alias_or_name.lower() == side.name.lower()), side)
    window = _row_number(side)
    if window is None:
        return False
    partition = window.args.get("partition_by") or []
    alias = (table.alias or table.name).lower()
    if not all(isinstance(p, exp.Column) and (not p.table or p.table.lower() == alias) for p in partition):
        return False
    return declared <= {p.name.lower() for p in partition}
