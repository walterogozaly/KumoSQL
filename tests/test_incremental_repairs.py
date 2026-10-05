"""Repairs of diverging incremental models: each one proven safe and proven to leave the full refresh unchanged.

The cases are the authored dev cases of ``tests/fixtures/incremental`` (never the held-out ones) and a small
generated Dataform project. A repair that cannot be proven is not offered; the near misses below each show one
condition of the two proofs failing.
"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

import duckdb

from kumosql import incremental_repairs as repairs
from kumosql.incremental import (
    SourceTable,
    parse_incremental_sqlx,
    prove,
    search_divergence,
)
from kumosql.incremental_repairs import (
    eliminate_key_dedups,
    propose_repairs,
    prove_full_refresh_unchanged,
)

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
_spec = importlib.util.spec_from_file_location("incremental_bench", _TOOLS / "incremental_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("incremental_bench", bench)
_spec.loader.exec_module(bench)

CASES = {c["id"]: c for c in bench.load_cases("dev")}
LATE_AND_DUPLICATE = ("insert_new", "insert_late", "duplicate", "empty")
EVENTS = {"events": SourceTable({"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, ("id",), "ts")}


def case_repairs(case_id: str, **kwargs):
    case = CASES[case_id]
    _, sources = bench.build(case)
    return propose_repairs(case["sqlx"], case["target"], sources, case["contract"]["kinds"], tables=tuple(case["contract"].get("tables") or ()) or None, **kwargs)


# (case id) -> the edits of the one minimal repair expected
REPAIRED = {
    "full-rerun-duplicate-delivery": ("dedup",),  # a merge of a full re-run, re-delivered rows
    "merge-error-duplicate-of-newest": ("dedup",),  # a >= merge: the same row twice in one run
    "strict-watermark-timestamp-tie": ("watermark_lookback",),  # ... and with no key the repair adds one
    "watermark-gte-no-key": ("watermark_lookback",),  # >= appends the newest rows again: it needs a key
    "lookback-window-too-short": ("full_rerun_merge",),  # late rows beyond the lookback: re-read everything instead
}
# Divergences no edit can repair under their contract (updates and deletes move or remove keys, NULL keys never
# match, a LIMIT or a lookback shorter than the lateness has nothing to fix), so nothing is offered.
UNREPAIRABLE = [
    "full-rerun-source-delete",
    "full-rerun-null-key",
    "full-rerun-limit",
    "full-rerun-filter-on-updated-column",
    "merge-null-key",
]


@pytest.mark.parametrize("case_id", sorted(REPAIRED))
def test_repairs_for_diverging_cases(case_id):
    report = case_repairs(case_id)
    assert [r.edits for r in report.repairs] == [REPAIRED[case_id]]
    repair = report.repairs[0]
    # a unified diff of the SQLX, never applied: the report only holds text
    assert repair.diff.startswith("--- a/model.sqlx\n+++ b/model.sqlx\n")
    assert repair.diff.count("\n-") >= 1 or repair.edits == ("dedup",) or "uniqueKey" in repair.diff
    assert repair.rule and repair.full_refresh
    # the proof is not the only evidence: an independent, deeper search finds nothing on the repaired model
    case = CASES[case_id]
    _, sources = bench.build(case)
    repaired = parse_incremental_sqlx(repair.sqlx, case["target"])
    assert search_divergence(repaired, sources, frozenset(case["contract"]["kinds"]), seeds=40, batches=5, time_limit=60) is None


@pytest.mark.parametrize("case_id", UNREPAIRABLE)
def test_nothing_is_offered_where_no_proof_exists(case_id):
    report = case_repairs(case_id)
    assert report.repairs == []
    assert not report.already_safe


def test_a_safe_model_has_nothing_to_repair():
    safe = next(c for c in CASES.values() if c["label"] == "safe" and c["id"] == "full-rerun-row-wise-inserts")
    _, sources = bench.build(safe)
    report = propose_repairs(safe["sqlx"], safe["target"], sources, safe["contract"]["kinds"])
    assert report.already_safe and report.repairs == []


# ---------------------------------------------------------------------------
# The generated fixture: every model sets updatePartitionFilter
# ---------------------------------------------------------------------------

FIXTURE_MODEL = """config {
  type: "incremental",
  schema: "fx_core_ops",
  uniqueKey: ["id"],
  bigquery: { partitionBy: "DATE(created_at)", updatePartitionFilter: "created_at >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)" }
}

select * from (
select
  a.id,
  a.created_at,
  coalesce(a.amount, 0) as amount
from ${ref("fx_core_ops", "ops_core_1")} as a
left join ${ref("ops_core_2")} as b1
  on a.id = b1.id
where a.created_at >= '2020-01-01'
)
${when(incremental(), `where 1 = 1`)}
"""
FIXTURE_SOURCES = {
    "ops_core_1": SourceTable({"id": "INT64", "created_at": "TIMESTAMP", "amount": "INT64"}, ("id",), "created_at"),
    "ops_core_2": SourceTable({"id": "INT64"}, ("id",), None),
}


def test_drop_the_filter_and_dedup_is_proven_by_r7():
    report = propose_repairs(FIXTURE_MODEL, "ops_inc", FIXTURE_SOURCES, LATE_AND_DUPLICATE, path="definitions/ops_inc.sqlx")
    assert [r.edits for r in report.repairs] == [("drop_update_partition_filter", "dedup")]
    repair = report.repairs[0]
    assert repair.rule.startswith("R7")
    assert "-  bigquery: { partitionBy: \"DATE(created_at)\", updatePartitionFilter" in repair.diff
    assert '+  bigquery: { partitionBy: "DATE(created_at)" }' in repair.diff
    # both sources are read through a de-duplication, the original reference kept inside it
    assert repair.diff.count("QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY created_at DESC) = 1") == 1
    assert repair.diff.count("QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1") == 1
    assert '${ref("fx_core_ops", "ops_core_1")}' in repair.sqlx and "AS b1" in repair.sqlx.replace(" as b1", " AS b1")
    assert "key de-duplication keeps every row" in repair.full_refresh
    assert any("distinct rows" in a for a in repair.assumptions)
    # neither edit alone is enough, and the report says so
    refused = {r.edits: r.reason for r in report.refused}
    assert "no proof rule" in refused[("drop_update_partition_filter",)]
    assert "no proof rule" in refused[("dedup",)]


def test_dropping_the_filter_alone_when_the_query_already_has_one_row_per_key():
    sqlx = FIXTURE_MODEL.replace("a.id,\n  a.created_at,\n  coalesce(a.amount, 0) as amount\nfrom ${ref(\"fx_core_ops\", \"ops_core_1\")} as a\nleft join ${ref(\"ops_core_2\")} as b1\n  on a.id = b1.id", "a.id,\n  max(a.created_at) as created_at\nfrom ${ref(\"fx_core_ops\", \"ops_core_1\")} as a\ngroup by a.id")
    sqlx = sqlx.replace("where a.created_at >= '2020-01-01'\n", "")
    report = propose_repairs(sqlx, "ops_inc", FIXTURE_SOURCES, LATE_AND_DUPLICATE)
    assert [r.edits for r in report.repairs] == [("drop_update_partition_filter",)]
    assert report.repairs[0].full_refresh == "the full query is untouched"


def test_executed_check_of_the_dedup_lemma_on_keyed_data():
    """The original and the repaired full queries return the same rows on random data whose keys are unique."""

    report = propose_repairs(FIXTURE_MODEL, "ops_inc", FIXTURE_SOURCES, LATE_AND_DUPLICATE)
    original = parse_incremental_sqlx(FIXTURE_MODEL, "ops_inc")
    repaired = parse_incremental_sqlx(report.repairs[0].sqlx, "ops_inc")
    rng = random.Random(5)
    for _ in range(20):
        con = duckdb.connect()
        con.execute("create table ops_core_1 (id bigint, created_at timestamp, amount bigint)")
        con.execute("create table ops_core_2 (id bigint)")
        ids = rng.sample(range(40), rng.randint(0, 15))
        for i in ids:
            con.execute("insert into ops_core_1 values (?, ?, ?)", [i, f"20{rng.choice([19, 20, 24])}-01-0{rng.randint(1, 9)} 00:00:00", rng.choice([None, 1, 2])])
        for i in rng.sample(range(40), rng.randint(0, 15)):
            con.execute("insert into ops_core_2 values (?)", [i])
        a = sorted(con.execute(original.full_sql).fetchall(), key=repr)
        b = sorted(con.execute(repaired.full_sql).fetchall(), key=repr)
        con.close()
        assert a == b


# ---------------------------------------------------------------------------
# The edits, one at a time
# ---------------------------------------------------------------------------


def model(config: str, body: str, target: str = "m"):
    text = f'config {{ type: "incremental"{config} }}\n{body}\n'
    return text, parse_incremental_sqlx(text, target)


@pytest.mark.parametrize(
    "block, expected",
    [
        ('bigquery: { partitionBy: "DATE(ts)", updatePartitionFilter: "ts > 1" }', 'bigquery: { partitionBy: "DATE(ts)" }'),
        ('bigquery: { updatePartitionFilter: "ts > 1", partitionBy: "DATE(ts)" }', 'bigquery: { partitionBy: "DATE(ts)" }'),
        ('bigquery: { updatePartitionFilter: "ts > 1" }', ""),
        ("bigquery: {\n    partitionBy: 'DATE(ts)',\n    updatePartitionFilter: 'ts > 1',\n  }", "bigquery: {\n    partitionBy: 'DATE(ts)'\n  }"),
    ],
)
def test_drop_update_partition_filter_forms(block, expected):
    sqlx, parsed = model(f', uniqueKey: ["id"], {block}', 'SELECT id, ts FROM ${ref("events")}')
    assert parsed.update_partition_filter
    text, why = repairs._drop_update_partition_filter(sqlx, parsed)
    assert why is None and "updatePartitionFilter" not in text
    again = parse_incremental_sqlx(text, "m")
    assert again.update_partition_filter == "" and again.unique_key == ("id",) and not again.unmodelled
    assert expected in text


def test_drop_update_partition_filter_needs_one():
    sqlx, parsed = model(', uniqueKey: ["id"]', 'SELECT id FROM ${ref("events")}')
    assert repairs._drop_update_partition_filter(sqlx, parsed)[0] is None


def test_full_rerun_merge_removes_the_incremental_filter_and_keeps_the_else_branch():
    sqlx, parsed = model(', uniqueKey: ["id"]', 'SELECT id, ts FROM ${ref("events")} ${when(incremental(), `WHERE ts > 5`, `WHERE ts > 1`)}')
    text, why = repairs._full_rerun_merge(sqlx, parsed)
    again = parse_incremental_sqlx(text, "m")
    assert why is None and again.full_sql == again.incremental_sql == parsed.full_sql
    assert repairs._full_rerun_merge(text, again)[0] is None  # already a full re-run


def test_full_rerun_merge_is_refused_without_a_key_or_with_pre_operations():
    sqlx, parsed = model("", 'SELECT id FROM ${ref("events")} ${when(incremental(), `WHERE id > 1`)}')
    assert "no uniqueKey" in repairs._full_rerun_merge(sqlx, parsed)[1]
    sqlx, parsed = model(
        ', uniqueKey: ["id"]',
        'pre_operations { ${when(incremental(), `DELETE FROM ${self()} WHERE id > 1`)} }\nSELECT id FROM ${ref("events")} ${when(incremental(), `WHERE id > 1`)}',
    )
    assert "pre_operations" in repairs._full_rerun_merge(sqlx, parsed)[1]


WM = "COALESCE((SELECT MAX(ts) FROM ${self()}), TIMESTAMP '1970-01-01')"


def test_watermark_lookback_changes_the_operator_adds_a_lookback_and_a_key():
    sqlx, parsed = model("", f'SELECT id, ts FROM ${{ref("events")}} ${{when(incremental(), `WHERE ts > {WM}`)}}')
    text, why = repairs._watermark_lookback(sqlx, parsed, EVENTS)
    assert why is None
    assert f"ts >= TIMESTAMP_SUB({WM}, INTERVAL 1 HOUR)" in text
    again = parse_incremental_sqlx(text, "m")
    assert again.unique_key == ("id",) and again.full_sql == parsed.full_sql and not again.unmodelled


def test_watermark_lookback_near_misses():
    # a key that is not the source's key: merging on it is a different model
    sqlx, parsed = model(', uniqueKey: ["customer_id"]', f'SELECT id, customer_id, ts FROM ${{ref("events")}} ${{when(incremental(), `WHERE ts > {WM}`)}}')
    assert "not the source's key" in repairs._watermark_lookback(sqlx, parsed, EVENTS)[1]
    # a watermark on another column
    sqlx, parsed = model("", 'SELECT id, v FROM ${ref("events")} ${when(incremental(), `WHERE v > COALESCE((SELECT MAX(v) FROM ${self()}), 0)`)}')
    assert "watermark" in repairs._watermark_lookback(sqlx, parsed, EVENTS)[1]
    # two sources: which one's key
    two = dict(EVENTS, other=SourceTable({"id": "INT64"}, ("id",)))
    sqlx, parsed = model("", f'SELECT id, ts FROM ${{ref("events")}} ${{when(incremental(), `WHERE ts > {WM}`)}}')
    assert "one source" in repairs._watermark_lookback(sqlx, parsed, two)[1]
    # no incremental filter at all
    sqlx, parsed = model("", 'SELECT id, ts FROM ${ref("events")}')
    assert "no watermark" in repairs._watermark_lookback(sqlx, parsed, EVENTS)[1]


def test_dedup_placement_single_table_goes_on_the_query_and_joins_wrap_each_source():
    sqlx, parsed = model(', uniqueKey: ["id"]', 'SELECT id, ts FROM ${ref("events")}')
    text, why = repairs._dedup(sqlx, parsed, EVENTS, frozenset(LATE_AND_DUPLICATE), None)
    assert why is None and text.rstrip().endswith("QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1")
    two = dict(EVENTS, customers=SourceTable({"id": "INT64", "tier": "STRING"}, ("id",)))
    sqlx, parsed = model(', uniqueKey: ["id"]', 'SELECT e.id, c.tier FROM ${ref("events")} e JOIN ${ref("customers")} ON true')
    text, why = repairs._dedup(sqlx, parsed, two, frozenset(LATE_AND_DUPLICATE), None)
    assert why is None and text.count("QUALIFY") == 2
    assert "(SELECT * FROM ${ref(\"events\")} WHERE TRUE QUALIFY" in text and ") e JOIN" in text  # the alias is kept
    assert ") AS customers ON" in text  # one the model did not alias gets its name back


def test_dedup_near_misses():
    sqlx, parsed = model(', uniqueKey: ["id"]', 'SELECT id, ts FROM ${ref("events")}')
    assert "does not re-deliver" in repairs._dedup(sqlx, parsed, EVENTS, frozenset({"insert_new"}), None)[1]
    assert "NULL keys" in repairs._dedup(sqlx, parsed, EVENTS, frozenset({"duplicate", "null_key"}), None)[1]
    no_key = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP"}, (), "ts")}
    assert "without a declared key" in repairs._dedup(sqlx, parsed, no_key, frozenset({"duplicate"}), None)[1]
    # only the tables the contract changes are de-duplicated: none here
    assert "no source table" in repairs._dedup(sqlx, parsed, EVENTS, frozenset({"duplicate"}), ("elsewhere",))[1]


# ---------------------------------------------------------------------------
# Full refresh unchanged: the lemma and the prover, and what each refuses
# ---------------------------------------------------------------------------


def pair(original: str, repaired: str):
    return prove_full_refresh_unchanged(
        parse_incremental_sqlx(f'config {{ type: "incremental" }}\n{original}', "m"),
        parse_incremental_sqlx(f'config {{ type: "incremental" }}\n{repaired}', "m"),
        EVENTS,
    )


def test_full_refresh_proof_accepts_key_dedup_and_refuses_everything_else():
    base = "SELECT id, v FROM events WHERE v > 0"
    dedup = "SELECT id, v FROM events WHERE v > 0 QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1"
    assert pair(base, dedup)[0]
    assert pair(base, "SELECT id, v FROM (SELECT * FROM events WHERE TRUE QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1) AS events WHERE v > 0")[0]
    assert pair(base, base)[:2] == (True, "the full query is untouched")
    # a superset of the key still gives one row per partition
    assert pair(base, dedup.replace("PARTITION BY id", "PARTITION BY id, customer_id"))[0]
    # near misses: each one changes the full refresh, so none is proven
    assert not pair(base, dedup.replace("PARTITION BY id", "PARTITION BY customer_id"))[0]  # a partition on a non-key keeps one row per customer
    assert not pair(base, dedup.replace("= 1", "= 2"))[0]
    assert not pair(base, dedup.replace("WHERE v > 0", "WHERE v > 1"))[0]  # a different filter beside a correct dedup
    assert not pair(base, "SELECT id, v FROM events WHERE v > 0 AND v < 9")[0]
    assert not pair(base, dedup.replace("ORDER BY ts DESC", "ORDER BY 10 / v"))[0]  # an ORDER BY expression can fail on a row


def test_the_lemma_needs_a_declared_key_that_the_partition_contains():
    dedup = "SELECT id, v FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts) = 1"
    assert eliminate_key_dedups(dedup, EVENTS) is None
    keyed = dedup.replace("customer_id", "id")
    assert "QUALIFY" not in eliminate_key_dedups(keyed, EVENTS)
    assert eliminate_key_dedups(keyed, {"events": SourceTable(EVENTS["events"].columns, (), "ts")}) is None  # no declared key
    assert eliminate_key_dedups(keyed.replace("FROM events", "FROM events JOIN events b USING (id)"), EVENTS) is None  # a join


def test_an_edit_that_changes_the_full_query_is_refused(monkeypatch):
    """A de-duplication that also filters the full query is safe under the contract, yet it is not offered."""

    real = repairs._dedup

    def bad(sqlx, model, *args, **kwargs):
        text, why = real(sqlx, model, *args, **kwargs)
        return text.replace('FROM ${ref("events")}', 'FROM ${ref("events")} WHERE v > 0'), why

    monkeypatch.setitem(repairs._APPLY, "dedup", bad)
    sqlx, _ = model(', uniqueKey: ["id"]', 'SELECT id, v FROM ${ref("events")}')
    report = propose_repairs(sqlx, "m", EVENTS, ("insert_new", "duplicate", "empty"))
    assert report.repairs == []
    assert any("full refresh is not proven unchanged" in r.reason for r in report.refused)


def test_unsupported_models_are_reported_not_repaired():
    report = propose_repairs('config { type: "incremental", uniqueKey: ["id"] }\nSELECT id FROM ${dateFilter("x")}', "m", EVENTS, LATE_AND_DUPLICATE)
    assert report.repairs == [] and "not supported" in report.refused[0].reason
    report = propose_repairs('config { type: "incremental", incrementalStrategy: "insert_overwrite" }\nSELECT id FROM ${ref("events")}', "m", EVENTS, LATE_AND_DUPLICATE)
    assert report.repairs == [] and "not modelled" in report.refused[0].reason


def test_every_offered_repair_is_proven_by_prove_itself():
    for case_id in REPAIRED:
        report = case_repairs(case_id)
        case = CASES[case_id]
        _, sources = bench.build(case)
        for repair in report.repairs:
            verdict = prove(parse_incremental_sqlx(repair.sqlx, case["target"]), sources, case["contract"]["kinds"])
            assert verdict is not None and verdict.rule == repair.rule


# ---------------------------------------------------------------------------
# The analysis the repairs lean on: a key de-duplication drops only copies
# ---------------------------------------------------------------------------

from kumosql.incremental_monotone import analyze

DEDUP_QUERY = "SELECT id, ts, v FROM events WHERE v > 0 QUALIFY ROW_NUMBER() OVER (PARTITION BY {p} ORDER BY ts DESC) = 1"


def stable(query: str, kinds):
    growth = analyze(query, EVENTS, kinds, None, target="m")
    return None if growth is None or growth.stable is None else {growth.names[i] for i in growth.stable}


def test_key_dedup_keeps_every_column_stable_when_only_copies_arrive():
    # without the dedup rule the filter on v, decided by a column the dedup does not fix, would lose the key
    assert stable(DEDUP_QUERY.format(p="id"), {"insert_new", "insert_late", "duplicate"}) == {"id", "ts", "v"}
    assert stable(DEDUP_QUERY.format(p="id, customer_id"), {"insert_new", "duplicate"}) == {"id", "ts", "v"}
    unfiltered = DEDUP_QUERY.format(p="id").replace(" WHERE v > 0", "")
    assert stable(unfiltered, {"insert_new", "update"}) == {"id", "ts"}  # keyed: updates change v, never the key or ts
    assert stable(DEDUP_QUERY.format(p="id"), {"insert_new", "update"}) is None  # ... so a filter on v can drop a key


@pytest.mark.parametrize(
    "query, kinds",
    [
        (DEDUP_QUERY.format(p="customer_id"), {"insert_new", "duplicate"}),  # a partition on a non-key keeps an arbitrary row
        (DEDUP_QUERY.format(p="id"), {"insert_new", "duplicate", "null_key"}),  # NULL keys share one partition
        (DEDUP_QUERY.format(p="id"), {"insert_new", "delete"}),  # a deleted row can be replaced by its copy-less successor
        ("SELECT e.id, e.ts, e.v FROM events e JOIN events f ON f.id = e.id WHERE e.v > 0 QUALIFY ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY e.ts DESC) = 1", {"insert_new", "duplicate"}),
    ],
)
def test_key_dedup_analysis_near_misses(query, kinds):
    result = stable(query, kinds)
    assert result is None or not {"ts", "v"} <= result
