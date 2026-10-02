"""Duplicate-SELECT detection across a pipeline."""

from __future__ import annotations

from collections import defaultdict
import hashlib

from sqlglot import exp

from .canonical import canonical_copy, scope_key
from .pipeline_types import DuplicateGroup, DuplicateOccurrence

def _select_location(select: exp.Expression) -> str:
    parent = select.parent
    if isinstance(parent, exp.CTE):
        return f"cte:{parent.alias_or_name}"
    if isinstance(parent, exp.Subquery):
        return f"subquery:{parent.alias_or_name or '<anonymous>'}"
    if parent is None:
        return "query"
    return f"nested:{type(parent).__name__.lower()}"


def _fingerprint(select: exp.Expression) -> tuple[str, str]:
    """A hash of the select's canonical text (aliases, conjunct order and the like ignored) and its own text."""

    def text(node: exp.Expression) -> str:
        return node.sql(dialect="bigquery", normalize=True, normalize_functions="upper", comments=False)

    sql = text(select)
    try:
        canonical = text(canonical_copy(select))
        canonical += scope_key(select, text)
    except Exception:  # a shape the canonicalizer cannot follow keeps its literal text
        canonical = sql
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16], sql


def _find_duplicates(parsed: dict[str, exp.Expression], *, min_nodes: int) -> list[DuplicateGroup]:
    groups: dict[str, list[tuple[str, exp.Expression]]] = defaultdict(list)
    sql_of: dict[str, str] = {}
    size_of: dict[str, int] = {}
    fingerprint_of: dict[int, str] = {}

    for key, query in parsed.items():
        for select in query.find_all(exp.Select):
            size = sum(1 for _ in select.walk())
            if size < min_nodes:
                continue
            fingerprint, sql = _fingerprint(select)
            groups[fingerprint].append((key, select))
            sql_of[fingerprint] = sql
            size_of[fingerprint] = size
            fingerprint_of[id(select)] = fingerprint

    duplicated = {fp for fp, members in groups.items() if len(members) > 1}

    def enclosing_duplicates(select: exp.Expression) -> set[str]:
        found: set[str] = set()
        parent = select.parent
        while parent is not None:
            if isinstance(parent, exp.Select) and fingerprint_of.get(id(parent)) in duplicated:
                found.add(fingerprint_of[id(parent)])
            parent = parent.parent
        return found

    result = []
    for fingerprint in duplicated:
        members = groups[fingerprint]
        # Reported through one larger duplicate that holds every copy. Copies nested in different
        # larger duplicates are what links those duplicates to each other, so the group stays.
        enclosing = [enclosing_duplicates(select) for _, select in members]
        if set.intersection(*enclosing):
            continue
        result.append(
            DuplicateGroup(
                fingerprint=fingerprint,
                node_count=size_of[fingerprint],
                sql=sql_of[fingerprint],
                occurrences=tuple(
                    DuplicateOccurrence(key, _select_location(select)) for key, select in members
                ),
            )
        )
    return sorted(result, key=lambda group: (-group.node_count, group.fingerprint))
