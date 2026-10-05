"""Array and struct source columns for the incremental simulator.

A source column the scan finds read through ``UNNEST(t.col) AS item`` with ``item.field`` references is an
``ARRAY<STRUCT<field INT64, ...>>`` (or an ``ARRAY<INT64>`` when the element is read whole). The simulator needs
three things for it, kept here so :mod:`kumosql.incremental` only calls them: the DuckDB column type, random
values for the change generator, and a literal for each value.

Only one level is supported: an array of scalars or of a struct of scalars. Anything else is not parsed here
and the column keeps the type string it was given.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import Any

_ARRAY = re.compile(r"\s*ARRAY\s*<(.*)>\s*$", re.IGNORECASE | re.DOTALL)
_STRUCT = re.compile(r"\s*STRUCT\s*<(.*)>\s*$", re.IGNORECASE | re.DOTALL)


def _fields(text: str) -> list[tuple[str, str]] | None:
    fields = []
    for part in text.split(","):
        pieces = part.split()
        if len(pieces) != 2:
            return None
        fields.append((pieces[0], pieces[1]))
    return fields


def parse_array(sql_type: str) -> tuple[list[tuple[str, str]], str] | tuple[None, str] | None:
    """``(fields, "")`` for ``ARRAY<STRUCT<a T, ...>>``, ``(None, element type)`` for ``ARRAY<T>``, else None."""

    found = _ARRAY.match(sql_type)
    if found is None:
        return None
    inner = found.group(1).strip()
    struct = _STRUCT.match(inner)
    if struct is not None:
        fields = _fields(struct.group(1))
        return (fields, "") if fields else None
    return (None, inner) if re.fullmatch(r"\w+", inner) else None


def duck_array_type(sql_type: str, scalar: dict[str, str]) -> str | None:
    """The DuckDB type of an array column (``scalar`` maps BigQuery scalar types to DuckDB ones), or None."""

    parsed = parse_array(sql_type)
    if parsed is None:
        return None
    fields, element = parsed
    if fields is not None:
        return "STRUCT(" + ", ".join(f'"{n}" {scalar.get(t.upper(), t)}' for n, t in fields) + ")[]"
    return f"{scalar.get(element.upper(), element)}[]"


def random_array(rng: random.Random, sql_type: str, scalar_value: Callable[[random.Random, str], Any]) -> Any:
    """A random list for an array column: up to three elements, each drawn like a scalar of its type."""

    parsed = parse_array(sql_type)
    if parsed is None:
        raise ValueError(sql_type)
    fields, element = parsed
    size = rng.randint(0, 3)
    if fields is not None:
        return [{n: scalar_value(rng, t) for n, t in fields} for _ in range(size)]
    return [scalar_value(rng, element) for _ in range(size)]
