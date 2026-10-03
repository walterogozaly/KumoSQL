import json
import threading
import urllib.request

import pytest

from kumosql import console, redact, ui


def test_same_name_gets_the_same_placeholder_and_kinds_count_separately():
    first = console.ref("repo", "git@github.com:acme/finance.git")
    assert first == "repo#1" and console.ref("repo", "git@github.com:acme/finance.git") == first
    assert console.ref("repo", "https://github.com/acme/other.git") == "repo#2"
    assert console.ref("project", "acme-prod-123") == "project#1"


def test_repository_urls_names_and_credentials_are_replaced():
    text = console.scrub("fatal: repository 'https://user:tok@git.acme.internal/acme/finance-dataform.git/' not found")
    assert "acme" not in text and "tok" not in text and "repo#" in text
    text = console.scrub("failed cloning git@git.acme.internal:acme/finance-dataform.git")
    assert "acme" not in text and "repo#" in text


def test_registered_names_are_replaced_everywhere_and_ids_split_by_part():
    console.register("model", ["orders_daily"])
    console.register("project", ["acme-prod-123"])
    text = console.scrub("model orders_daily read `acme-prod-123.sales.orders_daily` and acme-prod-123.sales.customers")
    assert "orders_daily" not in text and "acme" not in text and "sales" not in text
    assert text.count("model#1") >= 1 and "project#1" in text


def test_paths_keep_depth_and_extension_and_home_is_hidden(monkeypatch):
    redactor = redact.GLOBAL
    redactor._home, redactor._username = "/home/walter", "walter"
    text = console.scrub("[Errno 13] Permission denied: '/home/walter/work/finance/definitions/orders.sqlx'")
    assert "walter" not in text and "finance" not in text
    assert "dir/dir/dir/dir/file#1.sqlx" in text
    windows = console.scrub(r"Access is denied: 'C:\\Users\\Walter Ogozaly\\Repos\\finance\\a.sqlx'")
    assert "Ogozaly" not in windows and "finance" not in windows and windows.endswith("file#2.sqlx'")
    assert console.scrub("hello walter") == "hello user~"


def test_emails_secrets_hosts_and_routes():
    text = console.scrub("mail walter@acme.com token=abc123 ghp_" + "a" * 30 + " connect to host git.acme.internal port 22")
    assert "acme" not in text and "abc123" not in text and "ghp_" not in text
    assert console.scrub("GET /api/graph HTTP/1.1 200") == "GET /api/graph HTTP/1.1 200"


def test_redaction_can_be_turned_off_for_local_debugging():
    console.set_redaction(False)
    assert console.ref("repo", "git@github.com:acme/finance.git") == "git@github.com:acme/finance.git"
    assert console.scrub("git@github.com:acme/finance.git") == "git@github.com:acme/finance.git"


def test_log_and_console_never_hold_real_names(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    with pytest.raises(PermissionError):
        with console.task("repo load", repo="git@github.com:acme/finance.git"):
            with console.task("write cached clone"):
                raise PermissionError(13, "Permission denied", "/srv/secret-team/finance/definitions/orders.sqlx")
    import time
    time.sleep(0.2)  # the console writer is a thread
    shown = capsys.readouterr().err
    logged = (tmp_path / "ui.log").read_text(encoding="utf-8")
    for text in (shown, logged):
        assert "acme" not in text and "secret-team" not in text and "finance" not in text
    assert "repo load > write cached clone: failed (PermissionError)" in logged
    assert "[KS-IO-PERM]" in logged and "hint:" in logged
    assert logged.count("failed (PermissionError)") == 1  # logged once, by the innermost stage
    assert "repo load: failed after" in logged and "details above" in logged
    assert "at kumosql/test_redaction.py" not in logged  # test frames are not package frames
    assert "PermissionError: [KS-IO-PERM]" in logged  # frames and category kept, exception values withheld


def test_error_has_code_hint_in_python_m_form_and_traceback_only_in_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    from kumosql.git_repo import GitRepoError

    try:
        raise GitRepoError("fatal: Authentication failed for 'https://git.acme.internal/acme/x.git/'")
    except GitRepoError as exc:
        console.error("repo load failed", exc)
    logged = (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert "ERROR [KS-GIT-AUTH]" in logged and "python -m kumosql.ui --diagnose-repo" in logged
    assert "kumosql." + "exe" not in logged and "kumosql-ui" not in logged
    assert "acme" not in logged and "TRACE" in logged


def test_stage_reports_progress_and_slow_warning(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    with console.task("analyse", warn_after=0.1, heartbeat_after=0.1) as step:
        step.progress(5, 10)
        time.sleep(1.2)
        step.note(models=10)
    logged = (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert "analyse: still running after" in logged and "5/10" in logged and "WARN" in logged
    assert "analyse: finished in" in logged and "models 10" in logged


def test_private_map_is_saved_and_can_be_looked_up(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    placeholder = console.ref("model", "orders_daily")
    redact.GLOBAL.write_map()
    saved = json.loads((tmp_path / redact.MAP_NAME).read_text(encoding="utf-8"))
    assert "orders_daily" in json.dumps(saved)
    assert redact.lookup(placeholder)[0][2] == "orders_daily"
    assert ui.main(["--lookup", placeholder]) == 0
    assert ui.main(["--lookup", "model#999"]) == 1


def test_diagnostics_bundle_is_redacted_and_leaves_out_secrets_and_the_map(tmp_path, monkeypatch):
    from kumosql import state

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    state.set_section("repositories", {"items": [{"url": "git@github.com:acme/finance.git", "branch": "feature-x", "token": "s3cret"}]})
    state.set_section("scopes", {"query": "select * from acme.sales.orders", "name": "Finance prod"})
    state.set_section("ui", {"theme": "dark", "sidebar": True})
    console.say("loading git@github.com:acme/finance.git")
    text = console.diagnostics()
    assert "acme" not in text and "s3cret" not in text and "Finance" not in text and "select *" not in text
    assert '"theme": "dark"' in text and "Python:" in text and "OS:" in text and "== Recent log" in text
    assert redact.MAP_NAME not in text.replace(f"<data>/{redact.MAP_NAME}", "")
    assert "repo#" in text


def test_bundle_leaves_the_log_out_when_started_without_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    console.set_redaction(False)
    console.say("loading git@github.com:acme/finance.git")
    text = console.diagnostics()
    assert "acme" not in text and "--no-redact" in text


def test_diagnostics_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    server = ui.UIServer(("127.0.0.1", 0), ui.UIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/diagnostics").read()
    finally:
        server.shutdown()
        server.server_close()
    payload = json.loads(body)
    assert payload["redacted"] is True and "KumoSQL diagnostics" in payload["text"]


def test_request_log_drops_query_values(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    server = ui.UIServer(("127.0.0.1", 0), ui.UIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/catalog/table?project=acme-prod&table=orders").read()
    except Exception:
        pass
    finally:
        server.shutdown()
        server.server_close()
    logged = (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert "acme-prod" not in logged and "?project,table" in logged


def test_sql_parse_error_excerpts_keep_only_line_and_column():
    text = console.scrub("Invalid expression / Unexpected token. Line 3, Col: 14.\n  select a from \x1b[4mcorp_sales.orders\x1b[0m where")
    assert text == "Invalid expression / Unexpected token. Line 3, Col: 14. <SQL excerpt withheld>"


@pytest.mark.parametrize('payload', [
    "password='FAKE_PASSWORD'", 'password="FAKE_PASSWORD"', "password='FAKE_ONE''FAKE_TWO'",
    '{"access_token": "FAKE_ACCESS", "refresh_token": "FAKE_REFRESH"}',
    '{"client_secret": "FAKE_CLIENT", "private_key": "FAKE_PRIVATE"}',
    '{"api_key": "FAKE_API"}',
    '-----BEGIN PRIVATE KEY-----\nFAKE_PEM\n-----END PRIVATE KEY-----',
    '-----BEGIN RSA PRIVATE KEY-----\nFAKE_TRUNCATED',
])
def test_secret_formats_do_not_reach_logs_maps_or_final_diagnostics(payload, tmp_path, monkeypatch):
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    console.log(payload)
    console.say(payload, console=False)
    assert 'FAKE_' not in console.scrub(payload)
    assert 'FAKE_' not in (tmp_path / 'ui.log').read_text()
    assert 'FAKE_' not in console.diagnostics()
    assert 'FAKE_' not in json.dumps(redact.GLOBAL.mapping())


def test_url_credentials_never_enter_reverse_map_including_existing_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    old = {'sessions': {'old': {'started': '2026-01-01', 'map': {'repo:repo#1': 'https://FAKE_OLD@host/repo.git'}}}}
    (tmp_path / redact.MAP_NAME).write_text(json.dumps(old))
    assert 'FAKE_OLD' not in json.dumps(redact.lookup('repo#1'))
    url = 'https://user:FAKE_TOKEN@host/repo.git'
    console.ref('repo', url)
    console.scrub(url)
    redact.GLOBAL.write_map()
    assert 'FAKE_' not in json.dumps(redact.GLOBAL.mapping())
    assert 'FAKE_' not in (tmp_path / redact.MAP_NAME).read_text()


@pytest.mark.parametrize('payload', [
    "SELECT salary_private FROM internal_payroll WHERE note = 'FAKE_LITERAL'; row=('FAKE_ROW_VALUE', 987654)",
    "row=('FAKE_ROW_VALUE', 987654)",
    "variables={'region': 'FAKE_VARIABLE'}",
])
def test_data_dumps_are_withheld_at_sink_and_from_diagnostics(payload, tmp_path, monkeypatch):
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    console.log(payload)
    console.say(payload, console=False)
    assert console.scrub(payload) == '<data payload withheld>'
    text = (tmp_path / 'ui.log').read_text()
    for private in ('FAKE_', 'salary_private', 'internal_payroll', '987654'):
        assert private not in text and private not in console.diagnostics()


def test_diagnostics_omit_unstructured_historical_and_free_form_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    (tmp_path / 'ui.log').write_text('2026-01-01 old INFO FAKE_LEGACY salary_private 987654\n')
    console.log('FAKE_FREEFORM payroll_private 987654')
    console.say('analysis: finished in 1.0s (models 10)')
    text = console.diagnostics()
    assert 'FAKE_' not in text and 'salary_private' not in text and 'payroll_private' not in text and '987654' not in text
    assert '2 unsummarized log entries omitted' in text
    assert 'models 10' in text


def test_producer_exception_and_variable_values_are_not_logged(tmp_path, monkeypatch):
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    with pytest.raises(ValueError):
        with console.task('parse project', variables={'region': 'FAKE_VARIABLE'}, parameter=987654):
            raise ValueError("FAKE_ROW_VALUE salary_private 987654 Line 2, Col: 5. SELECT * FROM payroll")
    text = (tmp_path / 'ui.log').read_text() + console.diagnostics()
    assert 'FAKE_' not in text and 'salary_private' not in text and '987654' not in text
    assert 'Line 2, Col: 5' in text and '[KS-INTERNAL]' in text


def test_diagnostics_settings_do_not_leak_unknown_keys_or_variable_numbers(tmp_path, monkeypatch):
    from kumosql import state
    monkeypatch.setenv('KUMOSQL_HOME', str(tmp_path))
    state.set_section('settings', {'vars': {'salary_private': 987654}, 'salary_private': 987654, 'project': 'FAKE_PROJECT'})
    text = console.diagnostics()
    assert 'salary_private' not in text and '987654' not in text and 'FAKE_PROJECT' not in text
    assert 'FAKE_PROJECT' not in json.dumps(redact.GLOBAL.mapping())


def test_no_redact_still_never_retains_or_shows_credentials():
    console.set_redaction(False)
    url = 'https://FAKE_TOKEN@host/repo.git'
    assert console.ref('repo', url) == 'https://host/repo.git'
    assert 'FAKE_TOKEN' not in console.scrub(url)
    assert 'FAKE_PASSWORD' not in console.scrub("password='FAKE_PASSWORD'")
