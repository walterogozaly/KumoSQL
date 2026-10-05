"""View selection, its Lagrangian bound, the backtest metrics and the calibrated cost model."""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from kumosql import backtest as bt
from kumosql.cost_model import Pricing, calibrate, error_summary, fit
from kumosql.costs import ObservedJob
from kumosql.materialization import Candidate, Evidence, Option, Problem, Template, select, upper_bound

PROVEN = Evidence("proven", "test")


def _facility(seed: int, templates: int = 12, candidates: int = 6) -> Problem:
    rng = random.Random(seed)
    nodes = [f"v{i}" for i in range(candidates)]
    ts = []
    for i in range(templates):
        base = rng.uniform(1, 10)
        options = [Option(base)]
        for node in rng.sample(nodes, rng.randint(0, 3)):
            options.append(Option(base * rng.uniform(0.1, 1.3), frozenset([node])))
        ts.append(Template(f"t{i}", rng.choice([1, 2, 4]), tuple(options)))
    cs = [Candidate(f"c{i}", n, "store_view", n, 1.0, rng.uniform(0, 8), rng.uniform(1, 100), PROVEN) for i, n in enumerate(nodes)]
    return Problem(ts, cs, "seconds")


def _brute(problem: Problem) -> float:
    ids = [c.id for c in problem.candidates]
    return max(problem.saving(combo) for r in range(len(ids) + 1) for combo in itertools.combinations(ids, r))


@pytest.mark.parametrize("seed", range(8))
def test_exact_selection_finds_the_best_set_and_the_bound_holds(seed):
    problem = _facility(seed)
    best = _brute(problem)
    chosen = select(problem, exact_limit=12)
    assert chosen.method == "exact"
    assert chosen.saving == pytest.approx(best)
    assert chosen.upper_bound is not None and chosen.upper_bound >= best - 1e-9


@pytest.mark.parametrize("seed", range(6))
def test_greedy_with_local_search_is_feasible_and_bounded(seed):
    problem = _facility(seed, templates=30, candidates=9)
    best = _brute(problem)
    greedy = select(problem, exact_limit=0)
    assert greedy.method == "greedy with local search"
    assert 0 <= greedy.saving <= best + 1e-9
    assert greedy.upper_bound >= best - 1e-9
    assert greedy.saving == pytest.approx(problem.saving(greedy.chosen))


def test_only_proven_candidates_are_selected():
    t = Template("t", 1.0, (Option(10.0), Option(1.0, frozenset(["v"]))))
    c = Candidate("c", "store v", "store_view", "v", 1.0, 1.0, 10.0, Evidence("conditional", "source", ("fresh",)))
    problem = Problem([t], [c], "seconds")
    assert problem.saving(["c"]) == pytest.approx(8.0)
    assert select(problem).chosen == []


def test_storage_budget_and_storage_price():
    t = Template("t", 1.0, (Option(10.0), Option(1.0, frozenset(["a"])), Option(2.0, frozenset(["b"]))))
    a = Candidate("a", "a", "store_view", "a", 1.0, 0.0, 100.0, PROVEN)
    b = Candidate("b", "b", "store_view", "b", 1.0, 0.0, 10.0, PROVEN)
    problem = Problem([t], [a, b], "seconds")
    assert select(problem).chosen == ["a"]
    assert select(problem, budget_bytes=50).chosen == ["b"]
    priced = Problem([t], [a, b], "seconds", storage_per_byte_day=0.05)
    assert select(priced).chosen == ["b"]  # a saves 9 - 5, b saves 8 - 0.5


def test_unstoring_a_table_counts_its_refresh_as_saved():
    reader = Template("r", 1.0, (Option(5.0), Option(1.0, frozenset(["m"]))))
    unstore = Candidate("u", "m as a view", "unstore_table", "m", 1.0, 20.0, 1000.0, PROVEN, store=False)
    problem = Problem([reader], [unstore], "seconds", stored=frozenset(["m"]))
    assert problem.daily_cost() == pytest.approx(1.0)
    assert problem.saving(["u"]) == pytest.approx(20.0 - 4.0)
    assert not problem.facility_shaped


def test_first_option_must_need_nothing():
    with pytest.raises(ValueError):
        Template("t", 1.0, (Option(1.0, frozenset(["v"])),))


def test_rank_metrics_on_known_lists():
    assert bt.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert bt.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert bt.kendall([1, 2, 3], [1, 3, 2]) == pytest.approx(1 / 3)
    assert bt.top_k_precision([5, 4, 3, 2, 1], [1, 5, 4, 3, 2], 2) == pytest.approx(0.5)
    assert bt.sign_agreement([1, -1, 2], [1, 1, -2]) == pytest.approx(1 / 3)
    assert bt.median_ratio([2, 4, 1], [1, 2, -1]) == pytest.approx(2.0)
    assert bt.spearman([1, 1, 1], [1, 2, 3]) is None


def test_score_intervals_contain_the_point_and_are_seeded():
    rng = random.Random(3)
    measured = [rng.uniform(-5, 20) for _ in range(40)]
    predicted = [m + rng.gauss(0, 4) for m in measured]
    one = bt.score(predicted, measured, k=10, resamples=500)
    two = bt.score(predicted, measured, k=10, resamples=500)
    assert one == two
    for name in ("spearman", "kendall", "top_10_precision", "sign_agreement"):
        low, high = one[name]["interval"]
        assert low <= one[name]["value"] <= high
    assert one["spearman"]["value"] > 0.7


def _job(i: int, when: datetime, table: str, slot: int) -> ObservedJob:
    return ObservedJob(job_id=f"j{i}", creation_time=when.isoformat().replace("+00:00", "Z"),
                       referenced_tables=(table,), total_slot_ms=slot, total_bytes_billed=slot)


def test_measured_savings_compare_equal_windows_of_touched_jobs():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    jobs = []
    for day in range(14):
        cost = 100 if day < 7 else 60
        jobs.append(_job(len(jobs), t0 + timedelta(days=day, hours=1), "p.d.a", cost))
        jobs.append(_job(len(jobs), t0 + timedelta(days=day, hours=2), "p.d.other", 500))
    change = bt.Change("c1", t0 + timedelta(days=7), ("p.d.a",))
    touches = lambda job, ch: any(str(r) in ch.nodes for r in job.referenced_tables)  # noqa: E731
    claims = bt.measured_savings([change], jobs, touches, window=timedelta(days=7), unit="slot_ms")
    claim = claims["c1"]
    assert claim.basis == "measured"
    assert claim.before.value == pytest.approx(100) and claim.after.value == pytest.approx(60)
    assert claim.value == pytest.approx(40)
    assert claim.before.n_runs == 7 and claim.after.n_runs == 7


def test_measured_savings_stop_at_a_revert_and_need_jobs():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    jobs = [_job(i, t0 + timedelta(hours=i), "p.d.a", 10) for i in range(48)]
    touches = lambda job, ch: True  # noqa: E731
    reverted = bt.Change("c", t0 + timedelta(hours=24), ("p.d.a",), reverted_at=t0 + timedelta(hours=30))
    claim = bt.measured_savings([reverted], jobs, touches)["c"]
    assert claim.after.n_runs == 6 and claim.before.n_runs == 6
    lonely = bt.Change("d", t0 + timedelta(days=30), ("p.d.a",))
    assert isinstance(bt.measured_savings([lonely], jobs, touches)["d"], str)


def test_calibration_recovers_weights_and_reports_held_out_error():
    rng = random.Random(5)
    samples = []
    for _ in range(80):
        f = {"scan": rng.uniform(1e5, 1e8), "join": rng.uniform(0, 1e7), "query": 1.0}
        truth = 2e-8 * f["scan"] + 5e-8 * f["join"] + 0.003
        samples.append((f, truth * rng.uniform(0.95, 1.05)))
    cal = calibrate(samples, ["scan", "join", "query"])
    weights = dict(zip(cal.model.features, cal.model.weights))
    assert weights["scan"] == pytest.approx(2e-8, rel=0.15)
    assert weights["join"] == pytest.approx(5e-8, rel=0.15)
    assert cal.cross_validated["q_error_p50"] < 1.1
    assert all(w >= 0 for w in cal.model.weights)


def test_fit_keeps_weights_non_negative():
    samples = [({"a": x, "b": 1.0}, max(0.1, 5 - x)) for x in range(1, 5)]
    model = fit(samples, ["a", "b"])
    assert all(w >= 0 for w in model.weights)


def test_fit_drops_an_all_zero_feature():
    samples = [({"a": float(x), "dead": 0.0}, 2.0 * x) for x in range(1, 9)]
    model = fit(samples, ["a", "dead"])
    assert dict(zip(model.features, model.weights))["dead"] == 0.0
    cal = calibrate(samples, ["a", "dead"], folds=4)  # a fold with the zero column must not divide by it
    assert cal.cross_validated["q_error_max"] < 1.01


def test_error_summary_signs():
    summary = error_summary([2.0, 1.0], [1.0, 1.0])
    assert summary["q_error_max"] == pytest.approx(2.0)
    assert summary["median_log10_ratio"] >= 0


def test_pricing_units_and_storage_rate():
    assert Pricing().unit == "bytes_billed"
    assert Pricing("editions").unit == "slot_ms"
    assert Pricing().storage_per_byte_day() is None
    on_demand = Pricing(usd_per_tib=5.0, usd_per_gib_month=0.02)
    assert on_demand.unit == "usd"
    assert on_demand.compute_cost(1024 ** 4, 0) == pytest.approx(5.0)
    assert on_demand.storage_per_byte_day() == pytest.approx(0.02 / 1024 ** 3 / 30.4375)
    bytes_unit = Pricing(usd_per_gib_month=0.02)
    assert bytes_unit.storage_per_byte_day() is None  # storage cannot be weighed against unpriced bytes
    with pytest.raises(ValueError):
        Pricing(usd_per_tib=-1)
