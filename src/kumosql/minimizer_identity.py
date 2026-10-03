"""Names and statements for the table minimizer: what a table is called, and whether its text is one query.

Three rules keep a proof about the input's own tables (``docs/table-minimization.md``):

* **A script is not a query.** Text with more than one statement is never read as its first SELECT
  (:func:`single_query`); the minimizer keeps it as written and keeps every table it mentions.
* **Identity keeps its case.** BigQuery table and dataset names are case-sensitive by default, so
  ``p.D.stage`` and ``p.d.stage`` are two tables. Names that differ only by case are *ambiguous*
  (:func:`ambiguous`): a dataset set to case-insensitive names would make them one table, so the
  minimizer leaves such tables, and the tables that read them, exactly as written.
* **Internal names cannot collide with real ones.** Bare and two-part names live under a catalog that
  appears nowhere in the input (:func:`fresh_catalogs`), so a real table can never take their place.
"""

from __future__ import annotations

from typing import Iterable

import sqlglot
from sqlglot import exp

#: Dataset of the internal names that bare names get.
BARE_DATASET = "tables"


def single_query(sql: str, dialect: str) -> exp.Query | None:
    """The one query ``sql`` consists of, or ``None`` for a script, a non-query statement or unparseable text.

    ``sqlglot.parse_one`` keeps only the first statement on some releases and raises on others, so the
    statements are counted here.
    """

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.SqlglotError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


def fresh_catalogs(texts: Iterable[str]) -> tuple[str, str]:
    """Two catalog names that occur in none of ``texts``: one for bare names, one for two-part names.

    Different catalogs keep a bare ``t`` and a two-part ``tables.t`` apart, and a name found nowhere in the
    input cannot be the name of a real table.
    """

    haystack = "\n".join(texts).lower()
    found: list[str] = []
    index = 0
    while len(found) < 2:
        name = "kumo_min" if index == 0 else f"kumo_min{index}"
        if name not in haystack:
            found.append(name)
        index += 1
    return found[0], found[1]


def internal_name(parts: tuple[str, ...], catalogs: tuple[str, str]) -> tuple[str, str, str]:
    """The three-part name the minimizer uses for a table the input spelled with ``parts``."""

    if len(parts) == 1:
        return (catalogs[0], BARE_DATASET, parts[0])
    if len(parts) == 2:
        return (catalogs[1], *parts)
    return parts  # type: ignore[return-value]


def ambiguous(names: Iterable[tuple[str, ...]]) -> set[tuple[str, ...]]:
    """The names (tuples of parts) among ``names`` that equal another one when case is ignored."""

    groups: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for parts in names:
        groups.setdefault(tuple(p.lower() for p in parts), set()).add(parts)
    return {parts for group in groups.values() if len(group) > 1 for parts in group}
