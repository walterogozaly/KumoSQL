import json
from pathlib import Path

import pytest

from kumosql import relation_declarations
from kumosql.relation_refactor import RelationRefactorError, main, refactor_project, rewrite_consumer_query


SCHEMAS = {
    "table_y": {"col_old": "STRING"},
    "table_x": {"col_new": "STRING"},
}
FILTERED_SCHEMAS = {
    "table_y": {"col_old": "STRING", "category": "STRING", "is_current": "BOOL"},
    "table_x": {"category": "STRING", "col_new": "STRING", "is_active": "BOOL"},
}


def _declaration(*, scope=None):
    return relation_declarations.prepare(
        "SELECT col_old AS value FROM table_y",
        "SELECT col_new AS value FROM table_x",
        SCHEMAS,
        preferred_side="right",
        scope=scope or relation_declarations.Scope("all_snapshots"),
        declaration_id="39e18a5d-3cc8-4052-87f1-2309b761eb53",
    )


def _filtered_declaration():
    return relation_declarations.prepare(
        "SELECT col_old AS value, category FROM table_y WHERE is_current = TRUE",
        "SELECT category, col_new AS value FROM table_x WHERE is_active = TRUE",
        FILTERED_SCHEMAS,
        preferred_side="right",
        scope=relation_declarations.Scope("all_snapshots"),
        declaration_id="f6ef0e2b-59d6-4c71-a488-026e8f741995",
    )


def _project(root: Path, *models: tuple[str, str]) -> Path:
    root.mkdir(parents=True)
    (root / "workflow_settings.yaml").write_text(
        "defaultDatabase: demo\ndefaultSchema: raw\n", encoding="utf-8"
    )
    definitions = root / "definitions"
    definitions.mkdir()
    for name, query in models:
        (definitions / f"{name}.sqlx").write_text(
            f'config {{ type: "table" }}\n{query.strip()}\n', encoding="utf-8"
        )
    return root


def test_renamed_projection_preserves_its_public_output_name():
    result = rewrite_consumer_query(
        "SELECT col_old, COUNT(*) AS n FROM table_y GROUP BY col_old",
        _declaration(),
    )

    assert result.changed and not result.reason
    assert "col_new AS col_old" in result.sql
    assert "COUNT(*) AS n" in result.sql
    assert "GROUP BY\n  col_new" in result.sql
    assert "table_x" in result.sql


def test_qualified_rename_preserves_alias_and_unrelated_join_predicates():
    result = rewrite_consumer_query(
        "SELECT y.col_old AS old_value, z.other FROM table_y AS y "
        "JOIN table_z AS z ON y.col_old = z.old_value",
        _declaration(),
    )

    assert result.changed and not result.reason
    assert "table_x AS y" in result.sql
    assert "y.col_new AS old_value" in result.sql
    assert "z.other" in result.sql
    assert "y.col_new = z.old_value" in result.sql


def test_star_and_unqualified_join_columns_are_refused():
    declaration = _declaration()
    star = rewrite_consumer_query("SELECT * FROM table_y", declaration)
    nested_star = rewrite_consumer_query("SELECT STRUCT(y.*) AS row_value FROM table_y AS y", declaration)
    unqualified = rewrite_consumer_query(
        "SELECT col_old FROM table_y JOIN table_z ON table_y.col_old = table_z.id", declaration
    )

    assert not star.changed and "star projection" in star.reason
    assert not nested_star.changed and "star projection" in nested_star.reason
    assert not unqualified.changed and "unqualified columns" in unqualified.reason


def test_missing_scope_and_unmapped_consumer_column_are_refused():
    missing_scope = _declaration(scope=relation_declarations.Scope())
    result = rewrite_consumer_query("SELECT col_old FROM table_y", missing_scope)
    assert not result.changed and "freshness scope" in result.reason

    unmapped = rewrite_consumer_query("SELECT other FROM table_y", _declaration())
    assert not unmapped.changed and "undeclared column" in unmapped.reason


def test_distinct_declaration_sides_are_refused():
    declaration = relation_declarations.prepare(
        "SELECT DISTINCT col_old AS value FROM table_y",
        "SELECT col_new AS value FROM table_x",
        SCHEMAS,
        preferred_side="right",
        scope=relation_declarations.Scope("all_snapshots"),
    )
    result = rewrite_consumer_query("SELECT col_old FROM table_y", declaration)

    assert not result.changed and "declaration side" in result.reason


def test_filtered_declaration_replaces_only_an_exact_derived_query_and_keeps_column_order():
    declaration = _filtered_declaration()
    query = (
        "SELECT s.category, COUNT(*) AS n FROM ("
        "SELECT col_old AS value, category FROM demo.raw.table_y WHERE is_current = TRUE"
        ") AS s GROUP BY s.category"
    )
    result = rewrite_consumer_query(
        query,
        declaration,
        resolve_relation=lambda table: table.name.casefold(),
    )

    assert result.changed and not result.reason
    assert result.operation == "replace_exact_filtered_query"
    assert "FROM table_x" in result.sql
    assert "is_active = TRUE" in result.sql
    # The preferred query lists category first; the wrapper restores the old interface order.
    assert "SELECT\n    __kumo_preferred_relation.value AS value,\n    __kumo_preferred_relation.category AS category" in result.sql
    assert "GROUP BY\n  s.category" in result.sql


def test_filtered_declaration_does_not_rewrite_raw_or_differently_filtered_readers():
    declaration = _filtered_declaration()
    direct = rewrite_consumer_query(
        "SELECT col_old FROM table_y",
        declaration,
        resolve_relation=lambda table: table.name.casefold(),
    )
    mismatch = rewrite_consumer_query(
        "SELECT s.value FROM ("
        "SELECT col_old AS value, category FROM table_y WHERE is_current = FALSE"
        ") AS s",
        declaration,
        resolve_relation=lambda table: table.name.casefold(),
    )

    assert not direct.changed and "exact structural match" in direct.reason
    assert not mismatch.changed and "exact structural match" in mismatch.reason


def test_filtered_declaration_with_function_or_subquery_predicate_is_refused():
    for predicate in ("LOWER(col_old) = 'x'", "EXISTS (SELECT 1 FROM table_z)"):
        declaration = relation_declarations.prepare(
            f"SELECT col_old AS value FROM table_y WHERE {predicate}",
            "SELECT col_new AS value FROM table_x",
            SCHEMAS,
            preferred_side="right",
            scope=relation_declarations.Scope("all_snapshots"),
        )
        result = rewrite_consumer_query(
            f"SELECT s.value FROM (SELECT col_old AS value FROM table_y WHERE {predicate}) AS s",
            declaration,
        )

        assert not result.changed and "filters with" in result.reason


def test_cte_shadowing_and_using_join_are_refused():
    declaration = _declaration()
    shadowed = rewrite_consumer_query(
        "WITH table_y AS (SELECT 'x' AS col_old) SELECT col_old FROM table_y", declaration
    )
    using = rewrite_consumer_query(
        "SELECT y.col_old FROM table_y AS y JOIN table_z AS z USING (id)", declaration
    )

    assert not shadowed.changed and "CTEs" in shadowed.reason
    assert not using.changed and "USING" in using.reason


def test_grouped_migration_is_conditional_and_keeps_null_and_duplicate_semantics():
    result = rewrite_consumer_query(
        "SELECT col_old, COUNT(*) AS n FROM table_y GROUP BY col_old",
        _declaration(),
    )

    assert result.changed
    # No DISTINCT, NULL filter, or row-count rewrite is introduced. The report remains
    # conditional on the declared relation premise, rather than an independent proof.
    assert "DISTINCT" not in result.sql.upper()
    assert "IS NOT NULL" not in result.sql.upper()
    assert "COUNT(*)" in result.sql


def test_project_migration_updates_sqlx_refs_and_leaves_unrelated_reader_alone(tmp_path):
    root = _project(
        tmp_path / "shop",
        ("table_y", "SELECT CAST('x' AS STRING) AS col_old"),
        ("table_x", "SELECT CAST('x' AS STRING) AS col_new"),
        ("report", "SELECT col_old, COUNT(*) AS n FROM ${ref(\"table_y\")} GROUP BY col_old"),
        ("unrelated", "SELECT 1 AS n"),
    )

    result = refactor_project(root, _declaration())
    data = result.to_json()

    assert result.output_names_order_preserved
    assert data["preservation"]["status"] == "conditional"
    assert data["preservation"]["premises"][0]["declaration_id"] == _declaration().id
    assert data["preservation"]["premises"][0]["scope"] == {"kind": "all_snapshots"}
    assert [change.model for change in result.changes] == ["report"]
    assert [file.path for file in result.files] == ["definitions/report.sqlx"]
    assert '${ref("table_x")}' in result.files[0].after
    assert "col_new AS col_old" in result.files[0].after


def test_project_filtered_migration_rewrites_matching_subquery_only(tmp_path):
    root = _project(
        tmp_path / "filtered-shop",
        (
            "table_y",
            "SELECT CAST('x' AS STRING) AS col_old, 'a' AS category, TRUE AS is_current",
        ),
        (
            "table_x",
            "SELECT 'a' AS category, CAST('x' AS STRING) AS col_new, TRUE AS is_active",
        ),
        (
            "report",
            "SELECT s.category, COUNT(*) AS n FROM ("
            "SELECT col_old AS value, category FROM ${ref(\"table_y\")} WHERE is_current = TRUE"
            ") AS s GROUP BY s.category",
        ),
        ("raw_reader", "SELECT col_old FROM ${ref(\"table_y\")}"),
        ("unrelated", "SELECT 1 AS n"),
    )

    result = refactor_project(root, _filtered_declaration())

    assert result.output_names_order_preserved
    assert [change.model for change in result.changes] == ["report"]
    assert result.changes[0].operation == "replace_exact_filtered_query"
    assert [file.path for file in result.files] == ["definitions/report.sqlx"]
    assert '${ref("table_x")}' in result.files[0].after
    assert "__kumo_preferred_relation.value AS value" in result.files[0].after
    assert '${ref("table_y")}' in (root / "definitions" / "raw_reader.sqlx").read_text(encoding="utf-8")
    assert any(item.get("model") == "raw_reader" and "exact structural match" in item["reason"]
               for item in result.skipped)


def test_project_migration_skips_unmapped_readers_without_rewriting_them(tmp_path):
    root = _project(
        tmp_path / "shop",
        ("table_y", "SELECT CAST('x' AS STRING) AS col_old, 1 AS other"),
        ("table_x", "SELECT CAST('x' AS STRING) AS col_new"),
        ("report", "SELECT other FROM ${ref(\"table_y\")}"),
    )

    result = refactor_project(root, _declaration())

    assert not result.files
    assert result.skipped, result.skipped
    assert any(item["model"] == "report" and "undeclared column" in item["reason"]
               for item in result.skipped), result.skipped


def test_saved_declaration_cli_previews_conditional_migration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "kumosql-state"))
    declaration = relation_declarations.declare(
        "SELECT col_old AS value FROM table_y",
        "SELECT col_new AS value FROM table_x",
        SCHEMAS,
        preferred_side="right",
        scope=relation_declarations.Scope("all_snapshots"),
        declaration_id="ed1e6b68-11e2-46b4-a3d9-8266bc4208f3",
    )
    root = _project(
        tmp_path / "cli-shop",
        ("table_y", "SELECT CAST('x' AS STRING) AS col_old"),
        ("table_x", "SELECT CAST('x' AS STRING) AS col_new"),
        ("report", "SELECT col_old FROM ${ref(\"table_y\")}"),
    )

    assert main([str(root), "--declaration", declaration.id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["preservation"]["status"] == "conditional"
    assert report["output_names_order_preserved"]
    assert main([str(root), "--declaration", declaration.id, "--write"]) == 0
    assert '${ref("table_x")}' in (root / "definitions" / "report.sqlx").read_text(encoding="utf-8")


def test_apply_refuses_files_changed_after_migration_preview(tmp_path):
    root = _project(
        tmp_path / "stale-shop",
        ("table_y", "SELECT CAST('x' AS STRING) AS col_old"),
        ("table_x", "SELECT CAST('x' AS STRING) AS col_new"),
        ("report", "SELECT col_old FROM ${ref(\"table_y\")}"),
    )
    result = refactor_project(root, _declaration())
    report = root / "definitions" / "report.sqlx"
    report.write_text(report.read_text(encoding="utf-8") + "-- concurrent edit\n", encoding="utf-8")

    with pytest.raises(RelationRefactorError, match="edits made after planning"):
        result.apply(root)


def test_explicit_action_dependency_is_not_retargeted_implicitly(tmp_path):
    root = _project(
        tmp_path / "shop",
        ("table_y", "SELECT CAST('x' AS STRING) AS col_old"),
        ("table_x", "SELECT CAST('x' AS STRING) AS col_new"),
        ("report", "SELECT col_old FROM ${ref(\"table_y\")}"),
    )
    report = root / "definitions" / "report.sqlx"
    report.write_text(
        'config { type: "table", dependencies: ["table_y"] }\n'
        'SELECT col_old FROM ${ref("table_y")}\n',
        encoding="utf-8",
    )

    result = refactor_project(root, _declaration())

    assert not result.files
    assert any("explicit action dependency" in item["reason"] for item in result.skipped)
