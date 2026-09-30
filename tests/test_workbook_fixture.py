"""Full generic fixture verification.

Run with:

    pytest -m slow tests/test_workbook_fixture.py

The checked-in JSON contains hand-written sample queries. Set
KUMOSQL_TEST_FIXTURE to run the same check against another CSV or JSON fixture.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from kumosql import lift_subqueries


DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "sql_subquery_samples.json"


@pytest.mark.slow
def test_every_fixture_query_lifts_all_relational_subqueries():
    fixture_override = os.environ.get("KUMOSQL_TEST_FIXTURE")
    fixture_path = Path(fixture_override or DEFAULT_FIXTURE)
    if not fixture_path.exists():
        pytest.skip(f"External fixture not found: {fixture_path}")

    if fixture_path.suffix.lower() == ".json":
        rows = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    else:
        csv.field_size_limit(100_000_000)
        with fixture_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    failures: list[str] = []
    processed = 0
    lifted = 0
    sqlx_queries = 0
    for row in rows:
        processed += 1
        workbook_id = row.get("id", row.get("record_id", str(processed)))
        source = row.get("sql_text") or ""
        sqlx_queries += int("config {" in source)
        result = lift_subqueries(source)
        lifted += result.lifted_subqueries
        if not result.success:
            details = "; ".join(
                f"{d.code}: {d.message}" for d in result.diagnostics
            )
            failures.append(
                f"record_id={workbook_id} remaining={result.remaining_inline_subqueries} {details}"
            )

    assert processed == len(rows)
    if not fixture_override:
        assert processed == 32
        assert processed < 50
        assert lifted >= 32
        assert sqlx_queries >= 5
    assert not failures, "\n".join(failures[:50])
