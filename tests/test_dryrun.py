import json

import pytest

from kumosql import check_rewrite, dry_run, fetch_table_schemas
from kumosql.dryrun import Field, schema_differences


class FakeBigQuery:
    """Stands in for the jobs.insert endpoint; maps query text to responses."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, url, headers, body):
        payload = json.loads(body)
        self.requests.append((url, headers, payload))
        return self.responses[payload["configuration"]["query"]["query"]]


def ok(fields, processed="1024", tables=()):
    return 200, {
        "statistics": {
            "totalBytesProcessed": processed,
            "query": {
                "schema": {"fields": fields},
                "referencedTables": [
                    {"projectId": p, "datasetId": d, "tableId": t} for p, d, t in tables
                ],
            },
        }
    }


def failure(message, reason="invalidQuery", location="query"):
    return 400, {
        "error": {
            "code": 400,
            "message": message,
            "errors": [{"message": message, "reason": reason, "location": location}],
        }
    }


ID_TOTAL = [{"name": "id", "type": "INTEGER"}, {"name": "total", "type": "FLOAT"}]


def test_dry_run_sends_a_dry_run_job_and_parses_schema():
    fake = FakeBigQuery({"SELECT 1": ok(ID_TOTAL, tables=[("p", "d", "t")])})

    result = dry_run("SELECT 1", "billing-project", location="EU", token="t0k", transport=fake)

    url, headers, payload = fake.requests[0]
    assert url.endswith("/projects/billing-project/jobs")
    assert headers["Authorization"] == "Bearer t0k"
    assert payload["configuration"]["dryRun"] is True
    assert payload["configuration"]["query"]["useLegacySql"] is False
    assert payload["jobReference"] == {"location": "EU"}
    assert result.ok
    assert result.schema == (Field("id", "INT64"), Field("total", "FLOAT64"))
    assert result.total_bytes_processed == 1024
    assert result.referenced_tables == ("p.d.t",)


def test_dry_run_reports_bigquery_errors():
    fake = FakeBigQuery({"SELECT nope": failure("Unrecognized name: nope at [1:8]")})

    result = dry_run("SELECT nope", "p", token="t", transport=fake)

    assert not result.ok
    assert result.error_reason == "invalidQuery"
    assert "Unrecognized name" in result.error_message


def test_missing_credentials_are_a_clear_error(monkeypatch):
    for name in ("BQ_ACCESS_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="no BigQuery credentials"):
        dry_run("SELECT 1", "p", transport=FakeBigQuery({}))


def test_check_rewrite_accepts_same_schema_and_reports_bytes_saved():
    fake = FakeBigQuery({"old": ok(ID_TOTAL, "5000"), "new": ok(ID_TOTAL, "3000")})

    check = check_rewrite("old", "new", "p", token="t", transport=fake)

    assert check.ok
    assert check.bytes_delta == -2000


def test_check_rewrite_rejects_failing_rewrite():
    fake = FakeBigQuery({"old": ok(ID_TOTAL), "new": failure("Syntax error")})

    check = check_rewrite("old", "new", "p", token="t", transport=fake)

    assert not check.ok
    assert "rewritten query fails" in check.reason


def test_check_rewrite_rejects_schema_change():
    changed = [{"name": "id", "type": "INTEGER"}, {"name": "total", "type": "NUMERIC"}]
    fake = FakeBigQuery({"old": ok(ID_TOTAL), "new": ok(changed)})

    check = check_rewrite("old", "new", "p", token="t", transport=fake)

    assert not check.ok
    assert check.schema_differences == ("total: total FLOAT64 != total NUMERIC",)


def test_schema_differences_check_order_mode_and_nested_fields():
    struct = lambda inner_type, mode="NULLABLE": (
        Field("s", "STRUCT", mode, (Field("a", "INT64"), Field("b", inner_type))),
    )

    assert schema_differences(struct("STRING"), struct("STRING")) == ()
    assert schema_differences(struct("STRING"), struct("BYTES")) == ("s.b: b STRING != b BYTES",)
    assert schema_differences(struct("STRING"), struct("STRING", "REPEATED"))
    assert schema_differences(
        (Field("a", "INT64"), Field("b", "INT64")), (Field("b", "INT64"), Field("a", "INT64"))
    )
    assert schema_differences((Field("ID", "INT64"),), (Field("id", "INT64"),)) == ()


def test_fetch_table_schemas_feeds_pipeline_source_schema():
    fake = FakeBigQuery(
        {
            "SELECT * FROM `p.raw.orders`": ok(ID_TOTAL),
            "SELECT * FROM `p.raw.missing`": failure("Not found: Table p:raw.missing", "notFound"),
        }
    )

    schemas, errors = fetch_table_schemas(["p.raw.orders", "p.raw.missing"], "p", token="t", transport=fake)

    assert schemas == {"p.raw.orders": {"id": "INT64", "total": "FLOAT64"}}
    assert "Not found" in errors["p.raw.missing"]
