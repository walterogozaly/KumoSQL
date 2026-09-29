"""Full generic fixture verification.

Run with:

    pytest -m slow tests/test_workbook_fixture.py

The checked-in JSON is a sanitized generic copy of the private workbook
export. Set BQ_SQL_TOOLS_TEST_FIXTURE to run the same check against another
CSV or JSON fixture.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from bq_sql_tools import lift_subqueries


DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "generic_sql_workbooks.json"


@pytest.mark.slow
def test_every_workbook_lifts_all_relational_subqueries():
    fixture_path = Path(
        os.environ.get(
            "BQ_SQL_TOOLS_TEST_FIXTURE",
            os.environ.get("BQ_SQL_TOOLS_TEST_CSV", DEFAULT_FIXTURE),
        )
    )
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
    for row in rows:
        processed += 1
        workbook_id = row.get("id", row.get("record_id", str(processed)))
        source = row.get("sql_text") or ""
        result = lift_subqueries(source)
        if not result.success:
            details = "; ".join(
                f"{d.code}: {d.message}" for d in result.diagnostics
            )
            failures.append(
                f"record_id={workbook_id} remaining={result.remaining_inline_subqueries} {details}"
            )

    assert processed == 533
    assert not failures, "\n".join(failures[:50])
