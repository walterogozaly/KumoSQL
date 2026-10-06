import subprocess

import pytest

from kumosql import shared_models
from kumosql.pipeline_types import DuplicateGroup, DuplicateOccurrence
from kumosql.shared_models import (
    DIFFERS,
    PROVEN,
    PROVEN_WITH_ASSUMPTIONS,
    UNCHANGED,
    UNKNOWN,
    SharedModelError,
    cte_bodies,
    extract_shared_model,
    load_files,
    repeated_ctes,
)

SETTINGS = "defaultProject: acme\ndefaultDataset: analytics\ndataformCoreVersion: 3.0.0\n"
DECLARATIONS = {
    "definitions/sources/orders.sqlx": 'config { type: "declaration", schema: "raw", name: "orders" }\n',
    "definitions/sources/customers.sqlx": 'config { type: "declaration", schema: "raw", name: "customers" }\n',
}
REVENUE = """config {
  type: "table"
}

WITH paid AS (
  -- paid orders (a comment with a parenthesis: )
  SELECT o.order_id, o.customer_id, o.amount
  FROM ${ref("orders")} AS o
  WHERE o.status = 'paid)' AND o.amount > 0
),
totals AS (SELECT customer_id, SUM(amount) AS revenue FROM paid GROUP BY customer_id)
SELECT * FROM totals
"""
TOP = """config { type: "view" }

with paid as (
    SELECT o.order_id, o.customer_id, o.amount
    FROM ${ref("orders")} AS o
    WHERE o.amount > 0 AND o.status = 'paid)'
)
SELECT c.name, COUNT(*) AS orders
FROM paid JOIN ${ref("customers")} c ON c.customer_id = paid.customer_id
GROUP BY c.name
"""
REPORT = 'config { type: "view" }\nSELECT * FROM ${ref("revenue")} WHERE revenue > 100\n'
FILTER_BASE = (
    "SELECT o.order_id, o.customer_id, o.amount, o.status, o.created_at, o.currency, "
    "o.region, o.channel, o.payment_method, o.campaign_id "
    "FROM ${ref(\"orders\")} AS o WHERE o.amount > 0"
)


def project(**models: str) -> dict[str, str]:
    files = {"workflow_settings.yaml": SETTINGS, **DECLARATIONS}
    files.update({f"definitions/marts/{name}.sqlx": text for name, text in models.items()})
    return files


def only_group(files):
    groups = repeated_ctes(load_files(files), files)
    assert len(groups) == 1
    return groups[0]


def test_cte_bodies_skip_comments_strings_and_config_blocks():
    text = 'config { type: "view", description: "x AS (" }\nWITH a AS (SELECT ")" AS c /* ) */ FROM t), b AS (SELECT 1 AS d)\nSELECT * FROM a'
    (start, end), = cte_bodies(text, "a")
    assert text[start:end] == 'SELECT ")" AS c /* ) */ FROM t'
    (start, end), = cte_bodies(text, "B")
    assert text[start:end] == "SELECT 1 AS d"
    assert cte_bodies(text, "c") == []


def test_repeated_cte_becomes_a_shared_model_and_every_consumer_is_proven(tmp_path):
    files = project(revenue=REVENUE, top_customers=TOP, report=REPORT)
    group = only_group(files)
    assert group.extractable
    assert [site.model for site in group.sites] == ["acme.analytics.revenue", "acme.analytics.top_customers"]

    patch = extract_shared_model(files, group.id)

    assert patch.new_file == "definitions/marts/paid.sqlx"
    new = patch.files[patch.new_file]
    assert new.startswith('config {\n  type: "view"\n}\n')
    assert '${ref("orders")}' in new and "-- paid orders" in new
    # only the CTE body changes
    assert 'WITH paid AS (\n  SELECT order_id, customer_id, amount\n  FROM ${ref("paid")}\n),\ntotals AS' in patch.files["definitions/marts/revenue.sqlx"]
    assert 'with paid as (\n  SELECT order_id, customer_id, amount\n  FROM ${ref("paid")}\n)\nSELECT c.name' in patch.files["definitions/marts/top_customers.sqlx"]
    assert patch.files["definitions/marts/revenue.sqlx"].endswith("SELECT * FROM totals\n")

    labels = {check.model: (check.role, check.label) for check in patch.checks}
    assert labels["acme.analytics.revenue"][1] in (PROVEN, PROVEN_WITH_ASSUMPTIONS)
    assert labels["acme.analytics.top_customers"][1] in (PROVEN, PROVEN_WITH_ASSUMPTIONS)
    assert labels["acme.analytics.report"] == ("downstream", UNCHANGED)
    assert patch.verdict in (PROVEN, PROVEN_WITH_ASSUMPTIONS)
    if patch.verdict == PROVEN_WITH_ASSUMPTIONS:
        assert patch.assumptions

    # the diff applies with git
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (tmp_path / "change.diff").write_text(patch.diff)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", "change.diff"], cwd=tmp_path, check=True)
    for path, text in patch.files.items():
        assert (tmp_path / path).read_text() == text


def test_name_and_kind_are_chosen_and_taken_names_are_refused():
    files = project(revenue=REVENUE, top_customers=TOP)
    group = only_group(files)
    patch = extract_shared_model(files, group.id, name="paid_orders", kind="table")
    assert patch.new_file == "definitions/marts/paid_orders.sqlx"
    assert 'type: "table"' in patch.files[patch.new_file]
    assert '${ref("paid_orders")}' in patch.files["definitions/marts/revenue.sqlx"]
    with pytest.raises(SharedModelError, match="already exists"):
        extract_shared_model(files, group.id, name="revenue")
    with pytest.raises(SharedModelError, match="letters"):
        extract_shared_model(files, group.id, name="bad-name")
    with pytest.raises(SharedModelError, match="kind"):
        extract_shared_model(files, group.id, kind="incremental")
    with pytest.raises(SharedModelError, match="no repeated"):
        extract_shared_model(files, "0000")


def test_suggested_name_taken_by_a_model_gets_a_suffix():
    files = project(revenue=REVENUE, top_customers=TOP, paid='config { type: "view" }\nSELECT 1 AS x\n')
    patch = extract_shared_model(files, only_group(files).id)
    assert patch.name == "paid_shared"


def test_a_copy_that_only_looks_the_same_is_never_proven():
    # Aliases are case-insensitive: inner A shadows outer a in the first copy, b does not in the second.
    first = """config { type: "table" }
WITH counted AS (
  SELECT a.x, (SELECT COUNT(*) AS n FROM ${ref("customers")} AS A WHERE a.x = A.x) AS n
  FROM ${ref("orders")} AS a
)
SELECT * FROM counted
"""
    second = """config { type: "table" }
WITH counted AS (
  SELECT a.x, (SELECT COUNT(*) AS n FROM ${ref("customers")} AS b WHERE a.x = b.x) AS n
  FROM ${ref("orders")} AS a
)
SELECT * FROM counted
"""
    files = project(first=first, second=second)
    pipeline = load_files(files)
    # Whatever the duplicate detector says about them, group the two copies as if it had matched them.
    forced = DuplicateGroup("forced", 20, "", (
        DuplicateOccurrence("acme.analytics.first", "cte:counted"),
        DuplicateOccurrence("acme.analytics.second", "cte:counted"),
    ))
    pipeline.duplicate_selects = lambda min_nodes=12: [forced]
    patch = extract_shared_model(files, "forced", pipeline=pipeline)
    labels = {c.model: c.label for c in patch.checks}
    # the shared model is the first copy, so the second can only be shown to differ or left unknown
    assert labels["acme.analytics.first"] in (PROVEN, PROVEN_WITH_ASSUMPTIONS)
    assert labels["acme.analytics.second"] in (UNKNOWN, DIFFERS)
    assert patch.verdict in (UNKNOWN, DIFFERS)


def test_dataform_expressions_volatile_values_and_outer_ctes_are_not_offered():
    with_var = REVENUE.replace("o.amount > 0", "o.amount > ${dataform.projectConfig.vars.floor}")
    group = only_group(project(revenue=with_var, top_customers=TOP.replace("o.amount > 0", "o.amount > ${dataform.projectConfig.vars.floor}")))
    assert not group.extractable and "Dataform expression" in group.problem

    clock = REVENUE.replace("o.amount > 0", "o.created < CURRENT_TIMESTAMP()")
    files = project(revenue=clock, top_customers=TOP.replace("o.amount > 0", "o.created < CURRENT_TIMESTAMP()"))
    group = only_group(files)
    assert not group.extractable and "nondeterminism" in group.problem
    with pytest.raises(SharedModelError):
        extract_shared_model(files, group.id)

    outer = """config { type: "table" }
WITH base AS (SELECT * FROM ${ref("orders")}),
paid AS (SELECT b.order_id, b.customer_id, b.amount FROM base AS b WHERE b.status = 'paid' AND b.amount > 0)
SELECT * FROM paid
"""
    group = only_group(project(first=outer, second=outer.replace("SELECT * FROM paid", "SELECT order_id FROM paid")))
    assert not group.extractable and "base" in group.problem


def test_near_duplicate_extra_filter_ctes_make_a_source_preserving_checked_patch():
    first = f'''config {{ type: "table" }}
WITH base AS ({FILTER_BASE})
SELECT order_id, amount FROM base
'''
    second = f'''config {{ type: "view" }}
WITH paid AS ({FILTER_BASE} AND o.status = 'paid')
SELECT order_id FROM paid
'''
    files = project(first=first, second=second, reader='config { type: "view" }\nSELECT order_id FROM ${ref("second")}\n')
    pipeline = load_files(files)
    [group] = [
        item for item in shared_models._shared_model_groups(pipeline, files, min_nodes=1)
        if item.kind == "extra_filters"
    ]

    assert group.extractable
    assert {site.residual_filters for site in group.sites} == {(), ("status = 'paid'",)}
    patch = extract_shared_model(files, group.id, pipeline=pipeline, min_nodes=8)

    assert patch.new_file == "definitions/marts/base.sqlx"
    assert '${ref("orders")}' in patch.files[patch.new_file]
    assert 'FROM ${ref("base")}' in patch.files["definitions/marts/first.sqlx"]
    assert 'FROM ${ref("base")}' in patch.files["definitions/marts/second.sqlx"]
    assert "WHERE status = 'paid'" in patch.files["definitions/marts/second.sqlx"]
    assert "definitions/marts/reader.sqlx" not in patch.files
    assert {check.role for check in patch.checks if check.model.endswith((".first", ".second"))} == {"edited"}
    assert next(check for check in patch.checks if check.model.endswith(".reader")).role == "downstream"
    assert all(check.label in (PROVEN, PROVEN_WITH_ASSUMPTIONS) for check in patch.checks if check.role == "edited")
    assert next(check for check in patch.checks if check.model.endswith(".reader")).label == UNCHANGED


def test_near_duplicate_filter_requires_projected_columns_and_a_view():
    first = f'''config {{ type: "table" }}
WITH base AS ({FILTER_BASE.replace(", o.status", "")})
SELECT order_id, amount FROM base
'''
    second = f'''config {{ type: "view" }}
WITH paid AS ({FILTER_BASE.replace(", o.status", "")} AND o.status = 'paid')
SELECT order_id FROM paid
'''
    files = project(first=first, second=second)
    pipeline = load_files(files)
    [group] = [
        item for item in shared_models._shared_model_groups(pipeline, files, min_nodes=1)
        if item.kind == "extra_filters"
    ]

    assert not group.extractable
    assert "does not output" in group.problem
    with pytest.raises(SharedModelError, match="does not output"):
        extract_shared_model(files, group.id, pipeline=pipeline, min_nodes=1)

    supported = project(first=first, second=second.replace("o.status = 'paid'", "o.amount < 100"))
    supported_pipeline = load_files(supported)
    [supported_group] = [
        item for item in shared_models._shared_model_groups(supported_pipeline, supported, min_nodes=1)
        if item.kind == "extra_filters" and item.extractable
    ]
    with pytest.raises(SharedModelError, match="only be extracted into a view"):
        extract_shared_model(supported, supported_group.id, kind="table", pipeline=supported_pipeline, min_nodes=1)


def test_near_duplicate_patch_reports_a_failed_proof_as_unknown():
    from kumosql.smt_equivalence import SmtStatus

    first = f'''config {{ type: "view" }}
WITH base AS ({FILTER_BASE})
SELECT order_id FROM base
'''
    second = f'''config {{ type: "view" }}
WITH paid AS ({FILTER_BASE} AND o.amount < 100)
SELECT order_id FROM paid
'''
    files = project(first=first, second=second)
    pipeline = load_files(files)
    [group] = [
        item for item in shared_models._shared_model_groups(pipeline, files, min_nodes=1)
        if item.kind == "extra_filters" and item.extractable
    ]

    class Different:
        status = SmtStatus.NOT_EQUIVALENT
        reason = "counterexample"
        assumptions = ()

    patch = extract_shared_model(
        files, group.id, pipeline=pipeline, min_nodes=1,
        prove=lambda _old, _new: Different(),
    )

    assert patch.verdict == UNKNOWN
    assert all(check.label == UNKNOWN for check in patch.checks if check.role == "edited")


def test_downstream_reader_of_an_unproven_model_is_unknown(monkeypatch):
    files = project(revenue=REVENUE, top_customers=TOP, report=REPORT)

    class Unknown:
        status = None
        reason = "gave up"
        assumptions = ()

    patch = extract_shared_model(files, only_group(files).id, prove=lambda old, new: Unknown())
    labels = {c.model: c.label for c in patch.checks}
    assert labels["acme.analytics.revenue"] == UNKNOWN
    assert labels["acme.analytics.report"] == UNKNOWN
    assert patch.verdict == UNKNOWN


def test_cli_lists_groups_and_writes_the_patch(tmp_path, capsys):
    files = project(revenue=REVENUE, top_customers=TOP)
    for path, text in files.items():
        target = tmp_path / "proj" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    assert shared_models.main([str(tmp_path / "proj")]) == 0
    assert '"extractable": true' in capsys.readouterr().out
    group = only_group(files)
    code = shared_models.main([str(tmp_path / "proj"), group.id, "--patch", str(tmp_path / "out.diff")])
    assert code == 0
    assert (tmp_path / "out.diff").read_text().startswith("diff --git a/definitions/marts/paid.sqlx")


@pytest.fixture
def ui_server():
    from threading import Thread

    from kumosql.ui import UIHandler, UIServer

    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_page_lists_groups_and_returns_the_checked_patch(ui_server, monkeypatch):
    import json
    from urllib.error import HTTPError
    from urllib.request import Request
    from ui_http import urlopen

    from kumosql import live_graph

    files = project(revenue=REVENUE, top_customers=TOP, report=REPORT)
    pipeline = load_files(files)
    pipeline.source_files = files
    monkeypatch.setattr(live_graph, "loaded", lambda: {"pipeline": pipeline, "label": "demo"})

    with urlopen(ui_server + "/shared-models") as response:
        assert b"Shared models" in response.read()
    with urlopen(ui_server + "/assets/shared-models.js") as response:
        assert b"/api/shared-models/patch" in response.read()
    with urlopen(ui_server + "/api/shared-models") as response:
        data = json.load(response)
    assert data["loaded"] and data["files_available"]
    (group,) = data["groups"]
    assert group["extractable"] and group["suggested_name"] == "paid"

    def post(body):
        request = Request(ui_server + "/api/shared-models/patch", method="POST", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    patch = post({"id": group["id"], "name": "paid_orders", "kind": "table"})
    assert patch["new_file"] == "definitions/marts/paid_orders.sqlx"
    assert patch["verdict"] in (PROVEN, PROVEN_WITH_ASSUMPTIONS)
    assert {c["model"]: c["label"] for c in patch["checks"]}["acme.analytics.report"] == UNCHANGED
    assert patch["diff"].count("diff --git") == 3
    with pytest.raises(HTTPError) as error:
        post({"id": "nope"})
    assert error.value.code == 400

    pipeline.source_files = None
    with pytest.raises(HTTPError) as error:
        post({"id": group["id"]})
    assert error.value.code == 400


def test_loaded_project_keeps_its_source_files():
    from kumosql import live_graph

    files = project(revenue=REVENUE, top_customers=TOP)
    pipeline = live_graph.pipeline_from_files(files)
    assert pipeline.source_files == files
