"""Local gates and cache retention; every execution is a fake."""

import json
import sys
import types

import pytest

from kumosql import data_sources, dryrun, query_context, scope_queries, state
from kumosql.sql_validation import quote_table_path, validate_readonly_query

SQL = "SELECT email FROM p.d.team"
BAD_SQL = [
    "DELETE FROM p.d.t WHERE TRUE; SELECT email FROM p.d.team",
    "SELECT 1; SELECT 2", "UPDATE p.d.t SET x=1 WHERE TRUE",
    "INSERT INTO p.d.t VALUES (1)", "CREATE TABLE p.d.t AS SELECT 1",
    "DROP TABLE p.d.t", "BEGIN SELECT 1; END", "CALL p.d.proc()",
    "DECLARE x INT64", "EXECUTE IMMEDIATE 'SELECT 1'", "SELECT * FROM",
    "SELECT 1 INTO p.d.t", "WITH t AS (DELETE FROM p.d.t) SELECT * FROM t",
]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("BQ_ACCESS_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"client_email":"first@example.test"}')
    state.set_section("bigquery", {"billingProject": "billing-a"})
    scope_queries.clear_cache()
    yield
    scope_queries.clear_cache()


@pytest.mark.parametrize("sql", BAD_SQL)
def test_invalid_query_refused_before_credentials_planner_execution_or_cache(sql, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid SQL reached credentials, planner or execution")
    monkeypatch.setattr(dryrun, "access_token", forbidden)
    monkeypatch.setattr(dryrun, "dry_run", forbidden)
    monkeypatch.setattr(scope_queries, "RUNNER", forbidden)
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", forbidden)
    source = data_sources.Source("bad", "Bad", "bigquery_sql", sql)
    for check in (
        lambda: scope_queries.peek(sql), lambda: scope_queries.result_for(sql),
        lambda: scope_queries.dry_run(sql), lambda: scope_queries._bq_runner(sql, None, "p", 10_000_000),
        lambda: data_sources.populate(source), lambda: data_sources.peek(source),
        lambda: data_sources._bq_runner(source, "p", 10_000_000),
    ):
        with pytest.raises(ValueError, match="read-only SELECT"):
            check()


@pytest.mark.parametrize("sql", ["SELECT 1; -- tail", "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
                                    "SELECT 1 UNION ALL SELECT 2", "(SELECT 1)", "SELECT ';DELETE' AS x"])
def test_readonly_query_shapes_allowed(sql):
    validate_readonly_query(sql)


@pytest.mark.parametrize("table", ["p.d.t` WHERE FALSE; DROP TABLE p.d.t; --", "`p.d.t`", "p.d.t;",
                                   "p.d", "p.d.t.extra", "p.d.t\n", "p.d.*", "p.d.t$20261003"])
def test_schema_paths_refused_before_any_request(table, monkeypatch):
    monkeypatch.setattr(dryrun, "access_token", lambda: pytest.fail("credentials acquired"))
    with pytest.raises(ValueError, match="identifier"):
        dryrun.fetch_table_schemas(["p.d.valid", table], "p", transport=lambda *args: pytest.fail("request sent"))


def test_valid_schema_path_quoted():
    assert quote_table_path("project-a.dataset_1.table_2") == "`project-a.dataset_1.table_2`"


@pytest.mark.parametrize("switch", ["project", "principal", "token", "location"])
def test_scope_and_source_caches_drop_old_execution_context(switch, monkeypatch):
    calls = []
    def scope_runner(*args):
        calls.append("scope")
        return scope_queries.QueryRun([str(len(calls))], "email")
    def source_runner(*args):
        calls.append("source")
        return data_sources.Run(["email"], [[str(len(calls))]])
    monkeypatch.setattr(scope_queries, "RUNNER", scope_runner)
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", source_runner)
    source = data_sources.save_sources([{"name": "Team", "query": SQL}])[0]
    first = scope_queries.result_for(SQL)
    rows = data_sources.populate(source)
    scope_queries.result_for(SQL)
    data_sources.populate(source)
    assert calls == ["scope", "source"]
    if switch == "project":
        state.set_section("bigquery", {"billingProject": "billing-b"})
    elif switch == "location":
        state.set_section("bigquery", {"billingProject": "billing-a", "location": "EU"})
    elif switch == "token":
        monkeypatch.setenv("BQ_ACCESS_TOKEN", "opaque-secret-token")
    else:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"client_email":"second@example.test"}')
    assert scope_queries.peek(SQL) is None
    assert data_sources.peek(source) is None
    assert not data_sources._rows_path(source.id).exists()
    assert scope_queries.result_for(SQL).values != first.values
    assert data_sources.populate(source).rows != rows.rows
    assert calls == ["scope", "source", "scope", "source"]
    saved = scope_queries._cache_path().read_text() + data_sources._rows_path(source.id).read_text()
    assert "opaque-secret-token" not in saved and "example.test" not in saved and SQL not in saved


def test_expired_values_and_inactive_rows_deleted_from_disk_and_memory(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(scope_queries.time, "time", lambda: clock[0])
    state.set_section("bigquery", {"billingProject": "billing-a", "queryCacheHours": 1})
    monkeypatch.setattr(scope_queries, "RUNNER", lambda *args: scope_queries.QueryRun(["secret-value"], "email"))
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", lambda *args: data_sources.Run(["email"], [["needed-row"]]))
    sources = data_sources.save_sources([{"name": "A", "query": SQL}, {"name": "B", "query": SQL}])
    scope_queries.result_for(SQL)
    for source in sources:
        data_sources.populate(source)
    summary = json.loads(scope_queries._cache_path().read_text())
    assert next(iter(summary.values()))["count"] == 1
    assert "secret-value" not in json.dumps(summary) and "values" not in json.dumps(summary)
    clock[0] += 3601
    assert scope_queries.peek(SQL) is None and not scope_queries._memory
    assert json.loads(scope_queries._cache_path().read_text()) == {}
    assert data_sources.peek(sources[0]) is None
    assert all(not data_sources._rows_path(source.id).exists() for source in sources)
    monkeypatch.setattr(scope_queries, "RUNNER", lambda *args: (_ for _ in ()).throw(scope_queries.QueryError("offline")))
    with pytest.raises(scope_queries.QueryError, match="offline"):
        scope_queries.result_for(SQL)


def test_legacy_disk_scope_rows_removed_on_load(monkeypatch):
    path = scope_queries._cache_path()
    path.write_text(json.dumps({"old": {"at": 1, "values": ["legacy"], "sql": SQL}}))
    monkeypatch.setattr(scope_queries, "_disk_loaded", False)
    assert scope_queries.peek(SQL) is None
    assert json.loads(path.read_text()) == {}


def test_credential_parse_and_refresh_errors_omit_content(monkeypatch):
    class Credentials:
        @staticmethod
        def from_service_account_info(*args, **kwargs):
            raise ValueError("private_key=DO-NOT-SHARE")
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", types.SimpleNamespace(Request=lambda: None))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.SimpleNamespace(service_account=types.SimpleNamespace(Credentials=Credentials)))
    for raw in ('{"private_key": "DO-NOT-SHARE"}', "DO-NOT-SHARE"):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raw)
        with pytest.raises(RuntimeError) as error:
            dryrun.access_token()
        assert str(error.value) == "could not load or refresh service account credentials"
        assert error.value.__suppress_context__


def test_ui_query_modes_reject_scripts_without_runner_calls(monkeypatch):
    from kumosql.ui import _scope_query
    monkeypatch.setattr(scope_queries, "RUNNER", lambda *args: pytest.fail("runner called"))
    for mode in ("status", "check", "run", "refresh"):
        with pytest.raises(ValueError, match="read-only SELECT"):
            _scope_query({"mode": mode, "query": "SELECT 1; DELETE FROM p.d.t WHERE TRUE"})


def test_failed_refresh_cannot_return_rows_that_expired_during_execution(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(scope_queries.time, "time", lambda: clock[0])
    state.set_section("bigquery", {"billingProject": "billing-a", "queryCacheHours": 1})
    source = data_sources.save_sources([{"name": "A", "query": SQL}])[0]
    monkeypatch.setattr(scope_queries, "RUNNER", lambda *args: scope_queries.QueryRun(["old"], "email"))
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", lambda *args: data_sources.Run(["email"], [["old"]]))
    scope_queries.result_for(SQL)
    data_sources.populate(source)
    def broken(*args):
        clock[0] += 3601
        raise scope_queries.QueryError("offline")
    monkeypatch.setattr(scope_queries, "RUNNER", broken)
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", broken)
    with pytest.raises(scope_queries.QueryError, match="offline"):
        scope_queries.result_for(SQL, refresh=True)
    clock[0] = 1000.0
    with pytest.raises(data_sources.DataSourceError, match="offline"):
        data_sources.populate(source, refresh=True)
    assert scope_queries.peek(SQL) is None and data_sources.peek(source) is None


def test_known_principal_can_reuse_source_rows_across_restart(monkeypatch):
    monkeypatch.setattr(query_context, "_signals", None)
    monkeypatch.setattr(query_context, "_generation", "")
    source = data_sources.save_sources([{"name": "A", "query": SQL}])[0]
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", lambda *args: data_sources.Run(["email"], [["same-principal"]]))
    data_sources.populate(source)
    monkeypatch.setattr(query_context, "_signals", None)
    monkeypatch.setattr(query_context, "_generation", "")
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", lambda *args: pytest.fail("unnecessary rerun"))
    assert data_sources.populate(source).rows == [["same-principal"]]
