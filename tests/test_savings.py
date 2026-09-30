import pytest

from kumosql import savings
from kumosql.savings import CostWindow, Ledger, LedgerError, SavingClaim


def est(value, basis="estimate", unit=savings.DEFAULT_UNIT):
    return SavingClaim(basis, unit, "dry_run_bytes", value=value)


def window(value, runs=5):
    return CostWindow(value, runs, "7d")


def deployed(ledger, cid, estimate=None):
    ledger.accept(cid, "rec-" + cid, ["m"], estimate=estimate)
    ledger.mark_deployed(cid)


def test_suggestions_count_for_nothing():
    s = Ledger().summary()
    assert (s["accepted_changes"], s["validated_savings"], s["pending_estimates"]) == (0, 0, 0)


def test_accepted_undeployed_is_pending_not_validated():
    ledger = Ledger()
    ledger.accept("a", "r", estimate=est(100))
    s = ledger.summary()
    assert s["validated_savings"] == 0 and s["pending_estimates"] == 100 and s["accepted_changes"] == 1


def test_measurement_validates_and_removes_from_pending():
    ledger = Ledger()
    deployed(ledger, "a", est(100))
    ledger.record_measurement("a", window(500), window(420))
    s = ledger.summary()
    assert s["validated_savings"] == 80 and s["pending_estimates"] == 0
    assert ledger.get("a").measured.basis == "measured"


def test_estimate_and_upper_bound_never_total_with_measured():
    ledger = Ledger()
    deployed(ledger, "a", est(100))
    ledger.record_measurement("a", window(10), window(4))
    ledger.accept("b", "r", estimate=est(50, "upper_bound"))
    s = ledger.summary()
    assert s["validated_savings"] == 6 and s["pending_estimates"] == 50


def test_measured_needs_runs_and_window():
    with pytest.raises(LedgerError):
        SavingClaim("measured", before=window(1), after=None)
    with pytest.raises(LedgerError):
        SavingClaim("measured", before=window(1), after=window(1, runs=0))
    with pytest.raises(LedgerError):
        SavingClaim("measured", before=window(1), after=CostWindow(1, 1, ""))


def test_min_runs_threshold():
    ledger = Ledger()
    deployed(ledger, "a", est(9))
    ledger.record_measurement("a", window(10, 2), window(5, 2))
    assert ledger.summary(min_runs=3)["validated_savings"] == 0
    assert ledger.summary(min_runs=3)["pending_estimates"] == 9
    assert ledger.summary(min_runs=2)["validated_savings"] == 5


def test_undeployed_cannot_be_measured_and_revert_removes():
    ledger = Ledger()
    ledger.accept("a", "r", estimate=est(7))
    with pytest.raises(LedgerError):
        ledger.record_measurement("a", window(2), window(1))
    ledger.mark_deployed("a")
    ledger.record_measurement("a", window(2), window(1))
    ledger.revert("a")
    s = ledger.summary()
    assert s["accepted_changes"] == 0 and s["validated_savings"] == 0 and s["pending_estimates"] == 0


def test_regressions_are_kept():
    ledger = Ledger()
    deployed(ledger, "a")
    ledger.record_measurement("a", window(10), window(25))
    s = ledger.summary()
    assert s["validated_savings"] == -15 and s["regressions"] == ["a"]


def test_unit_mismatch_reported_not_summed():
    ledger = Ledger()
    ledger.accept("a", "r", estimate=est(5, unit="slot_ms"))
    s = ledger.summary()
    assert s["pending_estimates"] == 0 and s["other_units"] == ["a"]


def test_unproven_blocked_and_measured_not_an_estimate():
    ledger = Ledger()
    with pytest.raises(LedgerError):
        ledger.accept("a", "r", estimate=est(5), evidence="unproven")
    with pytest.raises(LedgerError):
        ledger.accept("a", "r", estimate=SavingClaim("measured", before=window(2), after=window(1)))


def test_duplicate_accept_rejected():
    ledger = Ledger()
    ledger.accept("a", "r", estimate=est(1))
    with pytest.raises(LedgerError):
        ledger.accept("a", "r")


def test_persistence_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    ledger = Ledger()
    deployed(ledger, "a", est(100))
    ledger.record_measurement("a", window(9), window(3), caveats=["single window"])
    savings.save(ledger)
    again = savings.load()
    assert again.summary() == ledger.summary()
    assert again.get("a").measured.caveats == ("single window",)
    assert savings.claim_json(again.get("a").measured) == {"value": 6, "basis": "measured"}
