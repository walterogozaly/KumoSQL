import json

import pytest

from kumosql import relation_declarations as declarations


SCHEMAS = {
    "table_y": {"col_id": "INT64", "col_old": "STRING", "keep": "DATE"},
    "table_x": {"col_new": "STRING", "keep": "DATE", "col_id": "INT64"},
    "table_z": {"col_id": "INT64", "col_old": "STRING", "keep": "DATE"},
}


def test_named_outputs_resolve_stars_and_align_independent_of_projection_order():
    left = "SELECT * EXCEPT (col_old), col_old AS col_new FROM TABLE_Y WHERE keep IS NOT NULL"
    right = "SELECT keep, col_id, col_new FROM TABLE_X"

    declaration = declarations.prepare(left, right, SCHEMAS, preferred_side="right")

    assert declaration.left_schema == (
        declarations.OutputColumn("col_id", "INT64"),
        declarations.OutputColumn("keep", "DATE"),
        declarations.OutputColumn("col_new", "STRING"),
    )
    assert declaration.output_mapping == (
        ("col_id", "col_id"),
        ("keep", "keep"),
        ("col_new", "col_new"),
    )
    assert declaration.left_sql == left  # filters remain part of the declared query side
    assert declaration.preferred_side == "right"
    assert declaration.evidence == declarations.Evidence()


@pytest.mark.parametrize(
    ("left", "right", "code"),
    [
        ("SELECT col_id, keep FROM table_y", "SELECT col_id, col_new FROM table_x", "output_names_mismatch"),
        ("SELECT col_id AS value FROM table_y", "SELECT col_old AS value FROM table_y", "incompatible_output_types"),
        ("SELECT col_id AS value, keep AS VALUE FROM table_y", "SELECT col_id AS value, keep AS VALUE FROM table_x", "duplicate_output_name"),
        ("SELECT * FROM missing_table", "SELECT * FROM table_x", "unknown_table_schema"),
        ("SELECT * EXCEPT (missing) FROM table_y", "SELECT * FROM table_x", "unknown_star_except"),
        ("SELECT col_id FROM table_y JOIN table_z ON table_y.col_id = table_z.col_id", "SELECT col_id FROM table_x", "ambiguous_column"),
        ("SELECT COUNT(*) AS n FROM table_y", "SELECT COUNT(*) AS n FROM table_x", "unknown_expression_type"),
    ],
)
def test_schema_mismatches_fail_with_specific_diagnostics(left, right, code):
    with pytest.raises(declarations.DeclarationError) as error:
        declarations.prepare(left, right, SCHEMAS)

    assert error.value.code == code


def test_qualified_star_expands_only_the_named_relation():
    resolved = declarations.resolve_output_schema(
        "SELECT y.* FROM table_y AS y JOIN table_z AS z ON y.col_id = z.col_id",
        SCHEMAS,
    )

    assert [column.name for column in resolved] == ["col_id", "col_old", "keep"]


def test_explicit_alias_hides_the_original_table_qualifier():
    with pytest.raises(declarations.DeclarationError) as error:
        declarations.resolve_output_schema("SELECT table_y.col_id FROM table_y AS y", SCHEMAS)

    assert error.value.code == "unknown_relation_alias"


def test_schema_type_names_are_normalized_but_never_coerced():
    assert declarations.prepare(
        "SELECT col_id FROM table_y",
        "SELECT col_id FROM table_x",
        {"table_y": {"col_id": "  int64  "}, "table_x": {"col_id": "INT64"}},
    ).output_mapping == (("col_id", "col_id"),)
    with pytest.raises(declarations.DeclarationError, match="incompatible types"):
        declarations.prepare(
            "SELECT col_id FROM table_y",
            "SELECT col_id FROM table_x",
            {"table_y": {"col_id": "INT64"}, "table_x": {"col_id": "NUMERIC"}},
        )


def test_records_persist_with_evidence_scope_provenance_and_stable_ids(tmp_path):
    left = "SELECT col_id FROM table_y WHERE keep IS NOT NULL"
    right = "SELECT col_id FROM table_x WHERE keep IS NOT NULL"
    declaration = declarations.prepare(
        left,
        right,
        SCHEMAS,
        evidence=declarations.Evidence("snapshot_validation", "test-runner", "run-42"),
        scope=declarations.Scope("snapshot", "warehouse-2026-10-05"),
        provenance={"source": "issue-713", "author": "test"},
        declaration_id="afe82732-339d-4db7-9d5a-4996e1f9e042",
    )

    saved = declarations.declare(
        left,
        right,
        SCHEMAS,
        evidence=declaration.evidence,
        scope=declaration.scope,
        provenance=dict(declaration.provenance),
        declaration_id=declaration.id,
    )

    assert declarations.load() == [saved]
    assert declarations.get(saved.id) == saved
    stored = json.loads((tmp_path / "kumosql-home" / "relation_declarations.json").read_text(encoding="utf-8"))
    assert stored["version"] == declarations.FORMAT_VERSION
    assert stored["declarations"][0]["evidence"] == {
        "kind": "snapshot_validation",
        "source": "test-runner",
        "reference": "run-42",
    }
    assert declarations.remove(saved.id) is True
    assert declarations.remove(saved.id) is False
    assert declarations.load() == []


def test_preference_cycles_and_duplicate_ids_are_rejected():
    first = declarations.prepare("SELECT col_id FROM table_y", "SELECT col_id FROM table_x", SCHEMAS, preferred_side="left")
    declarations.add(first)
    reverse = declarations.prepare("SELECT col_id FROM table_x", "SELECT col_id FROM table_y", SCHEMAS, preferred_side="left")
    with pytest.raises(declarations.DeclarationError) as error:
        declarations.add(reverse)
    assert error.value.code == "preference_cycle"

    with pytest.raises(declarations.DeclarationError) as error:
        declarations.add(first)
    assert error.value.code == "duplicate_id"


def test_store_rejects_invalid_version_and_preference_cycle(tmp_path):
    path = tmp_path / "kumosql-home" / "relation_declarations.json"
    path.parent.mkdir()
    path.write_text('{"version": true, "declarations": []}', encoding="utf-8")
    with pytest.raises(declarations.DeclarationError) as error:
        declarations.load()
    assert error.value.code == "unsupported_store_version"
