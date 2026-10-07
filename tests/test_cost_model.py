"""Focused tests for cost model calibration."""

import pytest

from kumosql.cost_model import calibrate


def test_calibration_handles_a_feature_missing_from_a_training_fold():
    samples = [
        ({"rows": float(index + 1), "rare": float(index == 0)}, 2.0 * (index + 1))
        for index in range(10)
    ]

    result = calibrate(samples, ("rows", "rare"), folds=5, seed=11)

    assert result.model.weights == pytest.approx((2.0, 0.0))
    assert result.cross_validated["jobs"] == len(samples)
    assert result.cross_validated["q_error_max"] == pytest.approx(1.0)
