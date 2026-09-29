"""Full generic fixture verification.

Run with:

    pytest -m slow tests/test_workbook_fixture.py

The checked-in CSV is a sanitized generic copy of the private workbook
export. Set BQ_SQL_TOOLS_TEST_CSV to run the same check against another CSV.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from bq_sql_tools import lift_subqueries


DEFAULT_CSV = Path(__file__).parent / "fixtures" / "generic_sql_workbooks.csv"


@pytest.mark.slow
def test_every_workbook_lifts_all_relational_subqueries():
    csv_path = Path(os.environ.get("BQ_SQL_TOOLS_TEST_CSV", DEFAULT_CSV))
    if not csv_path.exists():
        pytest.skip(f"External fixture not found: {csv_path}")

    csv.field_size_limit(100_000_000)
    failures: list[str] = []
    processed = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            processed += 1
            workbook_id = row.get("record_id", str(processed))
            source = row.get("sql_text") or ""
            result = lift_subqueries(source)
            if not result.success:
                details = "; ".join(
                    f"{d.code}: {d.message}" for d in result.diagnostics
                )
                failures.append(
                    f"record_id={workbook_id} remaining={result.remaining_inline_subqueries} {details}"
                )

    assert processed == 621
    assert not failures, "\n".join(failures[:50])
