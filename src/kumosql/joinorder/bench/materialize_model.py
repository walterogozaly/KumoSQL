"""Turn measured before/after pairs into runtime-model scores and an advisor comparison.

Input is a :class:`~.materialize.Measurements`. Work features come from three places:

* ``k_*``  KumoSQL's cardinality estimator on the query and on the query over the stored view
  (:mod:`kumosql.joinorder.view_cost`), available before anything is stored;
* ``e_*``  the engine's own plan estimates (``EXPLAIN``), which for the after-plan need the view
  to exist, so they say what a shadow materialization would predict, not what a catalog could;
* ``a_*``  the cardinalities the engine actually produced. These are diagnostics (they need the
  run they are meant to predict) and show how much of the error is the estimates and how much the
  model.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from kumosql import backtest
from kumosql.cost_model import calibrate, error_summary
from kumosql.joinorder.runtime_model import Pair, cross_validate, fit_ratio, folds_by_group, plan_work

from .materialize import Measurements, View, placements

KUMOSQL_FEATURES = ("scan", "join", "query", "build", "probe", "out_big_build", "peak", "joins")


def _prefixed(prefix: str, work: Mapping[str, float]) -> dict[str, float]:
    return {f"{prefix}_{k}": v for k, v in work.items()}


class KumoSQLWork:
    """Estimator features of every query and of every (query, view) pair, computed once."""

    def __init__(self, m: Measurements, stats: Any):
        from kumosql.joinorder.estimator import FactorEstimator
        from kumosql.joinorder.query import parse_join_query

        self.m = m
        self.estimator = FactorEstimator(stats)
        self.rows = {name: float(t.rows) for name, t in stats.tables.items()}
        self.queries = {q: parse_join_query(sql) for q, sql in m.work.queries.items()}
        self.place = placements(m.work)
        self._before: dict[str, dict] = {}
        self._view_rows: dict[str, float] = {}
        self._views = {v.id: v for v in m.views}

    def view_rows(self, view_id: str) -> float:
        """The estimator's row count for the view (what a catalog would know before storing it)."""

        if view_id not in self._view_rows:
            from kumosql.joinorder.query import parse_join_query

            vq = parse_join_query(self._views[view_id].sql)
            self._view_rows[view_id] = max(float(self.estimator.estimate(vq, frozenset(vq.tables))), 1.0)
        return self._view_rows[view_id]

    def before(self, q: str) -> dict[str, float]:
        if q not in self._before:
            from kumosql.joinorder.view_cost import query_features

            self._before[q] = query_features(self.queries[q], self.estimator, self.rows, plan_work=True)
        return self._before[q]

    def after(self, q: str, view_id: str) -> dict[str, float]:
        from kumosql.joinorder.view_cost import query_over_view_features

        group = frozenset(self.place[q][self._views[view_id].shape])
        return query_over_view_features(self.queries[q], group, self.estimator, self.rows,
                                        self.view_rows(view_id), plan_work=True)

    def build(self, view_id: str) -> dict[str, float]:
        from kumosql.joinorder.query import parse_join_query
        from kumosql.joinorder.view_cost import build_features

        return build_features(parse_join_query(self._views[view_id].sql), self.estimator, self.rows)


def make_pairs(rows: Sequence[Mapping], kw: KumoSQLWork, *, group: str = "view") -> list[Pair]:
    """Runtime-model pairs with the three feature families merged under ``k_``, ``e_`` and ``a_``."""

    pairs = []
    for r in rows:
        before = {**_prefixed("k", kw.before(r["query"])), **_prefixed("e", plan_work(r["estimate_before"], "est")),
                  **_prefixed("a", plan_work(r["plan_before"], "act"))}
        after = {**_prefixed("k", kw.after(r["query"], r["view"])), **_prefixed("e", plan_work(r["estimate_after"], "est")),
                 **_prefixed("a", plan_work(r["plan_after"], "act"))}
        pairs.append(Pair(r["query"], r[group], r["before"], r["after"], before, after))
    return pairs


#: Feature sets of the ratio models; ``needs`` says what must exist before the model can predict.
RATIO_MODELS: dict[str, dict] = {
    "ratio_kumosql": {"features": ("k_scan", "k_join"), "needs": "catalog statistics"},
    "ratio_kumosql_hash": {"features": ("k_scan", "k_join", "k_build", "k_probe", "k_out_big_build", "k_peak", "k_joins"),
                           "needs": "catalog statistics"},
    "ratio_engine_estimates": {"features": ("e_scan", "e_out", "e_build", "e_probe", "e_peak", "e_joins"),
                               "needs": "the stored view (EXPLAIN of the reader)"},
    "ratio_kumosql_and_engine": {"features": ("k_scan", "k_join", "e_scan", "e_out", "e_build", "e_probe", "e_peak"),
                                 "needs": "the stored view (EXPLAIN of the reader)"},
    "ratio_engine_actual": {"features": ("a_scan", "a_out", "a_build", "a_probe", "a_peak", "a_joins"),
                            "needs": "running the reader (diagnostic only)"},
}


def saving_from_log_ratio(before: float, log_ratio: float) -> float:
    return before * (1.0 - math.exp(log_ratio))


def level_model_savings(pairs: Sequence[Pair], baselines: Mapping[str, float], kw: KumoSQLWork, *, anchored: bool,
                        folds: int = 5, seed: int = 11) -> list[float]:
    """Out-of-fold savings of the level model fitted on baselines (``kumosql.cost_model.calibrate``).

    The model predicts seconds from scan, join and a constant; ``anchored=False`` subtracts the
    predicted after-time from the predicted before-time (the first prototype), ``anchored=True``
    from the measured before-time. Baselines of the queries in a held-out fold are not used to fit
    the predictions for that fold.
    """

    names = ("scan", "join", "query")
    queries = sorted({p.query for p in pairs})
    held_folds = folds_by_group(queries, folds, seed)
    out = [0.0] * len(pairs)
    for held in held_folds:
        samples = [({k: kw.before(q)[k] for k in names}, baselines[q]) for q in queries if q not in held and baselines.get(q)]
        model = calibrate(samples, names, folds=3).model
        for i, p in enumerate(pairs):
            if p.query in held:
                predicted_after = model.predict({k: p.work_after[f"k_{k}"] for k in names})
                reference = p.before if anchored else model.predict({k: p.work_before[f"k_{k}"] for k in names})
                out[i] = reference - predicted_after
    return out


def evaluate_pairs(pairs: Sequence[Pair], kw: KumoSQLWork, baselines: Mapping[str, float], *, l2: float = 1.0,
                   k: int = 20, resamples: int = 400, models: Sequence[str] | None = None) -> dict:
    """Out-of-fold rank scores of every model on ``pairs`` (folds hold whole views out together)."""

    measured = [p.before - p.after for p in pairs]
    by_query = [p.query for p in pairs]
    by_view = [p.group for p in pairs]
    predictions: dict[str, list[float]] = {
        "before_time_only": [p.before * 0.5 for p in pairs],
        "levels_difference": level_model_savings(pairs, baselines, kw, anchored=False),
        "levels_anchored": level_model_savings(pairs, baselines, kw, anchored=True),
    }
    for name, spec in RATIO_MODELS.items():
        if models is not None and name not in models:
            continue
        log_ratios = cross_validate(pairs, spec["features"], l2=l2)
        predictions[name] = [saving_from_log_ratio(p.before, y) for p, y in zip(pairs, log_ratios)]
    out = {}
    for name, predicted in predictions.items():
        scored = backtest.score(predicted, measured, k=k, resamples=resamples, groups=by_query, skip=("kendall",))
        view_clustered = backtest.score(predicted, measured, k=k, resamples=resamples, groups=by_view, skip=("kendall", "median_ratio"))
        out[name] = {"score": scored, "view_clustered": {m: view_clustered[m] for m in ("spearman", f"top_{k}_precision")},
                     "needs": RATIO_MODELS.get(name, {}).get("needs", "catalog statistics" if name.startswith("levels") else "nothing")}
    return out
