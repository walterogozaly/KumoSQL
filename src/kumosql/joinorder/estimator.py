"""Cardinality estimates for connected sub-joins of a join query.

``FactorEstimator`` works from :class:`~kumosql.joinorder.stats.Statistics` only:

* a relation's filtered size comes from running its filters on the stored rows
  (a sample, plus rows stored in full for frequent keys);
* lookup relations are folded into the relation they describe. When a relation
  joins the rest of the sub-join through one column only, and that column is
  unique in it (``company_name.id``, ``title.id``), it acts as a filter on its
  partner. Each stored row of the partner is weighted by whether its key passes
  the lookup's filters: known exactly when the key's row is stored, otherwise
  taken from the share that passes in the key's bin. That carries a filter
  across a join, so "movies made by one company" also changes the movie bins
  that the company's movies fall in. Folding repeats, so ``kind_type`` folds
  into ``title``, which then folds into ``movie_companies``;
* each set of columns that the remaining sub-join sets equal (an equivalence
  class, so ``a.x = b.y AND b.y = c.z`` is one three-way class) is joined bin by
  bin. Inside a bin, rows are assumed spread evenly over the distinct key
  values, which is exact for the frequent values that have a bin of their own;
* within each bin, the share of stored rows that pass (shrunk towards the
  overall share) scales that bin;
* different equivalence classes are treated as independent.

The binned join follows FactorJoin (Wu et al., SIGMOD 2023), simplified to
sample-based per-bin filter rates instead of learned single-table models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .predicates import compile_predicate
from .query import JoinQuery
from .stats import Statistics, TableStats


class Estimator(Protocol):
    def estimate(self, query: JoinQuery, subset: frozenset[str]) -> float: ...


@dataclass
class _Rel:
    rows: float                                       # filtered size
    weights: list[float]                              # pass weight of each stored row
    bins: dict[str, tuple[list[float], list[float]]]  # key col -> (count, distinct) per bin
    rates: dict[str, list[float]]                     # key col -> share of rows passing per bin


# A fold spec says which lookups are folded into a relation:
# a sorted tuple of (lookup alias, lookup column, own column, lookup's own spec).
Spec = tuple


class FactorEstimator:
    def __init__(self, stats: Statistics, shrink: float = 4.0, fold: bool = True):
        self.stats = stats
        self.shrink = shrink
        self.fold = fold
        self._query: JoinQuery | None = None
        self._rels: dict[tuple[str, Spec], _Rel] = {}
        self._est_cache: dict[frozenset[str], float] = {}
        self._pass_cache: dict[tuple, list[bool]] = {}
        self._unique: dict[tuple[str, str], bool] = {}
        self._lookup: dict[tuple[str, Spec, str], dict[Any, float]] = {}

    # -- helpers ----------------------------------------------------------
    def _is_unique(self, table: str, col: str) -> bool:
        key = (table, col)
        hit = self._unique.get(key)
        if hit is None:
            ks = self.stats.tables[table].keys.get(col)
            hit = bool(ks) and all(c == d for c, d in zip(ks.count, ks.distinct)) and sum(ks.count) > 0
            self._unique[key] = hit
        return hit

    def _local_pass(self, ts: TableStats, nodes: list) -> list[bool] | None:
        if not nodes:
            return None
        fkey = (ts.name, tuple(sorted(p.sql() for p in nodes)))
        passed = self._pass_cache.get(fkey)
        if passed is None:
            preds = [compile_predicate(p) for p in nodes]
            passed = [all(fn(r) is True for fn in preds) for r in ts.sample]
            if len(self._pass_cache) > 512:
                self._pass_cache.clear()
            self._pass_cache[fkey] = passed
        return passed

    def _zero_hit_rate(self, ts: TableStats, nodes: list) -> float:
        """Share of rows passing when no sampled row does: half a row, or the
        single predicates' product when smaller."""
        n = max(len(ts.sample) - ts.pinned, 1)
        prod = 1.0
        for p in nodes:
            fn = compile_predicate(p)
            kk = sum(1 for r in ts.sample[ts.pinned:] if fn(r) is True)
            prod *= (kk if kk else 0.5) / n
        return min(prod, 0.5 / n)

    def _lookup_weights(self, query: JoinQuery, alias: str, spec: Spec, col: str) -> dict[Any, float]:
        key = (alias, spec, col)
        hit = self._lookup.get(key)
        if hit is None:
            rel = self._rel(query, alias, spec)
            ts = self.stats.tables[query.tables[alias]]
            hit = {}
            for r, w in zip(ts.sample, rel.weights):
                v = r.get(col)
                if v is not None:
                    hit[v] = w
            self._lookup[key] = hit
        return hit

    # -- single relations -------------------------------------------------
    def _rel(self, query: JoinQuery, alias: str, spec: Spec = ()) -> _Rel:
        if self._query is not query:
            self._query = query
            self._rels, self._est_cache, self._lookup = {}, {}, {}
        ck = (alias, spec)
        rel = self._rels.get(ck)
        if rel is not None:
            return rel
        ts = self.stats.tables[query.tables[alias]]
        nodes = query.filters.get(alias, [])
        passed = self._local_pass(ts, nodes)
        local = [1.0] * len(ts.sample) if passed is None else [1.0 if p else 0.0 for p in passed]
        n_uni = len(ts.sample) - ts.pinned
        # One factor list per folded lookup: per stored row, and per bin of our column.
        folds: list[tuple[str, list[float], list[float]]] = []
        for (b, b_col, own_col, b_spec) in spec:
            look = self._lookup_weights(query, b, b_spec, b_col)
            b_ts = self.stats.tables[query.tables[b]]
            b_rel = self._rel(query, b, b_spec)
            rates = b_rel.rates.get(b_col)
            b_known = b_ts.keys[b_col].exact_bins
            domain = self.stats.domains[ts.keys[own_col].domain]

            # Keys of ours that the lookup lacks never match: per bin, at most
            # (lookup's distinct keys / our distinct keys) of our rows find a partner.
            d_own, d_b = ts.keys[own_col].distinct, b_ts.keys[b_col].distinct
            unknown = [0.0 if (b_ts.exact or bn in b_known or rates is None or d_own[bn] <= 0)
                       else rates[bn] * min(1.0, d_b[bn] / d_own[bn]) for bn in range(domain.nbins)]

            def factor(v: Any, bn: int) -> float:
                if v is None or bn < 0:
                    return 0.0
                f = look.get(v)
                return unknown[bn] if f is None else f
            bins_of = ts.keys[own_col].sample_bins
            per_row = [factor(r.get(own_col), bins_of[i]) for i, r in enumerate(ts.sample)]
            # per bin: exact for frequent values (a bin each), the lookup's pass share otherwise
            per_bin = list(unknown)
            if b_ts.exact and rates is not None:
                per_bin = [rates[bn] * min(1.0, d_b[bn] / d_own[bn]) if d_own[bn] > 0 else 0.0
                           for bn in range(domain.nbins)]
            for v, bn in domain.heavy.items():
                f = look.get(v)
                if f is not None:
                    per_bin[bn] = f
            folds.append((own_col, per_row, per_bin))

        def combine(skip: int = -1) -> list[float]:
            out = list(local)
            for k, (_, per_row, _) in enumerate(folds):
                if k != skip:
                    out = [w * f for w, f in zip(out, per_row)]
            return out
        weights = combine()
        uni = sum(weights[ts.pinned:])
        rows = sum(weights[:ts.pinned]) + uni * ts.weight
        if uni == 0.0 and not ts.exact:
            # Nothing sampled survives: local filters' rate times each fold's
            # share computed on the key bins (independence between them).
            if nodes:
                k_uni = sum(local[ts.pinned:])
                local_rows = (sum(local[:ts.pinned]) + k_uni * ts.weight) if k_uni else \
                    sum(local[:ts.pinned]) + self._zero_hit_rate(ts, nodes) * (ts.rows - ts.pinned)
            else:
                local_rows = float(ts.rows)
            rows = local_rows
            k_uni = sum(local[ts.pinned:])
            for own_col, _, per_bin in folds:
                if k_uni:
                    # where the locally passing sampled rows fall among the key bins
                    bins_of = ts.keys[own_col].sample_bins
                    mass = sum(per_bin[bins_of[i]] for i in range(ts.pinned, len(ts.sample))
                               if local[i] and bins_of[i] >= 0) / k_uni
                else:
                    count = ts.keys[own_col].count
                    mass = sum(c * f for c, f in zip(count, per_bin)) / (sum(count) or 1.0)
                rows *= mass
            # Never certain of zero from a sample: at most half a sampled row.
            rows = max(rows, min(local_rows, 0.5 * ts.weight) if any(f[1] for f in folds) or not nodes else 0.0)
        sel = rows / ts.rows if ts.rows else 0.0
        cols = {e.col(alias) for e in query.edges if alias in (e.left, e.right)}
        bins: dict[str, tuple[list[float], list[float]]] = {}
        rates_out: dict[str, list[float]] = {}
        filtered = passed is not None or bool(spec)
        for col in cols:
            ks = ts.keys.get(col)
            if ks is None:
                raise KeyError(f"no key statistics for {ts.name}.{col}")
            nb = len(ks.count)
            if not filtered:
                bins[col] = (ks.count, ks.distinct)
                rates_out[col] = [1.0] * nb
                continue
            own = [k for k, f in enumerate(folds) if f[0] == col]
            w_col = combine(own[0]) if own else weights
            seen = [0.0] * nb
            hit = [0.0] * nb
            for i, (b, w) in enumerate(zip(ks.sample_bins, w_col)):
                if b >= 0:
                    rw = 1.0 if i < ts.pinned else ts.weight
                    seen[b] += rw
                    hit[b] += w * rw
            prior = 0.0 if ts.exact else self.shrink * ts.weight
            base_sel = sel
            if own:
                # the shrink target excludes the fold applied per bin below
                mass = sum(c * f for c, f in zip(ks.count, folds[own[0]][2])) / (sum(ks.count) or 1.0)
                base_sel = min(sel / mass, 1.0) if mass > 0 else sel
            count, distinct, rate = [], [], []
            for b in range(nb):
                c, d = ks.count[b], ks.distinct[b]
                if c <= 0:
                    count.append(0.0)
                    distinct.append(0.0)
                    rate.append(0.0)
                    continue
                if b in ks.exact_bins or ts.exact:
                    r = hit[b] / seen[b] if seen[b] else 0.0
                elif seen[b] + prior > 0:
                    r = (hit[b] + prior * base_sel) / (seen[b] + prior)
                else:
                    r = base_sel
                if own:
                    r *= folds[own[0]][2][b]
                cc = c * r
                dd = d * (1.0 - (1.0 - r) ** (c / d)) if d else 0.0
                count.append(cc)
                distinct.append(max(min(dd, cc), 0.0))
                rate.append(r)
            bins[col] = (count, distinct)
            rates_out[col] = rate
        rel = _Rel(rows, weights, bins, rates_out)
        self._rels[ck] = rel
        return rel

    # -- sub-joins --------------------------------------------------------
    def _classes(self, query: JoinQuery, subset: frozenset[str]) -> list[list[tuple[str, str]]]:
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(x: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(x, x)
            while parent[x] != x:
                x = parent[x]
            return x

        for e in query.edges_within(subset):
            a, b = find((e.left, e.left_col)), find((e.right, e.right_col))
            if a != b:
                parent[a] = b
        classes: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for x in parent:
            classes.setdefault(find(x), []).append(x)
        return [sorted(m) for m in classes.values()]

    def _fold(self, query: JoinQuery, subset: frozenset[str]):
        classes = self._classes(query, subset)
        specs: dict[str, list] = {a: [] for a in subset}
        live = set(subset)
        changed = self.fold
        while changed:
            changed = False
            for b in sorted(live):
                mine = [c for c in classes if any(a == b for a, _ in c)]
                if len(mine) != 1:
                    continue
                cls = mine[0]
                b_cols = {col for a, col in cls if a == b}
                if len(b_cols) != 1:
                    continue
                b_col = next(iter(b_cols))
                if not self._is_unique(query.tables[b], b_col):
                    continue
                others = [(a, col) for a, col in cls if a != b]
                if not others:
                    continue

                def links(m: tuple[str, str]) -> tuple[int, bool, str]:
                    a, col = m
                    n_other = sum(1 for c in classes if c is not cls and any(x == a for x, _ in c))
                    return (n_other, not self._is_unique(query.tables[a], col), a)
                a, a_col = max(others, key=links)
                specs[a].append((b, b_col, a_col, tuple(sorted(specs[b]))))
                cls[:] = others
                live.discard(b)
                classes = [c for c in classes if len(c) > 1]
                changed = True
                break
        return live, classes, {a: tuple(sorted(specs[a])) for a in live}

    def estimate(self, query: JoinQuery, subset: frozenset[str]) -> float:
        if self._query is not query:
            self._rel(query, next(iter(query.tables)))
        hit = self._est_cache.get(subset)
        if hit is not None:
            return hit
        live, classes, specs = self._fold(query, subset)
        rels = {a: self._rel(query, a, specs[a]) for a in live}
        est = 1.0
        for a in live:
            est *= rels[a].rows
        for members in classes:
            if est == 0.0:
                break
            vecs = [rels[a].bins[c] for a, c in members]
            k = len(vecs)
            total = 0.0
            for b in range(len(vecs[0][0])):
                prod, dmax = 1.0, 0.0
                for count, distinct in vecs:
                    cb = count[b]
                    if cb <= 0.0:
                        prod = 0.0
                        break
                    prod *= cb
                    if distinct[b] > dmax:
                        dmax = distinct[b]
                if prod > 0.0:
                    total += prod / max(dmax, 1.0) ** (k - 1)
            denom = 1.0
            for a, _ in members:
                denom *= rels[a].rows
            est = 0.0 if denom == 0.0 else est * total / denom
        for refs, _ in query.residual:
            if refs <= subset:
                est /= 3.0
        self._est_cache[subset] = est
        return est
