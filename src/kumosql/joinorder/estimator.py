"""Cardinality estimates for connected sub-joins of a join query.

``FactorEstimator`` works from :class:`~kumosql.joinorder.stats.Statistics` only:

* a relation's filtered size comes from running its filters on the row sample;
* each set of columns that are equal to each other in the sub-join (an
  equivalence class, so ``a.x = b.y AND b.y = c.z`` is one 3-way class) is joined
  bin by bin. Inside a bin, rows are assumed spread evenly over the distinct key
  values, which is exact for the frequent values that have a bin of their own;
* filters are applied per bin. The share of sample rows that pass the filters in
  each bin (shrunk towards the overall pass rate) scales that bin, so a filter
  that keeps only some keys moves the join estimate with it;
* different equivalence classes are treated as independent.

The idea follows FactorJoin (Wu et al., SIGMOD 2023), simplified to sample-based
per-bin filter rates instead of learned single-table models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .predicates import compile_predicate
from .query import JoinQuery
from .stats import Statistics


class Estimator(Protocol):
    def estimate(self, query: JoinQuery, subset: frozenset[str]) -> float: ...


@dataclass
class _Rel:
    rows: float                       # filtered size
    bins: dict[str, tuple[list[float], list[float]]]  # key col -> (count, distinct) per bin


class FactorEstimator:
    def __init__(self, stats: Statistics, shrink: float = 4.0):
        self.stats = stats
        self.shrink = shrink
        # Per-query caches; the query object is kept so its id cannot be reused.
        self._query: JoinQuery | None = None
        self._rels: dict[str, _Rel] = {}
        self._est_cache: dict[frozenset[str], float] = {}
        self._pass_cache: dict[tuple, list[bool]] = {}

    # -- single relations -------------------------------------------------
    def _relation(self, query: JoinQuery, alias: str) -> _Rel:
        ts = self.stats.tables[query.tables[alias]]
        sample = ts.sample
        n_uniform = len(sample) - ts.pinned
        exact = ts.exact
        nodes = query.filters.get(alias, [])
        if nodes:
            fkey = (ts.name, tuple(sorted(p.sql() for p in nodes)))
            passed = self._pass_cache.get(fkey)
            if passed is None:
                preds = [compile_predicate(p) for p in nodes]
                passed = [all(fn(r) is True for fn in preds) for r in sample]
                if len(self._pass_cache) > 256:
                    self._pass_cache.clear()
                self._pass_cache[fkey] = passed
            k_pin = sum(passed[:ts.pinned])
            k_uni = sum(passed[ts.pinned:])
            rows = k_pin + k_uni * ts.weight
            if k_uni == 0 and not exact:
                # No sampled row passes: assume half a row did, but use the
                # single predicates' rates when their product is smaller.
                preds = [compile_predicate(p) for p in nodes]
                prod = 1.0
                for fn in preds:
                    kk = sum(1 for r in sample[ts.pinned:] if fn(r) is True)
                    prod *= (kk if kk else 0.5) / max(n_uniform, 1)
                rows = k_pin + min(prod, 0.5 / max(n_uniform, 1)) * (ts.rows - ts.pinned)
            sel = rows / ts.rows if ts.rows else 0.0
        else:
            passed, sel, rows = None, 1.0, float(ts.rows)
        cols = {e.col(alias) for e in query.edges if alias in (e.left, e.right)}
        bins: dict[str, tuple[list[float], list[float]]] = {}
        for col in cols:
            ks = ts.keys.get(col)
            if ks is None:
                raise KeyError(f"no key statistics for {ts.name}.{col}")
            if passed is None:
                bins[col] = (ks.count, ks.distinct)
                continue
            nb = len(ks.count)
            seen = [0.0] * nb
            hit = [0.0] * nb
            for i, (b, ok) in enumerate(zip(ks.sample_bins, passed)):
                if b >= 0:
                    w = 1.0 if i < ts.pinned else ts.weight
                    seen[b] += w
                    if ok:
                        hit[b] += w
            prior = 0.0 if exact else self.shrink * ts.weight
            count, distinct = [], []
            for b in range(nb):
                c, d = ks.count[b], ks.distinct[b]
                if c <= 0:
                    count.append(0.0)
                    distinct.append(0.0)
                    continue
                if b in ks.exact_bins:
                    r = hit[b] / seen[b] if seen[b] else 0.0
                elif seen[b] + prior > 0:
                    r = (hit[b] + prior * sel) / (seen[b] + prior)
                else:
                    r = sel
                cc = c * r
                dd = d * (1.0 - (1.0 - r) ** (c / d)) if d else 0.0
                count.append(cc)
                distinct.append(max(min(dd, cc), 0.0))
            bins[col] = (count, distinct)
        return _Rel(rows, bins)

    def _relations(self, query: JoinQuery) -> dict[str, _Rel]:
        if self._query is not query:
            self._query = query
            self._rels = {a: self._relation(query, a) for a in query.tables}
            self._est_cache = {}
        return self._rels

    # -- sub-joins --------------------------------------------------------
    def estimate(self, query: JoinQuery, subset: frozenset[str]) -> float:
        rels = self._relations(query)
        hit = self._est_cache.get(subset)
        if hit is not None:
            return hit
        est = 1.0
        for a in subset:
            est *= rels[a].rows
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
        for members in classes.values():
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
