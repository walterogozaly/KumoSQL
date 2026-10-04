"""Scripts that modify an existing table are compared by the rows they write.

DuckDB answers UPDATE, DELETE, MERGE and TRUNCATE with an affected-row
count, so two updates writing different values used to compare equal. The
written table is now read back and compared; a modifying statement whose
table cannot be compared faithfully is declined, never compared by count.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from kumosql.result_equivalence import (
    ExecutionError,
    ResultEquivalenceStatus,
    check_result_equivalence,
    execute_on_dataset,
    generate_synthetic_dataset,
    prepare_statements,
)


SCHEMA = {"t": {"a": "INT64", "b": "INT64"}, "s": {"a": "INT64", "b": "INT64"}}
SEEDS = [0, 1]


def check(left, right):
    return check_result_equivalence(left, right, SCHEMA, seeds=SEEDS)


def test_updates_writing_different_values_differ():
    result = check("UPDATE t SET a=1 WHERE TRUE", "UPDATE t SET a=2 WHERE TRUE")
    assert result.status is ResultEquivalenceStatus.DIFFERENT
    assert not result.equivalent
    assert result.left_output.columns == ("a", "b")


def test_updates_writing_the_same_rows_are_equivalent():
    assert check("UPDATE t SET a=1 WHERE TRUE", "UPDATE t SET a=1 WHERE 1 = 1").equivalent
    # Qualifiers by the table's own name or an alias still bind after renaming.
    assert check(
        "UPDATE t SET a = a + 1 WHERE t.b > 0", "UPDATE t AS x SET a = 1 + x.a WHERE 0 < x.b"
    ).equivalent


def test_the_written_table_is_returned_not_the_count():
    dataset = generate_synthetic_dataset(SCHEMA, seed=0)
    output, statements = execute_on_dataset("UPDATE t SET a = 7 WHERE TRUE", SCHEMA, dataset)
    assert output.columns == ("a", "b")
    assert [row[0] for row in output.rows] == [7] * len(dataset.tables["t"].rows)
    assert statements[0].startswith('CREATE TABLE "__eqv_run_target_001" AS SELECT * FROM "src__t"')


def test_deletes_are_compared_by_the_rows_left():
    assert check("DELETE FROM t WHERE a > 0", "DELETE t WHERE NOT (a <= 0)").equivalent
    assert check("DELETE FROM t WHERE a > 0", "DELETE FROM t WHERE a >= 0").status is (
        ResultEquivalenceStatus.DIFFERENT
    )
    assert check("TRUNCATE TABLE t", "DELETE FROM t WHERE TRUE").equivalent


def test_a_final_query_after_an_update_is_still_the_result():
    assert not check(
        "UPDATE t SET a = 1 WHERE TRUE; SELECT * FROM t", "UPDATE t SET a = 2 WHERE TRUE; SELECT * FROM t"
    ).equivalent
    assert check(
        "UPDATE t SET a = 1 WHERE TRUE; SELECT COUNT(*) FROM t",
        "UPDATE t SET a = 2 WHERE TRUE; SELECT COUNT(*) FROM t",
    ).equivalent


@pytest.mark.parametrize(
    "sql",
    [
        # BigQuery fails when a target row matches several source rows; DuckDB picks one.
        "MERGE t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET b = s.b",
        "MERGE t USING s ON t.a = s.a WHEN NOT MATCHED THEN INSERT (a, b) VALUES (s.a, s.b)",
        "UPDATE t SET b = s.b FROM s WHERE t.a = s.a",
        # BigQuery rejects UPDATE and DELETE without WHERE.
        "UPDATE t SET a = 1",
        "DELETE FROM t",
    ],
)
def test_writes_that_cannot_be_compared_faithfully_are_declined(sql):
    with pytest.raises(ExecutionError, match="not supported"):
        prepare_statements(sql, SCHEMA, run_tag="t")
    result = check(sql, sql)
    assert result.status is ResultEquivalenceStatus.ERROR
    assert not result.equivalent


def test_writes_to_unknown_tables_fail_closed():
    with pytest.raises(ExecutionError, match="neither created by the script nor in the synthetic schema"):
        prepare_statements("UPDATE u SET a = 1 WHERE TRUE", SCHEMA, run_tag="t")
