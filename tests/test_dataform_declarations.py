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


DYNAMIC = {
    "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
    "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
}


def load(tmp_path, compiled):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n")
    for name, text in DYNAMIC.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path, compiled_targets=compiled)


def test_dynamic_declarations_fall_back_to_the_compiled_graph(tmp_path):
    pl = load(tmp_path, lambda: [(("lake", "raw", "orders"), True)])
    assert reads(pl) == {"lake.raw.orders"}
    assert "lake.raw.orders" in pl.sources
    assert not any(d.code == "unsupported_ref" for d in pl.diagnostics)


def test_an_unreachable_compiled_graph_leaves_refs_unresolved(tmp_path):
    def broken():
        raise RuntimeError("no credentials")

    pl = load(tmp_path, broken)
    assert reads(pl) == set()
    assert {"compiled_graph_unavailable", "unsupported_ref"} <= {d.code for d in pl.diagnostics}


def test_the_compiled_graph_is_not_asked_when_the_files_are_enough(tmp_path):
    def forbidden():
        raise AssertionError("should not be called")

    pl = project(tmp_path, {"definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("t")}'})
    assert pl is not None
    (tmp_path / "definitions/decl.js").write_text('declare({ schema: "raw", name: "t" });')
    assert load_sqlx_project(tmp_path, compiled_targets=forbidden) is not None


def test_compiled_targets_reads_the_newest_release_compilation(monkeypatch):
    from kumosql import workflow_configs as wc

    monkeypatch.setattr(wc, "search_for", lambda url: {"projects": ["p"], "location": "l"})
    monkeypatch.setattr(wc, "find_repositories", lambda *a: (["projects/p/locations/l/repositories/r"], []))
    seen = []

    def fake_list(url, key):
        seen.append(url)
        if key == "releaseConfigs":
            return [{"releaseCompilationResult": "projects/p/locations/l/repositories/r/compilationResults/c1"}]
        return [
            {"target": {"database": "d", "schema": "raw", "name": "orders"}, "declaration": {}},
            {"target": {"database": "d", "schema": "m", "name": "x"}, "relation": {}},
        ]

    monkeypatch.setattr(wc, "_list_all", fake_list)
    assert wc.compiled_targets("https://github.com/o/r") == [(("d", "raw", "orders"), True), (("d", "m", "x"), False)]
    assert seen[-1].endswith("compilationResults/c1:query")
