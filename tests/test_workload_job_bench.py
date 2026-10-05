"""Fast checks for the JOB materialization eval's family split and result guard."""

from __future__ import annotations

from kumosql.joinorder.bench.job_materialize import _same_bag, split_families


def test_job_materialization_split_keeps_families_17_to_33_out_of_development():
    files = [(f"{family}a", f"SELECT {family}") for family in (1, 16, 17, 33)]

    development, held_out = split_families(files)

    assert list(development) == ["1a", "16a"]
    assert list(held_out) == ["17a", "33a"]


def test_job_materialization_split_refuses_a_missing_partition():
    try:
        split_families([("1a", "SELECT 1")])
    except ValueError as error:
        assert "both tuning families" in str(error)
    else:
        raise AssertionError("missing holdout must not silently turn into development data")


def test_job_result_recheck_compares_bags_not_incidental_row_order():
    assert _same_bag([(1, "a"), (2, "b"), (1, "a")], [(1, "a"), (1, "a"), (2, "b")])
    assert not _same_bag([(1, "a"), (2, "b")], [(1, "a")])
