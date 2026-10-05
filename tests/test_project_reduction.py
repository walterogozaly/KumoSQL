"""Project reduction: keep chosen outputs of a Dataform project and prove the smallest project that makes them."""

import json
import re
import shutil
import subprocess

import pytest
import sqlglot

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


def test_drop_only_deletes_what_is_not_needed_and_rewrites_nothing(tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    result = reduce_project(root, ["rpt_revenue"], rewrite=False)
    assert result.verified and result.moves == [] and result.changed == []
    assert {entry["model"] for entry in result.removed} == {"shop.an.rpt_events", "shop.raw.customers", "shop.raw.events"}
    assert all(change.action == "delete" for change in result.files)


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


def test_project_variables_as_names_and_as_values_are_moved(tmp_path):
    files = {
        "workflow_settings.yaml": SETTINGS + "  raw_schema: raw\n",
        "definitions/sources/orders.sqlx": _declare("orders"),
        # a variable as a value: an unknown constant, the same wherever it is written, never the string 'paid'
        "definitions/stg_var.sqlx": _sqlx(
            '  type: "view"', 'SELECT order_id, amount, status\nFROM ${ref("orders")}\nWHERE status = "${dataform.projectConfig.vars.paid}"'),
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
    assert not any(entry["model"] == "shop.an.stg_var" for entry in result.fixed)
    assert "fold shop.an.stg_var into shop.an.rpt_var" in result.moves
    assert "fold shop.an.stg_name into shop.an.rpt_name" in result.moves
    patched = _patched(tmp_path, root, result)
    folded = (patched / "definitions/rpt_name.sqlx").read_text(encoding="utf-8")
    assert "`${dataform.projectConfig.vars.raw_schema}.orders`" in folded and "stg_name" not in folded
    # the variable is written back as it was written, quotes included, and both conditions stay
    value = (patched / "definitions/rpt_var.sqlx").read_text(encoding="utf-8")
    assert '"${dataform.projectConfig.vars.paid}"' in value and "'paid'" in value and "stg_var" not in value
    for text in (folded, value):
        assert "__kumo_" not in text and "__sqlx_token" not in text
    assert any("project variable" in a for a in result.to_json()["assumptions"])
    _git_apply(root, result.patch())


# ------------------------------------------------------------------ project variables as values, run on DuckDB

ORDERS_ROWS = [(1, 10, "paid"), (2, 20, "paid"), (3, 5, "free"), (4, 7, "x"), (5, 3, "x"), (6, 9, None)]
VALUE_FILES = {
    "workflow_settings.yaml": SETTINGS,
    "definitions/sources/orders.sqlx": _declare("orders"),
}
VAR = "${dataform.projectConfig.vars.paid}"


def _compile(root, value):
    """``{name: SQL}`` of a project with ``paid`` bound to ``value``: ref() to names, the variable to its value."""

    out = {}
    for path in sorted((root / "definitions").rglob("*.sqlx")):
        text = path.read_text(encoding="utf-8")
        if '"declaration"' in text:
            continue
        body = re.sub(r'\$\{\s*ref\(\s*"(\w+)"\s*\)\s*\}', r"\1", text.split("}\n", 1)[1])
        body = re.sub(r"\$\{\s*dataform\.projectConfig\.vars\.paid\s*\}", value, body)
        assert "${" not in body, body
        out[path.stem] = sqlglot.transpile(body.strip(), read="bigquery", write="duckdb")[0]  # "x" is a string in BigQuery
    return out


def _run(root, value, output):
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE orders(order_id INT, amount INT, status VARCHAR)")
    connection.executemany("INSERT INTO orders VALUES (?, ?, ?)", ORDERS_ROWS)
    pending = _compile(root, value)
    while pending:
        built = False
        for name, sql in list(pending.items()):
            try:
                connection.execute(f"CREATE TABLE {name} AS {sql}")
            except duckdb.CatalogException:
                continue
            del pending[name]
            built = True
        assert built, f"cannot build {sorted(pending)}"
    return sorted(connection.execute(f"SELECT * FROM {output}").fetchall(), key=repr)


def _same_on_other_bindings(original, patched, output):
    """The kept output returns the same rows in both projects with the variable bound to each of three values."""

    seen = []
    for value in ("paid", "x", "never_used"):
        rows = _run(original, value, output)
        assert rows == _run(patched, value, output), value
        seen.append(rows)
    assert seen[0] != seen[1]  # the variable matters: a reduction that ignored it would show


def test_a_variable_is_not_the_string_it_currently_holds(tmp_path):
    # status = <paid> AND status = 'x' is not a contradiction when the variable is 'x'
    files = {
        **VALUE_FILES,
        "definitions/stg_var.sqlx": _sqlx(
            '  type: "view"', f'SELECT order_id, amount, status\nFROM ${{ref("orders")}}\nWHERE status = "{VAR}"'),
        "definitions/rpt_x.sqlx": _sqlx(
            '  type: "table"', "SELECT order_id, amount\nFROM ${ref(\"stg_var\")}\nWHERE status = 'x'"),
    }
    root = _write(tmp_path / "false_proof", files)
    result = reduce_project(root, ["rpt_x"], factor=False)
    assert result.verified and "fold shop.an.stg_var into shop.an.rpt_x" in result.moves
    patched = _patched(tmp_path, root, result)
    text = (patched / "definitions/rpt_x.sqlx").read_text(encoding="utf-8")
    assert VAR in text and "'x'" in text
    _same_on_other_bindings(root, patched, "rpt_x")


def test_the_same_variable_in_two_models_is_the_same_value(tmp_path):
    body = f'SELECT order_id, amount\nFROM ${{ref("orders")}}\nWHERE status = "{VAR}"'
    files = {
        **VALUE_FILES,
        "definitions/lo.sqlx": _sqlx('  type: "view"', body),
        "definitions/hi.sqlx": _sqlx('  type: "view"', body),
        "definitions/rpt.sqlx": _sqlx(
            '  type: "table"', 'SELECT l.order_id, l.amount + h.amount AS twice\n'
                              'FROM ${ref("lo")} AS l\nJOIN ${ref("hi")} AS h ON l.order_id = h.order_id'),
    }
    root = _write(tmp_path / "same", files)
    result = reduce_project(root, ["rpt"], factor=False)
    assert result.verified and result.improved
    assert result.actions_after == 1  # both views folded: the two copies of the variable are one value
    patched = _patched(tmp_path, root, result)
    text = (patched / "definitions/rpt.sqlx").read_text(encoding="utf-8")
    assert f'"{VAR}"' in text and '${ref("lo")}' not in text and '${ref("hi")}' not in text
    _same_on_other_bindings(root, patched, "rpt")


def test_a_variable_and_the_literal_it_holds_are_not_merged(tmp_path):
    files = {
        **VALUE_FILES,
        "definitions/by_var.sqlx": _sqlx(
            '  type: "view"', f'SELECT order_id\nFROM ${{ref("orders")}}\nWHERE status = "{VAR}"'),
        "definitions/by_literal.sqlx": _sqlx(
            '  type: "view"', "SELECT order_id\nFROM ${ref(\"orders\")}\nWHERE status = 'paid'"),
        "definitions/rpt.sqlx": _sqlx(
            '  type: "table"', 'SELECT order_id FROM ${ref("by_var")}\nUNION ALL\nSELECT order_id FROM ${ref("by_literal")}'),
    }
    root = _write(tmp_path / "not_merged", files)
    result = reduce_project(root, ["rpt"], factor=False)
    assert result.verified
    assert not any(move.startswith("merge") for move in result.moves)
    patched = _patched(tmp_path, root, result)
    text = (patched / "definitions/rpt.sqlx").read_text(encoding="utf-8")
    assert VAR in text and "'paid'" in text
    _same_on_other_bindings(root, patched, "rpt")


def test_variables_in_lists_and_either_quote_survive_reduction_on_other_values(tmp_path):
    files = {
        **VALUE_FILES,
        "definitions/stg.sqlx": _sqlx(
            '  type: "view"',
            'SELECT order_id, amount, status\nFROM ${ref("orders")}\n'
            f"WHERE status IN (\"{VAR}\", 'free') AND amount > 0"),
        "definitions/rpt.sqlx": _sqlx(
            '  type: "table"',
            'SELECT status, COUNT(*) AS n, SUM(amount) AS total\nFROM ${ref("stg")}\n'
            f"WHERE status <> 'free' OR status = '{VAR}'\nGROUP BY status"),
    }
    root = _write(tmp_path / "lists", files)
    result = reduce_project(root, ["rpt"], factor=False)
    assert result.verified
    patched = _patched(tmp_path, root, result)
    text = (patched / "definitions/rpt.sqlx").read_text(encoding="utf-8")
    assert "__kumo_" not in text and VAR in text
    _same_on_other_bindings(root, patched, "rpt")


def test_a_variable_inside_a_longer_string_stays_as_written(tmp_path):
    files = {
        **VALUE_FILES,
        "definitions/stg_embedded.sqlx": _sqlx(
            '  type: "view"', f'SELECT order_id, status\nFROM ${{ref("orders")}}\nWHERE status = "pre_{VAR}"'),
        "definitions/rpt.sqlx": _sqlx('  type: "table"', 'SELECT COUNT(*) AS n\nFROM ${ref("stg_embedded")}'),
    }
    root = _write(tmp_path / "embedded", files)
    result = reduce_project(root, ["rpt"], factor=False)
    assert result.verified
    fixed = {entry["model"]: entry["why"] for entry in result.fixed}
    assert "longer string" in fixed["shop.an.stg_embedded"]
    patched = _patched(tmp_path, root, result)
    assert (patched / "definitions/stg_embedded.sqlx").read_text(encoding="utf-8") == files["definitions/stg_embedded.sqlx"]


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


def test_only_a_string_that_is_exactly_one_token_becomes_an_unknown_value():
    from kumosql import project_reduction as pr

    a, b = pr._token("${vars.a}"), pr._token("${vars.b}")
    tokens = {a: "${vars.a}", b: "${vars.b}"}
    sql = f"""SELECT x FROM t WHERE s = "{a}" AND u = '{a}' AND v = '{b}' AND w = 'p_{a}' AND z = r'{a}'
AND y = '''{a}''' AND q = '{a}' '{b}' AND d IN ('{b}')"""
    out, atoms = pr._value_atoms(sql, tokens)
    d_a, s_a, s_b = (f"__kumo_v_{a[9:-2]}_d()", f"__kumo_v_{a[9:-2]}_s()", f"__kumo_v_{b[9:-2]}_s()")
    assert atoms == {d_a: '"${vars.a}"', s_a: "'${vars.a}'", s_b: "'${vars.b}'"}  # quote style is part of the value
    assert f"s = {d_a} AND u = {s_a} AND v = {s_b}" in out and f"d IN ({s_b})" in out
    # a token inside a longer string, a raw or triple-quoted string and adjacent literals stay as they are, and are refused
    assert f"'p_{a}'" in out and f"r'{a}'" in out and f"'''{a}'''" in out and f"'{a}' '{b}'" in out
    assert pr._value_tokens(out)
    assert not pr._value_tokens(f"SELECT x FROM {a}.t WHERE s = {s_a}")  # a name is not a value
    # each is restored byte for byte; an atom the reduction does not know is never written out
    assert pr._restore_tokens(f"s = {d_a} AND u = {s_a}", {**tokens, **atoms}) == "s = \"${vars.a}\" AND u = '${vars.a}'"
    with pytest.raises(pr._WriteBackError):
        pr._restore_tokens(f"s = {s_a}", tokens)
