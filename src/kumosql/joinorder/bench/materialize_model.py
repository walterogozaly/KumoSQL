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

from .materialize import Measurements, View, _queries_for_stage, placements

KUMOSQL_FEATURES = ("scan", "join", "query", "build", "probe", "out_big_build", "peak", "joins")


def _prefixed(prefix: str, work: Mapping[str, float]) -> dict[str, float]:
    return {f"{prefix}_{k}": v for k, v in work.items()}


class KumoSQLWork:
    """Estimator features of every query and of every (query, view) pair, computed once."""

    def __init__(self, m: Measurements, stats: Any, *, queries: Sequence[str] | None = None,
                 allow_held_out: bool = False):
        from kumosql.joinorder.estimator import FactorEstimator
        from kumosql.joinorder.query import parse_join_query

        self.m = m
        self.estimator = FactorEstimator(stats)
        self.rows = {name: float(t.rows) for name, t in stats.tables.items()}
        names = _queries_for_stage(m.work, queries, allow_held_out=allow_held_out, stage="runtime features")
        self.queries = {q: parse_join_query(m.work.queries[q]) for q in sorted(names)}
        self.place = placements(m.work, queries=names, allow_held_out=allow_held_out)
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
            if q not in self.queries:
                raise ValueError(f"query {q} was not released for runtime features")
            from kumosql.joinorder.view_cost import query_features

            self._before[q] = query_features(self.queries[q], self.estimator, self.rows, plan_work=True)
        return self._before[q]

    def after(self, q: str, view_id: str) -> dict[str, float]:
        if q not in self.queries:
            raise ValueError(f"query {q} was not released for runtime features")
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

    wrong = [r for r in rows if r.get("wrong")]
    if wrong:
        raise ValueError(f"cannot fit or score runtime models with {len(wrong)} wrong-result reader pairs")
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
                        training_pairs: Sequence[Pair] | None = None, training_kw: KumoSQLWork | None = None,
                        folds: int = 5, seed: int = 11) -> list[float]:
    """Level-model savings for ``pairs``, calibrated from development-query baselines.

    The model predicts seconds from scan, join and a constant; ``anchored=False`` subtracts the
    predicted after-time from the predicted before-time (the first prototype), ``anchored=True``
    from the measured before-time. With ``training_pairs``, only their distinct query baselines fit
    the model; without it, query-grouped cross-validation is intended for development diagnostics.
    """

    names = ("scan", "join", "query")
    fit_kw = training_kw or kw
    out = [0.0] * len(pairs)
    if training_pairs is None:
        queries = sorted({p.query for p in pairs})
        held_folds = folds_by_group(queries, folds, seed)
        for held in held_folds:
            samples = [({k: fit_kw.before(q)[k] for k in names}, baselines[q])
                       for q in queries if q not in held and baselines.get(q) is not None]
            if len(samples) < 2:
                raise ValueError("level-model cross-validation needs at least two training baselines per fold")
            model = calibrate(samples, names, folds=min(3, len(samples))).model
            for i, p in enumerate(pairs):
                if p.query in held:
                    predicted_after = model.predict({k: p.work_after[f"k_{k}"] for k in names})
                    reference = p.before if anchored else model.predict({k: p.work_before[f"k_{k}"] for k in names})
                    out[i] = reference - predicted_after
        return out

    queries = sorted({p.query for p in training_pairs})
    samples = [({k: fit_kw.before(q)[k] for k in names}, baselines[q])
               for q in queries if baselines.get(q) is not None]
    if len(samples) < 2:
        raise ValueError("level model needs at least two development baselines")
    model = calibrate(samples, names, folds=min(3, len(samples))).model
    for i, p in enumerate(pairs):
        predicted_after = model.predict({k: p.work_after[f"k_{k}"] for k in names})
        reference = p.before if anchored else model.predict({k: p.work_before[f"k_{k}"] for k in names})
        out[i] = reference - predicted_after
    return out


def evaluate_pairs(pairs: Sequence[Pair], kw: KumoSQLWork, baselines: Mapping[str, float], *,
                   training_pairs: Sequence[Pair] | None,
                   cross_validate_development: bool = False, training_kw: KumoSQLWork | None = None,
                   l2: float = 1.0,
                   k: int = 20, resamples: int = 400, models: Sequence[str] | None = None) -> dict:
    """Score runtime models on ``pairs``.

    For development diagnostics, pass ``training_pairs=None`` and explicitly set
    ``cross_validate_development=True``; predictions are cross-validated by whole view groups.
    To score a frozen model on held-out queries, pass development pairs explicitly as
    ``training_pairs``. Their query names must be disjoint from the scored pairs, and all fitted
    models use only those development pairs and their baselines. There is no implicit training
    fallback, so held-out pairs cannot accidentally train their own score.
    """

    measured = [p.before - p.after for p in pairs]
    by_query = [p.query for p in pairs]
    by_view = [p.group for p in pairs]
    if training_pairs is None and not cross_validate_development:
        raise ValueError("pass development training_pairs or opt in to development cross-validation")
    if training_pairs is not None and cross_validate_development:
        raise ValueError("choose either development training_pairs or development cross-validation")
    if training_pairs is not None:
        training_queries = {p.query for p in training_pairs}
        overlap = training_queries.intersection(by_query)
        if overlap:
            names = ", ".join(sorted(overlap)[:5])
            raise ValueError(f"development and scored queries overlap: {names}")
        if not training_pairs:
            raise ValueError("training_pairs cannot be empty")
    predictions: dict[str, list[float]] = {
        "before_time_only": [p.before * 0.5 for p in pairs],
        "levels_difference": level_model_savings(pairs, baselines, kw, anchored=False, training_pairs=training_pairs,
                                                 training_kw=training_kw),
        "levels_anchored": level_model_savings(pairs, baselines, kw, anchored=True, training_pairs=training_pairs,
                                               training_kw=training_kw),
    }
    for name, spec in RATIO_MODELS.items():
        if models is not None and name not in models:
            continue
        if training_pairs is None:
            log_ratios = cross_validate(pairs, spec["features"], l2=l2)
        else:
            model = fit_ratio(training_pairs, spec["features"], l2=l2)
            log_ratios = [model.log_ratio(p.work_before, p.work_after) for p in pairs]
        predictions[name] = [saving_from_log_ratio(p.before, y) for p, y in zip(pairs, log_ratios)]
    out = {}
    for name, predicted in predictions.items():
        scored = backtest.score(predicted, measured, k=k, resamples=resamples, groups=by_query, skip=("kendall",))
        view_clustered = backtest.score(predicted, measured, k=k, resamples=resamples, groups=by_view, skip=("kendall", "median_ratio"))
        out[name] = {"score": scored, "view_clustered": {m: view_clustered[m] for m in ("spearman", f"top_{k}_precision")},
                     "needs": RATIO_MODELS.get(name, {}).get("needs", "catalog statistics" if name.startswith("levels") else "nothing")}
    out["training"] = {
        "mode": "development_fit" if training_pairs is not None else "development_grouped_cross_validation",
        "pairs": len(training_pairs) if training_pairs is not None else len(pairs),
        "queries": len({p.query for p in training_pairs}) if training_pairs is not None else len(set(by_query)),
    }
    return out
