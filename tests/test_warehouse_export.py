"""The read-only warehouse export, tested against a fake BigQuery REST API (no credentials, no network)."""

import json

import pytest

from kumosql import dryrun, scope_queries, warehouse_export as export
from kumosql.advisor import load_sizes
from kumosql.costs import load_jobs
from kumosql.scope_queries import QueryError

PROJECT = "example-project"


class FakeBigQuery:
    """Records every request; answers dry runs and INFORMATION_SCHEMA reads."""

    def __init__(self, *, tables=None, jobs=None, estimate=5_000, fail_columns=(), pages=1):
        self.tables = tables if tables is not None else {("core", "events"): (1000, 64_000, {"id": ("INTEGER", 8000), "name": ("STRING", 40_000)})}
        self.jobs = jobs if jobs is not None else [
            ["j1", "2026-09-01T06:00:00Z", PROJECT, "a@example.test", "SELECT", '{"project_id":"example-project","dataset_id":"core","table_id":"events_daily"}',
             '[{"project_id":"example-project","dataset_id":"core","table_id":"events"}]', "64000", "10485760", "1200", "false", None, "SELECT 1"],
            ["j2", "2026-09-01T07:00:00Z", PROJECT, "b@example.test", "SELECT", None, "[]", "64000", None, "900", "true", None, "SELECT 2"],
        ]
        self.estimate = estimate
        self.fail_columns = set(fail_columns)
        self.pages = pages
        self.dry_runs: list[str] = []
        self.queries: list[dict] = []
        self.urls: list[str] = []

    # dry runs: POST .../jobs
    def transport(self, url, headers, body):
        self.urls.append(url)
        assert url.endswith("/jobs"), url
        configuration = json.loads(body)["configuration"]
        assert configuration["dryRun"] is True
        sql = configuration["query"]["query"]
        self.dry_runs.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            return 200, {"statistics": {"totalBytesProcessed": str(self.estimate), "query": {"schema": {"fields": []}}}}
        for (dataset, table), (_, _, columns) in self.tables.items():
            if f"`{PROJECT}.{dataset}.{table}`" in sql:
                if sql.startswith("SELECT *"):
                    fields = [{"name": c, "type": t} for c, (t, _) in columns.items()]
                    return 200, {"statistics": {"totalBytesProcessed": str(sum(b for _, b in columns.values())), "query": {"schema": {"fields": fields}}}}
                column = sql.split("`")[1]
                if column in self.fail_columns:
                    return 400, {"error": {"message": f"cannot read {column}"}}
                kind, size = columns[column]
                return 200, {"statistics": {"totalBytesProcessed": str(size), "query": {"schema": {"fields": [{"name": column, "type": kind}]}}}}
        return 404, {"error": {"message": "Not found"}}

    # reads: POST .../queries
    def post(self, url, headers, body):
        self.urls.append(url)
        assert url.endswith("/queries"), url
        request = json.loads(body)
        self.queries.append(request)
        sql = request["query"]
        if "TABLE_STORAGE" in sql:
            fields = [("table_schema", "STRING"), ("table_name", "STRING"), ("total_rows", "INTEGER"), ("total_logical_bytes", "INTEGER")]
            rows = [[d, t, str(r), str(b)] for (d, t), (r, b, _) in self.tables.items()]
        else:
            names = ["job_id", "creation_time", "project_id", "user_email", "statement_type", "destination_table", "referenced_tables",
                     "total_bytes_processed", "total_bytes_billed", "total_slot_ms", "cache_hit", "parent_job_id", "query"]
            kinds = {"total_bytes_processed": "INTEGER", "total_bytes_billed": "INTEGER", "total_slot_ms": "INTEGER", "cache_hit": "BOOLEAN"}
            fields = [(n, kinds.get(n, "STRING")) for n in names]
            rows = self.jobs
        payload = {"jobComplete": True, "jobReference": {"jobId": "q1", "location": "US"},
                   "schema": {"fields": [{"name": n, "type": t} for n, t in fields]},
                   "totalBytesBilled": "10485760"}
        if self.pages > 1:
            half = max(1, len(rows) // 2)
            payload["rows"] = [{"f": [{"v": v} for v in row]} for row in rows[:half]]
            payload["pageToken"] = "next"
            self.rest = rows[half:]
        else:
            payload["rows"] = [{"f": [{"v": v} for v in row]} for row in rows]
        return 200, payload

    def get(self, url, headers):
        self.urls.append(url)
        assert "pageToken=next" in url
        return 200, {"jobComplete": True, "rows": [{"f": [{"v": v} for v in row]} for row in self.rest],
                     "schema": {"fields": []}, "totalBytesBilled": "10485760"}


def parse(*more):
    return export.build_parser().parse_args(["--project", PROJECT, "--region", "us", *map(str, more)])


def execute(fake, args, **kw):
    out, err = _Sink(), _Sink()
    code = export.run(args, token="fake-token", transport=fake.transport, post=fake.post, get=fake.get, out=out, err=err, **kw)
    return code, out.text, err.text


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, value):
        self.text += value

    def flush(self):
        pass


# ------------------------------------------------------------------------ SQL


def test_generated_sql_is_one_read_only_select_scoped_to_the_given_project():
    queries = [
        export.jobs_sql(PROJECT, "us", days=7),
        export.jobs_sql(PROJECT, "region-eu", query_text=False, user_email=False),
        export.storage_sql(PROJECT, "us-east1", datasets=("core",), tables=("core.events",)),
        export.column_probe_sql(PROJECT, "core", "events"),
        export.column_probe_sql(PROJECT, "core", "events", "id"),
    ]
    for sql in queries:
        scope_queries.validate_query(sql)  # exactly one read-only SELECT
        assert PROJECT in sql
    assert "`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in queries[0]
    assert "INTERVAL 7 DAY" in queries[0] and "error_result IS NULL" in queries[0]
    assert "CAST(NULL AS STRING) AS query" in queries[1] and "CAST(NULL AS STRING) AS user_email" in queries[1]
    assert "`region-us-east1`.INFORMATION_SCHEMA.TABLE_STORAGE" in queries[2] and "deleted = FALSE" in queries[2]


@pytest.mark.parametrize("call", [
    lambda: export.region_qualifier("us; DROP TABLE x"),
    lambda: export.jobs_sql("proj'; DROP", "us"),
    lambda: export.jobs_sql(PROJECT, "us", days=0),
    lambda: export.storage_sql(PROJECT, "us", datasets=("a'b",)),
    lambda: export.storage_sql(PROJECT, "us", tables=("no_dot",)),
    lambda: export.column_probe_sql(PROJECT, "core", "ev`ents"),
    lambda: export.column_probe_sql(PROJECT, "core", "events", "a`b"),
])
def test_unsafe_names_are_refused_before_any_sql_is_built(call):
    with pytest.raises(ValueError):
        call()


# --------------------------------------------------------------------- dry run


def test_dry_run_prints_the_sql_and_touches_nothing(monkeypatch, tmp_path, capsys):
    def forbidden(*a, **k):
        raise AssertionError("--dry-run must not use credentials or the network")

    monkeypatch.setattr(dryrun, "access_token", forbidden)
    monkeypatch.setattr(dryrun, "dry_run", forbidden)
    monkeypatch.setattr(scope_queries, "_post", forbidden)
    monkeypatch.setattr(scope_queries, "_get", forbidden)
    jobs, sizes = tmp_path / "jobs.json", tmp_path / "sizes.json"
    assert export.main(["--project", PROJECT, "--region", "us", "--jobs-out", str(jobs), "--sizes-out", str(sizes), "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "JOBS_BY_PROJECT" in text and "TABLE_STORAGE" in text
    assert "SELECT * FROM `example-project.DATASET.TABLE`" in text and "free BigQuery dry runs" in text
    assert "nothing was sent" in text
    assert not jobs.exists() and not sizes.exists()


def test_the_project_is_always_explicit():
    with pytest.raises(SystemExit):
        export.build_parser().parse_args(["--region", "us", "--dry-run"])
    with pytest.raises(SystemExit):
        export.build_parser().parse_args(["--project", PROJECT, "--dry-run"])


def test_bad_arguments_exit_2_without_a_traceback(capsys):
    assert export.main(["--project", PROJECT, "--region", "us"]) == 2  # nothing to write
    assert "--jobs-out" in capsys.readouterr().err
    assert export.main(["--project", "bad project", "--region", "us", "--jobs-out", "x", "--dry-run"]) == 2
    assert export.main(["--project", PROJECT, "--region", "u s", "--jobs-out", "x", "--dry-run"]) == 2


# ------------------------------------------------------------------- full run


def test_exports_jobs_and_sizes_that_the_advisor_loads(tmp_path):
    fake = FakeBigQuery()
    args = parse("--jobs-out", tmp_path / "jobs.json", "--sizes-out", tmp_path / "sizes.json")
    code, _, err = execute(fake, args)
    assert code == 0
    jobs = load_jobs(tmp_path / "jobs.json")
    assert [j.job_id for j in jobs] == ["j1", "j2"]
    assert jobs[0].total_bytes_billed == 10_485_760 and jobs[0].total_slot_ms == 1200 and jobs[0].cache_hit is False
    assert jobs[1].cache_hit is True and jobs[1].total_bytes_billed is None  # unmeasured stays unmeasured, not zero
    assert jobs[0].referenced_tables and jobs[0].destination_table
    sizes = load_sizes(tmp_path / "sizes.json")
    events = sizes[f"{PROJECT}.core.events"]
    assert events.rows == 1000 and events.bytes == 64_000
    assert events.columns == {"id": 8000.0, "name": 40_000.0} and events.types == {"id": "INT64", "name": "STRING"}
    assert "2 written" in err or "jobs: 2" in err
    # 1 schema probe + 2 column probes for the one table, and 2 estimates for the INFORMATION_SCHEMA reads
    assert len(fake.dry_runs) == 2 + 3
    assert len(fake.queries) == 2


def test_only_information_schema_is_read_and_every_statement_is_read_only(tmp_path):
    fake = FakeBigQuery()
    execute(fake, parse("--jobs-out", tmp_path / "j.json", "--sizes-out", tmp_path / "s.json"))
    for sql in fake.dry_runs + [q["query"] for q in fake.queries]:
        scope_queries.validate_query(sql)
        assert sql.lstrip().upper().startswith("SELECT")
    assert all("INFORMATION_SCHEMA" in q["query"] for q in fake.queries)
    assert all(q["maximumBytesBilled"] == str(scope_queries.DEFAULT_MAX_BYTES_BILLED) for q in fake.queries)
    assert all(q["labels"] == {"kumosql": "warehouse_export"} for q in fake.queries)
    # the only endpoints used: dry-run job inserts and query reads in the given project
    assert all(f"/projects/{PROJECT}/" in url for url in fake.urls)
    assert all(url.endswith(("/jobs", "/queries")) for url in fake.urls)


def test_read_over_the_cap_is_refused_before_it_runs(tmp_path):
    fake = FakeBigQuery(estimate=10 * 1024 ** 3)
    with pytest.raises(QueryError, match="over the .* cap"):
        execute(fake, parse("--jobs-out", tmp_path / "j.json"))
    assert fake.queries == [] and not (tmp_path / "j.json").exists()
    fake = FakeBigQuery(estimate=10 * 1024 ** 3)
    code, _, _ = execute(fake, parse("--jobs-out", tmp_path / "j.json", "--max-bytes-billed", 20 * 1024 ** 3))
    assert code == 0 and fake.queries[0]["maximumBytesBilled"] == str(20 * 1024 ** 3)


def test_options_leave_out_text_and_emails_and_column_probes(tmp_path):
    fake = FakeBigQuery()
    args = parse("--jobs-out", tmp_path / "j.json", "--sizes-out", tmp_path / "s.json", "--skip-columns",
                 "--omit-query-text", "--omit-user-email", "--days", 3)
    execute(fake, args)
    assert "INTERVAL 3 DAY" in fake.queries[0]["query"] and "CAST(NULL AS STRING) AS query" in fake.queries[0]["query"]
    sizes = json.loads((tmp_path / "s.json").read_text())
    assert sizes[f"{PROJECT}.core.events"] == {"rows": 1000, "bytes": 64_000}  # no per-column dry runs
    assert not any(sql.startswith("SELECT *") for sql in fake.dry_runs)


def test_a_column_that_cannot_be_planned_is_reported_not_guessed(tmp_path):
    fake = FakeBigQuery(fail_columns={"name"})
    code, _, err = execute(fake, parse("--sizes-out", tmp_path / "s.json"))
    assert code == 0
    sizes = json.loads((tmp_path / "s.json").read_text())
    assert sizes[f"{PROJECT}.core.events"]["columns"] == {"id": 8000}
    assert "skipped: example-project.core.events.name: bytes unknown" in err


def test_too_many_tables_are_refused_rather_than_truncated(tmp_path):
    tables = {("core", f"t{i}"): (10, 100, {"id": ("INTEGER", 80)}) for i in range(5)}
    fake = FakeBigQuery(tables=tables)
    with pytest.raises(QueryError, match="5 tables match"):
        execute(fake, parse("--sizes-out", tmp_path / "s.json", "--max-tables", 3))
    assert not any(sql.startswith("SELECT *") for sql in fake.dry_runs)
    code, _, _ = execute(FakeBigQuery(tables=tables), parse("--sizes-out", tmp_path / "s.json", "--max-tables", 3, "--skip-columns"))
    assert code == 0


def test_existing_outputs_are_not_replaced_unless_asked(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text("keep", encoding="utf-8")
    fake = FakeBigQuery()
    with pytest.raises(QueryError, match="--overwrite"):
        execute(fake, parse("--jobs-out", target))
    assert target.read_text() == "keep" and fake.urls == []
    assert execute(fake, parse("--jobs-out", target, "--overwrite"))[0] == 0
    assert json.loads(target.read_text())[0]["job_id"] == "j1"


def test_paged_results_are_joined(tmp_path):
    fake = FakeBigQuery(pages=2)
    execute(fake, parse("--jobs-out", tmp_path / "j.json"))
    assert [j["job_id"] for j in json.loads((tmp_path / "j.json").read_text())] == ["j1", "j2"]


def test_a_full_job_page_warns_that_older_jobs_are_missing(tmp_path):
    fake = FakeBigQuery()
    _, _, err = execute(fake, parse("--jobs-out", tmp_path / "j.json", "--max-jobs", 2))
    assert "limit was reached" in err


def test_a_rejected_query_is_an_error_not_a_crash(tmp_path):
    fake = FakeBigQuery()
    fake.transport = lambda url, headers, body: (403, {"error": {"message": "Access Denied"}})
    with pytest.raises(QueryError, match="Access Denied"):
        execute(fake, parse("--jobs-out", tmp_path / "j.json"))
