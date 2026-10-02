"""Refactor module: saved PROTECTED/EDITABLE classes and the proved-safe Pareto search."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

pytest.importorskip("z3")

from kumosql import load_sqlx_project, refactor, scopes
from kumosql.ui import UIHandler


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/o.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    view = 'config { type: "view" }\n'
    write(tmp_path, "definitions/stg1.sqlx", view + 'SELECT id, customer_id, amount, status FROM ${ref("raw","orders")}\n')
    write(tmp_path, "definitions/stg2.sqlx", view + 'SELECT id, customer_id, amount, status FROM ${ref("stg1")} WHERE amount > 0\n')
    write(tmp_path, "definitions/stg2b.sqlx", view + 'SELECT id, customer_id, amount, status FROM ${ref("stg1")} WHERE amount > 0\n')
    write(tmp_path, "definitions/unused.sqlx", view + 'SELECT id FROM ${ref("stg1")}\n')
    table = 'config { type: "table" }\n'
    write(tmp_path, "definitions/report.sqlx", table + 'SELECT customer_id, SUM(amount) AS total FROM ${ref("stg2")} WHERE status = \'paid\' GROUP BY customer_id\n')
    write(tmp_path, "definitions/report2.sqlx", table + 'SELECT status, COUNT(*) AS n FROM ${ref("stg2b")} GROUP BY status\n')
    return load_sqlx_project(tmp_path)


def classes(protected=(), editable=(), protected_scopes=(), editable_scopes=()):
    return refactor.Classes(
        refactor.Selection(tuple(protected_scopes), tuple(protected)),
        refactor.Selection(tuple(editable_scopes), tuple(editable)),
    )


def test_classes_are_validated_saved_and_loaded():
    assert refactor.load_classes() == refactor.Classes()
    saved = refactor.save_classes({"protected": {"models": ["a", "a", "b"]}, "editable": {"models": ["c"]}})
    assert saved.protected.models == ("a", "b")
    assert refactor.load_classes() == saved
    for bad in ("x", {"protected": []}, {"protected": {"models": [1]}}, {"editable": {"scopes": ["nope"]}}):
        with pytest.raises(ValueError):
            refactor.parse_classes(bad)


def test_scopes_can_define_a_class(project):
    scopes.save_scopes([scopes.parse_scope({"name": "reports", "rule": {"field": "name", "op": "prefix", "value": "report"}})])
    roles = refactor.classify(project, classes(protected_scopes=["reports"], editable=["stg1"], protected=["stg1"]))
    assert roles["proj.an.report"] == roles["proj.an.report2"] == "protected"
    assert roles["proj.an.stg1"] == "protected"  # protected wins over editable
    assert roles["proj.an.stg2"] == "frozen"


def test_search_finds_proved_simplifications_and_keeps_protected_tables(project):
    editable = ["stg1", "stg2", "stg2b", "unused"]
    result = refactor.search(project, classes(["report", "report2"], editable))
    assert result.observable == ["proj.an.report", "proj.an.report2"]
    assert result.rejected == 0 or result.tried > result.rejected
    models = {entry.models for entry in result.front}
    assert result.baseline.models == 6 and min(models) < 6
    for entry in result.front:
        kept = entry.sql_map
        assert "proj.an.report" in kept and "proj.an.report2" in kept  # protected tables still exist
    best = min(result.front, key=lambda e: e.models)
    assert "drop proj.an.unused" in best.moves[0]
    # the front is a Pareto front: nothing is dominated
    for a in result.front:
        assert not any(refactor._dominates(b, a) for b in result.front)
    data = result.to_json(project)
    assert data["classes"] == {"protected": 2, "editable": 4, "frozen": 0 + 0} or data["classes"]["protected"] == 2


def test_a_frozen_reader_exposes_the_model_it_reads(project):
    # stg2 is neither protected nor editable: it stays as written, so stg1 (which it reads) must stay equal
    result = refactor.search(project, classes(["report"], ["stg1", "unused", "stg2b", "report2"]))
    assert "proj.an.stg1" in result.observable
    for entry in result.front:
        assert "proj.an.stg1" in entry.sql_map
        assert entry.sql_map["proj.an.stg2"] == project.models["proj.an.stg2"].sql  # frozen SQL untouched


def test_unproved_moves_are_rejected_not_accepted(project):
    reads = refactor._Reads(project)
    original = {k: m.sql for k, m in project.models.items()}
    wrong = dict(original)
    wrong["proj.an.stg2"] = original["proj.an.stg2"].replace("amount > 0", "amount > 5")
    ok, _, why = refactor.check_observable(project, wrong, ["proj.an.report"], reads)
    assert not ok and why
    same = dict(original)
    ok, assumptions, _ = refactor.check_observable(project, same, ["proj.an.report"], reads)
    assert ok
    gone = {k: v for k, v in original.items() if k != "proj.an.report"}
    assert not refactor.check_observable(project, gone, ["proj.an.report"], reads)[0]


@pytest.fixture
def ui_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_refactor_page_and_settings_routes(ui_server):
    with urlopen(ui_server + "/refactor") as response:
        assert b"Refactor" in response.read()
    with urlopen(ui_server + "/assets/refactor.js") as response:
        assert b"/api/refactor" in response.read()
    request = Request(
        ui_server + "/api/settings/refactor", method="PUT",
        data=json.dumps({"protected": {"models": ["a"]}, "editable": {"models": ["b"]}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        assert json.load(response)["protected"]["models"] == ["a"]
    with urlopen(ui_server + "/api/refactor") as response:
        data = json.load(response)
    assert data["classes"]["editable"]["models"] == ["b"] and data["models"] == []
    bad = Request(ui_server + "/api/settings/refactor", method="PUT", data=b'{"protected": 3}', headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as error:
        urlopen(bad)
    assert error.value.code == 400
    run = Request(ui_server + "/api/refactor/run", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as error:
        urlopen(run)
    assert error.value.code == 400  # no project loaded
