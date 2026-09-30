from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "sql_subquery_samples.json"


def test_authored_fixture_has_small_minimal_shape():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert len(rows) == 32
    assert len(rows) < 50
    assert all(set(row) == {"id", "sql_text"} for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)
    assert len({row["sql_text"] for row in rows}) == len(rows)
    assert all(
        isinstance(row["sql_text"], str) and row["sql_text"]
        for row in rows
    )
