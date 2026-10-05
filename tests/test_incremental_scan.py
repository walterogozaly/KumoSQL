"""Project scan: every incremental action is checked, unsupported ones are reported, not guessed."""

from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental_scan import CONTRACTS, scan_project, summarise  # noqa: E402

COAL = "COALESCE((SELECT MAX(created_at) FROM ${self()}), TIMESTAMP '1970-01-01')"


def write(root, name, text):
    path = root / "definitions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_finds_proof_counterexample_and_unsupported(tmp_path):
    write(tmp_path, "good.sqlx", f'config {{ type: "incremental" }}\nselect id, created_at, v from ${{ref("raw")}}\n${{when(incremental(), `where created_at > {COAL}`)}}\n')
    write(tmp_path, "late.sqlx", 'config { type: "incremental", uniqueKey: ["id"] }\nselect id, created_at, v from ${ref("raw")}\n${when(incremental(), `where created_at > (select max(created_at) from ${self()})`)}\n')
    write(tmp_path, "js.sqlx", 'config { type: "incremental" }\nselect id from ${ref("raw")} where ${dateFilter("created_at", 3)}\n')
    write(tmp_path, "plain.sqlx", 'config { type: "table" }\nselect 1 as x\n')
    rows = scan_project(tmp_path, seeds=15)
    by = {(r.model, r.contract): r for r in rows}
    assert {r.model for r in rows} == {"good", "late", "js"}  # plain tables are not incremental
    assert by[("good", "append_only")].outcome == "safe"
    assert by[("good", "late_and_duplicate")].outcome == "diverges"
    assert by[("late", "append_only")].outcome == "diverges"  # no default for an empty table
    assert all(by[("js", c)].outcome == "unsupported" for c in CONTRACTS)
    assert summarise(rows)["append_only"]["unsupported"] == 1


# ---------------------------------------------------------------------------
# Nondeterministic outcomes, repairs, and source inference
# ---------------------------------------------------------------------------

from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx  # noqa: E402
from kumosql.incremental_scan import infer_sources, repair_summary, row_json  # noqa: E402

FILTERED = (
    'config {\n  type: "incremental",\n  uniqueKey: ["id"],\n'
    '  bigquery: { partitionBy: "DATE(created_at)", updatePartitionFilter: "created_at >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)" }\n}\n'
    "select * from (\nselect a.id, a.created_at, coalesce(a.amount, 0) as amount\nfrom ${ref(\"raw\")} as a\nwhere a.created_at >= '2020-01-01'\n)\n"
    "${when(incremental(), `where 1 = 1`)}\n"
)


def test_a_tie_dependent_model_is_reported_nondeterministic_not_unknown(tmp_path):
    write(
        tmp_path,
        "ties.sqlx",
        'config { type: "incremental", uniqueKey: ["customer_id"] }\nselect customer_id, id, created_at from ${ref("raw")} where customer_id is not null\n'
        "qualify row_number() over (partition by customer_id order by created_at desc) = 1\n",
    )
    rows = scan_project(tmp_path, seeds=20)
    assert {r.outcome for r in rows} == {"nondeterministic"}
    assert all("can tie" in r.detail for r in rows) and all(not r.repairs for r in rows)
    assert summarise(rows)["append_only"] == {"nondeterministic": 1}


def test_repairs_are_attached_to_diverging_models_only(tmp_path):
    write(tmp_path, "filtered.sqlx", FILTERED)
    write(tmp_path, "good.sqlx", f'config {{ type: "incremental" }}\nselect id, created_at, v from ${{ref("raw")}}\n${{when(incremental(), `where created_at > {COAL}`)}}\n')
    rows = {(r.model, r.contract): r for r in scan_project(tmp_path, seeds=15)}
    late = rows[("filtered", "late_and_duplicate")]
    assert late.outcome == "diverges" and late.repairs
    assert late.repairs[0].edits == ("drop_update_partition_filter", "dedup") and late.repairs[0].rule.startswith("R7")
    assert "updatePartitionFilter" in late.repairs[0].diff and late.repairs[0].diff.startswith("--- a/definitions/filtered.sqlx")
    # a model that is safe, or that diverges only under a contract no edit can mend, carries no repair
    assert rows[("good", "append_only")].outcome == "safe" and rows[("good", "append_only")].repairs == ()
    mutable = rows[("filtered", "mutable")]
    assert mutable.outcome == "diverges" and mutable.repairs == () and mutable.repair_note
    assert repair_summary(list(rows.values()))["late_and_duplicate"] == {"diverges": 2, "repaired": 1}
    assert row_json(late)["repairs"][0]["edits"] == ["drop_update_partition_filter", "dedup"]


def test_the_scan_can_skip_repairs(tmp_path):
    write(tmp_path, "filtered.sqlx", FILTERED)
    rows = scan_project(tmp_path, contracts={"late_and_duplicate": CONTRACTS["late_and_duplicate"]}, seeds=10, repairs=False)
    assert [r.outcome for r in rows] == ["diverges"] and rows[0].repairs == () and rows[0].repair_note == ""


def parsed(body: str):
    return parse_incremental_sqlx(f'config {{ type: "incremental" }}\n{body}', "m")


def test_sources_are_read_per_select_through_star_ctes_and_unnest():
    # a CTE that only re-exposes a table: the columns the next select reads are the table's
    model = parsed('with base as (select * from ${ref("t")}) select id, max(created_at) as created_at from base group by id')
    assert infer_sources(model)["t"].columns == {"id": "INT64", "created_at": "TIMESTAMP"}
    # a.* names no column; a bare column in a select with two tables belongs to neither
    model = parsed('select a.*, a.id as k, b.tier from ${ref("t")} as a left join ${ref("u")} as b on a.id = b.id where status = 1')
    sources = infer_sources(model)
    assert "*" not in sources["t"].columns and set(sources["t"].columns) == {"id"} and "status" not in sources["u"].columns
    # a CTE with a projection is not a pass-through
    model = parsed('with agg as (select id, sum(v) as v from ${ref("t")} group by id) select id, v from agg')
    assert infer_sources(model)["t"].columns == {"id": "INT64", "v": "INT64"}  # read inside its own body only
    # an unnested array column of structs, and one read whole
    model = parsed('select a.id, item.qty * item.price as amount from ${ref("t")} as a, unnest(a.items) as item where item.qty > 0')
    assert infer_sources(model)["t"].columns["items"] == "ARRAY<STRUCT<qty INT64, price INT64>>"
    model = parsed('select a.id, x from ${ref("t")} as a, unnest(a.tags) as x')
    assert infer_sources(model)["t"].columns["tags"] == "ARRAY<INT64>"
    # declared columns replace the guess
    assert infer_sources(model, {"t": {"id": "INT64", "tags": "STRING"}})["t"].columns == {"id": "INT64", "tags": "STRING"}


def test_an_unnested_array_source_runs_and_a_fan_out_merge_diverges():
    model = parse_incremental_sqlx('config { type: "incremental", uniqueKey: ["id"] }\nselect a.id, item.qty as qty from ${ref("t")} as a, unnest(a.items) as item', "m")
    sources = infer_sources(model)
    verdict = check_incremental(model, sources, CONTRACTS["append_only"], seeds=20)
    assert verdict.outcome == "diverges"  # a row with two items puts one id in the merge twice


def test_a_model_the_simulator_cannot_run_is_unsupported_not_unknown():
    model = parse_incremental_sqlx('config { type: "incremental", uniqueKey: ["id"] }\nselect id, v, count(*) over () as n from ${ref("t")}', "m")
    sources = {"t": SourceTable({"id": "INT64"}, ("id",), None)}  # the table has no column v: no generated state can run the model
    verdict = check_incremental(model, sources, CONTRACTS["append_only"], seeds=5)
    assert verdict.outcome == "unsupported" and "could not run on any of 5" in verdict.detail


def test_array_column_types_for_the_simulator():
    import random

    from kumosql.incremental import _literal, _random_value
    from kumosql.incremental_types import duck_array_type, parse_array

    assert parse_array("ARRAY<STRUCT<qty INT64, price INT64>>") == ([("qty", "INT64"), ("price", "INT64")], "")
    assert parse_array("ARRAY<INT64>") == (None, "INT64")
    assert parse_array("INT64") is None and parse_array("ARRAY<STRUCT<a>>") is None and parse_array("ARRAY<ARRAY<INT64>>") is None
    scalar = {"INT64": "BIGINT"}
    assert duck_array_type("ARRAY<STRUCT<qty INT64, price INT64>>", scalar) == 'STRUCT("qty" BIGINT, "price" BIGINT)[]'
    assert duck_array_type("ARRAY<INT64>", scalar) == "BIGINT[]" and duck_array_type("STRING", scalar) is None
    rng = random.Random(1)
    values = [_random_value(rng, "ARRAY<STRUCT<qty INT64, price INT64>>") for _ in range(20)]
    assert all(isinstance(v, list) and len(v) <= 3 and all(set(e) == {"qty", "price"} for e in v) for v in values)
    assert any(len(v) >= 2 for v in values)
    assert _literal([{"qty": 1, "price": 2}, {"qty": 0, "price": 5}]) == "[{'qty': 1, 'price': 2}, {'qty': 0, 'price': 5}]" and _literal([]) == "[]"
    # scalar draws are unchanged: the random stream a seed produces does not move
    assert [_random_value(random.Random(3), "INT64") for _ in range(3)] == [random.Random(3).randint(0, 3)] * 3


def test_generated_fixture_scan_has_no_unknown_and_repairs_most_divergences(tmp_path):
    """A small project from the dataform fixture generator: every model there sets updatePartitionFilter and merges on id."""

    import importlib.util
    import sys

    from kumosql.incremental import prove

    path = Path(__file__).resolve().parent.parent / "tools" / "make_dataform_fixture.py"
    spec = importlib.util.spec_from_file_location("make_dataform_fixture_for_scan", path)
    generator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = generator
    spec.loader.exec_module(generator)
    root = tmp_path / "project"
    generator.generate(root, 150, 11)
    kinds = {"late_and_duplicate": CONTRACTS["late_and_duplicate"]}
    rows = scan_project(root, contracts=kinds, seeds=10)
    counts = summarise(rows)["late_and_duplicate"]
    assert counts.get("unknown", 0) == 0 and counts["diverges"] >= 5  # unrunnable models are unsupported, not unknown
    summary = repair_summary(rows)["late_and_duplicate"]
    assert summary["repaired"] >= 0.8 * summary["diverges"]
    # a repair is a patch, and is proven again from its own text
    for row in [r for r in rows if r.repairs][:3]:
        text = (root / row.path).read_text(encoding="utf-8")
        repair = row.repairs[0]
        assert repair.diff and repair.sqlx != text
        model = parse_incremental_sqlx(repair.sqlx, row.model)
        assert prove(model, infer_sources(parse_incremental_sqlx(text, row.model)), kinds["late_and_duplicate"]) is not None
