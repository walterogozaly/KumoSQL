"""The scope condition "is returned by this SQL query": guarded, cached, refreshable."""

import json
import time
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kumosql import dryrun, scope_queries, state
from kumosql.cli import scopes_main
from kumosql.scopes import describe_rule, parse_scope
from kumosql.scope_queries import QueryError, QueryRun
from kumosql.ui import UIHandler

SQL = "SELECT user_email FROM `p.d.my_team`"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    scope_queries.clear_cache()
    state.set_section("bigquery", {"billingProject": "billing-p"})
    calls = []

    def runner(sql, column, project, max_bytes):
        calls.append((sql, column, project, max_bytes))
        return QueryRun(["ana@co.com", "BO@co.com", "ana@co.com"], column or "user_email", 1_000_000, 10_000_000)

    monkeypatch.setattr(scope_queries, "RUNNER", runner)
    yield calls
    scope_queries.clear_cache()


def team_scope(**extra):
    return parse_scope({"name": "team", "rule": {"field": "user_email", "op": "in_query", "query": SQL, **extra}})


def test_condition_matches_values_returned_by_the_query(_fresh):
    scope = team_scope()
    assert scope.matches({"user_email": "ANA@co.com"}) and scope.matches({"user_email": "bo@co.com"})
    assert not scope.matches({"user_email": "cy@co.com"}) and not scope.matches({})
    assert len(_fresh) == 1  # one run for every record
    assert _fresh[0][2:] == ("billing-p", scope_queries.DEFAULT_MAX_BYTES_BILLED)
    assert scope.describe() == "user_email is returned by SQL query “SELECT user_email FROM `p.d.my_team`”"


def test_case_sensitive_and_composed_with_other_conditions():
    rule = {"all": [{"field": "user_email", "op": "in_query", "query": SQL, "case_sensitive": True},
                    {"field": "job_type", "op": "eq", "value": "query"}]}
    scope = parse_scope({"name": "s", "rule": rule})
    assert scope.matches({"user_email": "BO@co.com", "job_type": "QUERY"})
    assert not scope.matches({"user_email": "bo@co.com", "job_type": "QUERY"})
    assert not scope.matches({"user_email": "BO@co.com", "job_type": "LOAD"})


@pytest.mark.parametrize("bad", [
    {"field": "a", "op": "in_query"}, {"field": "a", "op": "in_query", "query": "  "},
    {"field": "a", "op": "in_query", "query": "SELECT 1", "value": ["x"]},
    {"field": "a", "op": "in_query", "query": "SELECT 1", "column": 3},
    {"field": "a", "op": "in_query", "query": "x" * 20_001},
])
def test_query_conditions_are_validated(bad):
    with pytest.raises(ValueError):
        parse_scope({"name": "s", "rule": bad})


def test_results_are_cached_until_the_timer_ends_and_refresh_runs_again(_fresh):
    scope_queries.result_for(SQL)
    scope_queries.result_for(SQL)
    assert len(_fresh) == 1
    scope_queries.result_for(SQL, refresh=True)
    assert len(_fresh) == 2
    state.set_section("scope_queries", {"max_bytes_billed": 50_000_000})
    state.set_section("bigquery", {"billingProject": "billing-p", "queryCacheHours": 0.0001})  # 0.36 seconds
    time.sleep(0.5)
    scope_queries.result_for(SQL)
    assert len(_fresh) == 3 and _fresh[-1][3] == 50_000_000


def test_default_timer_is_48_hours_and_restart_reloads_values(_fresh, monkeypatch):
    assert scope_queries.cache_seconds() == 48 * 3600
    result = scope_queries.result_for(SQL)
    assert result.expires_at - result.fetched_at == 48 * 3600
    monkeypatch.setattr(scope_queries, "_memory", {})
    monkeypatch.setattr(scope_queries, "_disk_loaded", False)
    assert scope_queries.result_for(SQL).values == result.values and len(_fresh) == 2


def test_a_failed_rerun_keeps_the_older_copy_and_says_so(monkeypatch):
    scope_queries.result_for(SQL)

    def broken(*args):
        raise QueryError("quota exceeded")

    monkeypatch.setattr(scope_queries, "RUNNER", broken)
    kept = scope_queries.result_for(SQL, refresh=True)
    assert kept.stale and kept.error == "quota exceeded" and "ana@co.com" in kept.values
    with pytest.raises(QueryError, match="quota exceeded"):
        scope_queries.result_for("SELECT other", refresh=True)


def test_a_billing_project_is_required():
    state.set_section("bigquery", {})
    with pytest.raises(QueryError, match="billing project is needed"):
        team_scope().matches({"user_email": "a"})


def test_settings_are_validated():
    assert scope_queries.save_settings({"max_bytes_billed": 100_000_000}).max_bytes_billed == 100_000_000
    for bad in ({"max_bytes_billed": "x"}, {"max_bytes_billed": 5}, {"max_bytes_billed": 1.5e9}, []):
        with pytest.raises(ValueError):
            scope_queries.save_settings(bad)


# ---------------------------------------------------------- the BigQuery runner


class FakeBigQuery:
    def __init__(self, estimate=2_000_000, rows=(("ana@co.com",), ("bo@co.com",)), pages=1):
        self.estimate, self.rows, self.pages = estimate, rows, pages
        self.posts, self.gets = [], []

    def post(self, url, headers, body):
        payload = json.loads(body)
        self.posts.append((url, payload))
        if payload.get("configuration", {}).get("dryRun"):
            return 200, {"statistics": {"totalBytesProcessed": str(self.estimate), "query": {
                "schema": {"fields": [{"name": "user_email", "type": "STRING"}, {"name": "team", "type": "STRING"}]}}}}
        first = [{"f": [{"v": r[0]}, {"v": "x"}]} for r in self.rows[: len(self.rows) // self.pages or 1]]
        return 200, {"jobComplete": True, "jobReference": {"jobId": "j1", "location": "US"},
                     "schema": {"fields": [{"name": "user_email"}, {"name": "team"}]}, "rows": first,
                     "pageToken": "t2" if self.pages > 1 else None, "totalBytesBilled": "10485760", "cacheHit": False}

    def get(self, url, headers):
        self.gets.append(url)
        rest = [{"f": [{"v": r[0]}, {"v": "x"}]} for r in self.rows[len(self.rows) // self.pages or 1:]]
        return 200, {"jobComplete": True, "rows": rest, "totalBytesBilled": "10485760"}


@pytest.fixture
def bq(monkeypatch):
    fake = FakeBigQuery()
    monkeypatch.setenv("BQ_ACCESS_TOKEN", "t")
    monkeypatch.setattr(dryrun, "_urllib_transport", fake.post)
    monkeypatch.setattr(scope_queries, "_get", fake.get)
    monkeypatch.setattr(scope_queries, "RUNNER", scope_queries._bq_runner)
    return fake


def test_runner_dry_runs_first_then_runs_with_a_byte_cap_in_the_billing_project(bq):
    result = scope_queries.result_for(SQL, "user_email")
    assert result.values == {"ana@co.com", "bo@co.com"} and result.bytes_billed == 10485760
    dry, real = bq.posts
    assert dry[1]["configuration"]["dryRun"] is True and "/projects/billing-p/jobs" in dry[0]
    assert "/projects/billing-p/" in real[0] and real[0].endswith("/queries")
    assert real[1]["maximumBytesBilled"] == str(scope_queries.DEFAULT_MAX_BYTES_BILLED)
    assert real[1]["labels"] == {"kumosql": "scope_query"} and real[1]["useQueryCache"] is True


def test_runner_refuses_a_query_over_the_cap_without_running_it(bq):
    bq.estimate = 5_000_000_000
    with pytest.raises(QueryError, match="over the 1.1 GB cap"):
        scope_queries.result_for(SQL)
    assert len(bq.posts) == 1  # only the dry run


def test_runner_picks_the_named_column_and_reports_missing_ones(bq):
    assert scope_queries.result_for(SQL, "team").values == {"x"}
    with pytest.raises(QueryError, match="no column 'nope'"):
        scope_queries.result_for(SQL, "nope")


def test_runner_reads_every_page(monkeypatch):
    fake = FakeBigQuery(rows=(("a",), ("b",), ("c",), ("d",)), pages=2)
    monkeypatch.setenv("BQ_ACCESS_TOKEN", "t")
    monkeypatch.setattr(dryrun, "_urllib_transport", fake.post)
    monkeypatch.setattr(scope_queries, "_get", fake.get)
    monkeypatch.setattr(scope_queries, "RUNNER", scope_queries._bq_runner)
    assert scope_queries.result_for(SQL).values == {"a", "b", "c", "d"} and len(fake.gets) == 1


# ------------------------------------------------------------ API and the CLI


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def call(base, path, payload=None, method=None):
    request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method=method or ("POST" if payload is not None else "GET"))
    with urlopen(request) as response:
        return json.load(response)


def test_api_status_run_refresh_settings_and_cache_listing(server, _fresh):
    assert call(server, "/api/scope-queries", {"mode": "status", "query": SQL})["result"] is None
    ran = call(server, "/api/scope-queries", {"mode": "run", "query": SQL})["result"]
    assert ran["count"] == 2 and ran["sample"] == ["BO@co.com", "ana@co.com"] and not ran["stale"]
    call(server, "/api/scope-queries", {"mode": "run", "query": SQL})
    assert len(_fresh) == 1
    call(server, "/api/scope-queries", {"mode": "refresh", "query": SQL})
    assert len(_fresh) == 2
    info = call(server, "/api/scope-queries")
    assert info["billing_project"] == "billing-p" and info["cache_hours"] == 48
    assert [c["count"] for c in info["cached"]] == [2]
    saved = call(server, "/api/settings/scope_queries", {"max_bytes_billed": 200_000_000}, "PUT")
    assert saved == {"max_bytes_billed": 200_000_000}
    for bad in ({"mode": "run", "query": ""}, {"mode": "nope", "query": SQL}):
        with pytest.raises(HTTPError) as error:
            call(server, "/api/scope-queries", bad)
        assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        call(server, "/api/settings/scope_queries", {"max_bytes_billed": -5}, "PUT")
    assert error.value.code == 400


def test_api_reports_a_missing_billing_project_clearly(server):
    state.set_section("bigquery", {})
    with pytest.raises(HTTPError) as error:
        call(server, "/api/scope-queries", {"mode": "run", "query": SQL})
    assert "billing project is needed" in json.load(error.value)["error"]


def test_cli_refreshes_query_conditions(capsys, _fresh):
    scopes_main(["add", "Team", "--rule", json.dumps({"field": "user_email", "op": "in_query", "query": SQL})])
    assert scopes_main(["refresh", "Team"]) == 0
    assert scopes_main(["refresh"]) == 0
    assert len(_fresh) == 2 and "Team: user_email: 2 values" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        scopes_main(["refresh", "Nope"])


def test_the_page_offers_the_query_condition(server):
    with urlopen(server + "/assets/scopes.js") as response:
        script = response.read().decode()
    assert "in_query" in script and "Refresh now" in script
    assert "in_query" in {o["op"] for o in call(server, "/api/scope-fields")["operators"]}


def test_fallback_operators_match_the_server_list(server):
    # Tag rules can open before /api/scope-fields answers; the page's own list must not drop an operator.
    import re

    with urlopen(server + "/assets/scopes.js") as response:
        script = response.read().decode()
    block = script.split("const FALLBACK_OPERATORS = [", 1)[1].split("].map", 1)[0]
    fallback = re.findall(r'\["(\w+)", "([^"]+)"\]', block)
    served = [(item["op"], item["label"]) for item in call(server, "/api/scope-fields")["operators"]]
    assert fallback == served
