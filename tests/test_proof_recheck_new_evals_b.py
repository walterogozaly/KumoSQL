"""The re-check adapters for the evals that landed after round one (numeric traps, sample databases, paired engine
tests, the soundness fuzzer, the pipeline-run evals).

Each adapter has to import, list its items, and turn one tiny pair into a ``Case`` the heavy search can run
(tools/recheck/new_evals_b.py and tools/recheck/new_evals_b_pipelines.py). A pair the eval does not count as proven gives
no case. The search itself is covered by tests/test_proof_recheck.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")
pytest.importorskip("z3")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

from recheck import engine  # noqa: E402
from recheck import new_evals_b as nb  # noqa: E402
from recheck import new_evals_b_pipelines as nbp  # noqa: E402

FIRST = [
    "numeric-traps", "sample-databases-pairs", "sample-databases-sakila-pairs", "sample-databases-pagila-pairs",
    "sample-databases-oracle_co-pairs", "sample-databases-oracle_hr-pairs", "engine-paired-tests", "soundness-fuzz",
]
PIPELINES = [
    "bigquery-edge-cases", "googlesql-behavior", "transformation-workloads", "job-alternative-forms",
    "sample-databases-rewrites", "sample-databases-sakila-rewrites", "sample-databases-pagila-rewrites",
    "sample-databases-oracle_co-rewrites", "sample-databases-oracle_hr-rewrites",
]


def test_every_adapter_is_registered_and_discovered():
    import proof_recheck

    found = proof_recheck.adapters()
    for name in FIRST:
        assert nb.ADAPTERS[name].name == name and name in found
    for name in PIPELINES:
        assert nbp.ADAPTERS[name].name == name and name in found


def test_numeric_traps_lists_each_case_twice_and_proves_a_clean_pair():
    adapter = nb.ADAPTERS["numeric-traps"]
    items = adapter.items()
    assert len(items) == 224
    assert {i["pair"] for i in items if i["special"]} == {i["id"] + "@special" for i in items if i["special"]}
    by_pair = {i["pair"]: i for i in items}
    case = adapter.case(by_pair["numeric-times-int-keeps-scale"])
    assert case is not None and case.dialect == "bigquery"
    assert "KUMO_BQ_MUL" in case.left.upper() and case.meta["results"] == "bigquery"
    assert [c.sql_type for c in case.tables["t"].columns] == ["BIGINT", "BIGINT", "DOUBLE", "DOUBLE", "DECIMAL(38,9)", "DECIMAL(38,9)", "VARCHAR", "BOOLEAN"]
    assert any(statement.startswith("SET TimeZone") for statement in case.setup)
    special = adapter.case(by_pair["nan-distinct-group@special"])
    assert special is not None and special.meta["numeric"] == "special" and "results" not in special.meta
    # the eval gives no proof to a rewrite that introduces an error, or to a trap pair
    assert adapter.case(by_pair["overflow-case-guard"]) is None
    assert adapter.case(by_pair["coerce-times-float-one"]) is None


def test_numeric_flavours_add_the_limits_and_nan_only_for_the_special_run():
    adapter = nb.ADAPTERS["numeric-traps"]
    by_pair = {i["pair"]: i for i in adapter.items()}
    finite = adapter.case(by_pair["nan-distinct-group"])
    special = adapter.case(by_pair["nan-distinct-group@special"])
    import random

    generator = engine.Generator(finite, random.Random(1))
    assert 2**63 - 1 in generator.flavours["int"]["limits"]
    assert not any(v != v for v in generator.flavours["float"]["limits"])
    assert any(v != v for v in engine.Generator(special, random.Random(1)).flavours["float"]["limits"])
    assert any(v == float("inf") for v in engine.Generator(special, random.Random(1)).flavours["float"]["special"])
    # a case from another adapter keeps the engine's own flavours
    plain = engine.Case("x", "y", "SELECT a FROM t", "SELECT a FROM t", {"t": engine.Table("t", [engine.Column("a")])})
    assert "limits" not in engine.Generator(plain, random.Random(1)).flavours["int"]


def test_numeric_pair_survives_and_a_wrong_one_is_found():
    adapter = nb.ADAPTERS["numeric-traps"]
    by_pair = {i["pair"]: i for i in adapter.items()}
    case = adapter.case(by_pair["numeric-times-int-keeps-scale"])
    assert engine.recheck(case, budget=60, seconds=20)["verdict"] == "survived"
    wrong = engine.Case(case.eval, "wrong", "SELECT x FROM t", "SELECT y FROM t", case.tables, setup=case.setup, meta=dict(case.meta))
    assert engine.recheck(wrong, budget=200, seconds=20)["verdict"] == "differs"


def test_sample_database_tables_follow_the_declared_constraints_and_drops():
    import sample_db_bench as sb

    adapter = sb.ADAPTERS["sakila"]
    full = nb.SampleDatabasePairs.tables(adapter, ())
    assert full["film"].keys == [("film_id",)] and any(fk[1] == "language" for fk in full["film"].foreign_keys)
    assert next(c for c in full["film"].columns if c.name == "title").not_null
    dropped = nb.SampleDatabasePairs.tables(adapter, ("pk:film", "not_null:film.title", "fk:film.language_id"))
    assert dropped["film"].keys == []
    assert not next(c for c in dropped["film"].columns if c.name == "title").not_null
    assert all("language_id" not in fk[0] for fk in dropped["film"].foreign_keys)
    kinds = {c.name: (c.kind, c.sql_type) for c in full["film"].columns}
    assert kinds["rental_rate"] == ("decimal", "DECIMAL(4,2)") and kinds["last_update"] == ("timestamp", "TIMESTAMP")
    # a BYTES column draws bytes, not accented strings that BLOB refuses
    blob = next(c for t in full.values() for c in t.columns if c.sql_type == "BLOB")
    assert blob.values and all(isinstance(v, bytes) for v in blob.values)


def test_sample_database_pair_case_and_items():
    adapter = nb.ADAPTERS["sample-databases-sakila-pairs"]
    items = adapter.items()
    assert items and all(i["pair"].startswith("sakila:") for i in items)
    case = adapter.case({"pair": "sakila:sk-fk-join-elimination", "database": "sakila", "id": "sk-fk-join-elimination", "label": "equivalent"})
    assert case is not None and case.meta["proof"] in ("structural", "algebraic")
    assert engine.recheck(case, budget=40, seconds=20)["verdict"] == "survived"
    # the sibling without the foreign key is not proven
    assert adapter.case({"pair": "x", "database": "sakila", "id": "sk-fk-chain-join-elimination-without-fk", "label": "different"}) is None
    both = nb.ADAPTERS["sample-databases-pairs"].items()
    assert {i["database"] for i in both} == {"chinook", "northwind"}


def test_engine_paired_tests_translate_as_the_eval_does():
    adapter = nb.ADAPTERS["engine-paired-tests"]
    items = adapter.items()
    assert len(items) == 157
    case = adapter.case(next(i for i in items if i["pair"] == "trino-1232"))
    assert case is not None and "src__orders" in case.left and case.meta["results"] == "bigquery"
    assert all(name.startswith("src__") for name in case.tables)
    assert engine.recheck(case, budget=30, seconds=20)["verdict"] in ("survived", "unrunnable")


def test_soundness_fuzz_case_uses_the_fixtures_schema_and_constraints():
    import soundness_fuzz as sf

    adapter = nb.ADAPTERS["soundness-fuzz"]
    case = {
        "id": "t-1", "family": "unit", "mutation": {"name": "x", "known_equivalent": True}, "dialect": "bigquery",
        "left": "SELECT t.id FROM t WHERE t.x = 1 OR t.x <> 1 OR t.x IS NULL", "right": "SELECT t.id FROM t",
        "schema": {"t": [["id", "INT64"], ["x", "INT64"], ["y", "INT64"], ["s", "STRING"]], "u": [["k", "INT64"], ["v", "STRING"]]},
        "tables": {"t": [[0, 1, 1, "a"]], "u": [[1, "a"]]}, "constraints": {"t": {"not_null": ["id"], "keys": [["id"]]}},
    }
    assert not sf.fixture_errors(case)
    built = adapter.case({"pair": "t-1", "case": case})
    assert built is not None
    assert built.tables["t"].keys == [("id",)] and next(c for c in built.tables["t"].columns if c.name == "id").not_null
    assert [c.sql_type for c in built.tables["u"].columns] == ["BIGINT", "VARCHAR"]
    assert engine.recheck(built, budget=60, seconds=20)["verdict"] == "survived"
    wrong = dict(case, right="SELECT t.id FROM t WHERE t.x = 1")
    assert adapter.case({"pair": "t-2", "case": wrong}) is None  # not proven


def test_pipeline_rewrite_keeps_only_proven_changes_beyond_layout():
    rules = ("remove_trivial_predicates",)
    changed = nbp.pipeline_rewrite("SELECT id FROM t WHERE 1 = 1 AND id > 2", rules)
    assert changed is not None and changed[1] == "proven" and "1 = 1" not in changed[0]
    assert nbp.pipeline_rewrite("SELECT id FROM t WHERE id > 2", rules) is None  # unchanged


def test_bigquery_edge_cases_run_over_the_eval_s_tables():
    tables = nbp.edge_tables()
    assert [c.sql_type for c in tables["t"].columns] == ["INTEGER", "INTEGER", "INTEGER", "VARCHAR", "DOUBLE", "INTEGER[]", "STRUCT(x INTEGER, y VARCHAR)"]
    assert [c.name for c in tables["u"].columns] == ["id", "x"]
    adapter = nbp.ADAPTERS["bigquery-edge-cases"]
    items = adapter.items()
    assert any(i["pair"].startswith("heldout:") and i["held_out"] for i in items) and any(not i["held_out"] for i in items)
    case = adapter.case({"pair": "unit", "id": "unit", "sql": "SELECT id FROM t WHERE 1 = 1 AND a > 5 ORDER BY id", "held_out": False})
    assert case is not None and case.meta["numeric"] == "special"
    verdict = engine.recheck(case, budget=60, seconds=20)
    assert verdict["verdict"] == "survived", verdict


def test_googlesql_cases_have_no_tables():
    adapter = nbp.ADAPTERS["googlesql-behavior"]
    case = adapter.case({"pair": "unit", "id": "unit", "sql": "SELECT 1 + 1 AND (1 = 1) AS x", "held_out": False})
    assert case is None or case.tables == {}


def test_workload_adapters_list_nothing_without_their_data(tmp_path, monkeypatch):
    # the workload evals need the benchmark checkouts; without them there is nothing to list, never a crash on import
    import benchmark_corpora as corpora

    monkeypatch.setattr(corpora, "BENCH_DIR", tmp_path)
    for name in ("transformation-workloads", "job-alternative-forms"):
        adapter = nbp.ADAPTERS[name]
        assert callable(adapter.items) and callable(adapter.case)


def test_sqlfluff_refusals_pair_a_proven_structural_change():
    adapter = nbp.ADAPTERS["sqlfluff-refusals"]
    items = adapter.items()
    assert len(items) == 212 * 7 and {i["pair"].split("#")[1] for i in items} >= {"lift_subqueries", "remove_unused_ctes"}
    changed = [adapter.case(i) for i in items if i["pair"] == "ST03/test_pass_nested_query_in_from_clause#lift_subqueries"]
    assert changed and changed[0] is not None and changed[0].meta["status"] == "proven"
    assert changed[0].tables  # the tables are read off the two queries
    unchanged = next(i for i in items if i["pair"].endswith("#remove_trivial_predicates"))
    assert adapter.case(unchanged) is None or adapter.case(unchanged).meta["rule"] == "remove_trivial_predicates"


def test_engine_suite_tables_come_from_the_suite_catalog():
    import duckdb

    from recheck import new_evals_b_suites as nbs

    connection = duckdb.connect()
    connection.execute("CREATE TABLE a (i INTEGER, d DECIMAL(10,2), s VARCHAR, f DOUBLE, t TIMESTAMP, b BOOLEAN)")
    tables = nbs.tables_of(connection)
    assert [(c.name, c.kind, c.sql_type) for c in tables["a"].columns] == [
        ("i", "int", "INTEGER"), ("d", "decimal", "DECIMAL(10,2)"), ("s", "text", "VARCHAR"), ("f", "float", "DOUBLE"),
        ("t", "timestamp", "TIMESTAMP"), ("b", "bool", "BOOLEAN")]
    connection.execute("CREATE TABLE odd (l INTEGER[])")
    assert nbs.tables_of(connection) is None  # a type the engine cannot draw
    for name in ("duckdb-slt", "sqlite-slt", "sqlglot-fixtures"):
        plain = nbs.ADAPTERS[f"engine-{name}-plain"]
        amplified = nbs.ADAPTERS[f"engine-{name}-amplified"]
        assert plain.variants == ("plain",) and "wrap-cte" in amplified.variants


def test_engine_suite_rewrite_pair_needs_a_proven_change():
    from recheck import new_evals_b_suites as nbs

    pair = nbs.rewrite_pair("SELECT a FROM x WHERE 1 = 1", "duckdb", "plain")
    assert pair is not None and "1 = 1" not in pair[1] and pair[0] != pair[1]
    assert nbs.rewrite_pair("SELECT a FROM x WHERE a > 1", "duckdb", "plain") is None  # nothing to change


def test_targeted_test_data_mutant_pairs_run_over_the_suite_schema():
    from recheck import new_evals_b_mutants as nbm

    items, _ = nbm.corpus()
    item = next(i for i in items if i["suite"] == "university")
    from kumosql.query_mutants import mutate

    mutants = mutate(nbm.original_of(item))
    assert mutants
    adapter = nbm.ADAPTERS["targeted-test-data"]
    cases = [adapter.case({"pair": "x", "suite": "university", "index": item["index"], "number": n, "held_out": False}) for n in range(len(mutants))]
    # a proven mutant, when there is one, is run over src__ tables carrying the schema's keys; most mutants are not proven
    for case in cases:
        if case is not None:
            assert all(name.startswith("src__") for name in case.tables) and case.meta["results"] == "bigquery"
