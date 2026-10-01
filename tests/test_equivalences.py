import json

import pytest

pytest.importorskip("z3")

from kumosql import equivalences, load_sqlx_project, prover_context
from kumosql.pipeline_equivalence import equivalence_main, main as prove_tables_main, prove_models
from kumosql.prover_schema import ProverSchema
from kumosql.smt_equivalence import SmtStatus

ORDERS = equivalences.Equivalence("proj.raw.orders", "proj.raw.orders_v2", (("id", "order_id"), ("amt", "amount")), True)


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project(tmp_path, tail=""):
    """A->B->C and A2->G->H, the two chains cut at the same places but with different column names."""

    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/s.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(tmp_path, "definitions/s2.sqlx", 'config { type: "declaration", schema: "raw", name: "orders_v2" }\n')
    write(tmp_path, "definitions/b.sqlx", 'config { type: "view" }\nSELECT id, amt FROM ${ref("raw","orders")} WHERE amt > 0\n')
    write(tmp_path, "definitions/c.sqlx", 'config { type: "table" }\nSELECT id, SUM(amt) AS tot FROM ${ref("b")} GROUP BY id\n')
    write(tmp_path, "definitions/g.sqlx", 'config { type: "view" }\nSELECT order_id, amount FROM ${ref("raw","orders_v2")} WHERE amount > 0\n')
    write(tmp_path, "definitions/h.sqlx", 'config { type: "table" }\nSELECT order_id AS k, SUM(amount) AS total FROM ${ref("g")} GROUP BY order_id\n' + tail)
    return load_sqlx_project(tmp_path)


def test_declarations_are_validated_saved_and_removed():
    assert equivalences.load() == []
    saved = equivalences.add({"left": "P.D.A", "right": "`p.d.b`", "columns": [["x", "y"]]})
    assert (saved.left, saved.right) == ("p.d.a", "p.d.b")
    assert equivalences.load() == [saved]  # persisted in the data folder
    for bad in (
        {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]},  # right already declared
        {"left": "p.d.b", "right": "p.d.a", "columns": [["y", "x"]]},  # a cycle
        {"left": "p.d.a", "right": "p.d.a", "whole": True},
        {"left": "p.d.a", "right": "p.d.c"},  # nothing declared
        {"left": "p.d.a", "right": "p.d.c", "columns": [["x"]]},
        {"left": "p.d.a", "right": "p.d.c", "columns": [["x", "y"], ["x", "z"]]},
        {"left": "p.d.a", "right": "p.d.c", "whole": "yes"},
        "nope",
    ):
        with pytest.raises(ValueError):
            equivalences.add(bad)
    assert equivalences.remove("p.d.b") is True
    assert equivalences.remove("p.d.b") is False
    assert equivalences.load() == []


def test_rewrite_replaces_a_table_by_the_equivalent_one_with_renamed_columns():
    item = equivalences.Equivalence("p.d.a", "p.d.b", (("x", "y"),))
    sql, used = equivalences.rewrite_sql("SELECT y, COUNT(*) FROM `p.d.b` GROUP BY y", [item])
    assert used == [item]
    assert "p.d.a" in sql and "x AS y" in sql and "p.d.b" not in sql
    unchanged, none_used = equivalences.rewrite_sql("SELECT y FROM p.d.c", [item])
    assert none_used == [] and unchanged == "SELECT y FROM p.d.c"


def test_a_saved_equivalence_enters_a_rewrite_proof_and_is_listed_as_an_assumption():
    old = "SELECT y, COUNT(*) AS n FROM p.d.b GROUP BY y"
    new = "SELECT x AS y, COUNT(*) AS n FROM p.d.a GROUP BY x"
    assert prover_context.prove(old, new, schema=ProverSchema()).status is not SmtStatus.PROVEN_EQUIVALENT
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]})
    proven = prover_context.prove(old, new, schema=ProverSchema())
    assert proven.status is SmtStatus.PROVEN_EQUIVALENT
    assert any("declared equivalences hold" in note for note in proven.assumptions)


def test_a_wrong_mapping_does_not_prove_a_different_query():
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]})
    result = prover_context.prove(
        "SELECT y FROM p.d.b", "SELECT z FROM p.d.a", schema=ProverSchema()
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_without_the_declaration_two_layers_are_unknown(tmp_path):
    assert not prove_models(project(tmp_path), "c", "h", declared=[]).proven


def test_a_declaration_on_the_sources_ripples_up_through_every_layer(tmp_path):
    pipeline = project(tmp_path)
    result = prove_models(pipeline, "c", "h", declared=[ORDERS])
    assert result.proven and result.method == "layers"
    assert result.lemmas == ["proj.an.g ≡ proj.an.b"]
    assert result.equivalences == [ORDERS.label]
    assert any("declared equivalences hold" in note for note in result.assumptions)


def test_columns_the_declaration_does_not_cover_leave_the_result_unknown(tmp_path):
    only_id = equivalences.Equivalence("proj.raw.orders", "proj.raw.orders_v2", (("id", "order_id"),))
    assert not prove_models(project(tmp_path), "c", "h", declared=[only_id]).proven


def test_a_different_filter_in_one_layer_breaks_the_chain(tmp_path):
    pipeline = project(tmp_path)
    write(tmp_path, "definitions/g.sqlx", 'config { type: "view" }\nSELECT order_id, amount FROM ${ref("raw","orders_v2")} WHERE amount > 1\n')
    assert not prove_models(load_sqlx_project(tmp_path), "c", "h", declared=[ORDERS]).proven
    assert pipeline  # the unchanged pipeline above still proves


def test_pipelines_cut_at_different_places_fall_back_to_inlining(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/s.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(tmp_path, "definitions/b.sqlx", 'config { type: "view" }\nSELECT id, amt FROM ${ref("raw","orders")} WHERE amt > 0\n')
    write(tmp_path, "definitions/c.sqlx", 'config { type: "table" }\nSELECT id, SUM(amt) AS tot FROM ${ref("b")} GROUP BY id\n')
    write(tmp_path, "definitions/d.sqlx", 'config { type: "table" }\nSELECT id, SUM(amt) AS tot FROM ${ref("raw","orders")} WHERE amt > 0 GROUP BY id\n')
    result = prove_models(load_sqlx_project(tmp_path), "c", "d", declared=[])
    assert result.proven and result.method == "inlined"


def test_a_table_is_equivalent_to_itself_and_unknown_names_are_rejected(tmp_path):
    pipeline = project(tmp_path)
    assert prove_models(pipeline, "c", "proj.an.c", declared=[]).method == "same"
    with pytest.raises(ValueError):
        prove_models(pipeline, "c", "missing", declared=[])


def test_command_line(tmp_path, capsys):
    assert equivalence_main(["add", "proj.raw.orders", "proj.raw.orders_v2", "id=order_id", "amt=amount", "--whole"]) == 0
    assert equivalence_main(["add", "proj.raw.orders", "proj.raw.orders_v2", "id=order_id"]) == 2
    capsys.readouterr()
    project(tmp_path)
    assert prove_tables_main(["c", "h", "--project", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "equivalent"
    assert equivalence_main(["remove", "proj.raw.orders_v2"]) == 0
    assert prove_tables_main(["c", "h", "--project", str(tmp_path)]) == 1
