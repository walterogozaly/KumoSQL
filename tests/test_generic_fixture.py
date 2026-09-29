from __future__ import annotations

import json
import re
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "generic_sql_workbooks.json"


def test_generic_fixture_preserves_shape_and_redacts_source_metadata():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert len(rows) == 533
    assert all(set(row) == {"id", "sql_text"} for row in rows)
    assert len({row["id"] for row in rows}) == 533
    assert all(
        isinstance(row["sql_text"], str) and row["sql_text"]
        for row in rows
    )

    all_text = "\n".join(row["sql_text"] for row in rows)
    assert not re.search(r"https?://", all_text, re.IGNORECASE)
    assert not re.search(r"\b[A-Za-z0-9._%+-]+@(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", all_text)
    for path in re.findall(r"`([^`]+)`", all_text):
        if path.count(".") >= 1:
            assert path.startswith(("generic_project.", "ops_dataset_"))
