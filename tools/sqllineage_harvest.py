"""Rebuild ``tests/fixtures/sqllineage/cases.json`` from SQLLineage's own tests.

SQLLineage (https://github.com/reata/sqllineage, MIT, Copyright (c) 2019 Reata) states each test as a SQL string plus the
table and column lineage it must produce, through two helpers in ``tests/helpers.py``. This is a pytest plugin that
swaps those helpers for recorders, so running SQLLineage's unmodified tests collects every (SQL, expected lineage) pair
instead of checking them. Nothing here changes an expectation.

    git clone https://github.com/reata/sqllineage /tmp/sqllineage && pip install sqllineage
    cd /tmp/sqllineage
    PYTHONPATH=<this repo>/tools python -m pytest tests/sql -p sqllineage_harvest -q

The plugin writes the fixture when the run ends. Variants that SQLLineage runs against a second metadata provider
(SQLAlchemy) repeat the same SQL without schema information, so they are dropped.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

import pytest

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sqllineage" / "cases.json"
SOURCE = "https://github.com/reata/sqllineage (MIT, Copyright (c) 2019 Reata), tests/sql; harvested by tools/sqllineage_harvest.py"

sys.path.insert(0, str(Path.cwd()))
import tests.helpers as helpers  # noqa: E402  (SQLLineage's own test helpers)

recorded: list[dict] = []
current = {"name": ""}


def _schemas(provider) -> object:
    if provider is None:
        return None
    metadata = getattr(provider, "metadata", None)  # the dummy provider keeps ``{table: [columns]}``
    return metadata if isinstance(metadata, dict) else {"unsupported": True}


def _table(sql, source_tables=None, target_tables=None, dialect="ansi", **_):
    recorded.append(
        {
            "kind": "table",
            "test": current["name"],
            "sql": sql,
            "dialect": dialect,
            "sources": sorted(str(t) for t in (source_tables or [])),
            "targets": sorted(str(t) for t in (target_tables or [])),
        }
    )


def _column(sql, column_lineages=None, dialect="ansi", metadata_provider=None, **_):
    edges = [[[s.qualifier, s.column], [t.qualifier, t.column]] for s, t in (column_lineages or [])]
    row = {"kind": "column", "test": current["name"], "sql": sql, "dialect": dialect, "edges": edges}
    schemas = _schemas(metadata_provider)
    if schemas is not None:
        row["provider"] = schemas
    recorded.append(row)


helpers.assert_table_lineage_equal = _table
helpers.assert_column_lineage_equal = _column


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    current["name"] = item.nodeid.split("tests/sql/")[-1]


def pytest_unconfigure(config):
    cases, seen, schema_sets = [], set(), {}
    for item in recorded:
        provider = item.get("provider")
        if isinstance(provider, dict) and provider.get("unsupported"):
            continue  # the SQLAlchemy-backed variant repeats a case without its schema
        name = item["test"].split("::", 1)
        row = {
            "id": name[0].replace(".py", "") + "::" + name[1].split("[")[0],
            "kind": item["kind"],
            "dialect": item["dialect"],
            "sql": item["sql"].strip("\n"),
        }
        if item["kind"] == "column":
            row["edges"] = sorted(item["edges"], key=json.dumps)
        else:
            row["sources"], row["targets"] = item["sources"], item["targets"]
        if provider:
            key = json.dumps(provider, sort_keys=True)
            row["schemas"] = schema_sets.setdefault(key, f"set{len(schema_sets) + 1}")
        fingerprint = json.dumps(row, sort_keys=True)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        cases.append(row)
    counts: Counter = Counter()
    for row in cases:
        counts[row["id"]] += 1
        row["id"] = f"{row['id']}#{counts[row['id']]}"
    OUT.write_text(
        json.dumps({"source": SOURCE, "schemas": {v: json.loads(k) for k, v in schema_sets.items()}, "cases": cases}, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    print(f"\nharvested {len(cases)} cases into {OUT}")
