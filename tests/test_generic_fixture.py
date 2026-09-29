from __future__ import annotations

import csv
import re
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "generic_sql_workbooks.csv"
FIELDS = [
    "record_id",
    "record_name",
    "sql_text",
    "created_by",
    "schedule",
    "disabled",
    "folder_path",
    "normalized_sql",
]


def test_generic_fixture_preserves_shape_and_redacts_source_metadata():
    csv.field_size_limit(100_000_000)
    with FIXTURE.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    assert reader.fieldnames == FIELDS
    assert len(rows) == 621
    assert len({row["record_id"] for row in rows}) == 621
    assert all(row["record_name"].startswith("Generic SQL Workbook ") for row in rows)
    assert all(
        re.sub(r"\s+", " ", row["sql_text"]).strip().lower()
        == row["normalized_sql"]
        for row in rows
    )

    all_text = "\n".join(",".join(row.values()) for row in rows)
    assert not re.search(r"https?://", all_text, re.IGNORECASE)
    assert not re.search(r"\b[A-Za-z0-9._%+-]+@(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", all_text)
    for path in re.findall(r"`([^`]+)`", all_text):
        if path.count(".") >= 1:
            assert path.startswith(("generic_project.", "ops_dataset_"))
