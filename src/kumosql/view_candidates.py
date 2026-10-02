"""Find views worth materializing for a workload, and pick a few.

``mine(queries, schema)`` reads each query's join graph (tables, the equalities between columns of
different tables, closed under transitivity) and counts every connected set of two to four tables
that several queries share, whatever their aliases or filters. A candidate is that join, exposing
every column of its tables that any query reads. ``select(candidates, queries, budget)`` keeps the
views that save the most joins across the workload (greedy; each query is served by its best view).

The candidates are only proposals. ``kumosql.model_reuse.rewrite_over_model`` decides whether a
query can read one, and only a proven rewrite counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations, permutations
from typing import Mapping, Sequence

from sqlglot import exp

from . import model_reuse as mr

MAX_TABLES = 4  # views over more tables than this are not proposed


@dataclass(frozen=True)
class Shape:
    """A join, independent of aliases: its tables in a canonical order and the column equalities."""

    tables: tuple[str, ...]
    edges: tuple[tuple[int, int, str, str], ...]  # (position, position, column, column), position order i < j


@dataclass
class Graph:
    """The join graph of one query: aliases with their tables, equalities between columns, columns read."""

    tables: dict[str, str]
    classes: list[set[tuple[str, str]]]  # equal columns, as (alias, column)
    columns: dict[str, set[str]]  # alias -> columns the query reads


@dataclass
class Candidate:
    shape: Shape
    columns: list[set[str]]  # per position: columns exposed
    queries: set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.shape.tables)

    def sql(self) -> str:
        names = [f"t{i}" for i in range(self.size)]
        items = [f"{names[i]}.{c} AS {names[i]}_{c}" for i in range(self.size) for c in sorted(self.columns[i])]
        sources = ", ".join(f"{t} AS {names[i]}" for i, t in enumerate(self.shape.tables))
        where = " AND ".join(f"{names[i]}.{ci} = {names[j]}.{cj}" for i, j, ci, cj in self.shape.edges)
        return f"SELECT {', '.join(items)} FROM {sources}" + (f" WHERE {where}" if where else "")


def graph_of(sql: str, schema: Mapping[str, Sequence[str]], dialect: str = "postgres") -> Graph | None:
    """The query's join graph, or None when the reuse engine cannot read the query."""

    try:
        tree = mr._prepare(sql, schema, dialect)
        block = mr._block(tree)
    except (mr._Unsupported, Exception):  # noqa: BLE001 - sqlglot raises many types; unreadable means no graph
        return None
    parents: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x):
        parents.setdefault(x, x)
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    for conjunct in block.conjuncts:
        for part in mr._expand_conjuncts([conjunct]):
            if isinstance(part, exp.EQ) and isinstance(part.this, exp.Column) and isinstance(part.expression, exp.Column):
                a, b = part.this, part.expression
                if a.table and b.table and a.table != b.table:
                    parents[find((a.table, a.name))] = find((b.table, b.name))
    groups: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for column in list(parents):
        groups.setdefault(find(column), set()).add(column)
    columns: dict[str, set[str]] = {}
    roots = [*block.outputs, *block.conjuncts, *block.group, *block.order] + ([block.having] if block.having is not None else [])
    for root in roots:
        for column in root.find_all(exp.Column):
            if column.table:
                columns.setdefault(column.table, set()).add(column.name)
    return Graph(dict(block.tables), list(groups.values()), columns)


def _subsets(graph: Graph, max_tables: int):
    """Connected alias sets of two to ``max_tables`` tables, joined through equal columns."""

    neighbours: dict[str, set[str]] = {a: set() for a in graph.tables}
    for members in graph.classes:
        aliases = {a for a, _ in members}
        for a in aliases:
            neighbours[a] |= aliases - {a}
    seen: set[frozenset[str]] = set()
    frontier = [frozenset([a]) for a in graph.tables]
    for _ in range(max_tables - 1):
        grown: set[frozenset[str]] = set()
        for subset in frontier:
            for alias in set().union(*(neighbours[a] for a in subset)) - subset:
                grown.add(subset | {alias})
        seen |= grown
        frontier = list(grown)
    return seen


def _shape(graph: Graph, aliases: frozenset[str]) -> tuple[Shape, list[str]]:
    """The canonical shape of the join of ``aliases`` and the alias at each position."""

    best: tuple | None = None
    for order in permutations(sorted(aliases)):
        position = {a: i for i, a in enumerate(order)}
        edges = set()
        for members in graph.classes:
            inside = sorted((position[a], c) for a, c in members if a in position)
            for (i, ci), (j, cj) in zip(inside, inside[1:]):
                if i != j:
                    edges.add((i, j, ci, cj))
        key = (tuple(graph.tables[a] for a in order), tuple(sorted(edges)))
        if best is None or key < best[0]:
            best = (key, list(order))
    assert best is not None
    return Shape(*best[0]), best[1]


def shapes_of(graph: Graph, max_tables: int = MAX_TABLES) -> dict[Shape, list[str]]:
    """Every shape of two to ``max_tables`` joined tables in the query, with one alias placement each."""

    found: dict[Shape, list[str]] = {}
    for aliases in _subsets(graph, max_tables):
        shape, order = _shape(graph, aliases)
        # a join that closes into a connected graph only through a third table is not a view of its own
        if _connected(shape):
            found.setdefault(shape, order)
    return found


def _connected(shape: Shape) -> bool:
    reached = {0}
    changed = True
    while changed:
        changed = False
        for i, j, _, _ in shape.edges:
            if (i in reached) != (j in reached):
                reached |= {i, j}
                changed = True
    return len(reached) == len(shape.tables)


def mine(queries: Mapping[str, str], schema: Mapping[str, Sequence[str]], *, min_support: int = 2, max_tables: int = MAX_TABLES, dialect: str = "postgres") -> list[Candidate]:
    """Candidate views: every join shape at least ``min_support`` queries share."""

    candidates: dict[Shape, Candidate] = {}
    for name, sql in queries.items():
        graph = graph_of(sql, schema, dialect)
        if graph is None:
            continue
        for shape, order in shapes_of(graph, max_tables).items():
            candidate = candidates.setdefault(shape, Candidate(shape, [set() for _ in shape.tables]))
            candidate.queries.add(name)
            for position, alias in enumerate(order):
                candidate.columns[position] |= graph.columns.get(alias, set())
    kept = [c for c in candidates.values() if len(c.queries) >= min_support]
    for candidate in kept:  # a view exposes the columns it joins on too, and at least one column per table
        for position, table in enumerate(candidate.shape.tables):
            for i, j, ci, cj in candidate.shape.edges:
                if i == position:
                    candidate.columns[position].add(ci)
                if j == position:
                    candidate.columns[position].add(cj)
    return sorted(kept, key=lambda c: (-len(c.queries) * (c.size - 1), -c.size, c.sql()))


def select(candidates: Sequence[Candidate], budget: int) -> list[Candidate]:
    """Greedy choice of ``budget`` views: each query is served by its largest chosen view."""

    best: dict[str, int] = {}
    chosen: list[Candidate] = []
    pool = list(candidates)
    while pool and len(chosen) < budget:
        gains = [(sum(max(0, c.size - 1 - best.get(q, 0)) for q in c.queries), c) for c in pool]
        gain, pick = max(gains, key=lambda g: (g[0], g[1].size, -len(g[1].sql())))
        if gain <= 0:
            break
        chosen.append(pick)
        pool.remove(pick)
        for q in pick.queries:
            best[q] = max(best.get(q, 0), pick.size - 1)
    return chosen
