"""Parse a select-project-join query into a join graph.

The join-order and cardinality code works on this small structure rather than on
SQL text: relations (alias to table), one filter list per relation, equi-join edges
between relations, and any leftover predicates that mention several relations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


@dataclass(frozen=True)
class Edge:
    left: str
    left_col: str
    right: str
    right_col: str

    def other(self, alias: str) -> str:
        return self.right if alias == self.left else self.left

    def col(self, alias: str) -> str:
        return self.left_col if alias == self.left else self.right_col


@dataclass
class JoinQuery:
    sql: str
    tables: dict[str, str]                      # alias -> table (lower case)
    filters: dict[str, list[exp.Expression]]    # alias -> single-relation predicates
    edges: list[Edge]
    residual: list[tuple[frozenset[str], exp.Expression]] = field(default_factory=list)
    select: list[exp.Expression] = field(default_factory=list)
    tail: dict[str, exp.Expression] = field(default_factory=dict)  # group/order/limit

    @property
    def aliases(self) -> list[str]:
        return list(self.tables)

    def neighbours(self, alias: str) -> set[str]:
        return {e.other(alias) for e in self.edges if alias in (e.left, e.right)}

    def edges_between(self, left: frozenset[str], right: frozenset[str]) -> list[Edge]:
        return [e for e in self.edges
                if (e.left in left and e.right in right) or (e.left in right and e.right in left)]

    def edges_within(self, subset: frozenset[str]) -> list[Edge]:
        return [e for e in self.edges if e.left in subset and e.right in subset]

    def is_connected(self, subset: frozenset[str]) -> bool:
        if not subset:
            return False
        todo = [next(iter(subset))]
        seen = {todo[0]}
        while todo:
            cur = todo.pop()
            for nxt in self.neighbours(cur) & subset:
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen == set(subset)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _lower_columns(node: exp.Expression) -> None:
    for col in node.find_all(exp.Column):
        col.set("this", exp.to_identifier(col.name.lower()))
        if col.table:
            col.set("table", exp.to_identifier(col.table.lower()))


def parse_join_query(sql: str, dialect: str = "postgres") -> JoinQuery:
    tree = sqlglot.parse_one(sql.strip().rstrip(";"), read=dialect)
    if not isinstance(tree, exp.Select):
        raise ValueError("only SELECT queries are supported")
    _lower_columns(tree)
    tables: dict[str, str] = {}
    sources = []
    from_ = tree.args.get("from_") or tree.args.get("from")
    if from_ is not None:
        sources.append(from_.this)
    for join in tree.args.get("joins") or []:
        sources.append(join.this)
    where_nodes = _conjuncts(tree.args["where"].this) if tree.args.get("where") else []
    for join in tree.args.get("joins") or []:
        if join.args.get("on") is not None:
            where_nodes += _conjuncts(join.args["on"])
    for src in sources:
        if not isinstance(src, exp.Table):
            raise ValueError("derived tables are not supported")
        alias = (src.alias or src.name).lower()
        tables[alias] = src.name.lower()
    filters: dict[str, list[exp.Expression]] = {a: [] for a in tables}
    edges: list[Edge] = []
    residual: list[tuple[frozenset[str], exp.Expression]] = []
    for node in where_nodes:
        refs = {c.table for c in node.find_all(exp.Column)}
        if (isinstance(node, exp.EQ) and isinstance(node.left, exp.Column)
                and isinstance(node.right, exp.Column)
                and node.left.table != node.right.table
                and node.left.table in tables and node.right.table in tables):
            edges.append(Edge(node.left.table, node.left.name, node.right.table, node.right.name))
        elif len(refs) == 1 and next(iter(refs)) in tables:
            filters[next(iter(refs))].append(node)
        elif not refs:
            continue
        else:
            residual.append((frozenset(refs), node))
    tail = {k: tree.args[k] for k in ("group", "having", "order", "limit") if tree.args.get(k)}
    return JoinQuery(sql=sql, tables=tables, filters=filters, edges=edges,
                     residual=residual, select=list(tree.expressions), tail=tail)
