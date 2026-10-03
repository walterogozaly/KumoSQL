"""Synthetic data does not depend on the order a schema lists its columns (determinism audit, DR-03).

``generate_synthetic_dataset`` drew values in the schema's column order, so ``{"a", "b"}`` and ``{"b", "a"}`` gave
different rows for the same seed and ``a = 0`` versus ``FALSE`` came out ``different`` for one listing and
``equivalent`` for the other.
"""

import itertools

import pytest

from kumosql.result_equivalence import ResultEquivalenceStatus, check_result_equivalence, generate_synthetic_dataset

COLUMNS = {"a": "INT64", "b": "INT64", "s": "STRING", "d": "DATE", "x": "FLOAT64"}


def _by_name(dataset, table="t"):
    names = [name for name, _ in dataset.tables[table].columns]
    return [dict(zip(names, row)) for row in dataset.tables[table].rows]


@pytest.mark.parametrize("seed", [1, 2, 7])
def test_every_column_order_gives_the_same_values_per_column(seed):
    expected = None
    for order in itertools.permutations(COLUMNS):
        schema = {"t": {name: COLUMNS[name] for name in order}, "u": {"k": "INT64"}}
        dataset = generate_synthetic_dataset(schema, seed=seed, rows_per_table=4)
        assert [name for name, _ in dataset.tables["t"].columns] == list(order)  # declared order kept for SELECT *
        rows = (_by_name(dataset), _by_name(dataset, "u"))
        expected = expected or rows
        assert rows == expected


def test_the_audit_example_has_one_verdict():
    left, right = "SELECT a FROM t WHERE a = 0", "SELECT a FROM t WHERE FALSE"
    verdicts = {
        check_result_equivalence(left, right, schema, seeds=[1], rows_per_table=1, null_rate=0).status
        for schema in ({"t": {"a": "INT64", "b": "INT64"}}, {"t": {"b": "INT64", "a": "INT64"}})
    }
    assert len(verdicts) == 1 and verdicts <= set(ResultEquivalenceStatus)
