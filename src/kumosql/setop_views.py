"""Candidate replacements over a model that is a set operation.

``model_reuse`` reads a model as one select-project-join block, so a model that is
``A UNION ALL B``, ``A INTERSECT ALL B`` or ``A EXCEPT ALL B`` is opaque to it. This module reads each
branch of the model as a block of its own and each branch of the query (or the query, when it is one
select) the same way, and proposes ``SELECT <query outputs> FROM model WHERE <the query branch's whole
filter>`` for every pairing of a query branch with a model branch. The model branch's own filter is
not assumed to hold: a view that is the union of two ranges is only a filter of one range once the
prover has seen the other one drop out. Pairing every query branch with every model branch finds the
branch order an INTERSECT ALL view was written in even when the query lists them the other way.

A query that removes duplicates (``UNION``, ``SELECT DISTINCT``) is also proposed with ``DISTINCT``
over the model, which is how ``UNION`` is read from a ``UNION ALL`` view.

The proposer only proposes: every candidate goes through the prover like any other, and for
INTERSECT ALL and EXCEPT ALL the proof is :mod:`kumosql.setop_congruence`.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Mapping

from sqlglot import exp

_MAX_CANDIDATES = 8


def _leaves(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, (exp.Paren, exp.Subquery)) and not (isinstance(node, exp.Subquery) and node.alias):
        node = node.this
    if isinstance(node, exp.SetOperation):
        return _leaves(node.this) + _leaves(node.expression)
    return [node]


def _removes_duplicates(query: exp.Expression) -> bool:
    while isinstance(query, (exp.Paren, exp.Subquery)):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return bool(query.args.get("distinct"))
    return isinstance(query, exp.Select) and bool(query.args.get("distinct"))


def candidates(query_tree: exp.Expression, model_tree: exp.Expression, names: list[str], model_name: str, not_null: Mapping[str, set[str]] | None = None) -> list:
    """Replacements of ``query_tree`` (prepared) over a set-operation model, none of them trusted."""

    from .model_reuse import _block, _candidates, _Candidate, _Unsupported

    def blocks(leaves, nulls=None):
        found = []
        for leaf in leaves:
            try:
                found.append(_block(leaf, nulls))
            except _Unsupported:
                found.append(None)
        return found

    from .setop_congruence import _expose

    exposed, _ = _expose(model_tree)  # a select that only lists the columns of a derived set operation reads as it
    if not isinstance(exposed, exp.SetOperation):
        return []
    model_blocks = blocks(_leaves(exposed))
    query_blocks = blocks(_leaves(query_tree), not_null)
    pairs = [(q, m) for q, m in itertools.product(range(len(query_blocks)), range(len(model_blocks)))]
    # the same position first, then the rest
    pairs.sort(key=lambda p: (p[0] != p[1], p))
    seen: set[str] = set()
    found: list = []
    distinct = _removes_duplicates(query_tree)
    for q, m in pairs:
        query, model = query_blocks[q], model_blocks[m]
        if query is None or model is None or model.is_aggregate or len(model.outputs) != len(names):
            continue
        pseudo = dataclasses.replace(model, conjuncts=[], order=[])
        for candidate in _candidates(query, pseudo, names, model_name):
            if not candidate.strategy.startswith("spj"):
                continue
            variants = [candidate.sql]
            if distinct and not query.distinct:
                variants.append(_with_distinct(candidate.sql))
            for sql in variants:
                if sql and sql not in seen:
                    seen.add(sql)
                    found.append(_Candidate(sql, "setop-" + candidate.strategy))
        if len(found) >= _MAX_CANDIDATES:
            break
    return found[:_MAX_CANDIDATES]


def _with_distinct(sql: str) -> str | None:
    import sqlglot

    tree = sqlglot.parse_one(sql, read="postgres")
    if not isinstance(tree, exp.Select) or tree.args.get("distinct"):
        return None
    tree.set("distinct", exp.Distinct())
    return tree.sql(dialect="postgres")
