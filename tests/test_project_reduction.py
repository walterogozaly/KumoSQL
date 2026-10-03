"""Project reduction: keep chosen outputs of a Dataform project and prove the smallest project that makes them."""

import json
import shutil
import subprocess

import pytest

pytest.importorskip("z3")

from kumosql.pipeline import load_sqlx_project
from kumosql.project_reduction import ReductionError, main, project_score, reduce_project

SETTINGS = "defaultProject: shop\ndefaultDataset: an\ndefaultAssertionDataset: an_checks\nvars:\n  paid: paid\n"


def _declare(name):
    return f'config {{\n  type: "declaration",\n  schema: "raw",\n  name: "{name}"\n}}\n'


def _sqlx(config, sql, header=""):
    return f"{header}config {{\n{config}\n}}\n\n{sql}\n"


SHOP = {
    "workflow_settings.yaml": SETTINGS,
    "definitions/sources/orders.sqlx": _declare("orders"),
    "definitions/sources/customers.sqlx": _declare("customers"),
    "definitions/sources/events.sqlx": _declare("events"),
    "definitions/staging/stg_orders.sqlx": _sqlx(
        '  type: "view",\n  tags: ["staging"]', 'SELECT order_id, customer_id, amount, status\nFROM ${ref("orders")}'),
    "definitions/staging/paid_orders.sqlx": _sqlx(
        '  type: "table",\n  dependencies: ["stg_orders_has_ids"]',
        'SELECT order_id, customer_id, amount\nFROM ${ref("stg_orders")}\nWHERE status = \'paid\''),
    "definitions/reports/rpt_revenue.sqlx": _sqlx(
        '  type: "table",\n  tags: ["reports"]',
        'SELECT customer_id, SUM(amount) AS revenue\nFROM ${ref("paid_orders")}\nGROUP BY customer_id',
        header="-- Revenue per customer.\n-- Owned by the finance team.\n\n"),
    "definitions/reports/rpt_events.sqlx": _sqlx('  type: "table"', 'SELECT COUNT(*) AS events\nFROM ${ref("events")}'),
    "definitions/assertions/stg_orders_has_ids.sqlx": _sqlx(
        '  type: "assertion"', 'SELECT *\nFROM ${ref("stg_orders")}\nWHERE order_id IS NULL'),
    "definitions/assertions/revenue_positive.sqlx": _sqlx(
        '  type: "assertion"', 'SELECT *\nFROM ${ref("rpt_revenue")}\nWHERE revenue < 0'),
    "definitions/assertions/events_seen.sqlx": _sqlx(
        '  type: "assertion"', 'SELECT *\nFROM ${ref("rpt_events")}\nWHERE events = 0'),
}


def _write(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _patched(tmp_path, root, result):
    copy = tmp_path / "patched"
    shutil.copytree(root, copy)
    result.apply(copy)
    return copy


def _git_apply(root, patch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    found = subprocess.run(["git", "apply", "--check", "-"], cwd=root, input=patch.encode("utf-8"), capture_output=True)
    assert found.returncode == 0, found.stderr.decode()


def test_one_output_kept_folds_its_chain_and_deletes_the_rest(tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    result = reduce_project(root, ["rpt_revenue"])
    assert result.verified and result.improved
    assert result.keep == ["shop.an.rpt_revenue"]
    assert result.proofs["shop.an.rpt_revenue"]["status"] == "proved"
    removed = {entry["model"]: entry["why"] for entry in result.removed}
    assert removed["shop.an.rpt_events"] == "not needed by any kept output"
    assert removed["shop.an.paid_orders"].startswith("folded into shop.an.rpt_revenue")
    assert removed["shop.raw.events"] == removed["shop.raw.customers"] == "no action that stays reads it"
    assert "shop.raw.orders" not in removed
    assert result.dropped_assertions == [{"model": "shop.an_checks.events_seen", "path": "definitions/assertions/events_seen.sqlx",
                                          "why": "reads shop.an.rpt_events, which is no longer in the project"}]
    assert {"model": "shop.an_checks.revenue_positive", "why": "assertion over actions that stay as they are"} in result.fixed
    assert result.actions_before == 7 and result.actions_after == 3  # declarations are not actions
    assert result.score_after < result.score_before

    patch = result.patch()
    assert "deleted file mode 100644" in patch
    _git_apply(root, patch)
    patched = _patched(tmp_path, root, result)
    report = (patched / "definitions/reports/rpt_revenue.sqlx").read_text(encoding="utf-8")
    # the header and config stay; the folded table's assertion gate is carried over; refs are written as the project writes them
    assert report.startswith("-- Revenue per customer.\n-- Owned by the finance team.\n\nconfig {\n")
    assert 'tags: ["reports"]' in report and 'dependencies: ["stg_orders_has_ids"]' in report
    assert '${ref("orders")}' in report and "paid_orders" not in report and "status = 'paid'" in report
    assert not (patched / "definitions/staging/paid_orders.sqlx").exists()
    assert (patched / "definitions/assertions/revenue_positive.sqlx").read_text(encoding="utf-8") == SHOP[
        "definitions/assertions/revenue_positive.sqlx"]
    pipeline = load_sqlx_project(patched)
    assert result.score_after == project_score(pipeline)
    assert "shop.an_checks.stg_orders_has_ids" in pipeline.models  # still gates the report

    data = result.to_json()
    json.dumps(data)
    assert data["verified"] and data["diff"] == patch and data["evidence"].startswith("proof")
    # the Shared models page's patch shape
    assert data["verdict"] == "proven_with_assumptions" and data["assumptions"] and data["diagnostics"] == []
    assert data["checks"][0]["model"] == "shop.an.rpt_revenue" and data["checks"][0]["role"] == "kept"
    assert "definitions/staging/paid_orders.sqlx" in data["changed_files"]


def test_kept_outputs_by_name_path_or_dataset_and_bad_names(tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    for name in ("an.rpt_events", "definitions/reports/rpt_events.sqlx", "shop.an.rpt_events"):
        assert reduce_project(root, [name], factor=False, max_seconds=5).keep == ["shop.an.rpt_events"]
    with pytest.raises(ReductionError, match="nope"):
        reduce_project(root, ["nope"])
    with pytest.raises(ReductionError, match="declaration"):
        reduce_project(root, ["orders"])


def test_incremental_tables_and_operations_stay_as_written(tmp_path):
    files = {
        "workflow_settings.yaml": SETTINGS,
        "definitions/sources/orders.sqlx": _declare("orders"),
        "definitions/inc_orders.sqlx": _sqlx(
            '  type: "incremental"',
            'SELECT order_id, amount\nFROM ${ref("orders")}\n'
            '${when(incremental(), `WHERE order_id > (SELECT MAX(order_id) FROM ${self()})`)}'),
        "definitions/stg_inc.sqlx": _sqlx('  type: "view"', 'SELECT order_id, amount\nFROM ${ref("inc_orders")}\nWHERE amount > 0'),
        "definitions/rpt_inc.sqlx": _sqlx('  type: "table"', 'SELECT COUNT(*) AS n\nFROM ${ref("stg_inc")}'),
        "definitions/ops/fix_orders.sqlx": _sqlx('  type: "operations"', 'DELETE FROM ${ref("orders")} WHERE order_id IS NULL'),
        "definitions/ops/drop_scratch.sqlx": _sqlx('  type: "operations"', "DROP TABLE IF EXISTS an.scratch"),
        "definitions/ops/mystery.sqlx": _sqlx('  type: "operations"', "CALL an.do_things()"),
    }
    root = _write(tmp_path / "inc", files)
    result = reduce_project(root, ["rpt_inc"])
    assert result.verified
    fixed = {entry["model"]: entry["why"] for entry in result.fixed}
    assert fixed["shop.an.inc_orders"].startswith("incremental table")
    assert "a kept action reads" in fixed["shop.an.fix_orders"]
    assert fixed["shop.an.mystery"] == "kept: what it writes is not known"
    removed = {entry["model"] for entry in result.removed}
    assert "shop.an.drop_scratch" in removed and "shop.an.stg_inc" in removed
    patched = _patched(tmp_path, root, result)
    assert (patched / "definitions/inc_orders.sqlx").read_text(encoding="utf-8") == files["definitions/inc_orders.sqlx"]
    assert '${ref("inc_orders")}' in (patched / "definitions/rpt_inc.sqlx").read_text(encoding="utf-8")


PAID_EU = ("SELECT o.customer_id, o.amount FROM ${ref(\"orders\")} AS o JOIN ${ref(\"customers\")} AS c "
           "ON o.customer_id = c.customer_id WHERE c.region = 'eu' AND o.status = 'paid'")
SHARED = {
    "workflow_settings.yaml": SETTINGS,
    "definitions/sources/orders.sqlx": _declare("orders"),
    "definitions/sources/customers.sqlx": _declare("customers"),
    "definitions/reports/rpt_a.sqlx": _sqlx(
        '  type: "table",\n  tags: ["finance"]',
        f"SELECT p.customer_id, SUM(p.amount) AS total\nFROM ({PAID_EU}) AS p\nGROUP BY p.customer_id"),
    "definitions/reports/rpt_b.sqlx": _sqlx(
        '  type: "table",\n  tags: ["sales"]',
        f"SELECT q.customer_id, COUNT(*) AS n\nFROM ({PAID_EU}) AS q\nGROUP BY q.customer_id"),
    "definitions/reports/rpt_c.sqlx": _sqlx(
        '  type: "table"', f"WITH paid_eu AS ({PAID_EU})\nSELECT MAX(amount) AS biggest\nFROM paid_eu"),
}
COLUMNS = {
    "shop.raw.orders": {"columns": {"order_id": "INT64", "customer_id": "INT64", "amount": "INT64", "status": "STRING"},
                        "key": ["order_id"]},
    "shop.raw.customers": {"columns": {"customer_id": "INT64", "region": "STRING"}, "key": ["customer_id"]},
}


def test_a_query_repeated_in_three_reports_becomes_one_shared_table(tmp_path):
    root = _write(tmp_path / "shared", SHARED)
    keep = ["rpt_a", "rpt_b", "rpt_c"]
    result = reduce_project(root, keep, source_columns=COLUMNS)
    assert result.verified and result.improved
    assert result.added == [{"model": "shop.an.paid_eu", "path": "definitions/reports/paid_eu.sqlx",
                             "readers": ["shop.an.rpt_a", "shop.an.rpt_b", "shop.an.rpt_c"]}]
    assert {proof["status"] for proof in result.proofs.values()} == {"proved"}
    patched = _patched(tmp_path, root, result)
    shared = (patched / "definitions/reports/paid_eu.sqlx").read_text(encoding="utf-8")
    assert shared.startswith('config {\n  type: "view"') and '"finance"' in shared and '"sales"' in shared
    assert "JOIN" in shared.upper()
    for name in ("rpt_a", "rpt_b", "rpt_c"):
        text = (patched / f"definitions/reports/{name}.sqlx").read_text(encoding="utf-8")
        assert '${ref("paid_eu")}' in text and "JOIN" not in text.upper()
    _git_apply(root, result.patch())
    plain = reduce_project(root, keep, source_columns=COLUMNS, factor=False)
    assert plain.added == [] and plain.score_after > result.score_after


def test_project_variables_as_names_are_moved_and_as_values_kept_as_written(tmp_path):
    files = {
        "workflow_settings.yaml": SETTINGS + "  raw_schema: raw\n",
        "definitions/sources/orders.sqlx": _declare("orders"),
        # a variable as a value: the prover would read it as one more string, different from 'paid'
        "definitions/stg_var.sqlx": _sqlx(
            '  type: "view"', 'SELECT order_id, amount\nFROM ${ref("orders")}\nWHERE status = "${dataform.projectConfig.vars.paid}"'),
        "definitions/rpt_var.sqlx": _sqlx(
            '  type: "table"', "SELECT SUM(amount) AS total\nFROM ${ref(\"stg_var\")}\nWHERE status = 'paid' AND amount > 0"),
        # a variable in a table name: the same expression is the same table wherever it is written
        "definitions/stg_name.sqlx": _sqlx(
            '  type: "view"', "SELECT order_id, amount\nFROM ${dataform.projectConfig.vars.raw_schema}.orders\nWHERE amount > 0"),
        "definitions/rpt_name.sqlx": _sqlx('  type: "table"', 'SELECT SUM(amount) AS total\nFROM ${ref("stg_name")}'),
    }
    root = _write(tmp_path / "vars", files)
    result = reduce_project(root, ["rpt_var", "rpt_name"], factor=False)
    assert result.verified
    assert {"model": "shop.an.stg_var", "why": "uses a project variable or constant as a value, which the prover cannot read"} in result.fixed
    assert result.moves == ["fold shop.an.stg_name into shop.an.rpt_name"]
    patched = _patched(tmp_path, root, result)
    assert (patched / "definitions/stg_var.sqlx").read_text(encoding="utf-8") == files["definitions/stg_var.sqlx"]
    folded = (patched / "definitions/rpt_name.sqlx").read_text(encoding="utf-8")
    assert "`${dataform.projectConfig.vars.raw_schema}.orders`" in folded and "stg_name" not in folded
    assert "__kumo_x_" not in folded and "__sqlx_token" not in folded
    _git_apply(root, result.patch())


def test_config_assertions_are_listed_when_their_table_goes_and_kept_when_awaited(tmp_path):
    files = {
        "workflow_settings.yaml": SETTINGS,
        "definitions/sources/orders.sqlx": _declare("orders"),
        "definitions/stg.sqlx": _sqlx('  type: "view",\n  assertions: {\n    nonNull: ["order_id"]\n  }',
                                      'SELECT order_id, amount\nFROM ${ref("orders")}\nWHERE amount > 0'),
        "definitions/rpt.sqlx": _sqlx('  type: "table"', 'SELECT SUM(amount) AS total\nFROM ${ref("stg")}'),
    }
    root = _write(tmp_path / "config_assertions", files)
    result = reduce_project(root, ["rpt"])
    assert result.verified and "shop.an.stg" in {entry["model"] for entry in result.removed}
    assert result.dropped_assertions == [{"model": "shop.an.stg", "path": None, "config": True,
                                          "why": "config assertions: its table was folded into shop.an.rpt"}]

    files["definitions/rpt.sqlx"] = _sqlx('  type: "table",\n  dependencies: ["an_stg_assertions_rowConditions"]',
                                          'SELECT SUM(amount) AS total\nFROM ${ref("stg")}')
    root = _write(tmp_path / "awaited", files)
    result = reduce_project(root, ["rpt"])
    assert result.verified and result.files == [] and result.dropped_assertions == []


def test_cli(tmp_path, capsys):
    root = _write(tmp_path / "shop", SHOP)
    assert main([str(root), "--keep", "nope"]) == 2
    capsys.readouterr()
    assert main([str(root), "--keep", "rpt_revenue", "--patch", "-"]) == 0
    assert capsys.readouterr().out.startswith("diff --git a/definitions/assertions/events_seen.sqlx")
    patch_file = tmp_path / "reduce.diff"
    assert main([str(root), "--keep", "rpt_revenue", "--patch", str(patch_file)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verified"] and "diff" not in data and patch_file.read_text(encoding="utf-8").startswith("diff --git")
    assert main([str(root), "--keep", "rpt_revenue", "--write"]) == 0
    assert not (root / "definitions/staging/paid_orders.sqlx").exists()
    assert "paid_orders" not in (root / "definitions/reports/rpt_revenue.sqlx").read_text(encoding="utf-8")


def test_declarations_stay_while_something_names_them(tmp_path):
    files = {
        "workflow_settings.yaml": SETTINGS,
        "definitions/sources/events.sqlx": _declare("events_*"),  # a wildcard table
        "definitions/sources/helper_only.sqlx": _declare("helper_only"),
        "definitions/sources/unused.sqlx": _declare("unused"),
        "includes/helpers.js": 'const source = "helper_only";\nmodule.exports = { source };\n',
        "definitions/rpt.sqlx": _sqlx('  type: "table"', 'SELECT COUNT(*) AS n\nFROM ${ref("events_*")}'),
    }
    root = _write(tmp_path / "declarations", files)
    result = reduce_project(root, ["rpt"])
    assert result.verified
    assert [entry["model"] for entry in result.removed] == ["shop.raw.unused"]
