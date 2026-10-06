"""Generalize mined join views so that one stored view answers queries over fewer tables.

``kumosql.view_candidates`` proposes inner-join views. A view ``orders JOIN customers ON customers.id =
orders.customer_id`` answers only queries that join both tables. When ``customers.id`` is a declared unique key,
the same rows are available from the more general view

    orders AS t0 LEFT JOIN customers AS t1 ON t1.id = t0.customer_id

because a LEFT JOIN onto a unique key keeps every ``orders`` row exactly once: a query over ``orders`` alone
reads the view's ``orders`` columns, and a query that joins both reads the view with ``t1_id IS NOT NULL``.

``generalize`` turns candidates into such views: it strips from each candidate the tables that hang off the
rest through their own single-column unique key (the *extras*), keeps what remains as the *core* and merges the
candidates that share a core into one view. ``GeneralView.slices_for`` lists the slices (the core plus a subset
of extras) a query could read; each slice is an ordinary inner-join view (``inner_sql``) that the rewrite engine
reads, and ``lemma`` asks the prover to prove that the slice equals the stored view filtered to the rows where
the slice's extras are present (``stored_sql``). A rewrite over a slice is proven to answer the query, and the
lemma proves the slice is what the stored view holds, so the rewrite is a rewrite over the stored view.

Everything here only proposes. A slice that the prover cannot show equal to the stored view is not used, and a
rewrite is accepted only when ``rewrite_over_model`` proves it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import model_reuse as mr
from . import view_candidates as vc
from .smt_equivalence import TableConstraints

MAX_SLICES = 6  # slices tried per query and view
_LEMMAS: dict[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...], str], bool] = {}  # proven slice lemmas


@dataclass(frozen=True)
class Extra:
    """A table LEFT-joined to the view through its own unique key: ``table.key = <via node>.via_column``."""

    table: str
    key: str
    via: tuple  # ("c", core position) or ("e", the Extra it hangs off)
    via_column: str


@dataclass
class GeneralView:
    core: vc.Shape
    extras: list[Extra]  # parents before children
    columns: list[set[str]]  # per node (core positions, then extras): columns exposed
    queries: set[str] = field(default_factory=set)

    @property
    def nodes(self) -> list[str]:
        return [*self.core.tables, *(e.table for e in self.extras)]

    @property
    def tables(self) -> list[str]:
        return sorted(self.nodes)

    @property
    def size(self) -> int:
        return len(self.nodes)

    # -- SQL ------------------------------------------------------------------------------------

    def _node_name(self, position: int) -> str:
        return f"t{position}"

    def _items(self, nodes: Sequence[int], prefix: str = "") -> str:
        return ", ".join(f"{prefix}{self._node_name(i)}_{c} AS {self._node_name(i)}_{c}" if prefix else f"{self._node_name(i)}.{c} AS {self._node_name(i)}_{c}" for i in nodes for c in sorted(self.columns[i]))

    def _via(self, extra: Extra) -> str:
        kind, target = extra.via
        position = target if kind == "c" else len(self.core.tables) + self.extras.index(target)
        return f"{self._node_name(position)}.{extra.via_column}"

    def _poison(self) -> str:
        column = sorted(self.columns[0])[0]
        return f"t0.{column} IS NULL AND t0.{column} IS NOT NULL"

    def sql(self, poison: bool = False) -> str:
        """The stored view: the core joined as an inner join, every extra LEFT-joined to it."""

        core = len(self.core.tables)
        sources = " CROSS JOIN ".join(f"{t} AS {self._node_name(i)}" for i, t in enumerate(self.core.tables))
        for k, extra in enumerate(self.extras):
            sources += f" LEFT JOIN {extra.table} AS {self._node_name(core + k)} ON {self._node_name(core + k)}.{extra.key} = {self._via(extra)}"
        where = [f"t{i}.{ci} = t{j}.{cj}" for i, j, ci, cj in self.core.edges]
        if poison:
            where.append(self._poison())
        return f"SELECT {self._items(range(self.size))} FROM {sources}" + (f" WHERE {' AND '.join(where)}" if where else "")

    def inner_sql(self, chosen: frozenset[int], poison: bool = False) -> str:
        """The inner join of the core and the chosen extras, exposing the same columns as the stored view."""

        core = len(self.core.tables)
        nodes = [*range(core), *(core + k for k in sorted(chosen))]
        sources = ", ".join(f"{self.nodes[i]} AS {self._node_name(i)}" for i in nodes)
        where = [f"t{i}.{ci} = t{j}.{cj}" for i, j, ci, cj in self.core.edges]
        where += [f"{self._node_name(core + k)}.{self.extras[k].key} = {self._via(self.extras[k])}" for k in sorted(chosen)]
        if poison:
            where.append(self._poison())
        return f"SELECT {self._items(nodes)} FROM {sources}" + (f" WHERE {' AND '.join(where)}" if where else "")

    def stored_sql(self, chosen: frozenset[int], source: str) -> str:
        """What a reader of the stored view ``source`` (a table name or a parenthesized query) sees for a slice."""

        core = len(self.core.tables)
        nodes = [*range(core), *(core + k for k in sorted(chosen))]
        present = [f"g.{self._node_name(core + k)}_{self.extras[k].key} IS NOT NULL" for k in sorted(chosen)]
        return f"SELECT {self._items(nodes, 'g.')} FROM {source} AS g" + (f" WHERE {' AND '.join(present)}" if present else "")

    def stored_inline(self, chosen: frozenset[int], poison: bool = False) -> str:
        return self.stored_sql(chosen, f"({self.sql(poison)})")

    # -- slices ---------------------------------------------------------------------------------

    def slices_for(self, graph: vc.Graph) -> list[frozenset[int]]:
        """Subsets of extras (parents included) that the query's join graph contains with the core, largest first.

        A slice fits when the query's tables can play the slice's tables so that every join of the slice (the
        core's equalities and each extra's key equalling its column) is an equality the query states."""

        have = Counter(graph.tables.values())
        for t, n in Counter(self.core.tables).items():
            have[t] -= n
            if have[t] < 0:
                return []
        found: list[frozenset[int]] = []

        def grow(k: int, chosen: tuple[int, ...], left: Counter) -> None:
            if k == len(self.extras):
                found.append(frozenset(chosen))
                return
            extra = self.extras[k]
            kind, target = extra.via
            parent_ok = kind == "c" or self.extras.index(target) in chosen
            if parent_ok and left[extra.table] > 0:
                taken = Counter(left)
                taken[extra.table] -= 1
                grow(k + 1, (*chosen, k), taken)
            grow(k + 1, chosen, left)

        grow(0, (), have)
        found.sort(key=lambda s: (-len(s), sorted(s)))
        return [s for s in found if self._embeds(graph, s)][:MAX_SLICES]

    def _embeds(self, graph: vc.Graph, chosen: frozenset[int]) -> bool:
        """Can the slice's tables be placed on distinct aliases of the query so that its joins hold there?"""

        core = len(self.core.tables)
        nodes = [*range(core), *(core + k for k in sorted(chosen))]
        class_of = {member: n for n, members in enumerate(graph.classes) for member in members}

        def same(a: str, ca: str, b: str, cb: str) -> bool:
            return (a, ca) in class_of and class_of.get((a, ca)) == class_of.get((b, cb))

        def place(at: int, used: dict[int, str]) -> bool:
            if at == len(nodes):
                return True
            node = nodes[at]
            for alias, table in graph.tables.items():
                if table != self.nodes[node] or alias in used.values():
                    continue
                used[node] = alias
                ok = True
                for i, j, ci, cj in self.core.edges:
                    if node in (i, j) and i in used and j in used and not same(used[i], ci, used[j], cj):
                        ok = False
                if node >= core:
                    extra = self.extras[node - core]
                    kind, target = extra.via
                    via_node = target if kind == "c" else core + self.extras.index(target)
                    ok = ok and same(alias, extra.key, used[via_node], extra.via_column)
                if ok and place(at + 1, used):
                    return True
                del used[node]
            return False

        return place(0, {})

    def lemma(self, chosen: frozenset[int], schema: Mapping[str, Sequence[str]], constraints: Mapping[str, TableConstraints] | None, timeout_ms: int = 10000, poison: bool = False) -> bool:
        """The prover shows the slice's inner join equals the stored view filtered to rows where the slice's extras are present."""

        from .algebraic_equivalence import prove_equivalent_algebraic

        inner, stored = self.inner_sql(chosen, poison), self.stored_inline(chosen, poison)
        schema_key = tuple(sorted((table, tuple(columns)) for table, columns in schema.items()))
        key = (inner, stored, schema_key, repr(sorted((constraints or {}).items())))
        if key not in _LEMMAS:
            try:
                result = prove_equivalent_algebraic(inner, stored, schema=schema, constraints=constraints, timeout_ms=timeout_ms, dialect="postgres", compare_names=False)
                _LEMMAS[key] = bool(result.proven)
            except Exception:  # noqa: BLE001 - a prover crash is never a proof
                return False
        return _LEMMAS[key]


# ---------------------------------------------------------------------------------------------
# generalizing candidates


def _single_keys(constraints: Mapping[str, TableConstraints] | None) -> dict[str, str]:
    """Each table's single-column unique key (the first declared), by table name."""

    out: dict[str, str] = {}
    for table, c in (constraints or {}).items():
        for key in c.keys:
            if len(key) == 1 and key[0] in c.not_null:
                out.setdefault(table, key[0])
    return out


def _classes(shape: vc.Shape) -> list[set[tuple[int, str]]]:
    parents: dict[tuple[int, str], tuple[int, str]] = {}

    def find(x):
        parents.setdefault(x, x)
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    for i, j, ci, cj in shape.edges:
        parents[find((i, ci))] = find((j, cj))
    groups: dict = {}
    for x in list(parents):
        groups.setdefault(find(x), set()).add(x)
    return list(groups.values())


def _connected(count: int, classes: list[set[tuple[int, str]]], alive: set[int]) -> bool:
    if len(alive) <= 1:
        return True
    start = min(alive)
    reached = {start}
    changed = True
    while changed:
        changed = False
        for members in classes:
            nodes = {p for p, _ in members if p in alive}
            if nodes & reached and not nodes <= reached:
                reached |= nodes
                changed = True
    return reached == alive


def split_shape(shape: vc.Shape, keys: Mapping[str, str]) -> tuple[vc.Shape, list[int], list[tuple[int, str, int, str]]] | None:
    """Strip keyed tables from a join shape.

    Returns the core shape, the old positions of the core's tables (in core order) and the stripped tables as
    ``(old position, key column, old position it hangs off, that column)``, parents first; None when no table can
    be stripped. A table is stripped when it takes part in exactly one equality class, through its own
    single-column key, the class has other members, and the rest stays connected."""

    classes = _classes(shape)
    alive = set(range(len(shape.tables)))
    removed: list[tuple[int, str, int, str]] = []
    changed = True
    while changed and len(alive) > 1:
        changed = False
        for j in sorted(alive, reverse=True):
            key = keys.get(shape.tables[j])
            mine = [c for c in classes if any(p == j for p, _ in c)]
            if key is None or len(mine) != 1:
                continue
            members = mine[0]
            if sum(1 for p, _ in members if p == j) != 1 or (j, key) not in members:
                continue
            others = sorted((p, c) for p, c in members if p != j and p in alive)
            if not others:
                continue
            rest = alive - {j}
            trial = [{m for m in c if m[0] != j} for c in classes]
            if not _connected(len(shape.tables), trial, rest):
                continue
            via = others[0]
            removed.append((j, key, via[0], via[1]))
            alive = rest
            classes = trial
            changed = True
            break
    if not removed:
        return None
    order = sorted(alive)
    index = {old: new for new, old in enumerate(order)}
    edges: set[tuple[int, int, str, str]] = set()
    for members in classes:
        inside = sorted((index[p], c) for p, c in members if p in alive)
        for (i, ci), (j, cj) in zip(inside, inside[1:]):
            if i != j:
                edges.add((i, j, ci, cj))
    return vc.Shape(tuple(shape.tables[o] for o in order), tuple(sorted(edges))), order, list(reversed(removed))


def generalize(candidates: Sequence[vc.Candidate], constraints: Mapping[str, TableConstraints] | None, graphs: Mapping[str, vc.Graph]) -> list[GeneralView]:
    """Merge candidates into general views: one per core, with every keyed table that hangs off it as an extra.

    ``graphs`` are the join graphs of the queries the views are mined from; the columns of a core are every
    column those queries read from the core's tables."""

    keys = _single_keys(constraints)
    views: dict[vc.Shape, GeneralView] = {}
    for candidate in candidates:
        split = split_shape(candidate.shape, keys)
        if split is None:
            core, order, stripped = candidate.shape, list(range(candidate.size)), []
        else:
            core, order, stripped = split
        view = views.get(core)
        if view is None:
            view = views[core] = GeneralView(core, [], [set() for _ in core.tables])
        node_of: dict[int, int] = {old: new for new, old in enumerate(order)}  # old position -> node index in the view
        identity: dict[int, tuple] = {old: ("c", new) for old, new in node_of.items()}
        for old, key, via_old, via_column in stripped:
            extra = Extra(candidate.shape.tables[old], key, identity[via_old], via_column)
            if extra not in view.extras:
                view.extras.append(extra)
                view.columns.append(set())
            identity[old] = ("e", extra)
            node_of[old] = len(core.tables) + view.extras.index(extra)
        for old, node in node_of.items():
            view.columns[node] |= candidate.columns[old]
        for old, key, via_old, via_column in stripped:
            node = node_of[old]
            view.columns[node].add(key)  # the presence test reads the key
            via_node = node_of[via_old]
            view.columns[via_node].add(via_column)
        for i, j, ci, cj in core.edges:
            view.columns[i].add(ci)
            view.columns[j].add(cj)
        view.queries |= candidate.queries
    # the core's columns: everything any query reads from the core's tables
    for view in views.values():
        for name, graph in graphs.items():
            for placement in _placements(view.core, graph):
                for position, alias in enumerate(placement):
                    view.columns[position] |= graph.columns.get(alias, set())
                view.queries.add(name)
    return sorted(views.values(), key=lambda v: (-len(v.queries), -v.size, v.sql()))


def _placements(core: vc.Shape, graph: vc.Graph) -> list[list[str]]:
    """The aliases of the query that can play the core's tables, in core order (one placement per shape)."""

    if len(core.tables) == 1:
        return [[a] for a, t in graph.tables.items() if t == core.tables[0]]
    order = vc.shapes_of(graph).get(core)
    return [order] if order else []


def slice_size(view: GeneralView, graph: vc.Graph) -> int:
    """The tables of the query that the best slice of the view would replace (0 when none applies)."""

    slices = view.slices_for(graph)
    return len(view.core.tables) + len(slices[0]) if slices else 0


def select(views: Sequence[GeneralView], graphs: Mapping[str, vc.Graph], budget: int) -> list[GeneralView]:
    """Greedy choice of ``budget`` views: each query is served by the view whose slice replaces most of its tables."""

    covers = {id(v): {q: slice_size(v, g) for q, g in graphs.items()} for v in views}
    best: dict[str, int] = {}
    chosen: list[GeneralView] = []
    pool = list(views)
    while pool and len(chosen) < budget:
        gains = [(sum(max(0, n - best.get(q, 0)) for q, n in covers[id(v)].items()), v) for v in pool]
        gain, pick = max(gains, key=lambda g: (g[0], g[1].size, -len(g[1].sql())))
        if gain <= 0:
            break
        chosen.append(pick)
        pool.remove(pick)
        for q, n in covers[id(pick)].items():
            best[q] = max(best.get(q, 0), n)
    return chosen
