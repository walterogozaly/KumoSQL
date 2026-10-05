"""Key-aware proposals for model reuse: joins that declared constraints make lossless.

A join that neither drops nor repeats rows can be ignored while a query is matched to a model, and
the prover's own key rules then confirm the answer. Three shapes are read, all from declared
constraints only:

* an inner join of a child to its parent through a declared foreign key, when every child column
  of the key is NOT NULL, the joined parent columns contain a unique key and the join is the only
  thing the block says about the parent (``emps e JOIN depts d ON e.deptno = d.deptno``): every
  child row finds exactly one parent row;
* a ``LEFT JOIN`` whose condition fixes a unique key of the null-supplying table, when nothing else
  reads that table: every row of the preserved side has at most one partner, and keeps its row either way;
* an aggregate model joined to extra tables of the query on the model's grouping columns: each
  model row stands for a group of rows that all meet the same partners, so a ``SUM`` over a partner's
  column is weighted by the model's ``COUNT(*)``.

This module only proposes. The prover still has to prove each replacement against the original query
and model, and the replacement is re-run on random databases that respect the same constraints.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterator, Mapping

from sqlglot import exp

from .fk_rules import _same_table
from .smt_equivalence import TableConstraints


class _Facts:
    """Declared constraints by lower-cased table name."""

    def __init__(self, constraints: Mapping[str, TableConstraints] | None):
        self.not_null: dict[str, set[str]] = {}
        self.keys: dict[str, list[frozenset[str]]] = {}
        self.foreign_keys: dict[str, list[tuple[dict[str, str], str]]] = {}
        for table, c in (constraints or {}).items():
            name = table.lower().split(".")[-1]
            keys = [frozenset(col.lower() for col in key) for key in c.keys if key]
            self.keys[name] = keys
            self.not_null[name] = {col.lower() for col in c.not_null} | {col for key in keys for col in key}
            self.foreign_keys[name] = [
                ({col.lower(): parent.lower() for col, parent in zip(cols, parent_cols)}, parent_table.lower())
                for cols, parent_table, parent_cols in c.foreign_keys
            ]

    @property
    def has_keys(self) -> bool:
        return any(self.keys.values())

    @property
    def has_foreign_keys(self) -> bool:
        return any(self.foreign_keys.values())


def _tables_of(node: exp.Expression) -> set[str]:
    return {c.table for c in node.find_all(exp.Column)}


def _reads(alias: str, nodes) -> bool:
    return any(alias in _tables_of(node) for node in nodes if node is not None)


def _column_pair(conjunct: exp.Expression) -> tuple[exp.Column, exp.Column] | None:
    if isinstance(conjunct, exp.Paren):
        conjunct = conjunct.this
    if not isinstance(conjunct, exp.EQ):
        return None
    left, right = conjunct.this, conjunct.expression
    if isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table and right.table:
        return left, right
    return None


def _parent_join(alias: str, table: str, tables: dict[str, str], conjuncts: list[exp.Expression], facts: _Facts):
    """``(conjuncts, filters)`` when the conjuncts join ``alias`` to its child through a declared foreign key
    onto a unique key of ``alias``, else None.

    The join keeps exactly the child rows whose key columns are not NULL (each has one parent), so a nullable
    key column costs nothing but a ``col IS NOT NULL`` filter, returned in ``filters``."""

    mentioning = [c for c in conjuncts if alias in _tables_of(c)]
    if not mentioning:
        return None
    pairs: dict[str, str] = {}  # child column -> parent column
    child: str | None = None
    for conjunct in mentioning:
        found = _column_pair(conjunct)
        if found is None:
            return None
        mine = [c for c in found if c.table == alias]
        other = [c for c in found if c.table != alias]
        if len(mine) != 1 or len(other) != 1 or other[0].table not in tables or child not in (None, other[0].table):
            return None
        child = other[0].table
        if pairs.setdefault(other[0].name, mine[0].name) != mine[0].name:
            return None
    child_table = tables[child or ""]
    if not any(key <= set(pairs.values()) for key in facts.keys.get(table, [])):
        return None
    if not any(mapping == pairs and _same_table(table, parent) for mapping, parent in facts.foreign_keys.get(child_table, [])):
        return None
    nullable = sorted(set(pairs) - facts.not_null.get(child_table, set()))
    return mentioning, [exp.Not(this=exp.Is(this=exp.column(name, table=child), expression=exp.Null())) for name in nullable]


def _has_unqualified(block) -> bool:
    nodes = [*block.conjuncts, *block.outputs, *block.group, *block.order] + ([block.having] if block.having is not None else [])
    return any(not c.table for node in nodes for c in node.find_all(exp.Column))


def _rename_alias(node: exp.Expression, old: str, new: str) -> exp.Expression:
    def visit(n):
        if isinstance(n, exp.Column) and n.table == old:
            return exp.column(n.name, table=new)
        return n

    return node.copy().transform(visit)


def _drop_parents(block, facts: _Facts, *, keep_reads: bool, only: set[str]):
    """``block`` without its lossless parent joins (None when there is none).

    With ``keep_reads`` (a model), a dropped table may still be read by the model's outputs; its alias is
    renamed so nothing in a query can be matched to it by accident, and its join conditions stay so
    that equalities of the model's rows are still known. Otherwise (a query) the table must not be read.
    Only tables named in ``only`` are dropped."""

    if not facts.has_foreign_keys or _has_unqualified(block):
        return None
    tables = {alias: table for alias, table in block.tables}
    live = list(block.conjuncts)
    absorbed: list[tuple[str, list[exp.Expression]]] = []
    filters: list[exp.Expression] = []
    roots = [*block.outputs, *block.group, *block.order] + ([block.having] if block.having is not None else [])
    changed = True
    while changed:
        changed = False
        for alias, table in list(tables.items()):
            if sum(1 for a, _ in block.tables if a == alias) != 1:
                continue
            if table not in only or (not keep_reads and _reads(alias, roots)):
                continue
            found = _parent_join(alias, table, tables, live, facts)
            if found is None:
                continue
            joined, not_null = found
            del tables[alias]
            live = [c for c in live if not any(c is j for j in joined)]
            filters += not_null
            absorbed.append((alias, joined))
            changed = True
            break
    if not absorbed:
        return None
    remaining = [(a, t) for a, t in block.tables if a in tables]
    if not keep_reads:
        return replace(block, tables=remaining, conjuncts=live + filters)
    conjuncts = live + filters
    for alias, joined in absorbed:
        conjuncts += joined
    outputs, group, having = list(block.outputs), list(block.group), block.having
    for alias, _ in absorbed:
        new = f"__lossless_{alias}"
        conjuncts = [_rename_alias(c, alias, new) for c in conjuncts]
        outputs = [_rename_alias(o, alias, new) for o in outputs]
        group = [_rename_alias(g, alias, new) for g in group]
        having = _rename_alias(having, alias, new) if having is not None else None
    return replace(block, tables=remaining, conjuncts=conjuncts, outputs=outputs, group=group, having=having)


def lossless_variants(query, model, constraints: Mapping[str, TableConstraints] | None) -> Iterator[tuple]:
    """``(query, model)`` pairs, other than the given one, with lossless parent joins taken out of either side."""

    facts = _Facts(constraints)
    if not facts.has_foreign_keys:
        return
    in_query, in_model = {t for _, t in query.tables}, {t for _, t in model.tables}
    # a table the other side also reads is matched to it, not dropped
    smaller_query = _drop_parents(query, facts, keep_reads=False, only=in_query - in_model)
    smaller_model = _drop_parents(model, facts, keep_reads=True, only=in_model - in_query)
    if smaller_query is not None:
        yield smaller_query, model
    if smaller_model is not None:
        yield query, smaller_model
    if smaller_query is not None and smaller_model is not None:
        yield smaller_query, smaller_model


# ---------------------------------------------------------------------------------------------
# a LEFT JOIN onto a unique key that nothing reads


def drop_unread_left_joins(tree: exp.Expression, constraints: Mapping[str, TableConstraints] | None) -> exp.Expression:
    """``tree`` with each ``LEFT JOIN`` removed whose null-supplying table has its unique key fixed by the
    join condition and is read nowhere else; the tree itself when there is none."""

    facts = _Facts(constraints)
    if not facts.has_keys or not isinstance(tree, exp.Select):
        return tree
    if not any((j.args.get("side") or "").upper() == "LEFT" for j in tree.args.get("joins") or []):
        return tree
    work = tree.copy()
    if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in work.find_all(exp.Star)):
        return tree
    dropped = False
    changed = True
    while changed:
        changed = False
        for join in list(work.args.get("joins") or []):
            if _unread_keyed_left_join(work, join, facts):
                join.pop()
                dropped = changed = True
                break
    if not dropped:
        return tree
    if not work.args.get("joins"):
        work.set("joins", None)
    return work


def _unread_keyed_left_join(select: exp.Select, join: exp.Join, facts: _Facts) -> bool:
    table = join.this
    if (join.args.get("side") or "").upper() != "LEFT" or (join.args.get("kind") or "").upper() not in ("", "OUTER") or join.args.get("using"):
        return False
    if not isinstance(table, exp.Table) or isinstance(table.this, exp.Func) or join.args.get("on") is None:
        return False
    alias = table.alias_or_name
    fixed: set[str] = set()
    for conjunct in _split_and(join.args["on"]):
        if isinstance(conjunct, exp.Paren):
            conjunct = conjunct.this
        if not isinstance(conjunct, exp.EQ):
            continue
        for mine, other in ((conjunct.this, conjunct.expression), (conjunct.expression, conjunct.this)):
            if isinstance(mine, exp.Column) and mine.table == alias and alias not in _tables_of(other):
                fixed.add(mine.name)
    if not any(key <= fixed for key in facts.keys.get(table.name, [])):
        return False
    on = join.args["on"]
    return not any(c.table == alias and not _inside(c, on) for c in select.find_all(exp.Column)) and not any(
        not c.table for c in select.find_all(exp.Column)
    )


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _split_and(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


# ---------------------------------------------------------------------------------------------
# aggregates read over a partner's column


def weighted_aggregate(agg: exp.AggFunc, rewriter, count_star: exp.Expression | None) -> exp.Expression | None:
    """``agg`` of a query that joins the model to extra tables, rebuilt from the model's rows.

    Each model row is a group whose rows all meet the same partners (the join reads only the model's
    grouping columns), so an expression over grouping columns and partner columns is one value per
    (model row, partner row) pair and stands for ``COUNT(*)`` rows: ``SUM(x)`` is ``SUM(x * n)`` and
    ``MIN`` and ``MAX`` are unchanged. ``COUNT(x)`` and ``AVG(x)`` are left alone: the prover does not
    yet prove their weighted forms. None when the argument cannot be read that way."""

    if agg.this is None or isinstance(agg.this, (exp.Distinct, exp.Star)) or not isinstance(agg, (exp.Sum, exp.Min, exp.Max)):
        return None
    argument = rewriter.rewrite(agg.this)
    if argument is None:
        return None
    if not isinstance(agg, exp.Sum):
        return type(agg)(this=argument)
    if count_star is None:
        return None
    operand = exp.Paren(this=argument) if isinstance(argument, (exp.Binary, exp.Unary)) else argument
    return exp.Sum(this=exp.Mul(this=operand, expression=count_star.copy()))
