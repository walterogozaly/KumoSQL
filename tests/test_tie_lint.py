"""The tie lint over a small Dataform project: findings need a replaying witness, nothing else is one."""

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request

import pytest

pytest.importorskip("duckdb")

from kumosql import load_sqlx_project
from kumosql.prover_schema import from_pipeline
from kumosql.tie_lint import lint_pipeline, lint_project, main, run_loaded
from kumosql.tie_witness import replay
from kumosql.ui import UIHandler, UIServer
from ui_http import urlopen

TABLE = 'config { type: "table" }\n'
KEYED = 'config { type: "table", assertions: { uniqueKey: ["id"], nonNull: ["id"] } }\n'


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root):
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(root, "definitions/events.sqlx", 'config { type: "declaration", schema: "raw", name: "events" }\n')
    write(root, "definitions/events2.sqlx", 'config { type: "declaration", schema: "raw", name: "events2" }\n')
    files = {
        # the latest row per user: tied rows of one user differ in value, so which one is kept varies
        "latest": TABLE + 'SELECT user_id, ts, value FROM ${ref("raw", "events")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1\n",
        # the same shape over another table: the search runs once and the witness is renamed and replayed
        "latest_copy": TABLE + 'SELECT user_id, ts, value FROM ${ref("raw", "events2")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1\n",
        # one value per user: ANY_VALUE picks among a user's rows
        "pick": TABLE + 'SELECT user_id, ANY_VALUE(value) AS value FROM ${ref("raw", "events")} GROUP BY user_id\n',
        # only the tie columns are read afterwards: the analysis shows it deterministic
        "keys_only": TABLE + 'SELECT user_id, ts FROM ${ref("raw", "events")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1\n",
        # user_id is unique in `totals` because it groups by it, so this dedup has no ties
        "totals": TABLE + 'SELECT user_id, MAX(ts) AS ts, SUM(value) AS value FROM ${ref("raw", "events")} GROUP BY user_id\n',
        "totals_latest": TABLE + 'SELECT user_id, ts, value FROM ${ref("totals")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1\n",
        # an incremental model's stored rows are not its query's output
        "grow": 'config { type: "incremental" }\nSELECT user_id, ts, value FROM ${ref("raw", "events")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1\n",
        # an unknown site DuckDB cannot run with the guessed column types: no witness, so no finding
        "exploded": TABLE + 'SELECT user_id, ANY_VALUE(item) AS item FROM ${ref("raw", "events")}, UNNEST(tags) AS item GROUP BY user_id\n',
    }
    for name, text in files.items():
        write(root, f"definitions/{name}.sqlx", text)
    return load_sqlx_project(root)


@pytest.fixture(scope="module")
def lint(tmp_path_factory):
    pipeline = build(tmp_path_factory.mktemp("project"))
    return lint_pipeline(pipeline, from_pipeline(pipeline), budget=10)


def names(rows):
    return {row.model.rsplit(".", 1)[-1] for row in rows}


def test_findings_are_the_unknown_sites_with_a_replaying_witness(lint):
    assert names(lint.findings) == {"latest", "latest_copy", "pick"}
    for finding in lint.findings:
        assert all(not site.deterministic for site in finding.sites)
        assert replay(json.loads(json.dumps(finding.witness)))
        assert sum(len(rows) for rows in finding.witness["tables"].values()) == 2
    copy = next(f for f in lint.findings if f.model.endswith("latest_copy"))
    assert list(copy.witness["tables"]) == ["proj.raw.events2"] and "events2" in copy.witness["sql"]


def test_a_deterministic_site_is_not_a_finding_and_an_unwitnessed_one_is_reported_apart(lint):
    assert "keys_only" not in names(lint.findings) | names(lint.unwitnessed)
    # the facts of an upstream model (its GROUP BY key) reach the models that read it
    assert "totals_latest" not in names(lint.findings) | names(lint.unwitnessed)
    assert names(lint.unwitnessed) == {"exploded"}
    assert not (names(lint.unwitnessed) & names(lint.findings))
    assert {name.rsplit(".", 1)[-1] for name, _ in lint.skipped} >= {"grow"}


def test_counts_add_up(lint):
    summary = lint.summary()
    assert summary["findings"] == 3 and summary["unwitnessed"] == 1
    assert summary["sites"] >= summary["deterministic_sites"] + summary["finding_sites"] + summary["unwitnessed_sites"]
    json.dumps(lint.to_json())


def test_a_declared_key_removes_a_finding(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/events.sqlx", 'config { type: "declaration", schema: "raw", name: "events" }\n')
    write(tmp_path, "definitions/keyed.sqlx", KEYED + 'SELECT id, user_id, ts FROM ${ref("raw", "events")}\n')
    write(
        tmp_path, "definitions/latest.sqlx",
        TABLE + 'SELECT id, user_id, ts, value FROM ${ref("keyed")}\n'
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC, id) = 1\n",
    )
    result = lint_project(tmp_path)
    assert result.findings == [] and result.unwitnessed == []


def test_command_prints_counts_and_json(tmp_path, capsys):
    build(tmp_path)
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "3 models with a replaying witness" in out and "latest" in out and "fix:" in out
    assert main([str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["findings"] == 3 and all(replay(f["witness"]) for f in data["findings"])
    assert main([str(tmp_path / "missing")]) == 2


def test_command_is_registered():
    from kumosql import __main__

    assert __main__.resolve("ties") == "kumosql.tie_lint:main"


@pytest.fixture
def ui_server():
    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_route_needs_a_loaded_project(ui_server, monkeypatch):
    from kumosql import live_graph

    monkeypatch.setattr(live_graph, "loaded", lambda: None)
    with pytest.raises(HTTPError) as error:
        urlopen(Request(ui_server + "/api/ties/run", method="POST", data=b"{}", headers={"Content-Type": "application/json"}))
    assert error.value.code == 400


def test_route_lints_the_loaded_project_in_the_background(ui_server, tmp_path, monkeypatch):
    import time

    from kumosql import live_graph, prover_context

    pipeline = build(tmp_path)
    monkeypatch.setattr(live_graph, "loaded", lambda: {"pipeline": pipeline})
    monkeypatch.setattr(prover_context, "current_schema", lambda: from_pipeline(pipeline))
    with urlopen(ui_server + "/api/ties") as response:
        assert json.load(response)["state"] in ("idle", "done", "cancelled", "error")
    request = Request(ui_server + "/api/ties/run", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        assert json.load(response)["state"] == "running"
    deadline = time.time() + 120
    while time.time() < deadline:
        with urlopen(ui_server + "/api/ties") as response:
            status = json.load(response)
        if status["state"] != "running":
            break
        time.sleep(0.2)
    assert status["state"] == "done"
    assert status["result"]["summary"]["findings"] == 3
