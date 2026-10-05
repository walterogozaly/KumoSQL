"""Proven "model A can read model B" proposals for a synthetic Dataform project (offline, no credentials)."""

import json
import subprocess
import sys

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")
import sqlglot  # noqa: E402

from kumosql import load_sqlx_project  # noqa: E402
from kumosql import model_proposals as mp  # noqa: E402
from kumosql.costs import ObservedJob  # noqa: E402

TABLE = 'config { type: "table" }\n'
VIEW = 'config { type: "view" }\n'

ORDERS = 'SELECT id, customer_id, amount, status FROM ${ref("raw", "orders")}'


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project(root, **models):
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(root, "definitions/orders.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(root, "definitions/events.sqlx", 'config { type: "declaration", schema: "raw", name: "events" }\n')
    for name, text in models.items():
        write(root, f"definitions/{name}.sqlx", text)
    return load_sqlx_project(root)


SCHEMA = {"proj.raw.orders": {"id": "INT64", "customer_id": "INT64", "amount": "FLOAT64", "status": "STRING"},
          "proj.raw.events": {"id": "INT64", "kind": "STRING"}}


def load(root, **models):
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    pipeline = project(root, **models)
    pipeline.source_schema = {k: dict(v) for k, v in SCHEMA.items()}
    return pipeline


BASE_MODELS = {
    "wide": TABLE + ORDERS + " WHERE amount IS NOT NULL\n",
    "paid": TABLE + 'SELECT customer_id, SUM(amount) AS total FROM ${ref("raw", "orders")} '
    "WHERE amount IS NOT NULL AND status = 'paid' GROUP BY customer_id\n",
    # needs the rows with a NULL amount, which wide dropped: nothing can be read from it
    "refunds": TABLE + 'SELECT customer_id, amount FROM ${ref("raw", "orders")} WHERE status = \'refund\'\n',
    "kinds": TABLE + 'SELECT kind, COUNT(*) AS n FROM ${ref("raw", "events")} GROUP BY kind\n',
}


@pytest.fixture
def report(tmp_path):
    return mp.propose_model_reuse(load(tmp_path, **BASE_MODELS))


def test_a_proven_proposal_carries_the_replacement_sql(report):
    assert [p.id for p in report.proposals] == ["proj.an.paid <- proj.an.wide"]
    proposal = report.proposals[0]
    assert proposal.strategy.startswith("spj")
    assert proposal.model_kind == "table"
    assert "`proj.an.wide`" in proposal.replacement_sql
    assert "proj.raw.orders" not in proposal.replacement_sql  # it reads only the model
    assert '${ref("an", "wide")}' in proposal.replacement_dataform
    assert proposal.assumptions  # the proof's assumptions travel with it
    assert proposal.to_json()["proven"] is True


def test_the_case_with_no_rewrite_is_not_listed(report):
    listed = {(p.reader, p.model) for p in report.proposals}
    assert ("proj.an.refunds", "proj.an.wide") not in listed  # wide dropped the NULL amounts refunds still needs
    assert not any("kinds" in p.id for p in report.proposals)  # a different source table
    assert report.skipped.get("not proven (no_rewrite)", 0) >= 1
    assert report.skipped.get("no source table in common", 0) >= 1


def test_a_project_with_nothing_to_reuse_has_no_proposals(tmp_path):
    pipeline = load(tmp_path, kinds=BASE_MODELS["kinds"], refunds=BASE_MODELS["refunds"])
    result = mp.propose_model_reuse(pipeline)
    assert result.proposals == []
    assert result.to_json()["counts"]["proposed"] == 0
    assert "0 proposal(s)" in mp.format_report(result)


def test_the_replacement_returns_the_same_rows_on_data(tmp_path):
    """Defence in depth for the test project: run the reader and the replacement on the same rows."""

    pipeline = load(tmp_path, **BASE_MODELS)
    proposal = mp.propose_model_reuse(pipeline).proposals[0]
    db = duckdb.connect()
    db.execute("ATTACH ':memory:' AS proj")
    db.execute("CREATE SCHEMA proj.raw")
    db.execute("CREATE SCHEMA proj.an")
    db.execute("CREATE TABLE proj.raw.orders (id BIGINT, customer_id BIGINT, amount DOUBLE, status VARCHAR)")
    rows = [(i, i % 4, None if i % 5 == 0 else float(i), ["paid", "refund", "open"][i % 3]) for i in range(60)]
    db.executemany("INSERT INTO proj.raw.orders VALUES (?, ?, ?, ?)", rows)

    def run(sql):
        return sorted(db.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]).fetchall())

    db.execute("CREATE TABLE proj.an.wide AS " + sqlglot.transpile(pipeline.models["proj.an.wide"].sql, read="bigquery", write="duckdb")[0])
    assert run(pipeline.models["proj.an.paid"].sql) == run(proposal.replacement_sql)
    assert run(proposal.replacement_sql)  # not vacuous


def test_output_columns_keep_the_readers_names(tmp_path):
    models = {
        "wide": TABLE + ORDERS + " WHERE amount IS NOT NULL\n",
        "same_rows": TABLE + "SELECT id AS order_id, customer_id AS who, amount AS value, status AS state "
        'FROM ${ref("raw", "orders")} WHERE amount IS NOT NULL\n',
    }
    proposals = mp.propose_model_reuse(load(tmp_path, **models)).proposals
    found = next(p for p in proposals if p.id == "proj.an.same_rows <- proj.an.wide")
    sql = found.replacement_sql
    for name in ("order_id", "who", "value", "state"):
        assert f"AS {name}" in sql  # the readers' consumers still find these columns by name
    assert "AS id" not in sql


def test_a_view_is_offered_without_a_size_claim(tmp_path):
    models = dict(BASE_MODELS, wide=VIEW + ORDERS + " WHERE amount IS NOT NULL\n")
    result = mp.propose_model_reuse(
        load(tmp_path, **models), table_bytes={"proj.raw.orders": 1000, "proj.an.wide": 10}
    )
    saving = result.proposals[0].saving
    assert result.proposals[0].model_kind == "view"
    assert saving.bytes_saved is None and saving.basis == "unknown" and "view" in saving.reason


def test_unsafe_models_are_neither_readers_nor_sources(tmp_path):
    models = dict(
        BASE_MODELS,
        wide=TABLE.replace('"table"', '"incremental"') + ORDERS + " WHERE amount IS NOT NULL\n",
    )
    assert mp.propose_model_reuse(load(tmp_path, **models)).proposals == []  # incremental rows depend on run history
    clock = {"wide": TABLE + ORDERS + " WHERE amount IS NOT NULL AND CURRENT_DATE() > DATE '2000-01-01'\n", "paid": BASE_MODELS["paid"]}
    other = tmp_path / "clock"
    other.mkdir()
    assert mp.propose_model_reuse(load(other, **clock)).proposals == []  # a clock in the source changes its rows


def test_models_that_cannot_be_read_as_queries_are_skipped(tmp_path):
    models = dict(BASE_MODELS, ops=TABLE.replace("}", ', hasOutput: true }') + "SELECT 1 AS x\n")
    result = mp.propose_model_reuse(load(tmp_path, **models))
    assert [p.id for p in result.proposals] == ["proj.an.paid <- proj.an.wide"]


def test_estimate_from_sizes_only(tmp_path):
    pipeline = load(tmp_path, **BASE_MODELS)
    result = mp.propose_model_reuse(pipeline, table_bytes={"proj.raw.orders": 1000, "proj.an.wide": 400})
    saving = result.proposals[0].saving
    assert (saving.bytes_saved, saving.at_most_bytes, saving.basis, saving.runs) == (600, 1000, "estimate", None)
    assert saving.to_json()["unit"] == "bytes_per_run"


def test_unknown_when_a_size_is_missing_and_never_invented(tmp_path):
    pipeline = load(tmp_path, **BASE_MODELS)
    none = mp.propose_model_reuse(pipeline).proposals[0].saving
    assert (none.bytes_saved, none.at_most_bytes, none.basis) == (None, None, "unknown") and none.reason
    only_reader = mp.propose_model_reuse(pipeline, table_bytes={"proj.raw.orders": 1000}).proposals[0].saving
    assert only_reader.bytes_saved is None and only_reader.at_most_bytes == 1000  # a ceiling is still true
    assert "proj.an.wide" in only_reader.reason
    only_model = mp.propose_model_reuse(pipeline, table_bytes={"proj.an.wide": 400}).proposals[0].saving
    assert only_model.bytes_saved is None and only_model.at_most_bytes is None
    junk = mp.propose_model_reuse(pipeline, table_bytes={"proj.raw.orders": -5, "proj.an.wide": True}).proposals[0].saving
    assert junk.bytes_saved is None  # a negative or boolean size is not a size


def test_estimate_from_the_readers_measured_job_history(tmp_path):
    pipeline = load(tmp_path, **BASE_MODELS)

    def job(job_id, billed):
        return ObservedJob.from_record({"job_id": job_id, "total_bytes_billed": billed, "total_bytes_processed": billed,
                                        "destination_table": "proj.an.paid", "creation_time": "2025-01-02T00:00:00Z"})

    jobs = [job("a", 900), job("b", 1100)]
    result = mp.propose_model_reuse(pipeline, jobs=jobs, table_bytes={"proj.an.wide": 400})
    saving = result.proposals[0].saving
    assert (saving.bytes_saved, saving.at_most_bytes, saving.runs) == (600, 1000, 2)  # mean 1000 per run minus 400
    # the measured cost wins over the sizes of the tables the reader reads
    both = mp.propose_model_reuse(pipeline, jobs=jobs, table_bytes={"proj.an.wide": 400, "proj.raw.orders": 5000})
    assert both.proposals[0].saving.bytes_saved == 600
    # a model larger than what the reader scans is a regression: kept, negative, ordered last
    worse = mp.propose_model_reuse(pipeline, jobs=jobs, table_bytes={"proj.an.wide": 4000})
    assert worse.proposals[0].saving.bytes_saved == -3000


def test_identical_models_conflict_with_each_other(tmp_path):
    models = {"first": TABLE + ORDERS + " WHERE amount IS NOT NULL\n", "second": TABLE + ORDERS + " WHERE amount IS NOT NULL\n"}
    proposals = mp.propose_model_reuse(load(tmp_path, **models)).proposals
    assert sorted(p.id for p in proposals) == ["proj.an.first <- proj.an.second", "proj.an.second <- proj.an.first"]
    for proposal in proposals:  # applying both would make a cycle
        assert len(proposal.conflicts) == 1 and proposal.conflicts[0] != proposal.id


def test_a_model_that_depends_on_the_reader_is_never_its_source(tmp_path):
    models = {
        "wide": TABLE + ORDERS + " WHERE amount IS NOT NULL\n",
        "narrow": TABLE + 'SELECT id, amount FROM ${ref("wide")} WHERE status = \'paid\'\n',
        "paid": BASE_MODELS["paid"],
    }
    ids = {p.id for p in mp.propose_model_reuse(load(tmp_path, **models)).proposals}
    assert "proj.an.wide <- proj.an.narrow" not in ids  # narrow reads wide
    assert "proj.an.narrow <- proj.an.wide" not in ids  # narrow already reads wide


def test_readers_and_models_filter_and_unknown_names_are_refused(tmp_path):
    pipeline = load(tmp_path, **BASE_MODELS)
    assert mp.propose_model_reuse(pipeline, readers=["kinds"]).proposals == []
    assert [p.id for p in mp.propose_model_reuse(pipeline, readers=["paid"], models=["wide"]).proposals] == ["proj.an.paid <- proj.an.wide"]
    with pytest.raises(mp.ProposalError):
        mp.propose_model_reuse(pipeline, readers=["nope"])


def test_the_report_is_deterministic(tmp_path):
    first = mp.propose_model_reuse(load(tmp_path, **BASE_MODELS)).to_json()
    second = mp.propose_model_reuse(load(tmp_path, **BASE_MODELS)).to_json()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_cli_prints_json_and_text(tmp_path):
    load(tmp_path, **BASE_MODELS)
    sizes = tmp_path / "sizes.json"
    sizes.write_text(json.dumps({"proj.raw.orders": 1000, "proj.an.wide": 400}), encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    base = [sys.executable, "-m", "kumosql", "model-proposals", str(tmp_path), "--source-schema", str(schema),
            "--table-bytes", str(sizes)]
    done = subprocess.run([*base, "--json"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout[done.stdout.index("{"):])
    assert [p["id"] for p in data["proposals"]] == ["proj.an.paid <- proj.an.wide"]
    assert data["proposals"][0]["saving"]["bytes_saved"] == 600
    text = subprocess.run(base, capture_output=True, text=True, timeout=120)
    assert text.returncode == 0 and "proj.an.paid can read proj.an.wide" in text.stdout and "replacement:" in text.stdout


def test_cli_reports_a_bad_input_file(tmp_path):
    load(tmp_path, **BASE_MODELS)
    done = subprocess.run([sys.executable, "-m", "kumosql", "model-proposals", str(tmp_path), "--table-bytes", str(tmp_path / "missing.json")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 2 and "error" in done.stderr
