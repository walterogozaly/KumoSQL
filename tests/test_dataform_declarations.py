"""Refs resolved through Dataform declarations (``.sqlx`` and JavaScript) instead of the default schema."""

from kumosql import load_sqlx_project
from kumosql.pipeline_loading import js_declared_targets
from kumosql.pipeline_types import Target

DEFAULT = Target("proj", "analytics", "")


def project(tmp_path, files):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n")
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def reads(pl, model="proj.analytics.m"):
    return set(pl.upstream[model])


def test_sqlx_declaration_with_its_own_schema_and_database(tmp_path):
    pl = project(tmp_path, {
        "definitions/src.sqlx": 'config { type: "declaration", database: "other", schema: "raw", name: "orders" }',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"other.raw.orders"}


def test_js_declare_resolves_ref(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'declare({ schema: "raw", name: "orders" });\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"proj.raw.orders"}


def test_declarations_in_a_literal_loop_and_in_includes(tmp_path):
    pl = project(tmp_path, {
        "includes/decl.js": 'const schema = "raw";\n["orders", "users"].forEach((t) => declare({ schema, name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")} JOIN ${ref("users")} USING (id)',
    })
    assert reads(pl) == {"proj.raw.orders", "proj.raw.users"}


def test_declaration_without_schema_uses_project_defaults():
    declared, _, complete = js_declared_targets('declare({ name: "t" })', DEFAULT)
    assert declared == [Target("proj", "analytics", "t")] and complete


def test_dynamic_declarations_leave_unlisted_refs_unresolved(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == set()
    kinds = {d.code for d in pl.diagnostics}
    assert {"js_declaration_dynamic", "unsupported_ref"} <= kinds


def test_a_name_declared_in_two_schemas_is_not_guessed(tmp_path):
    pl = project(tmp_path, {
        "definitions/a.js": 'declare({ schema: "a", name: "t" }); declare({ schema: "b", name: "t" });',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("t")}',
        "definitions/n.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a", "t")}',
    })
    assert reads(pl) == set()
    assert reads(pl, "proj.analytics.n") == {"proj.a.t"}


def test_without_any_declaration_the_default_schema_still_applies(tmp_path):
    pl = project(tmp_path, {"definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("t")}'})
    assert {d.key for d in pl.models["proj.analytics.m"].declared_dependencies} == {"proj.analytics.t"}
