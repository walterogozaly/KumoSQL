import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep saved UI, scope and formatting state out of the real user directory."""

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "kumosql-home"))


@pytest.fixture(autouse=True)
def no_project_loaded(monkeypatch):
    """The loaded project is process-wide state; a test that loads one must not leak it into the next test
    on the same worker (the empty-state UI tests failed under pytest-xdist because of that)."""

    import sys

    module = sys.modules.get("kumosql.live_graph")
    if module is not None:
        monkeypatch.setattr(module, "_LOADED", None)
        monkeypatch.setattr(module, "_JOBS", {"records": (), "label": "", "restored_from": None})
        # Parsed projects are cached in memory by content: a later test loading the same files would hit the
        # cache and never write its own snapshot into its own data folder.
        monkeypatch.setattr(module, "_PROJECT_CACHE", type(module._PROJECT_CACHE)())


@pytest.fixture(autouse=True)
def no_schema_fetch(monkeypatch):
    """Tests never look tables up in BigQuery unless they switch it on with a stub."""

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "0")


@pytest.fixture(autouse=True)
def fresh_redactor(monkeypatch):
    """Placeholders are per session; keep names one test registered from changing another test's log text."""

    from kumosql import redact

    monkeypatch.setattr(redact, "GLOBAL", redact.Redactor(enabled=True))


# Files whose tests take the longest, slowest first. Under pytest-xdist the scheduler hands tests out in
# collection order, so starting these first keeps one long benchmark from running alone at the end.
HEAVY_FILES = [
    "test_qed_benchmarks.py",
    "test_verieql_benchmarks.py",
    "test_singh_bedathur_benchmark.py",
    "test_spider2_bench.py",
    "test_sqlsolver_benchmarks.py",
    "test_sqlfluff_fixtures_bench.py",
    "test_lineage_benchmarks.py",
    "test_pipeline_bench.py",
    "test_unsafe_fuzz.py",
    "test_safety_corpus.py",
    "test_bq_syntax_coverage.py",
    "test_targeted_data_bench.py",
    "test_cosette_benchmarks.py",
    "test_rbot_benchmarks.py",
    "test_bq_behavior_eval.py",
    "test_incremental.py",
    "test_model_reuse_evals.py",
]


# Test files that score KumoSQL against a benchmark corpus and hold its floors. They are marked ``eval``, so
# ``pytest -m eval`` runs just the floors and ``pytest -m "not eval"`` everything else.
EVAL_FILES = {
    "test_bq_behavior_eval.py",
    "test_calcite_mined_benchmarks.py",
    "test_constraint_dependence.py",
    "test_cosette_benchmarks.py",
    "test_dup_bench.py",
    "test_incremental.py",
    "test_lineage_benchmarks.py",
    "test_lineage_goldens_bench.py",
    "test_llmr2_bench.py",
    "test_model_reuse_evals.py",
    "test_output_properties.py",
    "test_pipeline_bench.py",
    "test_qed_benchmarks.py",
    "test_rbot_benchmarks.py",
    "test_safety_corpus.py",
    "test_schema_change.py",
    "test_singh_bedathur_benchmark.py",
    "test_spider2_bench.py",
    "test_sqlfluff_fixtures_bench.py",
    "test_sqliq_bench.py",
    "test_sqlsolver_benchmarks.py",
    "test_targeted_data_bench.py",
    "test_transformation_bench.py",
    "test_unsafe_fuzz.py",
    "test_verieql_benchmarks.py",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name in EVAL_FILES:
            item.add_marker(pytest.mark.eval)
    parallel = hasattr(config, "workerinput") or getattr(config.option, "numprocesses", None)
    if not parallel:
        return  # a serial run keeps the natural order
    rank = {name: position for position, name in enumerate(HEAVY_FILES)}
    items.sort(key=lambda item: rank.get(item.path.name, len(rank)))
