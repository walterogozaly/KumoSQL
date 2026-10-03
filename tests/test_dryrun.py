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
    # Credentials saved on the machine (gcloud application-default login, a metadata server) would be found
    # even with the variables cleared, so discovery is made to find nothing.
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        pass  # without google-auth the error names the missing package, which is the same clear error
    else:
        def no_saved_credentials(*args, **kwargs):
            raise DefaultCredentialsError("no saved credentials")

        monkeypatch.setattr(google.auth, "default", no_saved_credentials)

    with pytest.raises(RuntimeError, match="no BigQuery credentials"):
        dry_run("SELECT 1", "p", transport=FakeBigQuery({}))


def test_check_rewrite_reports_matching_plans_and_estimated_bytes_delta():
    fake = FakeBigQuery({"SELECT 1": ok(ID_TOTAL, "5000"), "SELECT 2": ok(ID_TOTAL, "3000")})

    check = check_rewrite("SELECT 1", "SELECT 2", "p", token="t", transport=fake)

    assert check.planned_same_schema
    assert check.ok
    assert check.original_planned
    assert check.rewritten_planned
    assert check.schema_matches is True
    assert check.estimated_bytes_delta == -2000


def test_check_rewrite_rejects_failing_rewrite():
    fake = FakeBigQuery(
        {"SELECT 1": ok(ID_TOTAL), "SELECT missing FROM source": failure("Unknown column")}
    )

    check = check_rewrite(
        "SELECT 1", "SELECT missing FROM source", "p", token="t", transport=fake
    )

    assert not check.ok
    assert "rewritten SQL could not be planned" in check.reason
    assert check.original_planned
    assert not check.rewritten_planned
    assert check.schema_matches is None


def test_check_rewrite_rejects_schema_change():
    changed = [{"name": "id", "type": "INTEGER"}, {"name": "total", "type": "NUMERIC"}]
    fake = FakeBigQuery({"SELECT 1": ok(ID_TOTAL), "SELECT 2": ok(changed)})

    check = check_rewrite("SELECT 1", "SELECT 2", "p", token="t", transport=fake)

    assert not check.ok
    assert not check.planned_same_schema
    assert check.schema_matches is False
    assert check.schema_differences == ("total: total FLOAT64 != total NUMERIC",)


def test_check_rewrite_distinguishes_original_plan_failure():
    fake = FakeBigQuery({"SELECT * FROM missing": failure("Unknown source"), "SELECT 2": ok(ID_TOTAL)})

    check = check_rewrite("SELECT * FROM missing", "SELECT 2", "p", token="t", transport=fake)

    assert not check.planned_same_schema
    assert "original SQL could not be planned" in check.reason
    assert not check.original_planned
    assert check.rewritten_planned
    assert check.schema_matches is None


@pytest.mark.parametrize(
    ("original", "rewritten"),
    [
        ("UPDATE source SET value = 1", "UPDATE source SET value = 2"),
        ("SELECT 1; SELECT 2", "SELECT 1; SELECT 3"),
        ("CREATE TABLE target AS SELECT 1", "CREATE TABLE target AS SELECT 2"),
    ],
)
def test_check_rewrite_skips_nonselect_and_multistatement_inputs(original, rewritten):
    fake = FakeBigQuery({})

    check = check_rewrite(original, rewritten, "p", token="t", transport=fake)

    assert check.outcome == "not_run"
    assert check.planned_same_schema is None
    assert check.original_planned is None
    assert check.rewritten_planned is None
    assert check.schema_matches is None
    assert "single SELECT statements" in check.reason
    assert fake.requests == []


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


def test_a_planned_response_without_a_schema_is_not_a_schema_match():
    no_schema = (200, {"statistics": {"query": {}}})
    fake = FakeBigQuery({"SELECT 1 AS x": no_schema, "SELECT 'changed' AS y": no_schema})

    result = dry_run("SELECT 1 AS x", "p", token="t", transport=fake)
    assert result.ok and not result.schema_observed

    check = check_rewrite("SELECT 1 AS x", "SELECT 'changed' AS y", "p", token="t", transport=fake)
    assert check.planned_same_schema is None
    assert check.outcome == "not_run"
    assert "no output schema" in check.reason

    schemas, errors = fetch_table_schemas(["SELECT 1 AS x"], "p", token="t", transport=FakeBigQuery(
        {"SELECT * FROM `SELECT 1 AS x`": no_schema}
    ))
    assert schemas == {} and errors == {"SELECT 1 AS x": "the dry run returned no schema"}


def test_an_observed_empty_field_list_is_still_observed():
    fake = FakeBigQuery({"SELECT 1": ok([])})
    assert dry_run("SELECT 1", "p", token="t", transport=fake).schema_observed
